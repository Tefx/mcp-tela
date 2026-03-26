"""Upstream MCP handler for tools/list, tools/call, and open-mode initialize.

Implements the upstream-facing MCP protocol handler interfaces. Open-mode
initialize binding is preserved from prior implementation. tools/list filtering
uses the enforcement chain. tools/call strips _meta and runs enforcement.

Session capture and notification contracts:
- ``SessionCapture`` defines the interface for capturing upstream MCP sessions.
- ``notify_tools_changed`` uses captured sessions to send real notifications.
- Sessions are captured during handler registration in gateway wiring (not here).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

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
from tela.shell.config_loader import Result
from tela.shell.downstream import (
    call_tool,
    get_all_tools,
    get_registry,
)
from tela.shell.gateway import _runtime_lock, get_runtime
from tela.shell.idle_shutdown import get_idle_manager
from tela.shell.upstream_utils import (
    enforce_tool_call,
    filter_tools_for_profile,
    strip_meta,
)

logger = logging.getLogger(__name__)


# --- Session Capture Protocol ---


@runtime_checkable
class UpstreamSession(Protocol):
    """Protocol for upstream MCP session objects that support tool-list notifications.

    This protocol abstracts the ``ServerSession.send_tool_list_changed()`` method
    so that ``notify_tools_changed`` can send real MCP notifications without
    depending directly on the ``mcp`` package's concrete session type.

    Implementors:
        - ``mcp.server.session.ServerSession`` (production; satisfies this protocol)
        - Test doubles for unit testing notification delivery

    Gateway wiring captures the concrete session during handler registration
    and stores it via ``capture_session``.
    """

    async def send_tool_list_changed(self) -> None:
        """Send ``notifications/tools/list_changed`` to the upstream client."""
        ...


# Module-level session registry: connection_id -> UpstreamSession
_session_registry: dict[str, UpstreamSession] = {}
_session_registry_lock = threading.Lock()


def capture_session(connection_id: str, session: UpstreamSession) -> Result[None, str]:
    """Register an upstream MCP session for a connection.

    Called by gateway handler wiring when a session is available from the
    FastMCP context. The captured session enables ``notify_tools_changed``
    to send real ``notifications/tools/list_changed`` messages.

    Thread-safe: acquires ``_session_registry_lock``.

    Examples:
        >>> from tela.shell.upstream import capture_session, release_session
        >>> class FakeSession:
        ...     async def send_tool_list_changed(self) -> None: ...
        >>> r = capture_session("conn_abc", FakeSession())
        >>> r.is_ok
        True
        >>> _ = release_session("conn_abc")

    Args:
        connection_id: The connection identifier from ``ConnectionContext``.
        session: The upstream MCP session implementing ``UpstreamSession``.

    Returns:
        Result[None, str] on success, or error if connection_id is empty.
    """
    if not connection_id:
        return Result(error="SESSION_CAPTURE_FAILED: connection_id must not be empty")
    with _session_registry_lock:
        _session_registry[connection_id] = session
    return Result(value=None)


def release_session(connection_id: str) -> Result[None, str]:
    """Remove a captured session for a disconnected connection.

    Called during connection teardown to prevent session leaks.
    Silently succeeds if the connection_id is not in the registry
    (idempotent cleanup).

    Thread-safe: acquires ``_session_registry_lock``.

    Examples:
        >>> from tela.shell.upstream import release_session
        >>> r = release_session("nonexistent")
        >>> r.is_ok
        True

    Args:
        connection_id: The connection identifier to release.

    Returns:
        Result[None, str] always succeeds.
    """
    with _session_registry_lock:
        _session_registry.pop(connection_id, None)
    return Result(value=None)


def get_captured_session(connection_id: str) -> Result[UpstreamSession, str]:
    """Look up a captured session by connection ID.

    Returns the session if found, or an error string if no session
    is registered for the given connection.

    Thread-safe: acquires ``_session_registry_lock``.

    Examples:
        >>> from tela.shell.upstream import get_captured_session
        >>> r = get_captured_session("nonexistent")
        >>> r.is_err
        True
        >>> "not found" in r.error
        True

    Args:
        connection_id: The connection identifier to look up.

    Returns:
        Result[UpstreamSession, str] with the session or error.
    """
    with _session_registry_lock:
        session = _session_registry.get(connection_id)
    if session is None:
        return Result(error=f"SESSION_NOT_FOUND: session for '{connection_id}' not found")
    return Result(value=session)


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
        >>> from tela.shell.gateway import get_runtime
        >>> runtime = get_runtime()
        >>> runtime.config = None  # Gateway not started
        >>> result = asyncio.run(handle_initialize({}))
        >>> result.is_err
        True
        >>> "GATEWAY_NOT_STARTED" in result.error
        True

    Args:
        client_info: MCP clientInfo dict.

    Returns:
        Result[ConnectionContext, str] once implemented.
    """

    runtime = get_runtime()
    if runtime.config is None:
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

    connection_id = f"conn_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    if runtime.config.auth.mode == AuthMode.OPEN:
        status = (
            DefaultProfileResolutionStatus.RESOLVED
            if runtime.config.resolved_default_profile is not None
            else DefaultProfileResolutionStatus.MISSING
        )

        binding_result = resolve_initialize_profile_binding(
            resolved_default_profile=runtime.config.resolved_default_profile,
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

        secrets = runtime.secrets
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

    # Register connection in runtime.
    with _runtime_lock:
        runtime.connections.append(ctx)
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
        >>> from tela.shell.gateway import get_runtime
        >>> from tela.core.models import ConnectionContext
        >>> runtime = get_runtime()
        >>> runtime.config = None  # Gateway not started
        >>> conn = ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z")
        >>> result = asyncio.run(handle_tools_list(conn))
        >>> result.is_err and "GATEWAY_NOT_STARTED" in result.error
        True

    Args:
        connection: Active upstream connection context.

    Returns:
        List of tool dicts once implemented.
    """

    runtime = get_runtime()
    if runtime.config is None:
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

    profile = runtime.config.profiles.get(connection.profile_name)
    if profile is None:
        return Result(
            error=f"PROFILE_NOT_FOUND: profile '{connection.profile_name}' not found"
        )

    all_tools_result = get_all_tools()
    if all_tools_result.is_err:
        return Result(error=all_tools_result.error)
    assert all_tools_result.value is not None
    all_tools = all_tools_result.value
    server_default_postures: dict[str, Posture] = {}
    for sname, scfg in runtime.config.servers.items():
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
        >>> from tela.shell.gateway import get_runtime
        >>> from tela.core.models import ConnectionContext
        >>> runtime = get_runtime()
        >>> runtime.config = None  # Gateway not started
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

    runtime = get_runtime()
    if runtime.config is None:
        return Result(
            error=TelaError(
                code="GATEWAY_NOT_STARTED", message="Gateway has not been started"
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

    # Look up tool
    tool = get_registry().get_tool(tool_name)
    if tool is None:
        return Result(
            error=TelaError(
                code="TOOL_NOT_FOUND", message=f"Tool '{tool_name}' not found"
            )
        )

    # Look up profile
    profile = runtime.config.profiles.get(connection.profile_name)
    if profile is None:
        return Result(
            error=TelaError(
                code="PROFILE_NOT_FOUND",
                message=f"Profile '{connection.profile_name}' not found",
            )
        )

    # Enforce
    server_config = runtime.config.servers.get(tool.server_name)
    default_posture = server_config.default_posture if server_config else Posture.NONE
    enforcement_result = enforce_tool_call(tool_name, tool, profile, default_posture)
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

    with _runtime_lock:
        runtime.total_tool_calls += 1

    return await call_tool(tool.server_name, tool_name, stripped_args)


def handle_profiles_list() -> Result[list[dict], str]:
    """Return list of configured profiles.

    Returns list of configured profiles.

    Examples:
        >>> from tela.shell.gateway import get_runtime
        >>> runtime = get_runtime()
        >>> runtime.config = None  # Gateway not started
        >>> result = handle_profiles_list()
        >>> result.is_err and "GATEWAY_NOT_STARTED" in result.error
        True

    Returns:
        List of profile dicts once implemented.
    """

    runtime = get_runtime()
    if runtime.config is None:
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

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
            for name, p in runtime.config.profiles.items()
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
            "Failed to send tools/list_changed to %s (digest=%s)",
            connection.connection_id,
            tools_digest,
            exc_info=True,
        )
        return Result(error=f"NOTIFICATION_SEND_FAILED: {connection.connection_id}")
    return Result(value=None)
