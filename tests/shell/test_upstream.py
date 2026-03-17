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


# --- Existing contract tests (updated for implementation) ---


def test_initialize_context_exposes_connection_metadata_contract() -> None:
    context = InitializeContext(connection_metadata={"client": "desktop"})
    assert context.connection_metadata["client"] == "desktop"


def test_resolve_initialize_profile_binding_succeeds_for_resolved() -> None:
    """Resolved profile must produce a successful binding."""
    result = resolve_initialize_profile_binding(
        resolved_default_profile="production",
        default_resolution_status=DefaultProfileResolutionStatus.RESOLVED,
        context=InitializeContext(connection_metadata={}),
    )
    assert result.is_ok
    assert result.value is not None
    assert result.value.status == DefaultProfileResolutionStatus.RESOLVED
    assert result.value.resolved_default_profile == "production"


def test_resolve_initialize_profile_binding_rejects_missing() -> None:
    """Missing default-profile must reject initialize with error."""
    result = resolve_initialize_profile_binding(
        resolved_default_profile=None,
        default_resolution_status=DefaultProfileResolutionStatus.MISSING,
        context=InitializeContext(connection_metadata={"profile": "dev"}),
    )
    assert result.is_err
    assert "INITIALIZE_REJECTED" in (result.error or "")


# --- Initialize success cases ---


def test_resolve_binding_returns_binding_on_resolved_profile() -> None:
    """Resolved profile must return InitializeProfileBinding."""
    result = resolve_initialize_profile_binding(
        resolved_default_profile="production",
        default_resolution_status=DefaultProfileResolutionStatus.RESOLVED,
        context=InitializeContext(connection_metadata={}),
    )
    assert result.is_ok
    assert result.value is not None
    assert result.value.resolved_default_profile == "production"
    assert result.value.status == DefaultProfileResolutionStatus.RESOLVED


# --- Initialize rejection cases ---


def test_resolve_binding_rejects_on_missing_default() -> None:
    """Missing default-profile resolution must reject initialize."""
    result = resolve_initialize_profile_binding(
        resolved_default_profile=None,
        default_resolution_status=DefaultProfileResolutionStatus.MISSING,
        context=InitializeContext(connection_metadata={}),
    )
    assert result.is_err
    assert "INITIALIZE_REJECTED" in (result.error or "")
    assert "no default profile" in (result.error or "")


def test_resolve_binding_rejects_on_ambiguous_default() -> None:
    """Ambiguous default-profile resolution must reject initialize."""
    result = resolve_initialize_profile_binding(
        resolved_default_profile=None,
        default_resolution_status=DefaultProfileResolutionStatus.AMBIGUOUS,
        context=InitializeContext(connection_metadata={}),
    )
    assert result.is_err
    assert "INITIALIZE_REJECTED" in (result.error or "")
    assert "ambiguous" in (result.error or "")


def test_resolve_binding_rejects_resolved_with_none_profile() -> None:
    """Resolved status with None profile is an inconsistent state: reject."""
    result = resolve_initialize_profile_binding(
        resolved_default_profile=None,
        default_resolution_status=DefaultProfileResolutionStatus.RESOLVED,
        context=InitializeContext(connection_metadata={}),
    )
    assert result.is_err
    assert "INITIALIZE_REJECTED" in (result.error or "")


# --- Client metadata isolation ---


def test_connection_metadata_does_not_select_profile() -> None:
    """Client-provided metadata with profile hint must not influence selection.

    The contract explicitly states: 'Client-provided connection metadata is
    explicitly not a profile selection channel in open mode.'
    Even if metadata contains a 'profile' key, the function resolves profile
    only through resolved_default_profile parameter.
    """
    context = InitializeContext(
        connection_metadata={"profile": "should-be-ignored", "x-tenant": "acme"}
    )
    assert context.connection_metadata["profile"] == "should-be-ignored"

    # Despite metadata containing a "profile" hint, the binding uses
    # the explicit resolved_default_profile parameter
    result = resolve_initialize_profile_binding(
        resolved_default_profile="explicit-authority",
        default_resolution_status=DefaultProfileResolutionStatus.RESOLVED,
        context=context,
    )
    assert result.is_ok
    assert result.value is not None
    assert result.value.resolved_default_profile == "explicit-authority"


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
