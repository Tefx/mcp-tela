"""Upstream initialize handling for open-mode profile binding.

This module implements MCP initialize-time binding for open mode. Profile
resolution uses the shared resolved default-profile fact from config authority;
client-provided connection metadata is explicitly not a profile selection
channel in open mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tela.core.models import DefaultProfileResolutionStatus, InitializeProfileBinding
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

    # Client metadata is explicitly ignored for profile selection in open mode.
    # The context parameter exists for protocol completeness and future
    # extensibility, but must not influence the profile binding decision.
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
