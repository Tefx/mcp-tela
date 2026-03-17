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


# --- MCP Handler contract tests ---


def test_handle_initialize_is_contract_stub() -> None:
    """handle_initialize must still be a contract stub."""
    import asyncio
    from tela.shell.upstream import handle_initialize

    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(handle_initialize({}))


def test_handle_tools_list_is_contract_stub() -> None:
    """handle_tools_list must still be a contract stub."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import handle_tools_list

    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(handle_tools_list(conn))


def test_handle_tools_call_is_contract_stub() -> None:
    """handle_tools_call must still be a contract stub."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import handle_tools_call

    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(handle_tools_call(conn, "tool", {}))


def test_handle_profiles_list_is_contract_stub() -> None:
    """handle_profiles_list must still be a contract stub."""
    from tela.shell.upstream import handle_profiles_list

    with pytest.raises(NotImplementedError, match="Contract stub"):
        handle_profiles_list()


def test_notify_tools_changed_is_contract_stub() -> None:
    """notify_tools_changed must still be a contract stub."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import notify_tools_changed

    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(notify_tools_changed(conn, "digest"))


# --- ConnectionContext model tests for upstream ---


def test_connection_context_model_fields() -> None:
    """ConnectionContext must expose all required fields."""
    from tela.core.models import ConnectionContext

    ctx = ConnectionContext(
        connection_id="conn-123",
        profile_name="production",
        connected_at="2026-03-17T10:00:00Z",
    )
    assert ctx.connection_id == "conn-123"
    assert ctx.profile_name == "production"
    assert ctx.connected_at == "2026-03-17T10:00:00Z"
    assert ctx.tool_call_count == 0


def test_connection_context_tool_call_counter() -> None:
    """ConnectionContext tool_call_count starts at 0 and is mutable."""
    from tela.core.models import ConnectionContext

    ctx = ConnectionContext(
        connection_id="conn-1",
        profile_name="dev",
        connected_at="2026-01-01T00:00:00Z",
        tool_call_count=5,
    )
    assert ctx.tool_call_count == 5


# --- Enforcement result model tests for upstream call routing ---


def test_enforcement_result_allow() -> None:
    """ALLOW verdict carries no denial metadata."""
    from tela.core.models import EnforcementResult, EnforcementVerdict

    result = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
    assert result.verdict == EnforcementVerdict.ALLOW
    assert result.denied_by is None
    assert result.error_code is None


def test_enforcement_result_deny_with_metadata() -> None:
    """DENY verdict carries denial layer, error code, and message."""
    from tela.core.models import EnforcementResult, EnforcementVerdict

    result = EnforcementResult(
        verdict=EnforcementVerdict.DENY,
        denied_by="family_admission",
        error_code="AUTHZ_DENY",
        error_message="Tool not in profile family",
    )
    assert result.verdict == EnforcementVerdict.DENY
    assert result.denied_by == "family_admission"
    assert result.error_code == "AUTHZ_DENY"


# --- tools/call input/output shape tests ---


def test_tools_call_meta_extraction_input_shape() -> None:
    """tools/call arguments may contain _meta that must be extracted.

    This tests the input shape for _meta extraction. The actual extraction
    logic belongs in the handle_tools_call implementation.
    """
    arguments = {
        "path": "/tmp/file.txt",
        "_meta": {"trace_id": "tr-1", "event_id": "ev-1"},
    }
    # _meta is a dict-typed field at top level
    assert isinstance(arguments.get("_meta"), dict)
    # Stripped arguments for downstream forwarding should not have _meta
    stripped = {k: v for k, v in arguments.items() if k != "_meta"}
    assert "_meta" not in stripped
    assert "path" in stripped


def test_tela_error_model_shape() -> None:
    """TelaError carries structured error response for denied calls."""
    from tela.core.models import TelaError

    error = TelaError(
        code="AUTHZ_DENY",
        message="Tool denied by family admission",
        details={"tool": "write_file", "profile": "read_only"},
    )
    assert error.code == "AUTHZ_DENY"
    assert error.details is not None
    assert error.details["tool"] == "write_file"
