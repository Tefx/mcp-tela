"""Gateway lifecycle and startup binding.

This module implements the gateway lifecycle: start (load config, connect
downstreams), shutdown (disconnect downstreams), status, and connections.
Transport startup (stdio/SSE/HTTP) is wired via CLI in tela.cli.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
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
    expected_bearer_token: str | None = None
    secrets: list[str] = field(default_factory=list)


def _extract_bearer_token(request: Request) -> Result[str, str]:
    """Extract bearer token from Authorization header."""

    authorization_header = request.headers.get("authorization")
    if authorization_header is None or not authorization_header.startswith("Bearer "):
        return Result(error="AUTH_INVALID_TOKEN: bearer token validation failed")

    request_token = authorization_header[len("Bearer ") :].strip()
    if request_token == "":
        return Result(error="AUTH_INVALID_TOKEN: bearer token validation failed")

    return Result(value=request_token)


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
        token_result = _extract_bearer_token(request)
        if token_result.is_err:
            assert token_result.error is not None
            return _as_error_response(token_result.error)
        assert token_result.value is not None

        with _runtime_lock:
            expected_token = _runtime.expected_bearer_token or ""
        status_result = handle_status(token_result.value, expected_token)
        if status_result.is_err:
            assert status_result.error is not None
            return _as_error_response(status_result.error)
        assert status_result.value is not None
        return JSONResponse(content=status_result.value.model_dump())

    @upstream_server.custom_route("/connect", methods=["POST"])
    async def _connect_route(request: Request) -> Response:
        token_result = _extract_bearer_token(request)
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
        token_result = _extract_bearer_token(request)
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

    merge_result = _merge_downstream_instructions(tela_config)
    if merge_result.is_err:
        return Result(error=merge_result.error)
    merged_instructions = merge_result.value

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


# Module-level runtime state
_runtime = GatewayRuntime()
_runtime_lock = threading.RLock()


# @invar:allow shell_result: returns runtime state object, not a failable I/O boundary.
# @invar:allow dead_export: retained for single-threaded test setup only
def get_runtime() -> GatewayRuntime:
    """Return the module-level gateway runtime (**test-setup only**).

    .. deprecated::
        This function returns a **mutable** reference without lock protection.
        Production code MUST use the lock-safe accessor helpers below.
        This function is retained only for single-threaded test setup where
        lock-safe write helpers (``set_runtime_config``, ``set_runtime_running``,
        ``clear_runtime_connections``) are not sufficient (e.g. ``upstream_server``
        assignment in integration tests).

    .. warning::
        Callers MUST NOT cache the returned object or pass it across thread
        boundaries. Any mutation of fields outside ``_runtime_lock`` is a
        data race.
    """
    return _runtime


# --- Locked runtime accessors ------------------------------------------
#
# Authoritative runtime boundary rule:
#
#   READ ACCESS:   Returns a deep-copied / detached snapshot.  Callers
#                  may freely read or discard the returned value; it
#                  shares no mutable state with the runtime.
#
#   SNAPSHOT ACCESS: Same as read — frozen dataclass or deep-copied
#                    Pydantic models.  Nested containers (lists, dicts)
#                    and model members are deep-copied so no shallow
#                    alias back into runtime-owned objects survives.
#
#   STATUS ACCESS: Equivalent to snapshot — the RuntimeStatusSnapshot
#                  dataclass is frozen, and all mutable model fields are
#                  deep-copied before embedding.
#
#   WRITE ACCESS:  Locked mutators (set_*, add_*, remove_*, clear_*,
#                  increment_*) that acquire ``_runtime_lock`` for the
#                  full mutation.
#
# Why previous fix failed:
#   The prior hardening pass introduced lock-safe helpers but left some
#   helpers returning the *live* Pydantic model reference (e.g.
#   ``_runtime.config`` directly).  Because Pydantic V2 models are
#   mutable by default, callers could mutate fields on the returned
#   object and those mutations would propagate back into runtime state,
#   violating the lock boundary.  Shallow ``list()`` copies of
#   connection lists likewise preserved live ``ConnectionContext``
#   references.
#
# How this fix closes the blocker family:
#   Every read accessor now returns ``model_copy(deep=True)`` for
#   Pydantic models, ensuring no alias back into runtime-owned state.
#   ``RuntimeStatusSnapshot`` deep-copies both config and connection
#   list members.  ``get_runtime()`` is deprecated with explicit docs;
#   new lock-safe write helpers cover the remaining test-setup mutations.
# -----------------------------------------------------------------------


def get_runtime_config() -> TelaConfig | None:
    """Return a deep copy of the current runtime config under lock.

    The returned ``TelaConfig`` is a deep-copied Pydantic model captured
    while ``_runtime_lock`` is held.  Callers may read or mutate the
    returned object freely; changes do **not** propagate back to runtime
    state.

    Examples:
        >>> get_runtime_config() is None or isinstance(get_runtime_config(), TelaConfig)
        True
    """
    with _runtime_lock:
        if _runtime.config is None:
            return None
        return _runtime.config.model_copy(deep=True)


def set_runtime_config(config: TelaConfig | None) -> None:
    """Replace the runtime config under lock.

    Examples:
        >>> from tela.core.models import TelaConfig
        >>> set_runtime_config(TelaConfig())
        >>> get_runtime_config() is not None
        True
        >>> set_runtime_config(None)
    """
    with _runtime_lock:
        _runtime.config = config


def is_runtime_running() -> bool:
    """Return whether the gateway runtime is running, under lock.

    Examples:
        >>> isinstance(is_runtime_running(), bool)
        True
    """
    with _runtime_lock:
        return _runtime.running


def get_runtime_connections_snapshot() -> list[ConnectionContext]:
    """Return a deep-copied snapshot of the active connections list under lock.

    The returned list and its ``ConnectionContext`` members are fully
    detached from runtime state.  Mutations to the returned objects do
    not affect the runtime connections list.

    Examples:
        >>> get_runtime_connections_snapshot()
        []
    """
    with _runtime_lock:
        return [c.model_copy(deep=True) for c in _runtime.connections]


def add_runtime_connection(ctx: ConnectionContext) -> None:
    """Append a connection to the runtime connections list under lock.

    Examples:
        >>> c = ConnectionContext(connection_id="test", profile_name="p", connected_at="t")
        >>> add_runtime_connection(c)
        >>> len(get_runtime_connections_snapshot()) > 0
        True
        >>> remove_runtime_connection("test")
        True
    """
    with _runtime_lock:
        _runtime.connections.append(ctx)


def remove_runtime_connection(connection_id: str) -> bool:
    """Remove a connection by ID under lock.  Returns True if removed.

    Examples:
        >>> remove_runtime_connection("nonexistent")
        False
    """
    with _runtime_lock:
        original = len(_runtime.connections)
        _runtime.connections[:] = [
            c for c in _runtime.connections if c.connection_id != connection_id
        ]
        return len(_runtime.connections) != original


def set_runtime_running(running: bool) -> None:
    """Set the runtime running flag under lock.

    Examples:
        >>> set_runtime_running(True)
        >>> is_runtime_running()
        True
        >>> set_runtime_running(False)
    """
    with _runtime_lock:
        _runtime.running = running


def clear_runtime_connections() -> None:
    """Remove all connections from the runtime under lock.

    Examples:
        >>> clear_runtime_connections()
        >>> get_runtime_connections_snapshot()
        []
    """
    with _runtime_lock:
        _runtime.connections.clear()


def increment_tool_calls() -> None:
    """Atomically increment the tool-call counter under lock.

    Examples:
        >>> increment_tool_calls()
    """
    with _runtime_lock:
        _runtime.total_tool_calls += 1


def get_runtime_secrets() -> list[str]:
    """Return a copy of runtime auth secrets under lock.

    Examples:
        >>> isinstance(get_runtime_secrets(), list)
        True
    """
    with _runtime_lock:
        return list(_runtime.secrets)


def get_upstream_server() -> FastMCP | None:
    """Return the upstream FastMCP server instance under lock.

    Examples:
        >>> isinstance(get_upstream_server(), (type(None), FastMCP))
        True
    """
    with _runtime_lock:
        return _runtime.upstream_server


@dataclass(frozen=True)
class RuntimeStatusSnapshot:
    """Frozen snapshot of runtime fields needed for status queries.

    All Pydantic model members (``config``, ``connections``) are
    deep-copied at construction time so no mutable alias back into
    runtime-owned objects survives the snapshot boundary.
    """

    config: TelaConfig | None
    running: bool
    start_time: float | None
    total_tool_calls: int
    connections: tuple[ConnectionContext, ...]


def get_runtime_status_snapshot() -> RuntimeStatusSnapshot:
    """Return a frozen snapshot of runtime status fields under lock.

    Used by HTTP status handler to capture all fields atomically.
    Config and connection members are deep-copied; the snapshot is
    fully detached from runtime state.

    Examples:
        >>> snap = get_runtime_status_snapshot()
        >>> isinstance(snap.running, bool)
        True
    """
    with _runtime_lock:
        return RuntimeStatusSnapshot(
            config=(
                _runtime.config.model_copy(deep=True)
                if _runtime.config is not None
                else None
            ),
            running=_runtime.running,
            start_time=_runtime.start_time,
            total_tool_calls=_runtime.total_tool_calls,
            connections=tuple(
                c.model_copy(deep=True) for c in _runtime.connections
            ),
        )


def get_expected_bearer_token() -> Result[str | None, str]:
    """Return the current expected bearer token under runtime lock.

    Thread-safe accessor intended as the ``get_expected_token`` callable
    for ``BearerAuthMiddleware`` (via ``.value`` unwrap).
    """
    with _runtime_lock:
        return Result(value=_runtime.expected_bearer_token)


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


async def gateway_status() -> Result[GatewayStatus, str]:
    """Return current gateway runtime status.

    Examples:
        >>> import asyncio
        >>> asyncio.run(gateway_status()).value.server_count
        0

    Returns:
        GatewayStatus with current runtime metrics.
    """

    with _runtime_lock:
        all_tools_result = get_all_tools()
        if all_tools_result.is_err:
            return Result(error=all_tools_result.error)
        assert all_tools_result.value is not None
        all_tools = all_tools_result.value
        uptime = time.monotonic() - _runtime.start_time if _runtime.start_time else 0.0
        profile_count = len(_runtime.config.profiles) if _runtime.config else 0

        return Result(
            value=GatewayStatus(
                uptime_seconds=uptime,
                server_count=len(all_tools),
                connected_servers=list(all_tools.keys()),
                active_connections=len(_runtime.connections),
                profile_count=profile_count,
                total_tool_calls=_runtime.total_tool_calls,
            )
        )


async def gateway_connections() -> Result[list[ConnectionContext], str]:
    """Return list of active upstream connections.

    Examples:
        >>> import asyncio
        >>> asyncio.run(gateway_connections()).value
        []

    Returns:
        List of active ConnectionContext.
    """

    with _runtime_lock:
        return Result(
            value=[c.model_copy(deep=True) for c in _runtime.connections]
        )
