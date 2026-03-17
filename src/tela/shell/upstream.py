"""Upstream MCP handler for tools/list, tools/call, and open-mode initialize.

Implements the upstream-facing MCP protocol handler interfaces. Open-mode
initialize binding is preserved from prior implementation. tools/list filtering
uses the enforcement chain. tools/call strips _meta and runs enforcement.

Audit emission is deferred to audit.runtime.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from tela.core.enforcement import enforce
from tela.core.models import (
    ConnectionContext,
    DefaultProfileResolutionStatus,
    EnforcementResult,
    EnforcementVerdict,
    InitializeProfileBinding,
    Posture,
    ProfileConfig,
    ResolvedTool,
    TelaError,
)
from tela.shell.config_loader import Result
from tela.shell.downstream import call_tool, get_all_tools, get_tool_server, get_registry
from tela.shell.gateway import get_runtime


@dataclass(frozen=True)
class InitializeContext:
    """Connection metadata contract visible at MCP initialize boundary."""

    connection_metadata: Mapping[str, str]


# @invar:allow dead_export: initialize wiring is connected in a later runtime step.
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


# --- tools/list filtering ---


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns list per tools/list filtering spec.
def filter_tools_for_profile(
    all_tools: dict[str, list[ResolvedTool]],
    profile: ProfileConfig,
    server_default_postures: dict[str, Posture],
) -> list[ResolvedTool]:
    """Filter resolved tools to those permitted by a profile.

    A tool is included if and only if:
    1. Its family exists in the profile's tools map
    2. Its posture (classified or default) <= the profile's ceiling
    3. It is not explicitly denied by a profile tool_overrides entry
    4. If side_effect_policy is read_only, only tools with posture <= read_only

    Examples:
        >>> from tela.core.models import Posture, ProfileConfig, ResolvedTool
        >>> tools = {"fs": [ResolvedTool(name="read_file", server_name="fs", family="fs", posture=Posture.READ_ONLY)]}
        >>> profile = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
        >>> result = filter_tools_for_profile(tools, profile, {"fs": Posture.NONE})
        >>> len(result)
        1
        >>> result[0].name
        'read_file'

    Args:
        all_tools: Server name to resolved tool list mapping.
        profile: Bound profile configuration.
        server_default_postures: Server name to default posture mapping.

    Returns:
        List of tools permitted by the profile.
    """

    allowed_token = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
    permitted: list[ResolvedTool] = []

    for server_name, tools in all_tools.items():
        default_posture = server_default_postures.get(server_name, Posture.NONE)
        for tool in tools:
            result = enforce(
                tool.name, tool, profile, allowed_token, default_posture
            )
            if result.verdict == EnforcementVerdict.ALLOW:
                permitted.append(tool)

    return permitted


# --- tools/call with _meta stripping ---


# @invar:allow shell_result: returns tuple per _meta extraction spec, not a failable I/O boundary.
def strip_meta(arguments: dict) -> tuple[dict, dict | None]:
    """Strip _meta from tool call arguments.

    Returns (stripped_arguments, held_meta). held_meta is None if _meta
    was not present.

    Examples:
        >>> strip_meta({"path": "/tmp", "_meta": {"trace_id": "t1"}})
        ({'path': '/tmp'}, {'trace_id': 't1'})
        >>> strip_meta({"path": "/tmp"})
        ({'path': '/tmp'}, None)

    Args:
        arguments: Raw tool call arguments.

    Returns:
        Tuple of (stripped arguments, held _meta or None).
    """

    meta = arguments.get("_meta")
    stripped = {k: v for k, v in arguments.items() if k != "_meta"}
    return stripped, meta


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns EnforcementResult per enforcement chain spec.
def enforce_tool_call(
    tool_name: str,
    tool: ResolvedTool,
    profile: ProfileConfig,
    default_posture: Posture,
) -> EnforcementResult:
    """Run enforcement chain for a tool call in open mode (no token).

    Open mode uses a pre-allowed token result.

    Examples:
        >>> from tela.core.models import ResolvedTool, ProfileConfig, Posture
        >>> tool = ResolvedTool(name="read_file", server_name="fs", family="fs", posture=Posture.READ_ONLY)
        >>> profile = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
        >>> enforce_tool_call("read_file", tool, profile, Posture.NONE).verdict
        <EnforcementVerdict.ALLOW: 'allow'>

    Args:
        tool_name: Name of the tool.
        tool: Resolved tool metadata.
        profile: Bound profile configuration.
        default_posture: Server's default posture.

    Returns:
        EnforcementResult.
    """

    allowed_token = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
    return enforce(tool_name, tool, profile, allowed_token, default_posture)


# --- MCP Handler Stubs (remaining) ---


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def handle_initialize(
    client_info: dict,
) -> Result[ConnectionContext, str]:
    """Handle MCP initialize request.

    Creates a ConnectionContext and registers the connection.

    Examples:
        >>> # handle_initialize requires gateway to be started
        >>> pass  # doctest: +SKIP

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

    # In open mode, use the resolved default profile
    profile_name = runtime.config.resolved_default_profile or "default"

    ctx = ConnectionContext(
        connection_id=connection_id,
        profile_name=profile_name,
        connected_at=now_iso,
    )

    # Register connection in runtime
    runtime.connections.append(ctx)
    return Result(value=ctx)


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns list[dict] per DESIGN.md MCP protocol spec.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def handle_tools_list(
    connection: ConnectionContext,
) -> list[dict]:
    """Return filtered tool list for the bound profile.

    Returns filtered tool list for the bound profile.

    Examples:
        >>> # handle_tools_list requires gateway to be started
        >>> pass  # doctest: +SKIP

    Args:
        connection: Active upstream connection context.

    Returns:
        List of tool dicts once implemented.
    """

    runtime = get_runtime()
    if runtime.config is None:
        return []

    profile = runtime.config.profiles.get(connection.profile_name)
    if profile is None:
        return []

    all_tools = get_all_tools()
    server_default_postures: dict[str, Posture] = {}
    for sname, scfg in runtime.config.servers.items():
        server_default_postures[sname] = scfg.default_posture

    permitted = filter_tools_for_profile(all_tools, profile, server_default_postures)
    return [{"name": t.name, "inputSchema": t.schema_ or {}} for t in permitted]


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def handle_tools_call(
    connection: ConnectionContext,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]:
    """Handle a tools/call request.

    Runs enforcement chain and forwards to downstream.

    Examples:
        >>> # handle_tools_call requires gateway to be started
        >>> pass  # doctest: +SKIP

    Args:
        connection: Active upstream connection context.
        tool_name: Tool to invoke.
        arguments: Tool arguments (may contain _meta).

    Returns:
        Result[dict, TelaError] once implemented.
    """

    runtime = get_runtime()
    if runtime.config is None:
        return Result(error=TelaError(code="GATEWAY_NOT_STARTED", message="Gateway has not been started"))

    # Strip _meta
    stripped_args, held_meta = strip_meta(arguments)

    # Look up tool
    tool = get_registry().get_tool(tool_name)
    if tool is None:
        return Result(error=TelaError(code="TOOL_NOT_FOUND", message=f"Tool '{tool_name}' not found"))

    # Look up profile
    profile = runtime.config.profiles.get(connection.profile_name)
    if profile is None:
        return Result(error=TelaError(code="PROFILE_NOT_FOUND", message=f"Profile '{connection.profile_name}' not found"))

    # Enforce
    server_config = runtime.config.servers.get(tool.server_name)
    default_posture = server_config.default_posture if server_config else Posture.NONE
    enforcement = enforce_tool_call(tool_name, tool, profile, default_posture)

    if enforcement.verdict == EnforcementVerdict.DENY:
        return Result(error=TelaError(
            code=enforcement.error_code or "AUTHZ_DENY",
            message=enforcement.error_message or "Tool call denied",
        ))

    runtime.total_tool_calls += 1

    return await call_tool(tool.server_name, tool_name, stripped_args)


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns list[dict] per DESIGN.md MCP protocol spec.
def handle_profiles_list() -> list[dict]:
    """Return list of configured profiles.

    Returns list of configured profiles.

    Examples:
        >>> # handle_profiles_list requires gateway to be started
        >>> pass  # doctest: +SKIP

    Returns:
        List of profile dicts once implemented.
    """

    runtime = get_runtime()
    if runtime.config is None:
        return []

    return [
        {"name": name, "default": p.default, "tools": {k: v.value for k, v in p.tools.items()}}
        for name, p in runtime.config.profiles.items()
    ]


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def notify_tools_changed(
    connection: ConnectionContext,
    tools_digest: str,
) -> None:
    """Send notifications/tools/list_changed to an upstream client.

    No-op until actual MCP transport is wired.

    Examples:
        >>> import asyncio
        >>> asyncio.run(notify_tools_changed(
        ...     ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"),
        ...     "digest123",
        ... ))

    Args:
        connection: Target upstream connection.
        tools_digest: Digest of the updated tool list.
    """
    from tela.shell.reload import _notify_callback

    if _notify_callback is not None:
        await _notify_callback(tools_digest)
