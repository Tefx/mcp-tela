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
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from tela.core.models import AuthMode, GatewayTransport, LockfileData, TelaConfig
from tela.shell.config_loader import Result, load_config
from tela.shell.gateway import (
    GatewayStartupConfig,
    gateway_shutdown,
    gateway_start,
    get_runtime,
)
from tela.shell.lockfile import delete_lockfile, generate_bearer_token, write_lockfile


CONFIG_WATCH_POLL_SECONDS = 0.5


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

    return Result(value=str(generate_bearer_token()))


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

    old_token = os.environ.get("TELA_BEARER_TOKEN")
    os.environ["TELA_BEARER_TOKEN"] = bearer_token

    startup_result = await gateway_start(startup_config, tela_config=tela_config)
    if startup_result.is_err:
        _restore_bearer_token_env(old_token)
        return Result(error=startup_result.error)

    started_at_result = _utc_now_iso()
    if started_at_result.is_err:
        await gateway_shutdown()
        _restore_bearer_token_env(old_token)
        return Result(error=started_at_result.error)
    assert started_at_result.value is not None

    version_result = _package_version()
    if version_result.is_err:
        await gateway_shutdown()
        _restore_bearer_token_env(old_token)
        return Result(error=version_result.error)
    assert version_result.value is not None

    lockfile_result = write_lockfile(
        LockfileData(
            pid=os.getpid(),
            host=startup_config.host,
            port=startup_config.port or 0,
            token=bearer_token,
            started_at=started_at_result.value,
            config_path=str(config_path.resolve()),
            version=version_result.value,
        )
    )
    if lockfile_result.is_err:
        await gateway_shutdown()
        _restore_bearer_token_env(old_token)
        return Result(error=lockfile_result.error)

    runtime = get_runtime()
    if runtime.upstream_server is None:
        await gateway_shutdown()
        delete_lockfile()
        _restore_bearer_token_env(old_token)
        return Result(error="STARTUP_FAILED: upstream MCP server not initialized")

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    watcher_task = asyncio.create_task(
        _watch_config_changes(
            config_path=config_path,
            default_profile=startup_config.default_profile,
            stop_event=stop_event,
        )
    )
    idle_task = asyncio.create_task(
        _idle_shutdown_watch(
            idle_timeout_seconds=idle_timeout,
            stop_event=stop_event,
        )
    )
    stop_task = asyncio.create_task(stop_event.wait())
    server_task = asyncio.create_task(
        runtime.upstream_server.run_streamable_http_async()
    )

    error: str | None = None
    try:
        done, _pending = await asyncio.wait(
            {server_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if server_task in done:
            if server_task.cancelled():
                error = "HTTP_RUN_CANCELLED: streamable HTTP server cancelled"
            else:
                server_exc = server_task.exception()
                if server_exc is not None:
                    error = f"HTTP_RUN_FAILED: {server_exc}"
        else:
            if not server_task.done():
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass
    finally:
        stop_event.set()
        await _await_task(watcher_task)
        await _await_task(idle_task)
        stop_task.cancel()
        try:
            await stop_task
        except asyncio.CancelledError:
            pass
        await gateway_shutdown()
        delete_lockfile()
        _remove_signal_handlers()
        _restore_bearer_token_env(old_token)

    if error is not None:
        return Result(error=error)
    return Result(value=None)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Install SIGINT/SIGTERM handlers that trigger clean shutdown."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            continue


def _remove_signal_handlers() -> None:
    """Remove process signal handlers installed by this module."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.remove_signal_handler(sig)
        except NotImplementedError:
            continue


def _restore_bearer_token_env(previous_token: str | None) -> None:
    """Restore ``TELA_BEARER_TOKEN`` environment variable after command exit."""

    if previous_token is None:
        os.environ.pop("TELA_BEARER_TOKEN", None)
        return
    os.environ["TELA_BEARER_TOKEN"] = previous_token


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
    """Trigger shutdown when no active bridge connections for the idle timeout."""

    if idle_timeout_seconds == 0:
        return

    idle_started_at = time.monotonic()
    while not stop_event.is_set():
        runtime = get_runtime()
        if runtime.connections:
            idle_started_at = time.monotonic()
        elif time.monotonic() - idle_started_at >= float(idle_timeout_seconds):
            print("tela: idle timeout reached, shutting down", file=sys.stderr)
            stop_event.set()
            return
        await asyncio.sleep(poll_interval_seconds)


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
