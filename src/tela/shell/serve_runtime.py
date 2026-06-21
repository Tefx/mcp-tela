"""Runtime helpers for the ``tela serve`` HTTP gateway lifecycle.

Extracted from ``tela.commands.serve_cmd`` so that the command module stays
a thin CLI/config/token facade while the heavy runtime wiring — HTTP server
bind/stop, signal handlers, config watcher, idle shutdown, and rollback —
lives in Shell.

All public functions return ``Result[T, E]`` per Shell convention.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from starlette.applications import Starlette

from tela.shell.result import Result
from tela.shell.gateway import gateway_shutdown
from tela.shell.gateway_runtime import (
    get_expected_bearer_token,
)
from tela.shell.idle_shutdown import init_idle_manager, shutdown_idle_manager
from tela.shell.lockfile import delete_lockfile


HTTP_SERVER_BIND_TIMEOUT_SECONDS = 5.0
HTTP_SERVER_SHUTDOWN_TIMEOUT_SECONDS = 2.0
CONFIG_WATCH_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class HttpServerHandle:
    """Track running Streamable HTTP server task and bound port."""

    task: asyncio.Task[None]
    bound_port: int
    request_shutdown: Callable[[], None]


# --- HTTP server launch / stop ---


# @shell_complexity: server launch branches on port-binding, SSL, and signal-setup pathways
async def launch_streamable_http_server(
    *,
    upstream_app: Starlette,
    upstream_log_level: str,
    host: str,
    requested_port: int,
) -> Result[HttpServerHandle, str]:
    """Start Streamable HTTP server and resolve the actual bound port.

    Args:
        upstream_app: The Starlette ASGI application obtained via
            ``get_upstream_http_app()`` (boundary-safe; no live FastMCP ref).
        upstream_log_level: Log level string from ``get_upstream_log_level()``.
        host: Bind address.
        requested_port: Requested port (0 for auto-assign).

    Returns:
        Result containing the server handle, or error string.
    """

    import uvicorn

    from tela.shell.http_auth import BearerAuthMiddleware

    app = BearerAuthMiddleware(
        upstream_app, get_expected_token=lambda: get_expected_bearer_token().value
    )
    log_level = upstream_log_level
    config = uvicorn.Config(
        app,
        host=host,
        port=requested_port,
        log_level=log_level.lower(),
    )
    server = uvicorn.Server(config)
    task: asyncio.Task[None] = asyncio.create_task(server.serve())

    deadline = time.monotonic() + HTTP_SERVER_BIND_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if task.done():
            if task.cancelled():
                return Result(
                    error="HTTP_RUN_CANCELLED: streamable HTTP server cancelled"
                )
            server_exc = task.exception()
            if server_exc is None:
                return Result(
                    error="HTTP_RUN_FAILED: streamable HTTP server exited early"
                )
            return Result(error=f"HTTP_RUN_FAILED: {server_exc}")

        bound_port_result = extract_bound_port(server)
        if bound_port_result.is_err:
            await stop_http_server(
                HttpServerHandle(
                    task=task,
                    bound_port=requested_port,
                    request_shutdown=lambda: setattr(server, "should_exit", True),
                )
            )
            return Result(error=bound_port_result.error)

        bound_port = bound_port_result.value
        if bound_port is not None:
            return Result(
                value=HttpServerHandle(
                    task=task,
                    bound_port=bound_port,
                    request_shutdown=lambda: setattr(server, "should_exit", True),
                )
            )

        await asyncio.sleep(0.01)

    await stop_http_server(
        HttpServerHandle(
            task=task,
            bound_port=requested_port,
            request_shutdown=lambda: setattr(server, "should_exit", True),
        )
    )
    return Result(
        error=(
            "HTTP_BIND_TIMEOUT: timed out waiting for streamable HTTP server to bind"
        )
    )


# @shell_complexity: port extraction walks uvicorn internals with multiple fallback paths
def extract_bound_port(server: object) -> Result[int | None, str]:
    """Read resolved listen port from uvicorn server sockets."""

    listeners = getattr(server, "servers", None)
    if not listeners:
        return Result(value=None)

    for listener in listeners:
        sockets = getattr(listener, "sockets", None)
        if not sockets:
            continue
        for socket in sockets:
            sockname = socket.getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                port = int(sockname[1])
                if port > 0:
                    return Result(value=port)

    return Result(value=None)


async def stop_http_server(server: HttpServerHandle) -> None:
    """Request graceful HTTP server stop and await task completion."""

    if server.task.done():
        await server.task
        return

    server.request_shutdown()
    try:
        await asyncio.wait_for(
            server.task, timeout=HTTP_SERVER_SHUTDOWN_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        server.task.cancel()
        try:
            await server.task
        except asyncio.CancelledError:
            return


# --- Signal handling ---


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Install SIGINT/SIGTERM handlers that trigger clean shutdown."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            continue


def remove_signal_handlers() -> None:
    """Remove process signal handlers installed by this module."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.remove_signal_handler(sig)
        except NotImplementedError:
            continue


# --- Config watcher ---


# @shell_complexity: config reload watcher branches on profile validation, reaper params, and stop-event handling
async def watch_config_changes(
    *,
    config_path: Path,
    default_profile: str | None,
    reaper_sweep_interval: float | None,
    reaper_native_ttl: float | None,
    reaper_bridge_ttl: float | None,
    stop_event: asyncio.Event,
) -> None:
    """Poll config mtime and run hot-reload callback when it changes."""

    from tela.shell.gateway import gateway_reload_config_from_disk

    last_mtime_result = config_mtime_ns(config_path)
    if last_mtime_result.is_err:
        return
    last_mtime_ns = last_mtime_result.value

    while not stop_event.is_set():
        await asyncio.sleep(CONFIG_WATCH_POLL_SECONDS)
        current_mtime_result = config_mtime_ns(config_path)
        if current_mtime_result.is_err:
            continue
        current_mtime_ns = current_mtime_result.value

        if current_mtime_ns is None:
            continue

        if last_mtime_ns is not None and current_mtime_ns <= last_mtime_ns:
            continue

        reload_result = await gateway_reload_config_from_disk(
            config_path=config_path,
            default_profile=default_profile,
            sweep_interval_seconds=reaper_sweep_interval,
            native_idle_ttl_seconds=reaper_native_ttl,
            bridge_idle_ttl_seconds=reaper_bridge_ttl,
        )
        if reload_result.is_err:
            print(
                f"warning: config reload failed: {reload_result.error}",
                file=sys.stderr,
            )
        last_mtime_ns = current_mtime_ns


def config_mtime_ns(config_path: Path) -> Result[int | None, str]:
    """Return file mtime (ns) for config watcher, or None if unreadable."""

    try:
        return Result(value=config_path.stat().st_mtime_ns)
    except OSError:
        return Result(value=None)


# --- Idle shutdown watcher ---


# @shell_complexity: idle watcher coordinates timeout, stop-event, gateway shutdown, and lockfile cleanup branches in one lifecycle loop.
async def idle_shutdown_watch(
    *,
    idle_timeout_seconds: int,
    stop_event: asyncio.Event,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Initialize idle manager and hold it for process lifetime."""

    _ = poll_interval_seconds

    async def _on_idle_expiry() -> None:
        print("tela: idle timeout reached, shutting down", file=sys.stderr)
        stop_event.set()

    init_result = await init_idle_manager(
        timeout_seconds=float(idle_timeout_seconds),
        shutdown_callback=_on_idle_expiry,
    )
    if init_result.is_err:
        print(
            f"warning: idle shutdown init failed: {init_result.error}",
            file=sys.stderr,
        )
        return

    assert init_result.value is not None
    if idle_timeout_seconds > 0:
        prime_increment_result = await init_result.value.increment()
        if prime_increment_result.is_err:
            print(
                "warning: idle shutdown prime increment failed: "
                f"{prime_increment_result.error}",
                file=sys.stderr,
            )
        else:
            prime_decrement_result = await init_result.value.decrement()
            if prime_decrement_result.is_err:
                print(
                    "warning: idle shutdown prime decrement failed: "
                    f"{prime_decrement_result.error}",
                    file=sys.stderr,
                )

    try:
        await stop_event.wait()
    finally:
        _ = await shutdown_idle_manager()


# --- Rollback helper ---


async def rollback_after_post_bind_convergence_failure(
    *,
    http_server: HttpServerHandle,
    convergence_error: str | None,
) -> Result[None, str]:
    """Rollback explicit post-bind failure path.

    Sequence:
    1) remove lockfile discovery artifact,
    2) tear down bound HTTP server,
    3) clear lifecycle runtime state via gateway shutdown.
    """

    delete_lockfile()
    await stop_http_server(http_server)
    await gateway_shutdown()
    return Result(error=convergence_error or "STARTUP_CONVERGENCE_FAILED")


# --- Task helper ---


async def await_task(task: asyncio.Task[object]) -> None:
    """Await or cancel background task during shutdown."""

    if task.done():
        await task
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return


# --- Lockfile metadata helpers ---


def utc_now_iso() -> Result[str, str]:
    """Return current UTC timestamp in lockfile ISO-8601 format."""

    return Result(
        value=datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def package_version() -> Result[str, str]:
    """Return installed package version for lockfile metadata."""

    try:
        return Result(value=metadata.version("mcp-tela"))
    except metadata.PackageNotFoundError:
        return Result(value="0.1.2")
