"""Upstream MCP handler contracts for open-mode and gateway runtime.

This module defines the upstream-facing MCP protocol handler interfaces:
initialize, tools/list, tools/call, tela.profiles, and notifications.
The open-mode initialize binding is implemented; remaining handlers are
contract stubs for the gateway.runtime phase.

Client-provided connection metadata is explicitly not a profile selection
channel in open mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tela.core.models import (
    ConnectionContext,
    DefaultProfileResolutionStatus,
    InitializeProfileBinding,
    TelaError,
)
from tela.shell.config_loader import Result


@dataclass(frozen=True)
class InitializeContext:
    """Connection metadata contract visible at MCP initialize boundary.

    Client-provided connection metadata is explicitly not a profile selection
    channel in open mode.
    """

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

    The resolved default profile comes from the shared config authority helper
    (``resolve_open_mode_default_profile``). This function does not re-derive
    profile choice; it only validates and binds the pre-resolved fact.

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
        ``Result[InitializeProfileBinding, str]`` with the binding on success,
        or a rejection reason on failure.
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


# --- MCP Handler Contracts (stubs) ---


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def handle_initialize(
    client_info: dict,
) -> Result[ConnectionContext, str]:
    """Handle MCP initialize request.

    In token mode: extract capability_token from clientInfo, validate, bind profile.
    In open mode: bind explicit default profile.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(handle_initialize({}))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: handle_initialize pending

    Args:
        client_info: MCP clientInfo dict from initialize request.

    Returns:
        ``Result[ConnectionContext, str]`` once implemented.
    """

    raise NotImplementedError("Contract stub: handle_initialize pending")


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns list[dict] per DESIGN.md MCP protocol spec.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def handle_tools_list(
    connection: ConnectionContext,
) -> list[dict]:
    """Return filtered tool list for the bound profile.

    Each tool retains its original JSON Schema from downstream.
    Only tools permitted by the profile are included.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(handle_tools_list(
        ...     ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z")
        ... ))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: handle_tools_list pending

    Args:
        connection: Active upstream connection context.

    Returns:
        List of tool dicts once implemented.
    """

    raise NotImplementedError("Contract stub: handle_tools_list pending")


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def handle_tools_call(
    connection: ConnectionContext,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]:
    """Handle a tools/call request.

    1. Extract _meta from arguments (if present)
    2. Strip _meta from arguments
    3. Run enforcement chain
    4. If denied: return error, write audit entry
    5. If allowed: forward to downstream, return result
    6. Write audit entry

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(handle_tools_call(
        ...     ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"),
        ...     "some_tool",
        ...     {},
        ... ))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: handle_tools_call pending

    Args:
        connection: Active upstream connection context.
        tool_name: Tool to invoke.
        arguments: Tool arguments (may contain _meta).

    Returns:
        ``Result[dict, TelaError]`` once implemented.
    """

    raise NotImplementedError("Contract stub: handle_tools_call pending")


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns list[dict] per DESIGN.md MCP protocol spec.
def handle_profiles_list() -> list[dict]:
    """Return list of configured profiles (tela.profiles MCP method).

    Contract stub: raises NotImplementedError.

    Examples:
        >>> handle_profiles_list()
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: handle_profiles_list pending

    Returns:
        List of profile dicts once implemented.
    """

    raise NotImplementedError("Contract stub: handle_profiles_list pending")


# @invar:allow dead_export: handler wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def notify_tools_changed(
    connection: ConnectionContext,
    tools_digest: str,
) -> None:
    """Send notifications/tools/list_changed to an upstream client.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(notify_tools_changed(
        ...     ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"),
        ...     "digest123",
        ... ))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: notify_tools_changed pending

    Args:
        connection: Target upstream connection.
        tools_digest: Digest of the updated tool list.
    """

    raise NotImplementedError("Contract stub: notify_tools_changed pending")
