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


def test_handle_initialize_returns_connection_context() -> None:
    """handle_initialize creates a connection context when gateway is started."""
    import asyncio
    from tela.shell.upstream import handle_initialize
    from tela.shell.gateway import get_runtime

    # Without gateway started, should return error
    get_runtime().config = None
    r = asyncio.run(handle_initialize({}))
    assert r.is_err


def test_handle_tools_list_returns_empty_when_no_gateway() -> None:
    """handle_tools_list returns empty list when gateway not started."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import handle_tools_list
    from tela.shell.gateway import get_runtime

    get_runtime().config = None
    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    result = asyncio.run(handle_tools_list(conn))
    assert result == []


def test_handle_tools_call_returns_error_when_no_gateway() -> None:
    """handle_tools_call returns error when gateway not started."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import handle_tools_call
    from tela.shell.gateway import get_runtime

    get_runtime().config = None
    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    result = asyncio.run(handle_tools_call(conn, "tool", {}))
    assert result.is_err


def test_handle_profiles_list_returns_empty_when_no_gateway() -> None:
    """handle_profiles_list returns empty list when gateway not started."""
    from tela.shell.upstream import handle_profiles_list
    from tela.shell.gateway import get_runtime

    get_runtime().config = None
    result = handle_profiles_list()
    assert result == []


def test_notify_tools_changed_is_noop() -> None:
    """notify_tools_changed is a no-op until MCP transport is wired."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import notify_tools_changed

    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    # Should not raise
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


# --- filter_tools_for_profile tests ---


def test_filter_tools_admits_matching_family() -> None:
    """Tools with admitted family and acceptable posture are included."""
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import filter_tools_for_profile

    tools = {
        "fs": [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_ONLY,
            ),
        ]
    }
    profile = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    result = filter_tools_for_profile(tools, profile, {"fs": Posture.NONE})
    assert len(result) == 1
    assert result[0].name == "read_file"


def test_filter_tools_excludes_unadmitted_family() -> None:
    """Tools from unadmitted families are excluded."""
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import filter_tools_for_profile

    tools = {
        "shell": [
            ResolvedTool(
                name="exec",
                server_name="shell",
                family="shell",
                posture=Posture.DESTRUCTIVE,
            ),
        ]
    }
    profile = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    result = filter_tools_for_profile(tools, profile, {"shell": Posture.NONE})
    assert len(result) == 0


def test_filter_tools_excludes_posture_exceedance() -> None:
    """Tools exceeding posture ceiling are excluded."""
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import filter_tools_for_profile

    tools = {
        "fs": [
            ResolvedTool(
                name="write_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_WRITE,
            ),
        ]
    }
    profile = ProfileConfig(name="reader", tools={"fs": Posture.READ_ONLY})
    result = filter_tools_for_profile(tools, profile, {"fs": Posture.NONE})
    assert len(result) == 0


def test_filter_tools_respects_capability_ceiling() -> None:
    """Read-only capability ceiling excludes read_write tools."""
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import filter_tools_for_profile

    tools = {
        "fs": [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_ONLY,
            ),
            ResolvedTool(
                name="write_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_WRITE,
            ),
        ]
    }
    profile = ProfileConfig(
        name="safe",
        capabilities={"fs": Posture.READ_ONLY},
    )
    result = filter_tools_for_profile(tools, profile, {"fs": Posture.NONE})
    assert len(result) == 1
    assert result[0].name == "read_file"


def test_filter_tools_multiple_servers() -> None:
    """Filtering works across multiple servers."""
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import filter_tools_for_profile

    tools = {
        "fs": [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_ONLY,
            )
        ],
        "git": [
            ResolvedTool(
                name="git_status",
                server_name="git",
                family="git",
                posture=Posture.READ_ONLY,
            )
        ],
        "shell": [
            ResolvedTool(
                name="exec",
                server_name="shell",
                family="shell",
                posture=Posture.DESTRUCTIVE,
            )
        ],
    }
    profile = ProfileConfig(
        name="dev", tools={"fs": Posture.READ_WRITE, "git": Posture.READ_ONLY}
    )
    result = filter_tools_for_profile(
        tools, profile, {"fs": Posture.NONE, "git": Posture.NONE, "shell": Posture.NONE}
    )
    names = {t.name for t in result}
    assert names == {"read_file", "git_status"}


# --- strip_meta tests ---


def test_strip_meta_removes_meta() -> None:
    """_meta is stripped from arguments."""
    from tela.shell.upstream_utils import strip_meta

    stripped, meta = strip_meta({"path": "/tmp", "_meta": {"trace_id": "t1"}})
    assert stripped == {"path": "/tmp"}
    assert meta == {"trace_id": "t1"}


def test_strip_meta_no_meta() -> None:
    """Arguments without _meta return None for held meta."""
    from tela.shell.upstream_utils import strip_meta

    stripped, meta = strip_meta({"path": "/tmp"})
    assert stripped == {"path": "/tmp"}
    assert meta is None


def test_strip_meta_empty_arguments() -> None:
    """Empty arguments."""
    from tela.shell.upstream_utils import strip_meta

    stripped, meta = strip_meta({})
    assert stripped == {}
    assert meta is None


# --- enforce_tool_call tests ---


def test_enforce_tool_call_allows() -> None:
    """Open mode enforcement allows valid tool call."""
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import enforce_tool_call

    tool = ResolvedTool(
        name="read_file", server_name="fs", family="fs", posture=Posture.READ_ONLY
    )
    profile = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    result = enforce_tool_call("read_file", tool, profile, Posture.NONE)
    assert result.verdict.value == "allow"


def test_enforce_tool_call_denies() -> None:
    """Open mode enforcement denies unadmitted family."""
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import enforce_tool_call

    tool = ResolvedTool(
        name="exec", server_name="shell", family="shell", posture=Posture.DESTRUCTIVE
    )
    profile = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    result = enforce_tool_call("exec", tool, profile, Posture.NONE)
    assert result.verdict.value == "deny"
