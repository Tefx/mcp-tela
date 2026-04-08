"""Gateway lifecycle and startup binding.

This module implements the gateway lifecycle: start (load config, connect
downstreams), shutdown (disconnect downstreams), status, and connections.
Transport startup (stdio/SSE/HTTP) is wired via CLI in tela.cli.
"""

# @invar:allow file_size: Gateway initialization is a single-shot startup routine; splitting requires invasive refactor of lifecycle ownership. This module consolidates all lifecycle, HTTP routing, and server-creation logic that would otherwise need cross-module coordination across startup/shutdown/status/connections phases.

from __future__ import annotations

import json
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tela.core.errors import (
    ADMISSION_REJECTED_WARMING,
    AUTH_INVALID_TOKEN,
    CONNECTION_NOT_FOUND,
    DOWNSTREAM_UNAVAILABLE,
    GATEWAY_NOT_STARTED,
    error_to_http_status,
)
from tela.core.models import (
    AuditLevel,
    AuthMode,
    ConnectRequest,
    ConnectionContext,
    DisconnectRequest,
    EnforcementResult,
    EnforcementVerdict,
    GatewayStatus,
    GatewayTransport,
    RuntimeBindingContract,
    TelaConfig,
)
from tela.shell.config_loader import Result, load_config
from tela.shell.audit import audit_close, audit_init, build_audit_entry, audit_write
from tela.shell.builtin_tools import (
    BUILTIN_TOOLS,
    BUILTIN_TOOL_NAMES,
    handle_list_providers,
)
from tela.shell.connection_lifecycle import cleanup_connection_by_id
from tela.shell.connection_reaper import ConnectionReaper, ReaperConfig
from tela.shell.downstream import (
    connect_all,
    disconnect_all,
    get_all_tools,
    get_connected_server_names,
    get_registry,
    get_server_instructions,
)
from tela.shell.surface_instructions import (
    build_manifest_header,
    compose_gateway_and_downstream,
    get_gateway_surface_instructions,
)

from tela.shell.gateway_lifecycle import get_lifecycle_status_facts
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
    touch_connection_activity,
    with_upstream_server,
)

logger = logging.getLogger(__name__)

# Module-level manifest snapshot built at prepare_startup time.
_startup_manifest: str | None = None
_reaper: ConnectionReaper | None = None
_converge_event: asyncio.Event | None = None


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


# @shell_complexity: override projection is bounded field-mapping glue for shell startup wiring.
def apply_reaper_overrides(
    config: TelaConfig,
    *,
    sweep_interval_seconds: float | None = None,
    native_idle_ttl_seconds: float | None = None,
    bridge_idle_ttl_seconds: float | None = None,
) -> Result[TelaConfig, str]:
    """Apply CLI reaper overrides on top of config-file values."""

    update: dict[str, float] = {}
    if sweep_interval_seconds is not None:
        update["sweep_interval_seconds"] = sweep_interval_seconds
    if native_idle_ttl_seconds is not None:
        update["native_idle_ttl_seconds"] = native_idle_ttl_seconds
    if bridge_idle_ttl_seconds is not None:
        update["bridge_idle_ttl_seconds"] = bridge_idle_ttl_seconds

    if not update:
        return Result(value=config)

    return Result(
        value=config.model_copy(
            update={"reaper": config.reaper.model_copy(update=update)}
        )
    )


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
        status_code = error_to_http_status(error)
        return JSONResponse(status_code=status_code, content={"error": error})

    @upstream_server.custom_route("/health", methods=["GET"])
    async def _health_route(_request: Request) -> Response:
        health_result = handle_health()
        if health_result.is_err:
            return JSONResponse(status_code=500, content={"error": health_result.error})
        assert health_result.value is not None
        return JSONResponse(content=health_result.value.model_dump())

    def _build_auth_handoff(
        request: Request,
    ) -> Result[tuple[str, str], tuple[str, int]]:
        """Shared auth skeleton: extract bearer and retrieve expected runtime token.

        Returns:
            Result with (request_token, expected_token) on success.
            Result with (error_message, status_code) on failure, for use with
            _as_error_response.
        """
        token_result = extract_bearer_token(request)
        if token_result.is_err:
            assert token_result.error is not None
            return Result(error=(token_result.error, 401))
        assert token_result.value is not None

        with _runtime_lock:
            expected_token = _runtime.expected_bearer_token or ""
        return Result(value=(token_result.value, expected_token))

    @upstream_server.custom_route("/status", methods=["GET"])
    async def _status_route(request: Request) -> Response:
        auth_result = _build_auth_handoff(request)
        if auth_result.is_err:
            error, status_code = auth_result.error
            return JSONResponse(status_code=status_code, content={"error": error})
        request_token, expected_token = auth_result.value

        status_result = handle_status(request_token, expected_token)
        if status_result.is_err:
            assert status_result.error is not None
            return _as_error_response(status_result.error)
        assert status_result.value is not None
        return JSONResponse(content=status_result.value.model_dump())

    @upstream_server.custom_route("/connect", methods=["POST"])
    async def _connect_route(request: Request) -> Response:
        auth_result = _build_auth_handoff(request)
        if auth_result.is_err:
            error, status_code = auth_result.error
            return JSONResponse(status_code=status_code, content={"error": error})
        request_token, expected_token = auth_result.value

        try:
            payload = ConnectRequest.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "INVALID_REQUEST: invalid connect payload"},
            )

        connect_result = handle_connect(request_token, expected_token, payload)
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
        auth_result = _build_auth_handoff(request)
        if auth_result.is_err:
            error, status_code = auth_result.error
            return JSONResponse(status_code=status_code, content={"error": error})
        request_token, expected_token = auth_result.value

        try:
            payload = DisconnectRequest.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "INVALID_REQUEST: invalid disconnect payload"},
            )

        disconnect_result = handle_disconnect(request_token, expected_token, payload)
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

    gateway_result = get_gateway_surface_instructions(_startup_manifest)
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
        server = FastMCP(
            "tela-gateway",
            instructions=merged_instructions,
            host=startup_config.host,
            port=startup_config.port,
        )
    else:
        server = FastMCP("tela-gateway", instructions=merged_instructions)
    return Result(value=server)


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
        get_captured_session,
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
                touch_r = touch_connection_activity(
                    conn_r.value.connection_id, datetime.now(timezone.utc).isoformat()
                )
                if touch_r.is_err:
                    logger.warning(
                        "Failed to touch connection activity for %s: %s",
                        conn_r.value.connection_id,
                        touch_r.error,
                    )
                return conn_r.value
        except LookupError:
            pass
        # Adopt unbound bridge connection before creating a spurious conn_*.
        # Bridge connections are pre-registered via POST /connect but their
        # MCP session is not captured until the first list_tools/call_tool.
        try:
            current_session = request_ctx.get().session
            with _runtime_lock:
                candidates = list(_runtime.connections)
            for candidate in candidates:
                if not candidate.connection_id.startswith("bridge_"):
                    continue
                probe = get_captured_session(candidate.connection_id)
                if probe.is_err:
                    # Unbound bridge — adopt it for this session
                    capture_session(candidate.connection_id, current_session)
                    now_iso = datetime.now(timezone.utc).isoformat()
                    touch_connection_activity(candidate.connection_id, now_iso)
                    logger.debug(
                        "Adopted unbound bridge %s for session", candidate.connection_id
                    )
                    return candidate
        except LookupError:
            pass
        init_result = await handle_initialize({})
        if init_result.is_err:
            raise RuntimeError(init_result.error or "INITIALIZE_REJECTED")
        assert init_result.value is not None
        touch_r = touch_connection_activity(
            init_result.value.connection_id, datetime.now(timezone.utc).isoformat()
        )
        if touch_r.is_err:
            logger.warning(
                "Failed to touch connection activity for %s: %s",
                init_result.value.connection_id,
                touch_r.error,
            )
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
        # Wait for downstream convergence before listing tools.
        # Without this, bridges connecting during warming get an empty tool list
        # and the bridge transport cannot receive tools/list_changed push.
        if _converge_event is not None:
            await _converge_event.wait()

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
        downstream_tools = [
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
        # Merge builtin tools into the returned list
        builtin_tools = [
            mcp_types.Tool(
                name=bt["name"],
                inputSchema=dict(bt.get("inputSchema") or {}),
                description=bt.get("description", ""),
            )
            for bt in BUILTIN_TOOLS
        ]
        return downstream_tools + builtin_tools

    # @shell_complexity: builtin tool calls follow a different execution path than downstream tools.
    @upstream_server._mcp_server.call_tool(validate_input=False)
    async def _call_tool(
        tool_name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        # Check if this is a builtin tool
        if tool_name in BUILTIN_TOOL_NAMES:
            return await _handle_builtin_call(tool_name, arguments)

        connection = await _ensure_connection()
        # Wait for convergence before calling downstream tools.
        if _converge_event is not None:
            await _converge_event.wait()
        result = await handle_tools_call(connection, tool_name, dict(arguments))
        if result.is_err:
            assert result.error is not None
            raise RuntimeError(f"{result.error.code}: {result.error.message}")

        assert result.value is not None
        # Return CallToolResult to bypass output normalization/re-validation;
        # gateway proxies downstream results as-is.
        return mcp_types.CallToolResult.model_validate(result.value)


# @invar:allow shell_result: _handle_builtin_call is an async MCP callback invoked by FastMCP's call_tool handler; returning mcp_types.CallToolResult directly satisfies the MCP protocol contract. The function delegates to handle_list_providers (Shell) and returns a raw MCP type rather than Result[T, E], which is intentional — the function IS the boundary between Shell and MCP protocol layer.
async def _handle_builtin_call(
    tool_name: str,
    arguments: dict[str, object] | None,
) -> mcp_types.CallToolResult:
    """Handle a builtin tool call with L2 audit trail.

    Args:
        tool_name: Name of the builtin tool being invoked.
        arguments: Tool arguments dict, or None if empty.

    Returns:
        CallToolResult on success.

    Raises:
        RuntimeError: on internal builtin tool failure.
    """
    start_time = time.time()
    try:
        providers_result = await handle_list_providers()
        latency_ms = (time.time() - start_time) * 1000

        # L2 audit entry for builtin tool calls (if connection available)
        with _runtime_lock:
            connections = list(_runtime.connections)
        if connections:
            connection = connections[0]
            audit_entry_result = build_audit_entry(
                level=AuditLevel.L2,
                connection=connection,
                tool_name=tool_name,
                server_name="tela",  # builtin tools belong to "tela" pseudo-server
                result=EnforcementResult(verdict=EnforcementVerdict.ALLOW),
                latency_ms=latency_ms,
                arguments=dict(arguments) if arguments else None,
            )
            if audit_entry_result.is_ok and audit_entry_result.value is not None:
                await audit_write(audit_entry_result.value)

        # Convert ProviderInfo (TypedDict) to plain dict for JSON serialization
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=str(providers_result))],
            isError=False,
        )
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        # L2 audit entry for failed builtin tool call (if connection available)
        with _runtime_lock:
            connections = list(_runtime.connections)
        if connections:
            connection = connections[0]
            audit_entry_result = build_audit_entry(
                level=AuditLevel.L2,
                connection=connection,
                tool_name=tool_name,
                server_name="tela",
                result=EnforcementResult(
                    verdict=EnforcementVerdict.DENY,
                    denied_by="builtin_tool_error",
                    error_code="BUILTIN_TOOL_ERROR",
                    error_message=str(e),
                ),
                latency_ms=latency_ms,
                arguments=dict(arguments) if arguments else None,
            )
            if audit_entry_result.is_ok and audit_entry_result.value is not None:
                await audit_write(audit_entry_result.value)
        raise


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
    sweep_interval_seconds: float | None = None,
    native_idle_ttl_seconds: float | None = None,
    bridge_idle_ttl_seconds: float | None = None,
) -> Result[None, str]:
    """Load config from disk and apply runtime hot-reload callback.

    This is the production runtime callback target for config-file watcher
    integrations.

    Args:
        config_path: Path to runtime config file.
        default_profile: CLI default-profile override.
        sweep_interval_seconds: Optional CLI override for reaper sweep interval.
        native_idle_ttl_seconds: Optional CLI override for native idle TTL.
        bridge_idle_ttl_seconds: Optional CLI override for bridge idle TTL.

    Returns:
        Result[None, str] from config reload application.
    """

    config_result = load_config(path=config_path, default_profile=default_profile)
    if config_result.is_err:
        return Result(error=config_result.error)

    assert config_result.value is not None
    effective_config_result = apply_reaper_overrides(
        config_result.value,
        sweep_interval_seconds=sweep_interval_seconds,
        native_idle_ttl_seconds=native_idle_ttl_seconds,
        bridge_idle_ttl_seconds=bridge_idle_ttl_seconds,
    )
    if effective_config_result.is_err:
        return Result(error=effective_config_result.error)
    assert effective_config_result.value is not None
    effective_config = effective_config_result.value

    from tela.shell.reload import on_config_changed

    return await on_config_changed(effective_config)


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

    prepare_result = await gateway_prepare_startup(
        config,
        tela_config=tela_config,
        expected_bearer_token=expected_bearer_token,
    )
    if prepare_result.is_err:
        return Result(error=prepare_result.error)

    converge_result = await gateway_converge_startup(tool_lists=tool_lists)
    if converge_result.is_err:
        await gateway_shutdown()
        return Result(error=converge_result.error)

    return Result(value=None)


async def gateway_prepare_startup(
    config: GatewayStartupConfig,
    tela_config: TelaConfig | None = None,
    expected_bearer_token: str | None = None,
) -> Result[None, str]:
    """Prepare runtime state and upstream server before downstream convergence."""

    global _startup_manifest, _converge_event
    _converge_event = asyncio.Event()

    effective_config = tela_config or TelaConfig()

    # Build manifest snapshot before connecting (reflects config-defined servers only)
    connected_result = await get_connected_server_names()
    connected_names = connected_result.value or set()
    tools_by_server = get_registry().get_all_tools()
    _startup_manifest = build_manifest_header(
        effective_config.servers, connected_names, tools_by_server
    )

    upstream_server_result = _create_upstream_server(config, effective_config)
    if upstream_server_result.is_err:
        return Result(error=upstream_server_result.error)
    assert upstream_server_result.value is not None
    upstream_server = upstream_server_result.value

    _wire_upstream_handlers(upstream_server)
    _register_http_routes(upstream_server)
    _register_profiles_resource(upstream_server)
    _wire_reload_notifications()

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


async def gateway_converge_startup(
    tool_lists: dict[str, list[dict]] | None = None,
) -> Result[None, str]:
    """Converge downstream registry after startup preparation."""

    with _runtime_lock:
        runtime_config = _runtime.config

    if runtime_config is None:
        return Result(error="STARTUP_NOT_PREPARED: runtime config unavailable")

    connect_result = await connect_all(runtime_config.servers, tool_lists=tool_lists)
    if connect_result.is_err:
        return Result(error=connect_result.error)

    audit_result = await audit_init(runtime_config.audit)
    if audit_result.is_err:
        return Result(error=audit_result.error)

    global _reaper
    _reaper = ConnectionReaper(
        ReaperConfig.from_tela_config(runtime_config), use_runtime_config=True
    )
    await _reaper.start()

    # Notify bridges that connected during warming — tools are now available.
    from tela.shell.upstream import notify_tools_changed
    from tela.shell.gateway_runtime import get_runtime_connections_snapshot
    from tela.shell.downstream import get_registry

    snap = get_runtime_connections_snapshot()
    if snap.is_ok and snap.value:
        registry = get_registry()
        digest = str(
            sorted(t.name for ts in registry.get_all_tools().values() for t in ts)
        )
        for conn in snap.value:
            await notify_tools_changed(conn, digest)

    # Signal that downstream convergence is complete — tools are available.
    if _converge_event is not None:
        _converge_event.set()
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

    global _reaper, _converge_event
    if _reaper is not None:
        await _reaper.stop()
        _reaper = None

    _converge_event = None
    disconnect_result = await disconnect_all()
    audit_close_result = await audit_close()
    if audit_close_result.is_err:
        return audit_close_result
    _set_reload_notify_callback(None)

    with _runtime_lock:
        connection_ids = [c.connection_id for c in _runtime.connections]
    for cid in connection_ids:
        cleanup_result = cleanup_connection_by_id(cid)
        if cleanup_result.is_err:
            return Result(error=cleanup_result.error)
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

    lifecycle_result = get_lifecycle_status_facts()
    if lifecycle_result.is_err:
        return Result(error=lifecycle_result.error)
    assert lifecycle_result.value is not None
    facts = lifecycle_result.value

    snap = facts.snapshot
    uptime = time.monotonic() - snap.start_time if snap.start_time else 0.0

    return Result(
        value=GatewayStatus(
            uptime_seconds=uptime,
            server_count=facts.server_count,
            connected_servers=list(facts.connected_servers),
            active_connections=facts.active_connections,
            profile_count=facts.profile_count,
            total_tool_calls=facts.total_tool_calls,
            state=facts.state,
            degraded_reason=facts.degraded_reason,
        )
    )


async def gateway_connections() -> Result[list[ConnectionContext], str]:
    """Return active upstream connections via runtime snapshot accessor."""
    return get_runtime_connections_snapshot()
