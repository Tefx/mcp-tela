"""Gateway lifecycle and startup binding.

This module implements the gateway lifecycle: start (load config, connect
downstreams), shutdown (disconnect downstreams), status, and connections.
Transport startup (stdio/SSE/HTTP) is wired via CLI in tela.cli.
"""

# @invar:allow file_size: Gateway initialization is a single-shot startup routine; splitting requires invasive refactor of lifecycle ownership. This module consolidates all lifecycle, HTTP routing, and server-creation logic that would otherwise need cross-module coordination across startup/shutdown/status/connections phases.

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tela.core.models import (
    AuthMode,
    ConnectRequest,
    ConnectionContext,
    DisconnectRequest,
    GatewayStatus,
    GatewayTransport,
    RuntimeBindingContract,
    TelaConfig,
)
from tela.shell.config_loader import Result, load_config
from tela.shell.audit import audit_close, audit_init
from tela.shell.downstream import (
    connect_all,
    disconnect_all,
    get_all_tools,
    get_server_instructions,
)
from tela.shell.surface_instructions import (
    compose_gateway_and_downstream,
    get_gateway_surface_instructions,
)
from tela.shell.gateway_http_auth import extract_bearer_token
from tela.shell.gateway_runtime import (  # noqa: F401 — re-export for backward compat
    _runtime,
    _runtime_lock,
    RuntimeStatusSnapshot,
    add_runtime_connection,
    clear_runtime_connections,
    get_expected_bearer_token,
    get_runtime_config,
    get_runtime_connections_snapshot,
    get_runtime_secrets,
    get_runtime_status_snapshot,
    get_upstream_http_app,
    get_upstream_log_level,
    get_upstream_server,
    increment_tool_calls,
    is_runtime_running,
    is_upstream_server_initialized,
    remove_runtime_connection,
    set_runtime_config,
    set_runtime_running,
    set_runtime_secrets,
    set_runtime_total_tool_calls,
    set_upstream_server,
    with_upstream_server,
)


@dataclass(frozen=True)
class GatewayStartupConfig:
    """Resolved gateway startup contract consumed by runtime shell.

    Semantics:
    - stdio is the default transport.
    - HTTP (Streamable HTTP) is the default remote transport when a port is given.
    - SSE is the legacy remote transport, retained for backward compatibility.
    - open mode requires no token and must carry an explicit default profile.
    """

    transport: GatewayTransport
    port: int | None = None
    auth_mode: AuthMode = AuthMode.TOKEN
    default_profile: str | None = None
    host: str = "127.0.0.1"


# @shell_orchestration: wires HTTP endpoint handlers onto FastMCP Starlette app.
# @shell_complexity: mounted HTTP adapters enforce auth and payload contracts per endpoint.
def _register_http_routes(upstream_server: FastMCP) -> None:
    """Register mounted HTTP liveness and lifecycle routes on FastMCP app."""

    from tela.shell.http_routes import (
        handle_connect,
        handle_disconnect,
        handle_health,
        handle_status,
    )

    def _as_error_response(error: str) -> JSONResponse:
        status_code = 400
        if error.startswith("AUTH_INVALID_TOKEN"):
            status_code = 401
        elif error.startswith("CONNECTION_NOT_FOUND"):
            status_code = 404
        elif error.startswith("GATEWAY_NOT_STARTED"):
            status_code = 503
        return JSONResponse(status_code=status_code, content={"error": error})

    @upstream_server.custom_route("/health", methods=["GET"])
    async def _health_route(_request: Request) -> Response:
        health_result = handle_health()
        if health_result.is_err:
            return JSONResponse(status_code=500, content={"error": health_result.error})
        assert health_result.value is not None
        return JSONResponse(content=health_result.value.model_dump())

    @upstream_server.custom_route("/status", methods=["GET"])
    async def _status_route(request: Request) -> Response:
        token_result = extract_bearer_token(request)
        if token_result.is_err:
            assert token_result.error is not None
            return _as_error_response(token_result.error)
        assert token_result.value is not None

        with _runtime_lock:
            expected_token = _runtime.expected_bearer_token or ""
        # handle_status computes authoritative lifecycle facts (including connected_servers)
        # by querying the downstream registry directly, so no separate gateway_status() call needed.
        status_result = handle_status(token_result.value, expected_token)
        if status_result.is_err:
            assert status_result.error is not None
            return _as_error_response(status_result.error)
        assert status_result.value is not None
        return JSONResponse(content=status_result.value.model_dump())

    @upstream_server.custom_route("/connect", methods=["POST"])
    async def _connect_route(request: Request) -> Response:
        token_result = extract_bearer_token(request)
        if token_result.is_err:
            assert token_result.error is not None
            return _as_error_response(token_result.error)
        assert token_result.value is not None

        try:
            payload = ConnectRequest.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "INVALID_REQUEST: invalid connect payload"},
            )

        with _runtime_lock:
            expected_token = _runtime.expected_bearer_token or ""
        connect_result = handle_connect(token_result.value, expected_token, payload)
        if connect_result.is_err:
            assert connect_result.error is not None
            return _as_error_response(connect_result.error)
        from tela.shell.idle_shutdown import get_idle_manager

        idle_manager = get_idle_manager()
        if idle_manager is not None:
            _ = await idle_manager.increment()
        assert connect_result.value is not None
        return JSONResponse(content=dict(connect_result.value))

    @upstream_server.custom_route("/disconnect", methods=["POST"])
    async def _disconnect_route(request: Request) -> Response:
        token_result = extract_bearer_token(request)
        if token_result.is_err:
            assert token_result.error is not None
            return _as_error_response(token_result.error)
        assert token_result.value is not None

        try:
            payload = DisconnectRequest.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "INVALID_REQUEST: invalid disconnect payload"},
            )

        with _runtime_lock:
            expected_token = _runtime.expected_bearer_token or ""
        disconnect_result = handle_disconnect(
            token_result.value, expected_token, payload
        )
        if disconnect_result.is_err:
            assert disconnect_result.error is not None
            return _as_error_response(disconnect_result.error)
        from tela.shell.idle_shutdown import get_idle_manager

        idle_manager = get_idle_manager()
        if idle_manager is not None:
            _ = await idle_manager.decrement()
        assert disconnect_result.value is not None
        return JSONResponse(content=dict(disconnect_result.value))


# @shell_complexity: Lifecycle event handlers with inherently branching behavior — routes/priorities/status modes are mutually exclusive by design.
def _merge_downstream_instructions(config: TelaConfig) -> Result[str | None, str]:
    """Merge instructions from all downstream servers into a single Markdown string.

    Semantics per server ``instructions`` field:
    - ``None`` (default): Passthrough downstream instructions if available.
    - ``False``: Suppress this server's instructions entirely.
    - ``str``: Override with the provided string, ignoring downstream.

    Output format is Markdown with H2 headers for each contributing server:
    ```
    ## ServerName

    <instructions or override>

    Available tools:
    - tool_1
    - tool_2
    ```

    Returns Result with None if no servers contribute instructions after
    applying suppression/override rules.

    Args:
        config: TelaConfig with server configurations.

    Returns:
        Result with merged Markdown string, or None if nothing to merge.
    """

    instructions_result = get_server_instructions()
    if instructions_result.is_err:
        return Result(error=instructions_result.error)
    assert instructions_result.value is not None
    downstream_instructions = instructions_result.value

    tools_result = get_all_tools()
    if tools_result.is_err:
        return Result(error=tools_result.error)
    assert tools_result.value is not None
    tools_by_server = tools_result.value

    parts: list[str] = []
    for server_name, server_config in config.servers.items():
        final_instructions: str | None = None
        if server_config.instructions is False:
            continue
        elif isinstance(server_config.instructions, str):
            final_instructions = server_config.instructions
        else:
            final_instructions = downstream_instructions.get(server_name)
        if not final_instructions:
            continue

        section = f"## {server_name}\n\n{final_instructions}"
        server_tools = tools_by_server.get(server_name, [])
        if server_tools:
            tool_names = [tool.name for tool in server_tools]
            tools_list = "\n".join(f"- {name}" for name in sorted(tool_names))
            section += f"\n\nAvailable tools:\n{tools_list}"
        parts.append(section)

    if not parts:
        return Result(value=None)

    return Result(value="\n\n".join(parts))


# @invar:allow shell_too_complex: Lifecycle event handlers with inherently branching behavior — routes/priorities/status modes are mutually exclusive by design.
def _create_upstream_server(
    startup_config: GatewayStartupConfig,
    tela_config: TelaConfig,
) -> Result[FastMCP, str]:
    """Create FastMCP server instance from gateway transport config.

    Args:
        startup_config: Gateway startup config (transport, port, etc.).
        tela_config: Full tela config with servers and profiles.

    Returns:
        Result with FastMCP server instance.
    """

    downstream_result = _merge_downstream_instructions(tela_config)
    if downstream_result.is_err:
        return Result(error=downstream_result.error)

    gateway_result = get_gateway_surface_instructions()
    if gateway_result.is_err:
        return Result(error=gateway_result.error)
    assert gateway_result.value is not None

    compose_result = compose_gateway_and_downstream(
        gateway_result.value,
        downstream_result.value,
    )
    if compose_result.is_err:
        return Result(error=compose_result.error)
    merged_instructions = compose_result.value

    if (
        startup_config.transport in (GatewayTransport.SSE, GatewayTransport.HTTP)
        and startup_config.port is not None
    ):
        return Result(
            value=FastMCP(
                "tela-gateway",
                instructions=merged_instructions,
                host=startup_config.host,
                port=startup_config.port,
            )
        )

    return Result(value=FastMCP("tela-gateway", instructions=merged_instructions))


# @shell_orchestration: registers FastMCP resource endpoint for profile introspection.
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
        result = handle_profiles_list()
        if result.is_err:
            raise RuntimeError(result.error or "PROFILE_LIST_REJECTED")
        assert result.value is not None
        return json.dumps(result.value)


# @shell_complexity: wiring composes initialize/list/call adapters for FastMCP boundary.
def _wire_upstream_handlers(upstream_server: FastMCP) -> None:
    """Wire upstream handlers into FastMCP request handling."""

    from mcp.server.lowlevel.server import request_ctx

    from tela.shell.upstream import (
        capture_session,
        find_connection_for_session,
        handle_initialize,
        handle_tools_call,
        handle_tools_list,
    )

    async def _ensure_connection() -> ConnectionContext:
        # Session-aware: return existing connection, or create new one.
        # Use locked snapshot to prevent observing torn/stale connections.
        try:
            with _runtime_lock:
                connections_snapshot = list(_runtime.connections)
            conn_r = find_connection_for_session(
                request_ctx.get().session, connections_snapshot
            )
            if conn_r.is_ok and conn_r.value is not None:
                return conn_r.value
        except LookupError:
            pass
        init_result = await handle_initialize({})
        if init_result.is_err:
            raise RuntimeError(init_result.error or "INITIALIZE_REJECTED")
        assert init_result.value is not None
        return init_result.value

    def _build_tool_annotations(
        annotations: dict | None,
    ) -> mcp_types.ToolAnnotations | None:
        """Convert annotations dict to ToolAnnotations if present."""
        if annotations is None:
            return None
        return mcp_types.ToolAnnotations(**annotations)

    @upstream_server._mcp_server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        connection = await _ensure_connection()

        # Capture upstream MCP session for notification delivery.
        try:
            capture_session(connection.connection_id, request_ctx.get().session)
        except LookupError:
            pass  # No request context (e.g. stdio without session capture)

        tools_result = await handle_tools_list(connection)
        if tools_result.is_err:
            raise RuntimeError(tools_result.error or "TOOLS_LIST_REJECTED")
        assert tools_result.value is not None
        filtered_tools = tools_result.value
        return [
            mcp_types.Tool(
                name=tool["name"],
                inputSchema=dict(tool.get("inputSchema") or {}),
                description=tool.get("description", ""),
                title=tool.get("title"),
                outputSchema=tool.get("outputSchema"),
                annotations=_build_tool_annotations(tool.get("annotations")),
            )
            for tool in filtered_tools
        ]

    @upstream_server._mcp_server.call_tool(validate_input=False)
    async def _call_tool(
        tool_name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        connection = await _ensure_connection()
        result = await handle_tools_call(connection, tool_name, dict(arguments))
        if result.is_err:
            assert result.error is not None
            raise RuntimeError(f"{result.error.code}: {result.error.message}")

        assert result.value is not None
        # Return CallToolResult to bypass output normalization/re-validation;
        # gateway proxies downstream results as-is.
        return mcp_types.CallToolResult.model_validate(result.value)


def _wire_reload_notifications() -> None:
    """Bridge reload digest callback into upstream notification broadcaster."""

    from tela.shell.upstream import notify_tools_changed

    async def _notify_all_connections(tools_digest: str) -> None:
        with _runtime_lock:
            connections = list(_runtime.connections)
        for connection in connections:
            await notify_tools_changed(connection, tools_digest)

    _set_reload_notify_callback(_notify_all_connections)


# @shell_orchestration: lazy-import callback setter avoids module cycles in reload wiring.
def _set_reload_notify_callback(
    callback: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Set reload notify callback with lazy import to avoid module cycles."""

    from tela.shell.reload import set_notify_callback

    _ = set_notify_callback(callback)


async def gateway_reload_config_from_disk(
    config_path: Path,
    default_profile: str | None,
) -> Result[None, str]:
    """Load config from disk and apply runtime hot-reload callback.

    This is the production runtime callback target for config-file watcher
    integrations.

    Args:
        config_path: Path to runtime config file.
        default_profile: CLI default-profile override.

    Returns:
        Result[None, str] from config reload application.
    """

    config_result = load_config(path=config_path, default_profile=default_profile)
    if config_result.is_err:
        return Result(error=config_result.error)

    assert config_result.value is not None

    from tela.shell.reload import on_config_changed

    return await on_config_changed(config_result.value)


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
            host="127.0.0.1",
            port=runtime.port,
            auth_mode=AuthMode(auth_mode),
            default_profile=runtime.cli_default_profile,
        )
    )


async def gateway_start(
    config: GatewayStartupConfig,
    tela_config: TelaConfig | None = None,
    tool_lists: dict[str, list[dict]] | None = None,
    expected_bearer_token: str | None = None,
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

    upstream_server_result = _create_upstream_server(config, effective_config)
    if upstream_server_result.is_err:
        return Result(error=upstream_server_result.error)
    assert upstream_server_result.value is not None
    upstream_server = upstream_server_result.value
    _wire_upstream_handlers(upstream_server)
    _register_http_routes(upstream_server)
    _register_profiles_resource(upstream_server)
    _wire_reload_notifications()

    # Store runtime state
    with _runtime_lock:
        _runtime.total_tool_calls = 0
        _runtime.config = effective_config
        _runtime.startup_config = config
        _runtime.start_time = time.monotonic()
        _runtime.running = True
        _runtime.upstream_server = upstream_server
        _runtime.expected_bearer_token = expected_bearer_token
        _runtime.secrets = list(effective_config.auth.secrets)

    _ = await gateway_status()
    _ = await gateway_connections()

    return Result(value=None)


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
    audit_close_result = await audit_close()
    if audit_close_result.is_err:
        return audit_close_result
    _set_reload_notify_callback(None)

    # Release captured upstream sessions consistent with per-connection disconnect path.
    from tela.shell.upstream import release_session

    with _runtime_lock:
        connection_ids = [c.connection_id for c in _runtime.connections]
    for cid in connection_ids:
        release_session(cid)
    with _runtime_lock:
        _runtime.config = None
        _runtime.startup_config = None
        _runtime.upstream_server = None
        _runtime.running = False
        _runtime.start_time = None
        _runtime.total_tool_calls = 0
        _runtime.connections.clear()
        _runtime.expected_bearer_token = None
        _runtime.secrets = []
    return disconnect_result


# @shell_complexity: Lifecycle event handlers with inherently branching behavior — routes/priorities/status modes are mutually exclusive by design.
async def gateway_status() -> Result[GatewayStatus, str]:
    """Return current gateway runtime status."""

    snapshot_result = get_runtime_status_snapshot()
    if snapshot_result.is_err:
        return Result(error=snapshot_result.error)
    all_tools_result = get_all_tools()
    if all_tools_result.is_err:
        return Result(error=all_tools_result.error)
    assert snapshot_result.value is not None
    assert all_tools_result.value is not None
    snap = snapshot_result.value
    all_tools = all_tools_result.value
    uptime = time.monotonic() - snap.start_time if snap.start_time else 0.0
    profile_count = len(snap.config.profiles) if snap.config else 0
    configured_server_count = len(snap.config.servers) if snap.config else 0
    connected_servers_list = list(all_tools.keys())

    # Compute lifecycle state based on downstream convergence
    if connected_servers_list:
        if len(connected_servers_list) < configured_server_count:
            state = "degraded"
            degraded_reason = "downstream_not_fully_converged"
        else:
            state = "ready"
            degraded_reason = None
    else:
        if configured_server_count > 0:
            state = "warming"
            degraded_reason = None
        else:
            state = "ready"
            degraded_reason = None

    return Result(
        value=GatewayStatus(
            uptime_seconds=uptime,
            server_count=configured_server_count,
            connected_servers=connected_servers_list,
            active_connections=len(snap.connections),
            profile_count=profile_count,
            total_tool_calls=snap.total_tool_calls,
            state=state,
            degraded_reason=degraded_reason,
        )
    )


async def gateway_connections() -> Result[list[ConnectionContext], str]:
    """Return active upstream connections via runtime snapshot accessor."""
    return get_runtime_connections_snapshot()
