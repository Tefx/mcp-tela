"""Gateway lifecycle and startup binding.

This module implements the gateway lifecycle: start (load config, connect
downstreams), shutdown (disconnect downstreams), status, and connections.
Transport startup (stdio/SSE MCP server) is deferred.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP

from tela.core.models import (
    AuthMode,
    ConnectionContext,
    GatewayStatus,
    GatewayTransport,
    RuntimeBindingContract,
    TelaConfig,
)
from tela.shell.config_loader import Result, load_config
from tela.shell.audit import audit_init
from tela.shell.downstream import call_tool, connect_all, disconnect_all, get_all_tools


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
    upstream_server: FastMCP | None = None


# @invar:allow shell_result: returns FastMCP runtime object for gateway lifecycle wiring.
def _create_upstream_server(config: GatewayStartupConfig) -> FastMCP:
    """Create FastMCP server instance from gateway transport config."""

    if config.transport == GatewayTransport.SSE and config.port is not None:
        return FastMCP("tela-gateway", port=config.port)

    return FastMCP("tela-gateway")


def _make_registry_tool_handler(server_name: str, tool_name: str):
    """Build per-tool FastMCP handler that forwards to downstream server."""

    async def _forward_tool(**arguments: object) -> dict:
        result = await call_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=dict(arguments),
        )
        if result.is_err:
            assert result.error is not None
            raise RuntimeError(f"{result.error.code}: {result.error.message}")

        assert result.value is not None
        return result.value

    safe_name = "".join(ch if ch.isalnum() else "_" for ch in tool_name)
    _forward_tool.__name__ = f"tool_{safe_name}"
    _forward_tool.__doc__ = (
        f"Forward MCP tool '{tool_name}' to downstream server '{server_name}'."
    )
    return _forward_tool


def _register_profiles_resource(upstream_server: FastMCP) -> None:
    """Register tela.profiles resource on the upstream FastMCP server."""

    from tela.shell.upstream import handle_profiles_list

    @upstream_server.resource(
        "tela://profiles",
        name="tela.profiles",
        description="List configured tela profiles.",
        mime_type="application/json",
    )
    def _profiles_resource() -> str:
        return json.dumps(handle_profiles_list())


def _wire_upstream_handlers(upstream_server: FastMCP) -> None:
    """Wire upstream handlers into FastMCP request handling."""

    from tela.shell.upstream import (
        handle_initialize,
        handle_tools_call,
        handle_tools_list,
    )

    async def _ensure_connection() -> ConnectionContext:
        runtime = get_runtime()
        if runtime.connections:
            return runtime.connections[0]

        init_result = await handle_initialize({})
        if init_result.is_err:
            raise RuntimeError(init_result.error or "INITIALIZE_REJECTED")

        assert init_result.value is not None
        return init_result.value

    @upstream_server._mcp_server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        connection = await _ensure_connection()
        filtered_tools = await handle_tools_list(connection)
        return [
            mcp_types.Tool(
                name=tool["name"],
                inputSchema=dict(tool.get("inputSchema") or {}),
                description="Upstream filtered tool.",
            )
            for tool in filtered_tools
        ]

    @upstream_server._mcp_server.call_tool(validate_input=False)
    async def _call_tool(tool_name: str, arguments: dict[str, object]) -> dict:
        connection = await _ensure_connection()
        result = await handle_tools_call(connection, tool_name, dict(arguments))
        if result.is_err:
            assert result.error is not None
            raise RuntimeError(f"{result.error.code}: {result.error.message}")

        assert result.value is not None
        return result.value


def _register_registry_tools(upstream_server: FastMCP) -> None:
    """Register all resolved registry tools as FastMCP tools."""

    for server_name, resolved_tools in get_all_tools().items():
        for resolved_tool in resolved_tools:
            upstream_server.add_tool(
                _make_registry_tool_handler(server_name, resolved_tool.name),
                name=resolved_tool.name,
                description=(
                    f"Forwarded downstream tool '{resolved_tool.name}' "
                    f"from server '{server_name}'."
                ),
            )


def _wire_reload_notifications() -> None:
    """Bridge reload digest callback into upstream notification broadcaster."""

    from tela.shell.upstream import notify_tools_changed

    async def _notify_all_connections(tools_digest: str) -> None:
        runtime = get_runtime()
        for connection in list(runtime.connections):
            await notify_tools_changed(connection, tools_digest)

    _set_reload_notify_callback(_notify_all_connections)


def _set_reload_notify_callback(callback: object | None) -> None:
    """Set reload notify callback with lazy import to avoid module cycles."""

    from tela.shell.reload import set_notify_callback

    set_notify_callback(callback)


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
    config: TelaConfig | None = None,
) -> Result[GatewayStartupConfig, str]:
    """Bind CLI runtime contract into gateway startup configuration.

    When ``config`` is provided, it is used directly (avoiding a redundant
    ``load_config`` call when the caller has already parsed the config).
    When ``config`` is None, the config is loaded from ``runtime.config_path``.

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
        config: Already-parsed TelaConfig. If provided, skips ``load_config``.

    Returns:
        Result with resolved gateway startup config.
    """

    if config is not None:
        parsed_config = config
    else:
        config_result = load_config(
            path=Path(runtime.config_path),
            default_profile=runtime.cli_default_profile,
        )

        if config_result.is_err:
            return Result(error=config_result.error)

        assert config_result.value is not None
        parsed_config = config_result.value

    auth_mode = parsed_config.auth.mode

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
    connect_result = await connect_all(effective_config.servers, tool_lists=tool_lists)
    if connect_result.is_err:
        return Result(error=connect_result.error)

    # Initialize audit subsystem from config
    audit_result = await audit_init(effective_config.audit)
    if audit_result.is_err:
        return Result(error=audit_result.error)

    upstream_server = _create_upstream_server(config)
    _wire_upstream_handlers(upstream_server)
    _register_profiles_resource(upstream_server)
    _register_registry_tools(upstream_server)
    _wire_reload_notifications()

    # Store runtime state
    async with _runtime_lock:
        _runtime.config = effective_config
        _runtime.startup_config = config
        _runtime.start_time = time.monotonic()
        _runtime.running = True
        _runtime.upstream_server = upstream_server

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
    _set_reload_notify_callback(None)
    async with _runtime_lock:
        _runtime.upstream_server = None
        _runtime.running = False
        _runtime.start_time = None
        _runtime.connections.clear()
    return disconnect_result


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
# @invar:allow shell_result: returns GatewayStatus per DESIGN.md spec, not a failable I/O boundary.
async def gateway_status() -> GatewayStatus:
    """Return current gateway runtime status.

    Examples:
        >>> import asyncio
        >>> asyncio.run(gateway_status()).server_count
        0

    Returns:
        GatewayStatus with current runtime metrics.
    """

    async with _runtime_lock:
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
async def gateway_connections() -> list[ConnectionContext]:
    """Return list of active upstream connections.

    Examples:
        >>> import asyncio
        >>> asyncio.run(gateway_connections())
        []

    Returns:
        List of active ConnectionContext.
    """

    async with _runtime_lock:
        return list(_runtime.connections)
