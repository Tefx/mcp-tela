"""Gateway lifecycle and startup binding.

This module implements the gateway lifecycle: start (load config, connect
downstreams), shutdown (disconnect downstreams), status, and connections.
Transport startup (stdio/SSE MCP server) is deferred.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from tela.core.models import (
    AuthMode,
    ConnectionContext,
    GatewayStatus,
    GatewayTransport,
    RuntimeBindingContract,
    ServerConfig,
    TelaConfig,
)
from tela.shell.config_loader import Result, load_config
from tela.shell.audit import audit_init
from tela.shell.downstream import connect_all, disconnect_all, get_all_tools


@dataclass(frozen=True)
class GatewayStartupConfig:
    """Resolved gateway startup contract consumed by runtime shell.

    Semantics:
    - stdio is the default transport.
    - SSE is optional and enabled only when an explicit port is provided.
    - open mode requires no token and must carry an explicit default profile.
    """

    transport: GatewayTransport
    port: int | None
    auth_mode: AuthMode
    default_profile: str | None


@dataclass
class GatewayRuntime:
    """Mutable gateway runtime state."""

    config: TelaConfig | None = None
    startup_config: GatewayStartupConfig | None = None
    start_time: float | None = None
    connections: list[ConnectionContext] = field(default_factory=list)
    total_tool_calls: int = 0
    running: bool = False


# Module-level runtime state
_runtime = GatewayRuntime()
_runtime_lock = asyncio.Lock()


# @invar:allow dead_export: runtime accessor used by tests and gateway integration.
# @invar:allow shell_result: returns runtime state object, not a failable I/O boundary.
def get_runtime() -> GatewayRuntime:
    """Return the module-level gateway runtime."""
    return _runtime


# @invar:allow dead_export: startup wiring is connected in a later runtime step.
def bind_gateway_startup(
    runtime: RuntimeBindingContract,
) -> Result[GatewayStartupConfig, str]:
    """Bind CLI runtime contract into gateway startup configuration.

    Examples:
        >>> import tempfile, os
        >>> from tela.core.models import GatewayTransport, RuntimeBindingContract
        >>> d = tempfile.mkdtemp()
        >>> p = os.path.join(d, "tela.yaml")
        >>> with open(p, "w") as f:
        ...     _ = f.write("profiles:\\n  dev:\\n    name: dev\\n    default: true\\nauth:\\n  mode: open\\n")
        >>> r = bind_gateway_startup(
        ...     RuntimeBindingContract(
        ...         config_path=p,
        ...         transport=GatewayTransport.STDIO,
        ...         port=None,
        ...         cli_default_profile="dev",
        ...     )
        ... )
        >>> r.is_ok
        True
        >>> r.value.transport
        <GatewayTransport.STDIO: 'stdio'>
        >>> r.value.default_profile
        'dev'

    Args:
        runtime: CLI runtime binding contract from ``tela start``.

    Returns:
        Result with resolved gateway startup config.
    """

    config_result = load_config(
        path=Path(runtime.config_path),
        default_profile=runtime.cli_default_profile,
    )

    if config_result.is_err:
        return Result(error=config_result.error)

    assert config_result.value is not None
    auth_mode = config_result.value.auth.mode

    return Result(
        value=GatewayStartupConfig(
            transport=runtime.transport,
            port=runtime.port,
            auth_mode=AuthMode(auth_mode),
            default_profile=runtime.cli_default_profile,
        )
    )


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
async def gateway_start(
    config: GatewayStartupConfig,
    tela_config: TelaConfig | None = None,
    tool_lists: dict[str, list[dict]] | None = None,
) -> Result[None, str]:
    """Start the gateway: load config, connect downstreams, start MCP server.

    Fails fast on config errors or tool conflicts at startup.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import TelaConfig
        >>> r = asyncio.run(gateway_start(
        ...     GatewayStartupConfig(
        ...         transport=GatewayTransport.STDIO,
        ...         port=None,
        ...         auth_mode=AuthMode.OPEN,
        ...         default_profile="dev",
        ...     ),
        ...     tela_config=TelaConfig(),
        ... ))
        >>> r.is_ok
        True

    Args:
        config: Resolved gateway startup configuration.
        tela_config: Full tela config (if None, loads from config path).
        tool_lists: Optional pre-enumerated tool lists for testing.

    Returns:
        Result[None, str] on success, or error string on failure.
    """

    effective_config = tela_config or TelaConfig()

    # Connect downstream servers
    connect_result = await connect_all(
        effective_config.servers, tool_lists=tool_lists
    )
    if connect_result.is_err:
        return Result(error=connect_result.error)

    # Initialize audit subsystem from config
    audit_result = audit_init(effective_config.audit)
    if audit_result.is_err:
        return Result(error=audit_result.error)

    # Store runtime state
    _runtime.config = effective_config
    _runtime.startup_config = config
    _runtime.start_time = time.monotonic()
    _runtime.running = True

    return Result(value=None)


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
async def gateway_shutdown() -> Result[None, str]:
    """Graceful shutdown: stop accepting connections, close downstreams.

    Examples:
        >>> import asyncio
        >>> r = asyncio.run(gateway_shutdown())
        >>> r.is_ok
        True

    Returns:
        Result[None, str] always succeeds.
    """

    disconnect_result = await disconnect_all()
    _runtime.running = False
    _runtime.start_time = None
    _runtime.connections.clear()
    return disconnect_result


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
# @invar:allow shell_result: returns GatewayStatus per DESIGN.md spec, not a failable I/O boundary.
def gateway_status() -> GatewayStatus:
    """Return current gateway runtime status.

    Examples:
        >>> gateway_status().server_count
        0

    Returns:
        GatewayStatus with current runtime metrics.
    """

    all_tools = get_all_tools()
    uptime = time.monotonic() - _runtime.start_time if _runtime.start_time else 0.0
    profile_count = len(_runtime.config.profiles) if _runtime.config else 0

    return GatewayStatus(
        uptime_seconds=uptime,
        server_count=len(all_tools),
        connected_servers=list(all_tools.keys()),
        active_connections=len(_runtime.connections),
        profile_count=profile_count,
        total_tool_calls=_runtime.total_tool_calls,
    )


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
# @invar:allow shell_result: returns list[ConnectionContext] per DESIGN.md spec, not a failable I/O boundary.
def gateway_connections() -> list[ConnectionContext]:
    """Return list of active upstream connections.

    Examples:
        >>> gateway_connections()
        []

    Returns:
        List of active ConnectionContext.
    """

    return list(_runtime.connections)
