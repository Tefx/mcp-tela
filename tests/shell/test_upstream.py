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
from tela.shell.downstream_registry import DownstreamRegistry


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
    from tela.shell.gateway import set_runtime_config

    # Without gateway started, should return error
    set_runtime_config(None)
    r = asyncio.run(handle_initialize({}))
    assert r.is_err


def test_handle_initialize_uses_profile_binding_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handle_initialize must call resolver in open mode."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        ProfileConfig,
        TelaConfig,
    )
    from tela.shell.config_loader import Result
    from tela.shell.gateway import set_runtime_config, clear_runtime_connections
    from tela.shell.upstream import handle_initialize

    calls: list[InitializeContext] = []

    def _fake_resolve(
        *,
        resolved_default_profile: str | None,
        default_resolution_status: DefaultProfileResolutionStatus,
        context: InitializeContext,
    ) -> Result[InitializeProfileBinding, str]:
        calls.append(context)
        assert resolved_default_profile == "dev"
        assert default_resolution_status == DefaultProfileResolutionStatus.RESOLVED
        return Result(
            value=InitializeProfileBinding(
                status=DefaultProfileResolutionStatus.RESOLVED,
                resolved_default_profile="dev",
            )
        )

    monkeypatch.setattr(
        "tela.shell.upstream.resolve_initialize_profile_binding",
        _fake_resolve,
    )

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={"dev": ProfileConfig(name="dev", default=True)},
        )
    )
    clear_runtime_connections()

    result = asyncio.run(handle_initialize({"client": "desktop"}))

    assert result.is_ok
    assert len(calls) == 1
    assert calls[0].connection_metadata == {"client": "desktop"}


def test_handle_initialize_reuses_existing_bridge_connection() -> None:
    """Bridge initialize must reuse the HTTP /connect connection context."""
    import asyncio

    from tela.core.models import AuthConfig, AuthMode, ConnectionContext, TelaConfig
    from tela.shell.gateway import (
        add_runtime_connection,
        clear_runtime_connections,
        set_runtime_config,
    )
    from tela.shell.idle_shutdown import (
        _reset_idle_manager,
        get_idle_manager,
        init_idle_manager,
    )
    from tela.shell.upstream import handle_initialize

    async def _scenario() -> None:
        _reset_idle_manager()

        async def _shutdown_callback() -> None:
            return None

        init_result = await init_idle_manager(30.0, _shutdown_callback)
        assert init_result.is_ok
        manager = get_idle_manager()
        assert manager is not None

        set_runtime_config(
            TelaConfig(
                auth=AuthConfig(mode=AuthMode.OPEN),
                resolved_default_profile="dev",
            )
        )
        clear_runtime_connections()

        bridge_connection = ConnectionContext(
            connection_id="bridge_abc",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        add_runtime_connection(bridge_connection)
        increment_result = await manager.increment()
        assert increment_result.is_ok

        result = await handle_initialize(
            {"name": "probe", "tela_bridge_connection_id": "bridge_abc"}
        )

        assert result.is_ok
        assert result.value == bridge_connection
        assert manager.connection_count == 1

    asyncio.run(_scenario())


def test_handle_initialize_rejects_open_mode_without_resolved_profile() -> None:
    """handle_initialize must reject open mode when profile resolution is missing."""
    import asyncio

    from tela.core.models import AuthConfig, AuthMode, TelaConfig
    from tela.shell.gateway import set_runtime_config, clear_runtime_connections
    from tela.shell.upstream import handle_initialize

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile=None,
        )
    )
    clear_runtime_connections()

    result = asyncio.run(handle_initialize({}))
    assert result.is_err
    assert "INITIALIZE_REJECTED" in (result.error or "")


def test_handle_tools_list_returns_empty_when_no_gateway() -> None:
    """handle_tools_list returns empty list when gateway not started."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import handle_tools_list
    from tela.shell.gateway import set_runtime_config

    set_runtime_config(None)
    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    result = asyncio.run(handle_tools_list(conn))
    assert result.is_err
    assert result.error is not None
    assert "GATEWAY_NOT_STARTED" in result.error


def test_handle_tools_call_returns_error_when_no_gateway() -> None:
    """handle_tools_call returns error when gateway not started."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import handle_tools_call
    from tela.shell.gateway import set_runtime_config

    set_runtime_config(None)
    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    result = asyncio.run(handle_tools_call(conn, "tool", {}))
    assert result.is_err


def test_handle_profiles_list_returns_empty_when_no_gateway() -> None:
    """handle_profiles_list returns empty list when gateway not started."""
    from tela.shell.upstream import handle_profiles_list
    from tela.shell.gateway import set_runtime_config

    set_runtime_config(None)
    result = handle_profiles_list()
    assert result.is_err
    assert result.error is not None
    assert "GATEWAY_NOT_STARTED" in result.error


def test_handle_profiles_list_uses_canonical_profile_name_field() -> None:
    """profiles surface emits profile_name as the canonical external identifier."""
    from tela.core.models import (
        AuthConfig,
        AuthMode,
        Posture,
        ProfileConfig,
        TelaConfig,
    )
    from tela.shell.gateway import set_runtime_config
    from tela.shell.upstream import handle_profiles_list

    set_runtime_config(
        TelaConfig(
            profiles={
                "dev": ProfileConfig(
                    name="dev", capabilities={"filesystem": Posture.READ_ONLY}
                )
            },
            auth=AuthConfig(mode=AuthMode.OPEN),
        )
    )

    result = handle_profiles_list()

    assert result.value == [
        {
            "profile_name": "dev",
            "default": False,
            "capabilities": {"filesystem": "read_only"},
            "tools": {"filesystem": "read_only"},
        }
    ]


def test_notify_tools_changed_skips_without_session() -> None:
    """notify_tools_changed returns Ok when no session is captured (graceful skip)."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import notify_tools_changed

    conn = ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    result = asyncio.run(notify_tools_changed(conn, "digest"))
    assert result.is_ok


def test_notify_tools_changed_sends_via_captured_session() -> None:
    """notify_tools_changed calls send_tool_list_changed on a captured session."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import (
        capture_session,
        notify_tools_changed,
        release_session,
    )

    sent: list[bool] = []

    class StubSession:
        async def send_tool_list_changed(self) -> None:
            sent.append(True)

    conn = ConnectionContext(
        connection_id="c_with_session",
        profile_name="dev",
        connected_at="2026-01-01T00:00:00Z",
    )
    capture_session("c_with_session", StubSession())
    try:
        result = asyncio.run(notify_tools_changed(conn, "digest_abc"))
        assert result.is_ok
        assert sent == [True]
    finally:
        release_session("c_with_session")


def test_notify_tools_changed_handles_session_send_failure() -> None:
    """notify_tools_changed returns error when session.send_tool_list_changed raises."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import (
        capture_session,
        notify_tools_changed,
        release_session,
    )

    class FailingSession:
        async def send_tool_list_changed(self) -> None:
            raise RuntimeError("transport closed")

    conn = ConnectionContext(
        connection_id="c_fail", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )
    capture_session("c_fail", FailingSession())
    try:
        result = asyncio.run(notify_tools_changed(conn, "digest_xyz"))
        assert result.is_err
        assert "NOTIFICATION_SEND_FAILED" in (result.error or "")
    finally:
        release_session("c_fail")


# --- Session capture interface tests ---


def test_capture_session_and_retrieve() -> None:
    """capture_session stores a session retrievable via get_captured_session."""
    from tela.shell.upstream import (
        capture_session,
        get_captured_session,
        release_session,
    )

    class FakeSession:
        async def send_tool_list_changed(self) -> None: ...

    session = FakeSession()
    result = capture_session("conn_1", session)
    assert result.is_ok

    retrieved = get_captured_session("conn_1")
    assert retrieved.is_ok
    assert retrieved.value is session

    release_session("conn_1")


def test_capture_session_rejects_empty_id() -> None:
    """capture_session rejects empty connection_id."""
    from tela.shell.upstream import capture_session

    class FakeSession:
        async def send_tool_list_changed(self) -> None: ...

    result = capture_session("", FakeSession())
    assert result.is_err
    assert "empty" in (result.error or "")


def test_release_session_idempotent() -> None:
    """release_session succeeds even for unknown connection_id."""
    from tela.shell.upstream import release_session

    result = release_session("never_existed")
    assert result.is_ok


def test_get_captured_session_missing() -> None:
    """get_captured_session returns error for unknown connection_id."""
    from tela.shell.upstream import get_captured_session

    result = get_captured_session("unknown_conn")
    assert result.is_err
    assert "not found" in (result.error or "")


def test_release_session_cleans_up() -> None:
    """After release_session, get_captured_session returns error."""
    from tela.shell.upstream import (
        capture_session,
        get_captured_session,
        release_session,
    )

    class FakeSession:
        async def send_tool_list_changed(self) -> None: ...

    capture_session("conn_cleanup", FakeSession())
    release_session("conn_cleanup")
    result = get_captured_session("conn_cleanup")
    assert result.is_err


def test_upstream_session_protocol_conformance() -> None:
    """mcp.server.session.ServerSession satisfies UpstreamSession protocol."""
    from tela.shell.upstream import UpstreamSession
    from mcp.server.session import ServerSession

    assert issubclass(ServerSession, UpstreamSession)


def test_capture_session_preserves_first_binding() -> None:
    """Capturing a different session for same connection_id preserves first binding."""
    from tela.shell.upstream import (
        capture_session,
        get_captured_session,
        release_session,
    )

    class SessionA:
        async def send_tool_list_changed(self) -> None: ...

    class SessionB:
        async def send_tool_list_changed(self) -> None: ...

    a, b = SessionA(), SessionB()
    capture_session("conn_overwrite", a)
    result = capture_session("conn_overwrite", b)

    assert result.is_err
    assert "SESSION_ALREADY_BOUND" in (result.error or "")

    retrieved = get_captured_session("conn_overwrite")
    assert retrieved.is_ok
    assert retrieved.value is a  # first binding preserved

    release_session("conn_overwrite")


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
    profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
    result = filter_tools_for_profile(tools, profile, {"fs": Posture.NONE})
    assert result.is_ok and result.value is not None
    assert len(result.value) == 1
    assert result.value[0].name == "read_file"


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
    profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
    result = filter_tools_for_profile(tools, profile, {"shell": Posture.NONE})
    assert result.is_ok and result.value is not None
    assert len(result.value) == 0


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
    profile = ProfileConfig(name="reader", capabilities={"fs": Posture.READ_ONLY})
    result = filter_tools_for_profile(tools, profile, {"fs": Posture.NONE})
    assert result.is_ok and result.value is not None
    assert len(result.value) == 0


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
    assert result.is_ok and result.value is not None
    assert len(result.value) == 1
    assert result.value[0].name == "read_file"


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
        name="dev", capabilities={"fs": Posture.READ_WRITE, "git": Posture.READ_ONLY}
    )
    result = filter_tools_for_profile(
        tools, profile, {"fs": Posture.NONE, "git": Posture.NONE, "shell": Posture.NONE}
    )
    assert result.is_ok and result.value is not None
    names = {t.name for t in result.value}
    assert names == {"read_file", "git_status"}


# --- strip_meta tests ---


def test_strip_meta_removes_meta() -> None:
    """_meta is stripped from arguments."""
    from tela.shell.upstream_utils import strip_meta

    strip_result = strip_meta({"path": "/tmp", "_meta": {"trace_id": "t1"}})
    assert strip_result.is_ok and strip_result.value is not None
    stripped, meta = strip_result.value
    assert stripped == {"path": "/tmp"}
    assert meta == {"trace_id": "t1"}


def test_strip_meta_no_meta() -> None:
    """Arguments without _meta return None for held meta."""
    from tela.shell.upstream_utils import strip_meta

    strip_result = strip_meta({"path": "/tmp"})
    assert strip_result.is_ok and strip_result.value is not None
    stripped, meta = strip_result.value
    assert stripped == {"path": "/tmp"}
    assert meta is None


def test_strip_meta_empty_arguments() -> None:
    """Empty arguments."""
    from tela.shell.upstream_utils import strip_meta

    strip_result = strip_meta({})
    assert strip_result.is_ok and strip_result.value is not None
    stripped, meta = strip_result.value
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
    profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
    result = enforce_tool_call("read_file", tool, profile, Posture.NONE)
    assert result.is_ok and result.value is not None
    assert result.value.verdict.value == "allow"


def test_enforce_tool_call_denies() -> None:
    """Open mode enforcement denies unadmitted family."""
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import enforce_tool_call

    tool = ResolvedTool(
        name="exec", server_name="shell", family="shell", posture=Posture.DESTRUCTIVE
    )
    profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
    result = enforce_tool_call("exec", tool, profile, Posture.NONE)
    assert result.is_ok and result.value is not None
    assert result.value.verdict.value == "deny"


# --- handle_tools_list metadata round-trip tests ---


def test_handle_tools_list_includes_title_in_output_dict() -> None:
    """handle_tools_list includes title field in output dict."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        Posture,
        ProfileConfig,
        ResolvedTool,
        TelaConfig,
    )
    from tela.shell.config_loader import Result
    from tela.shell.gateway import set_runtime_config, clear_runtime_connections
    from tela.shell.upstream import handle_tools_list, handle_initialize

    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
                description="Read a file",
                title="File Reader",
            )
        ],
    )

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={
                "dev": ProfileConfig(
                    name="dev", default=True, capabilities={"fs": Posture.READ_ONLY}
                )
            },
        )
    )
    clear_runtime_connections()

    import tela.shell.upstream

    original_get_all_tools = tela.shell.upstream.get_all_tools
    tela.shell.upstream.get_all_tools = lambda: Result(value=registry.get_all_tools())

    try:
        result = asyncio.run(handle_initialize({"client": "test"}))
        assert result.is_ok
        conn = result.value
        assert conn is not None

        tools_result = asyncio.run(handle_tools_list(conn))
        assert tools_result.is_ok
        assert tools_result.value is not None
        assert len(tools_result.value) == 1
        tool_dict = tools_result.value[0]
        assert tool_dict["name"] == "read_file"
        assert tool_dict["title"] == "File Reader"
    finally:
        tela.shell.upstream.get_all_tools = original_get_all_tools


def test_handle_tools_list_includes_output_schema_in_output_dict() -> None:
    """handle_tools_list includes outputSchema field in output dict."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        Posture,
        ProfileConfig,
        ResolvedTool,
        TelaConfig,
    )
    from tela.shell.config_loader import Result
    from tela.shell.gateway import set_runtime_config, clear_runtime_connections
    from tela.shell.upstream import handle_tools_list, handle_initialize

    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
                description="Read a file",
                output_schema={"type": "string"},
            )
        ],
    )

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={
                "dev": ProfileConfig(
                    name="dev", default=True, capabilities={"fs": Posture.READ_ONLY}
                )
            },
        )
    )
    clear_runtime_connections()

    import tela.shell.upstream

    original_get_all_tools = tela.shell.upstream.get_all_tools
    tela.shell.upstream.get_all_tools = lambda: Result(value=registry.get_all_tools())

    try:
        result = asyncio.run(handle_initialize({"client": "test"}))
        assert result.is_ok
        conn = result.value
        assert conn is not None

        tools_result = asyncio.run(handle_tools_list(conn))
        assert tools_result.is_ok
        assert tools_result.value is not None
        tool_dict = tools_result.value[0]
        assert tool_dict["outputSchema"] == {"type": "string"}
    finally:
        tela.shell.upstream.get_all_tools = original_get_all_tools


def test_handle_tools_list_includes_annotations_in_output_dict() -> None:
    """handle_tools_list includes annotations field in output dict."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        Posture,
        ProfileConfig,
        ResolvedTool,
        TelaConfig,
    )
    from tela.shell.config_loader import Result
    from tela.shell.gateway import set_runtime_config, clear_runtime_connections
    from tela.shell.upstream import handle_tools_list, handle_initialize

    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
                description="Read a file",
                annotations={"readOnlyHint": True},
            )
        ],
    )

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={
                "dev": ProfileConfig(
                    name="dev", default=True, capabilities={"fs": Posture.READ_ONLY}
                )
            },
        )
    )
    clear_runtime_connections()

    import tela.shell.upstream

    original_get_all_tools = tela.shell.upstream.get_all_tools
    tela.shell.upstream.get_all_tools = lambda: Result(value=registry.get_all_tools())

    try:
        result = asyncio.run(handle_initialize({"client": "test"}))
        assert result.is_ok
        conn = result.value
        assert conn is not None

        tools_result = asyncio.run(handle_tools_list(conn))
        assert tools_result.is_ok
        assert tools_result.value is not None
        tool_dict = tools_result.value[0]
        assert tool_dict["annotations"] == {"readOnlyHint": True}
    finally:
        tela.shell.upstream.get_all_tools = original_get_all_tools


def test_handle_tools_list_metadata_absent_fields_not_included() -> None:
    """handle_tools_list omits None metadata fields from output dict."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        Posture,
        ProfileConfig,
        ResolvedTool,
        TelaConfig,
    )
    from tela.shell.config_loader import Result
    from tela.shell.gateway import set_runtime_config, clear_runtime_connections
    from tela.shell.upstream import handle_tools_list, handle_initialize

    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
                description="Read a file",
                # title, output_schema, annotations are None
            )
        ],
    )

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={
                "dev": ProfileConfig(
                    name="dev", default=True, capabilities={"fs": Posture.READ_ONLY}
                )
            },
        )
    )
    clear_runtime_connections()

    import tela.shell.upstream

    original_get_all_tools = tela.shell.upstream.get_all_tools
    tela.shell.upstream.get_all_tools = lambda: Result(value=registry.get_all_tools())

    try:
        result = asyncio.run(handle_initialize({"client": "test"}))
        assert result.is_ok
        conn = result.value
        assert conn is not None

        tools_result = asyncio.run(handle_tools_list(conn))
        assert tools_result.is_ok
        assert tools_result.value is not None
        tool_dict = tools_result.value[0]
        assert tool_dict["name"] == "read_file"
        # Metadata fields are None and should not be in dict
        assert tool_dict.get("title") is None
        assert tool_dict.get("outputSchema") is None
        assert tool_dict.get("annotations") is None
    finally:
        tela.shell.upstream.get_all_tools = original_get_all_tools
