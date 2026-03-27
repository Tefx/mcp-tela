"""Serve command surface for HTTP gateway startup.

Implements ``tela serve`` as the HTTP gateway entrypoint with lockfile,
bearer-token lifecycle, config watching, and optional idle shutdown.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from starlette.applications import Starlette

from tela.core.models import AuthMode, GatewayTransport, LockfileData, TelaConfig
from tela.shell.config_loader import Result, load_config
from tela.shell.gateway import (
    GatewayStartupConfig,
    gateway_shutdown,
    gateway_start,
)
from tela.shell.gateway_runtime import (
    get_expected_bearer_token,
    get_upstream_http_app,
    get_upstream_log_level,
    is_upstream_server_initialized,
)
from tela.shell.idle_shutdown import init_idle_manager, shutdown_idle_manager
from tela.shell.lockfile import delete_lockfile, generate_bearer_token, write_lockfile


CONFIG_WATCH_POLL_SECONDS = 0.5
HTTP_SERVER_BIND_TIMEOUT_SECONDS = 5.0
HTTP_SERVER_SHUTDOWN_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class _HttpServerHandle:
    """Track running Streamable HTTP server task and bound port."""

    task: asyncio.Task[None]
    bound_port: int
    request_shutdown: Callable[[], None]


# @shell_complexity: serve command validates CLI contracts and delegates lifecycle wiring.
def serve_command(
    config_path: str = "tela.yaml",
    port: int = 0,
    host: str = "127.0.0.1",
    default_profile: str | None = None,
    idle_timeout: int = 300,
    token: str | None = None,
) -> Result[int, str]:
    """Run ``tela serve`` as the HTTP gateway process.

    Args:
        config_path: Path to gateway config file.
        port: HTTP port to bind (``0`` means ephemeral OS-selected port).
        host: HTTP bind host address.
        default_profile: Open-mode default profile override.
        idle_timeout: Seconds to wait before idle-triggered shutdown.
        token: Optional bearer token override.

    Returns:
        Result containing process exit code.

    Raises:
        None. Failures are encoded in ``Result.error``.
    """

    if idle_timeout < 0:
        return Result(error="INVALID_IDLE_TIMEOUT: --idle-timeout must be >= 0")
    if port < 0 or port > 65535:
        return Result(error="INVALID_PORT: --port must be in range 0..65535")

    config_result = load_config(path=Path(config_path), default_profile=default_profile)
    if config_result.is_err:
        return Result(error=config_result.error)
    assert config_result.value is not None

    startup_config = GatewayStartupConfig(
        transport=GatewayTransport.HTTP,
        host=host,
        port=port,
        auth_mode=AuthMode(config_result.value.auth.mode),
        default_profile=default_profile or config_result.value.resolved_default_profile,
    )

    token_result = _resolve_bearer_token(token)
    if token_result.is_err:
        return Result(error=token_result.error)
    assert token_result.value is not None
    resolved_token = token_result.value
    print(f"tela: bearer token: {resolved_token}", file=sys.stderr)

    run_result = asyncio.run(
        _run_serve_gateway(
            startup_config=startup_config,
            tela_config=config_result.value,
            config_path=Path(config_path),
            idle_timeout=idle_timeout,
            bearer_token=resolved_token,
        )
    )
    if run_result.is_err:
        return Result(error=run_result.error)
    return Result(value=0)


# @shell_orchestration: token resolution selects CLI/env/generated source for HTTP auth boundary.
def _resolve_bearer_token(cli_token: str | None) -> Result[str, str]:
    """Resolve bearer-token source with strict precedence.

    Precedence order:
    1. ``--token``
    2. ``TELA_BEARER_TOKEN`` environment variable
    3. Generated token via ``secrets.token_urlsafe(32)``
    """

    if cli_token is not None:
        return Result(value=cli_token)

    env_token = os.environ.get("TELA_BEARER_TOKEN")
    if env_token is not None:
        return Result(value=env_token)

    generated_result = generate_bearer_token()
    if generated_result.is_err:
        return Result(error=generated_result.error)
    assert generated_result.value is not None
    return Result(value=generated_result.value)


# @shell_complexity: lifecycle orchestration coordinates startup, signals, server run, and cleanup.
async def _run_serve_gateway(
    *,
    startup_config: GatewayStartupConfig,
    tela_config: TelaConfig,
    config_path: Path,
    idle_timeout: int,
    bearer_token: str,
) -> Result[None, str]:
    """Run HTTP gateway lifecycle with lockfile and background watchers."""
    stop_event = asyncio.Event()

    startup_result = await gateway_start(
        startup_config,
        tela_config=tela_config,
        expected_bearer_token=bearer_token,
    )
    if startup_result.is_err:
        return Result(error=startup_result.error)

    started_at_result = _utc_now_iso()
    if started_at_result.is_err:
        await gateway_shutdown()
        return Result(error=started_at_result.error)
    assert started_at_result.value is not None

    version_result = _package_version()
    if version_result.is_err:
        await gateway_shutdown()
        return Result(error=version_result.error)
    assert version_result.value is not None

    if not is_upstream_server_initialized().value:
        await gateway_shutdown()
        delete_lockfile()
        return Result(error="STARTUP_FAILED: upstream MCP server not initialized")

    upstream_app_result = get_upstream_http_app()
    if upstream_app_result.is_err:
        await gateway_shutdown()
        delete_lockfile()
        return Result(error=upstream_app_result.error)
    assert upstream_app_result.value is not None

    http_server_result = await _launch_streamable_http_server(
        upstream_app=upstream_app_result.value,
        upstream_log_level=get_upstream_log_level().value,
        host=startup_config.host,
        requested_port=startup_config.port or 0,
    )
    if http_server_result.is_err:
        await gateway_shutdown()
        return Result(error=http_server_result.error)
    assert http_server_result.value is not None
    http_server = http_server_result.value

    lockfile_result = write_lockfile(
        LockfileData(
            pid=os.getpid(),
            host=startup_config.host,
            port=http_server.bound_port,
            token=bearer_token,
            started_at=started_at_result.value,
            config_path=str(config_path.resolve()),
            version=version_result.value,
        )
    )
    if lockfile_result.is_err:
        await _stop_http_server(http_server)
        await gateway_shutdown()
        return Result(error=lockfile_result.error)

    _install_signal_handlers(stop_event)
    watcher_task = asyncio.create_task(
        _watch_config_changes(
            config_path=config_path,
            default_profile=startup_config.default_profile,
            stop_event=stop_event,
        )
    )
    idle_task = asyncio.create_task(
        _idle_shutdown_watch(idle_timeout_seconds=idle_timeout, stop_event=stop_event)
    )
    stop_task = asyncio.create_task(stop_event.wait())

    error: str | None = None
    try:
        done, _pending = await asyncio.wait(
            {http_server.task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if http_server.task in done:
            if http_server.task.cancelled():
                error = "HTTP_RUN_CANCELLED: streamable HTTP server cancelled"
            else:
                server_exc = http_server.task.exception()
                if server_exc is not None:
                    error = f"HTTP_RUN_FAILED: {server_exc}"
    finally:
        stop_event.set()
        await _await_task(watcher_task)
        await _await_task(idle_task)
        await _stop_http_server(http_server)
        stop_task.cancel()
        try:
            await stop_task
        except asyncio.CancelledError:
            pass
        await gateway_shutdown()
        delete_lockfile()
        _remove_signal_handlers()

    if error is not None:
        return Result(error=error)
    return Result(value=None)


# @shell_complexity: startup requires observing uvicorn socket bind before lockfile publication.
async def _launch_streamable_http_server(
    *,
    upstream_app: Starlette,
    upstream_log_level: str,
    host: str,
    requested_port: int,
) -> Result[_HttpServerHandle, str]:
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

        bound_port_result = _extract_bound_port(server)
        if bound_port_result.is_err:
            await _stop_http_server(
                _HttpServerHandle(
                    task=task,
                    bound_port=requested_port,
                    request_shutdown=lambda: setattr(server, "should_exit", True),
                )
            )
            return Result(error=bound_port_result.error)

        bound_port = bound_port_result.value
        if bound_port is not None:
            return Result(
                value=_HttpServerHandle(
                    task=task,
                    bound_port=bound_port,
                    request_shutdown=lambda: setattr(server, "should_exit", True),
                )
            )

        await asyncio.sleep(0.01)

    await _stop_http_server(
        _HttpServerHandle(
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


# @shell_complexity: loops through uvicorn listener/socket structures to resolve bound port.
def _extract_bound_port(server: object) -> Result[int | None, str]:
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


# @shell_orchestration: cancels and awaits asyncio server task with timeout for graceful shutdown.
async def _stop_http_server(server: _HttpServerHandle) -> None:
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


# @shell_orchestration: registers OS signal handlers on the asyncio event loop.
def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Install SIGINT/SIGTERM handlers that trigger clean shutdown."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            continue


# @shell_orchestration: removes OS signal handlers from the asyncio event loop.
def _remove_signal_handlers() -> None:
    """Remove process signal handlers installed by this module."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.remove_signal_handler(sig)
        except NotImplementedError:
            continue


# @shell_complexity: watcher handles mtime polling and error-tolerant reload dispatch.
async def _watch_config_changes(
    *,
    config_path: Path,
    default_profile: str | None,
    stop_event: asyncio.Event,
) -> None:
    """Poll config mtime and run hot-reload callback when it changes."""

    from tela.shell.gateway import gateway_reload_config_from_disk

    last_mtime_result = _config_mtime_ns(config_path)
    if last_mtime_result.is_err:
        return
    last_mtime_ns = last_mtime_result.value

    while not stop_event.is_set():
        await asyncio.sleep(CONFIG_WATCH_POLL_SECONDS)
        current_mtime_result = _config_mtime_ns(config_path)
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
        )
        if reload_result.is_err:
            print(
                f"warning: config reload failed: {reload_result.error}",
                file=sys.stderr,
            )
        last_mtime_ns = current_mtime_ns


def _config_mtime_ns(config_path: Path) -> Result[int | None, str]:
    """Return file mtime (ns) for config watcher, or None if unreadable."""

    try:
        return Result(value=config_path.stat().st_mtime_ns)
    except OSError:
        return Result(value=None)


# @shell_complexity: idle monitor handles connection reset and timeout-triggered shutdown.
async def _idle_shutdown_watch(
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
            f"warning: idle shutdown init failed: {init_result.error}", file=sys.stderr
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


# @shell_orchestration: cancels and awaits asyncio background task during shutdown sequence.
async def _await_task(task: asyncio.Task[object]) -> None:
    """Await or cancel background task during shutdown."""

    if task.done():
        await task
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return


# @shell_orchestration: shell lockfile timestamp formatting is startup metadata plumbing.
def _utc_now_iso() -> Result[str, str]:
    """Return current UTC timestamp in lockfile ISO-8601 format."""

    return Result(
        value=datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# @shell_orchestration: shell lockfile version is process/package metadata plumbing.
def _package_version() -> Result[str, str]:
    """Return installed package version for lockfile metadata."""

    try:
        return Result(value=metadata.version("mcp-tela"))
    except metadata.PackageNotFoundError:
        return Result(value="0.1.0")
