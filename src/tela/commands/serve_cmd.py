"""Serve command surface for CLI/config/token facade.

Implements ``tela serve`` as the HTTP gateway entrypoint. This module is a
thin facade: it validates CLI arguments, resolves config and bearer tokens,
and delegates the runtime loop to ``tela.shell.serve_runtime``.

Startup log-state vocabulary is contract-owned by
``tela.commands.remote_state`` so serve, connect, CLI status, and ``GET /status``
share one diagnostic fact model.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from tela.core.models import AuthMode, GatewayTransport, LockfileData, TelaConfig
from tela.shell.config_loader import load_config
from tela.shell.result import Result
from tela.shell.gateway import (
    GatewayStartupConfig,
    apply_reaper_overrides,
    gateway_converge_startup,
    gateway_prepare_startup,
    gateway_shutdown,
)
from tela.shell.gateway_runtime import (
    is_upstream_server_initialized,
    get_upstream_http_app,
    get_upstream_log_level,
)
from tela.shell.lockfile import delete_lockfile, generate_bearer_token, write_lockfile
from tela.shell.serve_runtime import (
    await_task,
    idle_shutdown_watch,
    install_signal_handlers,
    launch_streamable_http_server,
    package_version,
    remove_signal_handlers,
    rollback_after_post_bind_convergence_failure,
    stop_http_server,
    utc_now_iso,
    watch_config_changes,
)


# @shell_complexity: serve command validates CLI contracts and delegates lifecycle wiring.
def serve_command(
    config_path: str = "tela.yaml",
    port: int = 0,
    host: str = "127.0.0.1",
    default_profile: str | None = None,
    idle_timeout: int = 300,
    reaper_sweep_interval: float | None = None,
    reaper_native_ttl: float | None = None,
    reaper_bridge_ttl: float | None = None,
    token: str | None = None,
) -> Result[int, str]:
    """Run ``tela serve`` as the HTTP gateway process.

    Args:
        config_path: Path to gateway config file.
        port: HTTP port to bind (``0`` means ephemeral OS-selected port).
        host: HTTP bind host address.
        default_profile: Open-mode default profile override.
        idle_timeout: Seconds to wait before idle-triggered shutdown.
        reaper_sweep_interval: Optional CLI override for reaper sweep interval.
        reaper_native_ttl: Optional CLI override for native idle TTL.
        reaper_bridge_ttl: Optional CLI override for bridge idle TTL.
        token: Optional bearer token override.

    Returns:
        Result containing process exit code.

    Raises:
        None. Failures are encoded in ``Result.error``.
    """

    if idle_timeout < 0:
        return Result(error="INVALID_IDLE_TIMEOUT: --idle-timeout must be >= 0")
    if reaper_sweep_interval is not None and reaper_sweep_interval < 0:
        return Result(
            error="INVALID_REAPER_SWEEP_INTERVAL: --reaper-sweep-interval must be >= 0"
        )
    if reaper_native_ttl is not None and reaper_native_ttl < 0:
        return Result(
            error="INVALID_REAPER_NATIVE_TTL: --reaper-native-ttl must be >= 0"
        )
    if reaper_bridge_ttl is not None and reaper_bridge_ttl < 0:
        return Result(
            error="INVALID_REAPER_BRIDGE_TTL: --reaper-bridge-ttl must be >= 0"
        )
    if port < 0 or port > 65535:
        return Result(error="INVALID_PORT: --port must be in range 0..65535")

    config_result = load_config(path=Path(config_path), default_profile=default_profile)
    if config_result.is_err:
        return Result(error=config_result.error)
    assert config_result.value is not None
    effective_config_result = apply_reaper_overrides(
        config_result.value,
        sweep_interval_seconds=reaper_sweep_interval,
        native_idle_ttl_seconds=reaper_native_ttl,
        bridge_idle_ttl_seconds=reaper_bridge_ttl,
    )
    if effective_config_result.is_err:
        return Result(error=effective_config_result.error)
    assert effective_config_result.value is not None
    effective_config = effective_config_result.value

    startup_config = GatewayStartupConfig(
        transport=GatewayTransport.HTTP,
        host=host,
        port=port,
        auth_mode=AuthMode(effective_config.auth.mode),
        default_profile=default_profile or effective_config.resolved_default_profile,
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
            tela_config=effective_config,
            config_path=Path(config_path),
            idle_timeout=idle_timeout,
            reaper_sweep_interval=reaper_sweep_interval,
            reaper_native_ttl=reaper_native_ttl,
            reaper_bridge_ttl=reaper_bridge_ttl,
            bearer_token=resolved_token,
        )
    )
    if run_result.is_err:
        return Result(error=run_result.error)
    return Result(value=0)


# @shell_orchestration: shared CLI/env token precedence for connect and serve commands.
def _resolve_bearer_token_cli_or_env(cli_token: str | None) -> Result[str, str]:
    """Resolve bearer token from CLI or environment with strict precedence.

    This helper is shared between ``tela serve`` and ``tela connect`` for the
    CLI/env portion of their token precedence. Each command adds its own
    command-specific fallback after this helper returns.

    Precedence order:
    1. ``--token`` CLI argument
    2. ``TELA_BEARER_TOKEN`` environment variable

    Returns:
        Result with the resolved token string, or an error if neither
        CLI token nor environment token is available.
    """

    if cli_token is not None:
        return Result(value=cli_token)

    env_token = os.environ.get("TELA_BEARER_TOKEN")
    if env_token is not None:
        return Result(value=env_token)

    return Result(error="MISSING_TOKEN: --token or TELA_BEARER_TOKEN is required")


# @shell_orchestration: token resolution selects CLI/env/generated source for HTTP auth boundary.
def _resolve_bearer_token(cli_token: str | None) -> Result[str, str]:
    """Resolve bearer-token source with strict precedence.

    Precedence order:
    1. ``--token``
    2. ``TELA_BEARER_TOKEN`` environment variable
    3. Generated token via ``secrets.token_urlsafe(32)``
    """

    cli_env_result = _resolve_bearer_token_cli_or_env(cli_token)
    if cli_env_result.is_ok:
        return cli_env_result

    # Command-specific fallback: generate a token
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
    reaper_sweep_interval: float | None,
    reaper_native_ttl: float | None,
    reaper_bridge_ttl: float | None,
    bearer_token: str,
) -> Result[None, str]:
    """Run HTTP gateway lifecycle with lockfile and background watchers."""
    stop_event = asyncio.Event()

    prepare_result = await gateway_prepare_startup(
        startup_config,
        tela_config=tela_config,
        expected_bearer_token=bearer_token,
    )
    if prepare_result.is_err:
        return Result(error=prepare_result.error)

    started_at_result = utc_now_iso()
    if started_at_result.is_err:
        await gateway_shutdown()
        return Result(error=started_at_result.error)
    assert started_at_result.value is not None

    version_result = package_version()
    if version_result.is_err:
        await gateway_shutdown()
        return Result(error=version_result.error)
    assert version_result.value is not None

    init_result = is_upstream_server_initialized()
    if init_result.is_err:
        await gateway_shutdown()
        delete_lockfile()
        return Result(error=init_result.error)
    if not init_result.value:
        await gateway_shutdown()
        delete_lockfile()
        return Result(error="STARTUP_FAILED: upstream MCP server not initialized")

    upstream_app_result = get_upstream_http_app()
    if upstream_app_result.is_err:
        await gateway_shutdown()
        delete_lockfile()
        return Result(error=upstream_app_result.error)
    assert upstream_app_result.value is not None

    log_level_result = get_upstream_log_level()
    _log_level_raw = log_level_result.value
    resolved_log_level: str = _log_level_raw if _log_level_raw is not None else "info"

    http_server_result = await launch_streamable_http_server(
        upstream_app=upstream_app_result.value,
        upstream_log_level=resolved_log_level,
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
        await stop_http_server(http_server)
        await gateway_shutdown()
        return Result(error=lockfile_result.error)

    converge_result = await gateway_converge_startup()
    if converge_result.is_err:
        rollback_result = await rollback_after_post_bind_convergence_failure(
            http_server=http_server,
            convergence_error=converge_result.error,
        )
        return rollback_result

    install_signal_handlers(stop_event)
    watcher_task = asyncio.create_task(
        watch_config_changes(
            config_path=config_path,
            default_profile=startup_config.default_profile,
            reaper_sweep_interval=reaper_sweep_interval,
            reaper_native_ttl=reaper_native_ttl,
            reaper_bridge_ttl=reaper_bridge_ttl,
            stop_event=stop_event,
        )
    )
    idle_task = asyncio.create_task(
        idle_shutdown_watch(idle_timeout_seconds=idle_timeout, stop_event=stop_event)
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
        await await_task(watcher_task)
        await await_task(idle_task)
        await stop_http_server(http_server)
        stop_task.cancel()
        try:
            await stop_task
        except asyncio.CancelledError:
            pass
        await gateway_shutdown()
        delete_lockfile()
        remove_signal_handlers()

    if error is not None:
        return Result(error=error)
    return Result(value=None)
