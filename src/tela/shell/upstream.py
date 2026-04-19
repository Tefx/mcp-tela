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
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Mapping, cast

from pydantic import ValidationError

from tela.core.errors import (
    CONNECTION_NOT_FOUND,
    GATEWAY_NOT_STARTED,
    PROFILE_NOT_FOUND,
)
from tela.core.models import (
    AuditLevel,
    AuthMode,
    CapabilityToken,
    ConnectionContext,
    DefaultProfileResolutionStatus,
    EnforcementResult,
    EnforcementVerdict,
    InitializeProfileBinding,
    Posture,
    ProfileInfo,
    TelaError,
    MetaField,
)
from tela.core.token import resolve_token_init_binding
from tela.shell.result import Result
from tela.shell.audit import audit_write, build_audit_entry
from tela.shell.downstream import (
    call_tool,
    get_all_tools,
    get_registry,
)
from tela.shell.builtin_tools import handle_profiles_list as build_profile_list_payload
from tela.shell.gateway_runtime import (
    UpstreamSession,
    add_runtime_connection,
    capture_session,
    get_captured_session,
    get_connection_id_for_session,
    get_runtime_config,
    get_runtime_secrets,
    has_bridge_registration,
    increment_tool_calls,
    release_session,
    remove_runtime_connection,
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
_CAPABILITY_TOKEN_KEY = "capability_token"
_TOKEN_ALIAS_FIELD_PRESENT = "alias_field_present"
_TOKEN_SCHEMA_INVALID = "token_schema_invalid"
_TOKEN_FIELD_OUTSIDE_CAPABILITY_TOKEN = "extra_key"
_SHARED_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_CANONICAL_TOKEN_FIELDS = frozenset(
    {
        "token_id",
        "profile_id",
        "persona_ref",
        "instance_id",
        "max_depth",
        "issued_at",
        "expires_at",
        "token_version",
        "signature",
    }
)
_TOKEN_ALIAS_FIELDS = frozenset({"profile_name", "tools_profile"})


# @shell_orchestration: maps shell-bound pydantic validation failures into canonical initialize rejection codes.
def _canonical_validation_error(
    exc: ValidationError,
) -> Result[tuple[str, str, str | None], str]:
    """Project pydantic validation failures onto canonical conformance codes."""

    first_error = exc.errors()[0]
    field = ".".join(str(part) for part in first_error.get("loc", ())) or None
    error_type = str(first_error.get("type", ""))
    if error_type == "missing":
        return Result(
            value=("missing_required_field", first_error.get("msg", error_type), field)
        )
    if error_type.endswith("_type") or error_type in {"int_type", "string_type"}:
        return Result(value=("wrong_type", first_error.get("msg", error_type), field))
    if error_type == "extra_forbidden":
        return Result(value=("extra_key", first_error.get("msg", error_type), field))
    return Result(
        value=("token_schema_invalid", first_error.get("msg", error_type), field)
    )


# @shell_orchestration: validates _meta before audit emission so shell-bound audit entries stay typed.
def _audit_meta_field(arguments_meta: dict | None) -> Result[MetaField | None, str]:
    """Return validated audit metadata when the call supplied canonical _meta."""

    if arguments_meta is None:
        return Result(value=None)
    try:
        return Result(value=MetaField.model_validate(arguments_meta))
    except ValidationError as exc:
        return Result(error=f"invalid _meta audit payload: {exc}")


# @shell_orchestration: audit emission coordinates shell-level audit entry construction and write side effects for the MCP tools/call boundary.
async def _audit_tool_call(
    *,
    connection: ConnectionContext,
    config_level: AuditLevel,
    tool_name: str,
    server_name: str,
    result: EnforcementResult,
    latency_ms: float,
    arguments: dict | None,
    held_meta: dict | None,
) -> None:
    """Write an audit entry for downstream tool calls when construction succeeds."""

    meta_result = _audit_meta_field(held_meta)
    audit_entry_result = build_audit_entry(
        level=config_level,
        connection=connection,
        tool_name=tool_name,
        server_name=server_name,
        result=result,
        latency_ms=latency_ms,
        arguments=arguments,
        meta=meta_result.value if meta_result.is_ok else None,
    )
    if audit_entry_result.is_ok and audit_entry_result.value is not None:
        await audit_write(audit_entry_result.value)


def _audit_initialize_rejection(
    code: str,
    detail: str,
    *,
    location: str,
    field: str | None = None,
) -> None:
    audit_parts = [f"code={code}", f"location={location}"]
    if field is not None:
        audit_parts.append(f"field={field}")
    audit_parts.append(f"detail={detail}")
    logger.warning("INITIALIZE_AUDIT %s", " ".join(audit_parts))


def _reject_initialize(
    code: str,
    detail: str,
    *,
    location: str,
    field: str | None = None,
) -> Result[CapabilityToken, str]:
    _audit_initialize_rejection(code, detail, location=location, field=field)
    if field is None:
        return Result(error=f"INITIALIZE_REJECTED: {code}: {detail}")
    return Result(error=f"INITIALIZE_REJECTED: {code}: field={field}: {detail}")


# @invar:allow shell_result: pure validator used only inside shell-bound initialize handler.
# @shell_complexity: clientInfo key validation deliberately branches across reserved namespace, top-level token fields, and alias fail-closed checks.
def _invalid_reserved_client_info_key(
    client_info: Mapping[object, object],
) -> str | None:
    """Return the first reserved top-level key that must be rejected.

    Source: opifex/final-canonical-contract.md requires capability-token fields
    to remain canonical-only on the shared token surface and forbids alias keys;
    this shell boundary therefore rejects conflicting top-level semantics instead
    of reinterpreting them.
    """

    for key in client_info:
        key_str = str(key)
        if key_str in _CANONICAL_TOKEN_FIELDS or key_str in _TOKEN_ALIAS_FIELDS:
            return key_str
        if key_str == _BRIDGE_CONNECTION_ID_KEY or key_str == _CAPABILITY_TOKEN_KEY:
            continue
        if key_str.startswith("tela_") or key_str.startswith("opifex_"):
            return key_str
    return None


# @invar:allow shell_result: pure snapshot helper used only inside shell-bound initialize handler.
def _build_client_info_snapshot(
    client_info: Mapping[object, object],
    token: CapabilityToken | None,
) -> dict[str, str]:
    """Preserve accepted clientInfo hints while flattening canonical token fields."""

    snapshot = {
        str(key): str(value)
        for key, value in client_info.items()
        if str(key) != _CAPABILITY_TOKEN_KEY
    }
    if token is not None:
        snapshot.update(
            {
                key: str(value)
                for key, value in token.model_dump(exclude_none=True).items()
            }
        )
    return snapshot


# @shell_complexity: canonical token extraction must branch across reserved keys, legacy aliases, extra fields, and pydantic schema failures before fail-closed rejection.
def _extract_capability_token(
    client_info: Mapping[object, object],
) -> Result[CapabilityToken, str]:
    """Extract the nested canonical capability token from MCP clientInfo."""

    invalid_key = _invalid_reserved_client_info_key(client_info)
    if invalid_key is not None:
        if invalid_key in _TOKEN_ALIAS_FIELDS:
            return _reject_initialize(
                _TOKEN_ALIAS_FIELD_PRESENT,
                (
                    f"top-level client_info key '{invalid_key}' is invalid; "
                    "use client_info.capability_token.profile_id"
                ),
                location="client_info",
                field=invalid_key,
            )
        return _reject_initialize(
            _TOKEN_FIELD_OUTSIDE_CAPABILITY_TOKEN,
            (
                f"top-level client_info key '{invalid_key}' is reserved; "
                "use client_info.capability_token"
            ),
            location="client_info",
            field=invalid_key,
        )

    token_payload = client_info.get(_CAPABILITY_TOKEN_KEY)
    if not isinstance(token_payload, dict):
        return Result(
            error=(
                "INITIALIZE_REJECTED: token mode requires client_info."
                "capability_token object"
            )
        )

    alias_fields = sorted(
        str(key) for key in token_payload if str(key) in _TOKEN_ALIAS_FIELDS
    )
    if alias_fields:
        joined = ", ".join(alias_fields)
        return _reject_initialize(
            _TOKEN_ALIAS_FIELD_PRESENT,
            (f"capability_token field(s) {joined} are invalid; use 'profile_id'"),
            location="capability_token",
            field=joined,
        )

    extra_fields = sorted(
        str(key)
        for key in token_payload
        if str(key) not in _CANONICAL_TOKEN_FIELDS
        and str(key) not in _TOKEN_ALIAS_FIELDS
    )
    if extra_fields:
        return _reject_initialize(
            _TOKEN_FIELD_OUTSIDE_CAPABILITY_TOKEN,
            "rejected_keys=" + ",".join(extra_fields),
            location="capability_token",
        )

    try:
        return Result(value=CapabilityToken(**token_payload))
    except ValidationError as exc:
        validation_result = _canonical_validation_error(exc)
        assert validation_result.value is not None
        error_code, detail, field = validation_result.value
        return _reject_initialize(
            error_code,
            detail,
            location="capability_token",
            field=field,
        )


# @invar:allow shell_result: pure validator used only inside shared MCP surface emission.
def _invalid_shared_tool_name(tool_name: str) -> str | None:
    """Return the tool name when it violates shared snake_case naming."""

    if _SHARED_TOOL_NAME_PATTERN.fullmatch(tool_name) is None:
        return tool_name
    return None


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
        >>> conn = ConnectionContext(connection_id="c1", profile_id="p", connected_at="t")
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
    bridge_connection_id_str: str | None = None
    had_existing_bridge_connection = False
    if bridge_connection_id is not None:
        bridge_connection_id_str = str(bridge_connection_id)
        registration_result = has_bridge_registration(bridge_connection_id_str)
        if registration_result.is_err:
            return Result(error=registration_result.error)
        if not registration_result.value:
            return Result(
                error=f"{CONNECTION_NOT_FOUND}: bridge initialize requires pre-registered connection '{bridge_connection_id_str}'"
            )

        removed_result = remove_runtime_connection(bridge_connection_id_str)
        if removed_result.is_err:
            return Result(error=removed_result.error)
        had_existing_bridge_connection = bool(removed_result.value)

        if had_existing_bridge_connection:
            idle_manager = get_idle_manager()
            if idle_manager is not None:
                decrement_result = await idle_manager.decrement()
                if decrement_result.is_err:
                    return Result(error=decrement_result.error)

        released_result = release_session(bridge_connection_id_str)
        if released_result.is_err:
            return Result(error=released_result.error)

    connection_id = bridge_connection_id_str or f"conn_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    token: CapabilityToken | None = None

    if config.auth.mode == AuthMode.OPEN:
        if config.resolved_default_profile is not None:
            status = DefaultProfileResolutionStatus.RESOLVED
        else:
            default_count = sum(
                1 for profile in config.profiles.values() if profile.default
            )
            status = (
                DefaultProfileResolutionStatus.AMBIGUOUS
                if default_count > 1
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
        profile_id = binding_result.value.resolved_default_profile
        assert profile_id is not None
    else:
        # Token mode: validate capability token and bind to token's canonical profile_id.
        token_result = _extract_capability_token(client_info)
        if token_result.is_err:
            return Result(error=token_result.error)
        assert token_result.value is not None
        token = token_result.value

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

        profile_id = binding.profile_id
        if profile_id not in config.profiles:
            return Result(
                error=(
                    "INITIALIZE_REJECTED: unknown_profile_binding: "
                    f"profile_id '{profile_id}' is not configured"
                )
            )

    ctx = ConnectionContext(
        connection_id=connection_id,
        profile_id=profile_id,
        connected_at=now_iso,
        init_mode=config.auth.mode,
        client_info_snapshot=_build_client_info_snapshot(
            client_info,
            token if config.auth.mode == AuthMode.TOKEN else None,
        ),
        bridge_connection_id=(
            str(bridge_connection_id) if bridge_connection_id is not None else None
        ),
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
        >>> conn = ConnectionContext(connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z")
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

    profile = config.profiles.get(connection.profile_id)
    if profile is None:
        return Result(
            error=f"PROFILE_NOT_FOUND: profile '{connection.profile_id}' not found"
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
    for tool in permitted:
        invalid_name = _invalid_shared_tool_name(tool.name)
        if invalid_name is not None:
            return Result(
                error=(
                    "INVALID_TOOL_NAME: shared MCP tool names must be snake_case; "
                    f"got '{invalid_name}'"
                )
            )
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
        >>> conn = ConnectionContext(connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z")
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

    if not isinstance(connection, ConnectionContext):
        return Result(
            error=TelaError(
                code="missing_bound_connection",
                message="tools/call requires an admitted connection",
            )
        )

    start_time = time.monotonic()
    invalid_tool_name = _invalid_shared_tool_name(tool_name)
    if invalid_tool_name is not None:
        latency_ms = max(0.0, (time.monotonic() - start_time) * 1000.0)
        await _audit_tool_call(
            connection=connection,
            config_level=config.audit.level,
            tool_name=tool_name,
            server_name="unknown",
            result=EnforcementResult(
                verdict=EnforcementVerdict.DENY,
                denied_by="shared_surface_validation",
                error_code="invalid_tool_name",
                error_message=(
                    "shared MCP tool names must be snake_case; "
                    f"got '{invalid_tool_name}'"
                ),
            ),
            latency_ms=latency_ms,
            arguments=arguments,
            held_meta=None,
        )
        return Result(
            error=TelaError(
                code="invalid_tool_name",
                message=(
                    "shared MCP tool names must be snake_case; "
                    f"got '{invalid_tool_name}'"
                ),
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
        latency_ms = max(0.0, (time.monotonic() - start_time) * 1000.0)
        await _audit_tool_call(
            connection=connection,
            config_level=config.audit.level,
            tool_name=tool_name,
            server_name="unknown",
            result=EnforcementResult(
                verdict=EnforcementVerdict.DENY,
                denied_by="registry_lookup",
                error_code="TOOL_NOT_FOUND",
                error_message=f"Tool '{tool_name}' not found",
            ),
            latency_ms=latency_ms,
            arguments=stripped_args,
            held_meta=held_meta,
        )
        return Result(
            error=TelaError(
                code="TOOL_NOT_FOUND", message=f"Tool '{tool_name}' not found"
            )
        )
    routing_name = tool.raw_name or tool.name
    profile = config.profiles.get(connection.profile_id)
    if profile is None:
        latency_ms = max(0.0, (time.monotonic() - start_time) * 1000.0)
        await _audit_tool_call(
            connection=connection,
            config_level=config.audit.level,
            tool_name=tool_name,
            server_name=tool.server_name,
            result=EnforcementResult(
                verdict=EnforcementVerdict.DENY,
                denied_by="profile_lookup",
                error_code="PROFILE_NOT_FOUND",
                error_message=f"Profile '{connection.profile_id}' not found",
            ),
            latency_ms=latency_ms,
            arguments=stripped_args,
            held_meta=held_meta,
        )
        return Result(
            error=TelaError(
                code="PROFILE_NOT_FOUND",
                message=f"Profile '{connection.profile_id}' not found",
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
        latency_ms = max(0.0, (time.monotonic() - start_time) * 1000.0)
        await _audit_tool_call(
            connection=connection,
            config_level=config.audit.level,
            tool_name=tool_name,
            server_name=tool.server_name,
            result=enforcement,
            latency_ms=latency_ms,
            arguments=stripped_args,
            held_meta=held_meta,
        )
        return Result(
            error=TelaError(
                code=enforcement.error_code or "AUTHZ_DENY",
                message=enforcement.error_message or "Tool call denied",
            )
        )

    increment_tool_calls()
    call_result = await call_tool(tool.server_name, routing_name, stripped_args)
    latency_ms = max(0.0, (time.monotonic() - start_time) * 1000.0)
    if call_result.is_err:
        await _audit_tool_call(
            connection=connection,
            config_level=config.audit.level,
            tool_name=tool_name,
            server_name=tool.server_name,
            result=EnforcementResult(
                verdict=EnforcementVerdict.DENY,
                denied_by="downstream_call",
                error_code=(
                    call_result.error.code if call_result.error is not None else None
                ),
                error_message=(
                    call_result.error.message if call_result.error is not None else None
                ),
            ),
            latency_ms=latency_ms,
            arguments=stripped_args,
            held_meta=held_meta,
        )
        return call_result

    await _audit_tool_call(
        connection=connection,
        config_level=config.audit.level,
        tool_name=tool_name,
        server_name=tool.server_name,
        result=EnforcementResult(verdict=EnforcementVerdict.ALLOW),
        latency_ms=latency_ms,
        arguments=stripped_args,
        held_meta=held_meta,
    )
    return call_result


def handle_profiles_list() -> Result[list[ProfileInfo], str]:
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

    try:
        return cast(
            Result[list[ProfileInfo], str], Result(value=build_profile_list_payload())
        )
    except RuntimeError as exc:
        return Result(error=str(exc))


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
        ...     ConnectionContext(connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z"),
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
