"""Upstream initialize contracts for open-mode profile binding.

This module defines acceptance-only interfaces for handling MCP initialize in
open mode. No request handling implementation is provided in this step.
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

    Examples:
        >>> resolve_initialize_profile_binding(
        ...     resolved_default_profile=None,
        ...     default_resolution_status=DefaultProfileResolutionStatus.MISSING,
        ...     context=InitializeContext(connection_metadata={"profile": "dev"}),
        ... )
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: resolve_initialize_profile_binding pending

    Args:
        resolved_default_profile: Profile selected by config/CLI authority.
        default_resolution_status: Prior open-mode default resolution outcome.
        context: Initialize request metadata; profile hints here are ignored.

    Returns:
        `Result[InitializeProfileBinding, str]` once implemented.

    Raises:
        NotImplementedError: This step is contract-only.
    """

    _ = (resolved_default_profile, default_resolution_status, context)
    raise NotImplementedError(
        "Contract stub: resolve_initialize_profile_binding pending"
    )
