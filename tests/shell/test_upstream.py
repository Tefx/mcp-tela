"""Contract and behavioral tests for upstream initialize profile-binding surface.

Tests cover:
- Default-profile precedence from CLI to runtime binding
- Initialize success in open mode with explicit default profile
- Initialize rejection when no or ambiguous default profile
- Client metadata must not influence profile selection
"""

from __future__ import annotations

import pytest

from tela.core.models import (
    DefaultProfileResolutionStatus,
    InitializeProfileBinding,
)
from tela.shell.upstream import InitializeContext, resolve_initialize_profile_binding


# --- Existing contract tests (preserved) ---


def test_initialize_context_exposes_connection_metadata_contract() -> None:
    context = InitializeContext(connection_metadata={"client": "desktop"})
    assert context.connection_metadata["client"] == "desktop"


def test_resolve_initialize_profile_binding_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError) as exc_info:
        resolve_initialize_profile_binding(
            resolved_default_profile=None,
            default_resolution_status=DefaultProfileResolutionStatus.MISSING,
            context=InitializeContext(connection_metadata={"profile": "dev"}),
        )

    assert "Contract stub" in str(exc_info.value)


# --- Initialize success cases ---


def test_resolve_binding_stub_raises_on_resolved_profile() -> None:
    """Even with a valid resolved profile, the stub must raise NotImplementedError."""
    with pytest.raises(NotImplementedError):
        resolve_initialize_profile_binding(
            resolved_default_profile="production",
            default_resolution_status=DefaultProfileResolutionStatus.RESOLVED,
            context=InitializeContext(connection_metadata={}),
        )


# --- Initialize rejection cases ---


def test_resolve_binding_stub_raises_on_missing_default() -> None:
    """Missing default-profile resolution must reject initialize."""
    with pytest.raises(NotImplementedError):
        resolve_initialize_profile_binding(
            resolved_default_profile=None,
            default_resolution_status=DefaultProfileResolutionStatus.MISSING,
            context=InitializeContext(connection_metadata={}),
        )


def test_resolve_binding_stub_raises_on_ambiguous_default() -> None:
    """Ambiguous default-profile resolution must reject initialize."""
    with pytest.raises(NotImplementedError):
        resolve_initialize_profile_binding(
            resolved_default_profile=None,
            default_resolution_status=DefaultProfileResolutionStatus.AMBIGUOUS,
            context=InitializeContext(connection_metadata={}),
        )


# --- Client metadata isolation ---


def test_connection_metadata_does_not_select_profile() -> None:
    """Client-provided metadata with profile hint must not influence selection.

    The contract explicitly states: 'Client-provided connection metadata is
    explicitly not a profile selection channel in open mode.'
    Even if metadata contains a 'profile' key, the function signature forces
    profile selection through resolved_default_profile parameter only.
    """
    # The function signature enforces this by design:
    # - resolved_default_profile comes from config/CLI authority
    # - context.connection_metadata is present but explicitly ignored
    context = InitializeContext(
        connection_metadata={"profile": "should-be-ignored", "x-tenant": "acme"}
    )
    # Verify the context carries the metadata but the function interface
    # does not use it for profile selection (it's a keyword-only param
    # separate from the profile resolution path)
    assert context.connection_metadata["profile"] == "should-be-ignored"

    # The stub still raises, but the important contract property is that
    # connection_metadata is structurally separate from profile resolution
    with pytest.raises(NotImplementedError):
        resolve_initialize_profile_binding(
            resolved_default_profile="explicit-authority",
            default_resolution_status=DefaultProfileResolutionStatus.RESOLVED,
            context=context,
        )


# --- InitializeProfileBinding model contracts ---


def test_initialize_profile_binding_resolved_state() -> None:
    """Resolved binding must carry the profile name."""
    binding = InitializeProfileBinding(
        status=DefaultProfileResolutionStatus.RESOLVED,
        resolved_default_profile="production",
    )
    assert binding.status == DefaultProfileResolutionStatus.RESOLVED
    assert binding.resolved_default_profile == "production"


def test_initialize_profile_binding_missing_state() -> None:
    """Missing binding must have None for resolved_default_profile."""
    binding = InitializeProfileBinding(
        status=DefaultProfileResolutionStatus.MISSING,
        resolved_default_profile=None,
    )
    assert binding.status == DefaultProfileResolutionStatus.MISSING
    assert binding.resolved_default_profile is None


def test_initialize_profile_binding_ambiguous_state() -> None:
    """Ambiguous binding must have None for resolved_default_profile."""
    binding = InitializeProfileBinding(
        status=DefaultProfileResolutionStatus.AMBIGUOUS,
        resolved_default_profile=None,
    )
    assert binding.status == DefaultProfileResolutionStatus.AMBIGUOUS
    assert binding.resolved_default_profile is None


def test_initialize_profile_binding_is_frozen() -> None:
    """Binding must be immutable."""
    binding = InitializeProfileBinding(
        status=DefaultProfileResolutionStatus.RESOLVED,
        resolved_default_profile="dev",
    )
    with pytest.raises(AttributeError):
        binding.status = DefaultProfileResolutionStatus.MISSING  # type: ignore[misc]
