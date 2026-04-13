"""Upstream MCP handlers plus session capture/notification contracts.

Implements initialize, tools/list, and tools/call with enforcement, and tracks
captured sessions for tools/list_changed notifications. Tool-prefix contract:
upstream uses exposed names from the resolved registry; downstream routing stays
bound to ``ResolvedTool.raw_name`` rather than prefix-stripping at call time.

Session registry authority: all session capture/release/lookup state is owned
by ``gateway_runtime.py`` (``_runtime.session_registry``).  The convenience
wrappers here delegate to locked accessors there.  Direct access to the
registry dict or its lock is no longer available from this module.
"""

# @invar:allow file_size: touch_connection_activity wiring adds fire-and-forget calls to handle_tools_list and handle_tools_call; splitting these handlers into separate modules would break cohesion of the upstream MCP handler group.

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from tela.core.errors import (
    CONNECTION_NOT_FOUND,
    GATEWAY_NOT_STARTED,
)
from tela.core.models import (
    AuthMode,
    CapabilityToken,
    ConnectionContext,
    DefaultProfileResolutionStatus,
    EnforcementVerdict,
    InitializeProfileBinding,
    Posture,
    TelaError,
)
from tela.core.token import resolve_token_init_binding
from tela.shell.result import Result
from tela.shell.downstream import (
    call_tool,
    get_all_tools,
    get_registry,
)
from tela.shell.gateway_runtime import (
    UpstreamSession,
    add_runtime_connection,
    get_captured_session,
    get_connection_id_for_session,
    get_runtime_config,
    get_runtime_connections_snapshot,
    get_runtime_secrets,
    increment_tool_calls,
    release_session,
    set_runtime_config,  # noqa: F401 — used in doctests
    touch_connection_activity,
)
from tela.shell.idle_shutdown import get_idle_manager
from tela.shell.upstream_utils import (
    enforce_tool_call,
    filter_tools_for_profile,
    strip_meta,
)

logger = logging.getLogger(__name__)

_BRIDGE_CONNECTION_ID_KEY = "tela_bridge_connection_id"


# --- Session Capture Protocol (re-exported from gateway_runtime) ---
# UpstreamSession is now defined in gateway_runtime.py as the single authority.
# Re-export for backward compatibility so that existing callers importing
# from tela.shell.upstream still resolve the protocol.
# (Already imported above from gateway_runtime)


def find_connection_for_session(
    session: UpstreamSession,
    connections: list[ConnectionContext],
) -> Result[ConnectionContext, str]:
    """Find the ConnectionContext bound to a session.

    Combines reverse session lookup with connection list scan.
    Used by gateway to route distinct sessions to distinct connections.

    Examples:
        >>> from tela.shell.upstream import (
        ...     capture_session, find_connection_for_session, release_session,
        ... )
        >>> class S:
        ...     async def send_tool_list_changed(self) -> None: ...
        >>> s = S()
        >>> _ = capture_session("c1", s)
        >>> conn = ConnectionContext(connection_id="c1", profile_name="p", connected_at="t")
        >>> r = find_connection_for_session(s, [conn])
        >>> r.is_ok and r.value is conn
        True
        >>> _ = release_session("c1")

    Args:
        session: The upstream session to look up.
        connections: Live connection list.

    Returns:
        Result with matching ConnectionContext or error.
    """
    cid_result = get_connection_id_for_session(session)
    if cid_result.is_err:
        return Result(error="SESSION_NOT_REGISTERED: session has no binding")
    for conn in connections:
        if conn.connection_id == cid_result.value:
            return Result(value=conn)
    return Result(
        error=f"{CONNECTION_NOT_FOUND}: no connection for '{cid_result.value}'"
    )


@dataclass(frozen=True)
class InitializeContext:
    """Connection metadata contract visible at MCP initialize boundary."""

    connection_metadata: Mapping[str, str]


def resolve_initialize_profile_binding(
    *,
    resolved_default_profile: str | None,
    default_resolution_status: DefaultProfileResolutionStatus,
    context: InitializeContext,
) -> Result[InitializeProfileBinding, str]:
    """Resolve initialize binding to explicit default profile authority.

    Acceptance semantics:
    - Missing default-profile resolution rejects initialize.
    - Ambiguous default-profile resolution rejects initialize.
    - Client metadata must not influence profile selection.

    Examples:
        >>> r = resolve_initialize_profile_binding(
        ...     resolved_default_profile="production",
        ...     default_resolution_status=DefaultProfileResolutionStatus.RESOLVED,
        ...     context=InitializeContext(connection_metadata={}),
        ... )
        >>> r.is_ok
        True
        >>> r.value.resolved_default_profile
        'production'

    Args:
        resolved_default_profile: Profile selected by config/CLI authority.
        default_resolution_status: Prior open-mode default resolution outcome.
        context: Initialize request metadata; profile hints here are ignored.

    Returns:
        Result with binding on success, or rejection reason on failure.
    """

    _ = context

    if default_resolution_status == DefaultProfileResolutionStatus.MISSING:
        return Result(
            error=(
                "INITIALIZE_REJECTED: no default profile resolved; "
                "open mode requires an explicit default profile from config "
                "or CLI --default-profile"
            )
        )

    if default_resolution_status == DefaultProfileResolutionStatus.AMBIGUOUS:
        return Result(
            error=(
                "INITIALIZE_REJECTED: ambiguous default profile; "
                "multiple profiles marked default=true in open mode"
            )
        )

    if (
        default_resolution_status == DefaultProfileResolutionStatus.RESOLVED
        and resolved_default_profile is None
    ):
        return Result(
            error=(
                "INITIALIZE_REJECTED: status is RESOLVED but "
                "resolved_default_profile is None"
            )
        )

    return Result(
        value=InitializeProfileBinding(
            status=default_resolution_status,
            resolved_default_profile=resolved_default_profile,
        )
    )


# --- MCP Handler functions ---


# @shell_complexity: open-mode initialize resolves profile authority and validation branches.
async def handle_initialize(
    client_info: dict,
) -> Result[ConnectionContext, str]:
    """Handle MCP initialize request.

    Creates a ConnectionContext and registers the connection.

    Examples:
        >>> import asyncio
        >>> from tela.shell.gateway_runtime import set_runtime_config
        >>> set_runtime_config(None)  # Gateway not started
        >>> result = asyncio.run(handle_initialize({}))
        >>> result.is_err
        True
        >>> "GATEWAY_NOT_STARTED" in result.error
        True

    Args:
        client_info: MCP clientInfo dict.

    Returns: Result[ConnectionContext, str] once implemented.
    """

    config = get_runtime_config().value
    if config is None:
        return Result(error=f"{GATEWAY_NOT_STARTED}: gateway has not been started")

    bridge_connection_id = client_info.get(_BRIDGE_CONNECTION_ID_KEY)
    if bridge_connection_id is not None:
        bridge_connection_id_str = str(bridge_connection_id)
        connections_result = get_runtime_connections_snapshot()
        if connections_result.is_err:
            return Result(error=connections_result.error)
        assert connections_result.value is not None
        for existing in connections_result.value:
            if existing.connection_id == bridge_connection_id_str:
                return Result(value=existing)
        return Result(
            error=f"{CONNECTION_NOT_FOUND}: bridge initialize requires pre-registered connection '{bridge_connection_id_str}'"
        )

    connection_id = f"conn_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    if config.auth.mode == AuthMode.OPEN:
        status = (
            DefaultProfileResolutionStatus.RESOLVED
            if config.resolved_default_profile is not None
            else DefaultProfileResolutionStatus.MISSING
        )

        binding_result = resolve_initialize_profile_binding(
            resolved_default_profile=config.resolved_default_profile,
            default_resolution_status=status,
            context=InitializeContext(
                connection_metadata={
                    str(key): str(value) for key, value in client_info.items()
                }
            ),
        )
        if binding_result.is_err:
            return Result(error=binding_result.error)

        assert binding_result.value is not None
        profile_name = binding_result.value.resolved_default_profile
        assert profile_name is not None
    else:
        # Token mode: validate capability token and bind to token's profile.
        required_fields = (
            "token_id",
            "profile_name",
            "issued_at",
            "expires_at",
            "signature",
        )
        missing = [f for f in required_fields if f not in client_info]
        if missing:
            return Result(
                error=f"INITIALIZE_REJECTED: token mode requires client_info fields: {', '.join(missing)}"
            )

        try:
            token = CapabilityToken(
                token_id=str(client_info["token_id"]),
                profile_name=str(client_info["profile_name"]),
                issued_at=str(client_info["issued_at"]),
                expires_at=str(client_info["expires_at"]),
                signature=str(client_info["signature"]),
                persona_ref=client_info.get("persona_ref"),
                instance_id=client_info.get("instance_id"),
                max_depth=client_info.get("max_depth"),
            )
        except Exception as e:
            return Result(error=f"INITIALIZE_REJECTED: invalid token fields: {e}")

        secrets = get_runtime_secrets().value
        if not secrets:
            return Result(
                error="INITIALIZE_REJECTED: token mode requires secrets configured"
            )

        binding = resolve_token_init_binding(token, secrets, now_iso)
        if binding.token_result.verdict == EnforcementVerdict.DENY:
            error_msg = binding.token_result.error_message or "Token validation failed"
            error_code = binding.token_result.error_code or "TOKEN_INVALID"
            return Result(error=f"INITIALIZE_REJECTED: {error_code}: {error_msg}")

        profile_name = binding.profile_name

    ctx = ConnectionContext(
        connection_id=connection_id,
        profile_name=profile_name,
        connected_at=now_iso,
    )

    # Register connection in runtime via locked accessor.
    add_runtime_connection(ctx)
    idle_manager = get_idle_manager()
    if idle_manager is not None:
        increment_result = await idle_manager.increment()
        if increment_result.is_err:
            return Result(error=increment_result.error)
    return Result(value=ctx)


# @shell_complexity: tool listing enforces profile binding and per-server posture defaults.
async def handle_tools_list(
    connection: ConnectionContext,
) -> Result[list[dict], str]:
    """Return filtered tool list for the bound profile.

    Returns filtered tool list for the bound profile.

    Examples:
        >>> import asyncio
        >>> from tela.shell.gateway_runtime import set_runtime_config
        >>> from tela.core.models import ConnectionContext
        >>> set_runtime_config(None)  # Gateway not started
        >>> conn = ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z")
        >>> result = asyncio.run(handle_tools_list(conn))
        >>> result.is_err and "GATEWAY_NOT_STARTED" in result.error
        True

    Args:
        connection: Active upstream connection context.

    Returns:
        List of tool dicts once implemented.
    """

    config = get_runtime_config().value
    if config is None:
        return Result(error=f"{GATEWAY_NOT_STARTED}: gateway has not been started")

    profile = config.profiles.get(connection.profile_name)
    if profile is None:
        return Result(
            error=f"PROFILE_NOT_FOUND: profile '{connection.profile_name}' not found"
        )

    touch_r = touch_connection_activity(
        connection.connection_id, datetime.now(timezone.utc).isoformat()
    )
    if touch_r.is_err:
        logger.warning(
            "Failed to touch connection activity for %s: %s",
            connection.connection_id,
            touch_r.error,
        )

    all_tools_result = get_all_tools()
    if all_tools_result.is_err:
        return Result(error=all_tools_result.error)
    assert all_tools_result.value is not None
    all_tools = all_tools_result.value
    server_default_postures: dict[str, Posture] = {}
    for sname, scfg in config.servers.items():
        server_default_postures[sname] = scfg.default_posture

    permitted_result = filter_tools_for_profile(
        all_tools, profile, server_default_postures
    )
    if permitted_result.is_err:
        return Result(error=permitted_result.error)
    assert permitted_result.value is not None
    permitted = permitted_result.value
    return Result(
        value=[
            {
                "name": t.name,
                "inputSchema": t.schema_ or {},
                "description": t.description,
                **({"annotations": t.annotations} if t.annotations is not None else {}),
                **({"title": t.title} if t.title is not None else {}),
                **(
                    {"outputSchema": t.output_schema}
                    if t.output_schema is not None
                    else {}
                ),
            }
            for t in permitted
        ]
    )


# @shell_complexity: tool call path validates runtime/profile/tool and enforcement chain outcomes.
async def handle_tools_call(
    connection: ConnectionContext,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]:
    """Handle a tools/call request.

    Runs enforcement chain and forwards to downstream.

    Examples:
        >>> import asyncio
        >>> from tela.shell.gateway_runtime import set_runtime_config
        >>> from tela.core.models import ConnectionContext
        >>> set_runtime_config(None)  # Gateway not started
        >>> conn = ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z")
        >>> result = asyncio.run(handle_tools_call(conn, "read_file", {"path": "/tmp"}))
        >>> result.is_err
        True
        >>> "GATEWAY_NOT_STARTED" in result.error.code
        True

    Args:
        connection: Active upstream connection context.
        tool_name: Tool to invoke.
        arguments: Tool arguments (may contain _meta).

    Returns:
        Result[dict, TelaError] once implemented.
    """

    config = get_runtime_config().value
    if config is None:
        return Result(
            error=TelaError(
                code=GATEWAY_NOT_STARTED, message="Gateway has not been started"
            )
        )

    stripped_result = strip_meta(arguments)
    if stripped_result.is_err:
        return Result(
            error=TelaError(
                code="INTERNAL_ERROR",
                message=str(stripped_result.error or "Failed to strip _meta"),
            )
        )
    assert stripped_result.value is not None
    stripped_args, held_meta = stripped_result.value
    _ = held_meta

    tool = get_registry().get_tool(tool_name)
    if tool is None:
        return Result(
            error=TelaError(
                code="TOOL_NOT_FOUND", message=f"Tool '{tool_name}' not found"
            )
        )
    routing_name = tool.raw_name or tool.name
    profile = config.profiles.get(connection.profile_name)
    if profile is None:
        return Result(
            error=TelaError(
                code="PROFILE_NOT_FOUND",
                message=f"Profile '{connection.profile_name}' not found",
            )
        )

    touch_r = touch_connection_activity(
        connection.connection_id, datetime.now(timezone.utc).isoformat()
    )
    if touch_r.is_err:
        logger.warning(
            "Failed to touch connection activity for %s: %s",
            connection.connection_id,
            touch_r.error,
        )

    server_config = config.servers.get(tool.server_name)
    default_posture = server_config.default_posture if server_config else Posture.NONE
    enforcement_result = enforce_tool_call(routing_name, tool, profile, default_posture)
    if enforcement_result.is_err:
        return Result(
            error=TelaError(
                code="INTERNAL_ERROR",
                message=str(enforcement_result.error or "Enforcement unavailable"),
            )
        )
    assert enforcement_result.value is not None
    enforcement = enforcement_result.value

    if enforcement.verdict == EnforcementVerdict.DENY:
        return Result(
            error=TelaError(
                code=enforcement.error_code or "AUTHZ_DENY",
                message=enforcement.error_message or "Tool call denied",
            )
        )

    increment_tool_calls()
    return await call_tool(tool.server_name, routing_name, stripped_args)


def handle_profiles_list() -> Result[list[dict], str]:
    """Return list of configured profiles.

    Returns list of configured profiles.

    Examples:
        >>> from tela.shell.gateway_runtime import set_runtime_config
        >>> set_runtime_config(None)  # Gateway not started
        >>> result = handle_profiles_list()
        >>> result.is_err and "GATEWAY_NOT_STARTED" in result.error
        True

    Returns:
        List of profile dicts once implemented.
    """

    config = get_runtime_config().value
    if config is None:
        return Result(error=f"{GATEWAY_NOT_STARTED}: gateway has not been started")

    # Migration: emit both 'capabilities' and 'tools' keys per ADR-003.
    # Canonical external profile identifier field is 'profile_name'.
    return Result(
        value=[
            {
                "profile_name": name,
                "default": p.default,
                "capabilities": {k: v.value for k, v in p.capabilities.items()},
                "tools": {k: v.value for k, v in p.capabilities.items()},
            }
            for name, p in config.profiles.items()
        ]
    )


async def notify_tools_changed(
    connection: ConnectionContext,
    tools_digest: str,
) -> Result[None, str]:
    """Send notifications/tools/list_changed to an upstream client.

    Looks up the captured ``UpstreamSession`` for the connection and calls
    ``send_tool_list_changed()``. If no session is captured (e.g. the
    connection was established before session capture was wired), the
    notification is skipped with a debug log — this is not an error since
    stdio transports may not have capturable sessions.

    The ``tools_digest`` parameter is retained for audit/logging purposes
    but is not sent over the wire (MCP ``tools/list_changed`` is a
    parameter-less notification).

    Examples:
        >>> import asyncio
        >>> from tela.core.models import ConnectionContext
        >>> r = asyncio.run(notify_tools_changed(
        ...     ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"),
        ...     "digest123",
        ... ))
        >>> r.is_ok
        True

    Args:
        connection: Target upstream connection.
        tools_digest: Digest of the updated tool list (for audit/logging).

    Returns:
        Result[None, str] on success, or error string on send failure.
    """
    session_result = get_captured_session(connection.connection_id)
    if session_result.is_err:
        logger.debug(
            "No captured session for %s (digest=%s), skipping notification",
            connection.connection_id,
            tools_digest,
        )
        return Result(value=None)

    assert session_result.value is not None
    session = session_result.value
    try:
        await session.send_tool_list_changed()
    except Exception:
        logger.warning(
            "Failed to send tools/list_changed to %s (digest=%s), removing stale session",
            connection.connection_id,
            tools_digest,
            exc_info=True,
        )
        release_session(connection.connection_id)
        return Result(error=f"NOTIFICATION_SEND_FAILED: {connection.connection_id}")
    return Result(value=None)
