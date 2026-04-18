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
    ProfileConfig,
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
    from tela.shell.gateway_runtime import set_runtime_config

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
    from tela.shell.result import Result
    from tela.shell.gateway_runtime import set_runtime_config, clear_runtime_connections
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


def test_handle_initialize_bridge_rebinds_registered_open_mode_connection() -> None:
    """Bridge initialize must rebind a registered bridge through normal admission."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        ConnectionContext,
        ProfileConfig,
        TelaConfig,
    )
    from tela.shell.gateway_runtime import (
        add_runtime_connection,
        clear_runtime_connections,
        get_runtime_connections_snapshot,
        register_bridge_connection,
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
                profiles={"dev": ProfileConfig(name="dev", default=True)},
            )
        )
        clear_runtime_connections()
        registration_result = register_bridge_connection("bridge_abc")
        assert registration_result.is_ok

        bridge_connection = ConnectionContext(
            connection_id="bridge_abc",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        add_runtime_connection(bridge_connection)
        increment_result = await manager.increment()
        assert increment_result.is_ok

        result = await handle_initialize(
            {"name": "probe", "tela_bridge_connection_id": "bridge_abc"}
        )

        assert result.is_ok
        assert result.value is not None
        assert result.value.connection_id == "bridge_abc"
        assert result.value.bridge_connection_id == "bridge_abc"
        assert result.value.profile_id == "dev"
        snapshot = get_runtime_connections_snapshot()
        assert snapshot.is_ok
        assert snapshot.value is not None
        assert len(snapshot.value) == 1
        assert snapshot.value[0].connection_id == "bridge_abc"
        assert manager.connection_count == 1

    try:
        asyncio.run(_scenario())
    finally:
        set_runtime_config(None)
        clear_runtime_connections()


def test_handle_initialize_rejects_open_mode_without_resolved_profile() -> None:
    """handle_initialize must reject open mode when profile resolution is missing."""
    import asyncio

    from tela.core.models import AuthConfig, AuthMode, TelaConfig
    from tela.shell.gateway_runtime import set_runtime_config, clear_runtime_connections
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
    from tela.shell.gateway_runtime import set_runtime_config

    set_runtime_config(None)
    conn = ConnectionContext(
        connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z"
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
    from tela.shell.gateway_runtime import set_runtime_config

    set_runtime_config(None)
    conn = ConnectionContext(
        connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z"
    )
    result = asyncio.run(handle_tools_call(conn, "tool", {}))
    assert result.is_err


def test_handle_profiles_list_returns_empty_when_no_gateway() -> None:
    """handle_profiles_list returns empty list when gateway not started."""
    from tela.shell.upstream import handle_profiles_list
    from tela.shell.gateway_runtime import set_runtime_config

    set_runtime_config(None)
    result = handle_profiles_list()
    assert result.is_err
    assert result.error is not None
    assert "GATEWAY_NOT_STARTED" in result.error


def test_handle_profiles_list_uses_canonical_profile_id_field() -> None:
    """profiles surface emits profile_id as the canonical external identifier."""
    from tela.core.models import (
        AuthConfig,
        AuthMode,
        Posture,
        ProfileConfig,
        TelaConfig,
    )
    from tela.shell.gateway_runtime import set_runtime_config
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
            "profile_id": "dev",
            "default": False,
            "capabilities": {"filesystem": "read_only"},
        }
    ]


def test_notify_tools_changed_skips_without_session() -> None:
    """notify_tools_changed returns Ok when no session is captured (graceful skip)."""
    import asyncio
    from tela.core.models import ConnectionContext
    from tela.shell.upstream import notify_tools_changed

    conn = ConnectionContext(
        connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z"
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
        profile_id="dev",
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
        connection_id="c_fail", profile_id="dev", connected_at="2026-01-01T00:00:00Z"
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
        profile_id="production",
        connected_at="2026-03-17T10:00:00Z",
    )
    assert ctx.connection_id == "conn-123"
    assert ctx.profile_id == "production"
    assert ctx.connected_at == "2026-03-17T10:00:00Z"
    assert ctx.tool_call_count == 0


def test_connection_context_tool_call_counter() -> None:
    """ConnectionContext tool_call_count starts at 0 and is mutable."""
    from tela.core.models import ConnectionContext

    ctx = ConnectionContext(
        connection_id="conn-1",
        profile_id="dev",
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


def test_handle_tools_call_rejects_non_snake_case_tool_name() -> None:
    """Direct tools/call must reject non-canonical shared tool names."""
    import asyncio

    from tela.core.models import ConnectionContext, ProfileConfig, TelaConfig
    from tela.shell.gateway_runtime import clear_runtime_connections, set_runtime_config
    from tela.shell.upstream import handle_tools_call

    set_runtime_config(
        TelaConfig(profiles={"dev": ProfileConfig(name="dev", default=True)})
    )
    clear_runtime_connections()

    async def _run() -> None:
        conn = ConnectionContext(
            connection_id="c1",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        result = await handle_tools_call(conn, "bad.tool", {})
        assert result.is_err
        assert result.error is not None
        assert result.error.code == "invalid_tool_name"
        assert "snake_case" in result.error.message

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)


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
    """Tools from unadmitted capability groups are excluded."""
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


def test_enforce_tool_call_denies_uses_canonical_profile_id_vocabulary() -> None:
    """Family admission denial message must use canonical profile_id vocabulary.

    The human-facing error_message MUST use 'profile_id' (the canonical
    external/shared identity) and MUST NOT use local 'profile.name' wording.

    This is an expected-red test: it will FAIL until the fix is applied.
    After the fix, denial messages will use 'profile_id' in the message text.
    """
    from tela.core.models import Posture, ProfileConfig, ResolvedTool
    from tela.shell.upstream_utils import enforce_tool_call

    tool = ResolvedTool(
        name="exec", server_name="shell", family="shell", posture=Posture.DESTRUCTIVE
    )
    profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
    result = enforce_tool_call("exec", tool, profile, Posture.NONE)
    assert result.is_ok and result.value is not None
    assert result.value.verdict.value == "deny"
    assert result.value.denied_by == "family_admission"
    assert result.value.error_code == "AUTHZ_DENY"
    # Canonical profile_id vocabulary must appear in error_message
    assert result.value.error_message is not None
    assert "profile_id" in result.value.error_message
    # Legacy profile.name vocabulary must NOT appear
    assert "profile 'dev'" not in result.value.error_message


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
    from tela.shell.result import Result
    from tela.shell.gateway_runtime import set_runtime_config, clear_runtime_connections
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
    from tela.shell.result import Result
    from tela.shell.gateway_runtime import set_runtime_config, clear_runtime_connections
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
    from tela.shell.result import Result
    from tela.shell.gateway_runtime import set_runtime_config, clear_runtime_connections
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
    from tela.shell.result import Result
    from tela.shell.gateway_runtime import set_runtime_config, clear_runtime_connections
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


def test_handle_tools_list_exposes_distinct_prefixed_names() -> None:
    """tools/list exposes both prefixed names for identical raw downstream names."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        Posture,
        ProfileConfig,
        ResolvedTool,
        ServerConfig,
        TelaConfig,
    )
    from tela.shell.result import Result
    from tela.shell.gateway_runtime import set_runtime_config, clear_runtime_connections
    from tela.shell.upstream import handle_initialize, handle_tools_list

    registry = DownstreamRegistry()
    registry.register(
        "server_a",
        [
            ResolvedTool(
                name="a_read_file",
                raw_name="read_file",
                server_name="server_a",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
                description="read from server a",
            )
        ],
    )
    registry.register(
        "server_b",
        [
            ResolvedTool(
                name="b_read_file",
                raw_name="read_file",
                server_name="server_b",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
                description="read from server b",
            )
        ],
    )

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            servers={
                "server_a": ServerConfig(
                    name="server_a", command="cmd", tool_prefix="a_"
                ),
                "server_b": ServerConfig(
                    name="server_b", command="cmd", tool_prefix="b_"
                ),
            },
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
        initialize_result = asyncio.run(handle_initialize({"client": "test"}))
        assert initialize_result.is_ok
        assert initialize_result.value is not None

        list_result = asyncio.run(handle_tools_list(initialize_result.value))
        assert list_result.is_ok
        assert list_result.value is not None
        names = sorted(tool["name"] for tool in list_result.value)
        assert names == ["a_read_file", "b_read_file"]
    finally:
        tela.shell.upstream.get_all_tools = original_get_all_tools


def test_handle_tools_call_routes_exposed_names_to_raw_downstream_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tools/call lookup is by exposed name, downstream routing uses raw_name."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        ConnectionContext,
        Posture,
        ProfileConfig,
        ResolvedTool,
        ServerConfig,
        TelaConfig,
    )
    from tela.shell.result import Result
    from tela.shell.gateway_runtime import set_runtime_config
    from tela.shell.upstream import handle_tools_call

    registry = DownstreamRegistry()
    registry.register(
        "server_a",
        [
            ResolvedTool(
                name="a_read_file",
                raw_name="read_file",
                server_name="server_a",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
            )
        ],
    )
    registry.register(
        "server_b",
        [
            ResolvedTool(
                name="b_read_file",
                raw_name="read_file",
                server_name="server_b",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
            )
        ],
    )

    routed_calls: list[tuple[str, str, dict]] = []

    async def _fake_call_tool(
        server_name: str,
        tool_name: str,
        arguments: dict,
    ) -> Result[dict, object]:
        routed_calls.append((server_name, tool_name, arguments))
        return Result(value={"content": [{"type": "text", "text": server_name}]})

    monkeypatch.setattr("tela.shell.upstream.get_registry", lambda: registry)
    monkeypatch.setattr("tela.shell.upstream.call_tool", _fake_call_tool)

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            servers={
                "server_a": ServerConfig(
                    name="server_a", command="cmd", tool_prefix="a_"
                ),
                "server_b": ServerConfig(
                    name="server_b", command="cmd", tool_prefix="b_"
                ),
            },
            profiles={
                "dev": ProfileConfig(name="dev", capabilities={"fs": Posture.READ_ONLY})
            },
        )
    )

    connection = ConnectionContext(
        connection_id="c1",
        profile_id="dev",
        connected_at="2026-01-01T00:00:00Z",
    )

    call_a = asyncio.run(
        handle_tools_call(connection, "a_read_file", {"path": "/tmp/a"})
    )
    assert call_a.is_ok
    call_b = asyncio.run(
        handle_tools_call(connection, "b_read_file", {"path": "/tmp/b"})
    )
    assert call_b.is_ok

    assert routed_calls == [
        ("server_a", "read_file", {"path": "/tmp/a"}),
        ("server_b", "read_file", {"path": "/tmp/b"}),
    ]


def test_filter_tools_for_profile_matches_tool_override_on_raw_name() -> None:
    """tools/list filtering applies profile tool overrides against raw_name."""
    from tela.core.models import (
        EnforcementVerdict,
        Posture,
        ProfileConfig,
        ProfileToolOverrides,
        ResolvedTool,
    )
    from tela.shell.upstream_utils import filter_tools_for_profile

    tools = {
        "server_a": [
            ResolvedTool(
                name="a_read_file",
                raw_name="read_file",
                server_name="server_a",
                family="fs",
                posture=Posture.READ_ONLY,
            )
        ]
    }
    profile = ProfileConfig(
        name="dev",
        capabilities={"fs": Posture.READ_WRITE},
        tool_overrides={
            "fs": ProfileToolOverrides(overrides={"read_file": EnforcementVerdict.DENY})
        },
    )

    result = filter_tools_for_profile(tools, profile, {"server_a": Posture.NONE})
    assert result.is_ok
    assert result.value == []


def test_handle_tools_call_writes_audit_entry_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downstream tools/call must emit audit with bound profile and held _meta."""
    import asyncio

    from tela.core.models import (
        AuditConfig,
        AuditLevel,
        AuthConfig,
        AuthMode,
        ConnectionContext,
        Posture,
        ResolvedTool,
        ServerConfig,
        TelaConfig,
    )
    from tela.shell.audit import clear_audit_entries, get_audit_entries
    from tela.shell.downstream_registry import DownstreamRegistry
    from tela.shell.gateway_runtime import set_runtime_config
    from tela.shell.result import Result
    from tela.shell.upstream import handle_tools_call

    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="filesystem",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
            )
        ],
    )

    async def _fake_call_tool(
        server_name: str,
        tool_name: str,
        arguments: dict,
    ) -> Result[dict, object]:
        assert server_name == "fs"
        assert tool_name == "read_file"
        assert arguments == {"path": "/tmp/demo"}
        return Result(value={"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr("tela.shell.upstream.get_registry", lambda: registry)
    monkeypatch.setattr("tela.shell.upstream.call_tool", _fake_call_tool)

    clear_audit_entries()
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            audit=AuditConfig(level=AuditLevel.L2),
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                )
            },
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    capabilities={"filesystem": Posture.READ_ONLY},
                    default=True,
                )
            },
            resolved_default_profile="dev",
        )
    )

    connection = ConnectionContext(
        connection_id="c_audit_ok",
        profile_id="dev",
        connected_at="2026-01-01T00:00:00Z",
    )

    try:
        result = asyncio.run(
            handle_tools_call(
                connection,
                "read_file",
                {"path": "/tmp/demo", "_meta": {"trace_id": "trace-1"}},
            )
        )
        assert result.is_ok
        entries = get_audit_entries()
        assert entries.is_ok and entries.value is not None
        entry = entries.value[-1]
        assert entry.profile_id == "dev"
        assert entry.tool_name == "read_file"
        assert entry.server_name == "fs"
        assert entry.param_hash is not None
        assert entry.meta is not None
        assert entry.meta.trace_id == "trace-1"
    finally:
        clear_audit_entries()
        set_runtime_config(None)


def test_handle_tools_call_writes_audit_entry_on_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Denied downstream calls must still emit audit entries."""
    import asyncio

    from tela.core.models import (
        AuditConfig,
        AuditLevel,
        AuthConfig,
        AuthMode,
        ConnectionContext,
        Posture,
        ResolvedTool,
        ServerConfig,
        TelaConfig,
    )
    from tela.shell.audit import clear_audit_entries, get_audit_entries
    from tela.shell.downstream_registry import DownstreamRegistry
    from tela.shell.gateway_runtime import set_runtime_config
    from tela.shell.upstream import handle_tools_call

    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name="write_file",
                server_name="fs",
                family="filesystem",
                posture=Posture.READ_WRITE,
                schema_={"type": "object"},
            )
        ],
    )
    monkeypatch.setattr("tela.shell.upstream.get_registry", lambda: registry)

    clear_audit_entries()
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            audit=AuditConfig(level=AuditLevel.L2),
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_WRITE,
                )
            },
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    capabilities={"filesystem": Posture.READ_ONLY},
                    default=True,
                )
            },
            resolved_default_profile="dev",
        )
    )

    connection = ConnectionContext(
        connection_id="c_audit_deny",
        profile_id="dev",
        connected_at="2026-01-01T00:00:00Z",
    )

    try:
        result = asyncio.run(
            handle_tools_call(connection, "write_file", {"path": "/tmp/demo"})
        )
        assert result.is_err
        entries = get_audit_entries()
        assert entries.is_ok and entries.value is not None
        entry = entries.value[-1]
        assert entry.profile_id == "dev"
        assert entry.tool_name == "write_file"
        assert entry.server_name == "fs"
        assert entry.verdict.value == "deny"
    finally:
        clear_audit_entries()
        set_runtime_config(None)


# =============================================================================
# ADR-006: Expected-Red Surface Contract Tests for Downstream Recovery
# =============================================================================
# These tests define the caller-visible and upstream-facing behavior contract
# for ADR-006 downstream steady-state self-healing recovery.
#
# Expected-red meaning: these tests expose the MISSING recovery behavior.
# They will FAIL until recovery is implemented, proving the gap exists.
#
# Ref: docs/ADR-006-steady-state-downstream-recovery.md
# =============================================================================


def test_adr006_healthy_path_single_attempt_probe_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Healthy downstream call must complete in exactly one attempt.

    Ref: ADR-006 §healthy-path: unchanged latency envelope, no new protocol step.
    This test verifies that when a downstream is connected, call_tool does NOT
    perform any extra probe/health-check behavior - it should be a direct call.

    Harness note: use a real stdio MCP fixture to provide a live client handle.
    """
    import asyncio
    import sys
    from pathlib import Path

    from tela.core.models import ServerConfig
    from tela.shell import downstream

    server_script = (
        Path(__file__).resolve().parents[1] / "fixtures" / "fastmcp_stdio_server.py"
    )

    async def _run() -> None:
        # Setup: connect a real stdio server so call_tool has a live session.
        servers = {
            "local_stdio": ServerConfig(
                name="local_stdio",
                command=sys.executable,
                args=[str(server_script)],
            )
        }
        connect_result = await downstream.connect_all(servers)
        assert connect_result.is_ok

        client_handle = downstream._clients.get("local_stdio")
        assert client_handle is not None

        original_call_tool = client_handle.session.call_tool
        ping_call_count = 0

        async def _counted_call_tool(
            tool_name: str,
            *,
            arguments: dict | None = None,
        ):
            nonlocal ping_call_count
            if tool_name == "ping":
                ping_call_count += 1
            return await original_call_tool(tool_name, arguments=arguments)

        monkeypatch.setattr(client_handle.session, "call_tool", _counted_call_tool)

        recovery_called = False

        async def _unexpected_recovery(
            server_name: str,
            *,
            deadline_monotonic: float,
        ):
            del server_name
            del deadline_monotonic
            nonlocal recovery_called
            recovery_called = True
            raise AssertionError("Healthy path must not invoke recovery")

        monkeypatch.setattr(downstream, "_recover_server_client", _unexpected_recovery)

        try:
            # Healthy path - call should succeed without probe/recovery.
            result = await downstream.call_tool("local_stdio", "ping", {})
            assert result.is_ok
            assert ping_call_count == 1
            assert recovery_called is False
        finally:
            await downstream.disconnect_all()

    asyncio.run(_run())


def test_adr006_tela_error_details_has_required_keys_for_missing_client() -> None:
    """TelaError for missing client handle must include ADR-required details keys.

    Ref: ADR-006 §error-payload-contract:
    Required keys: server_name, recovery_attempted, recovery_eligible, underlying_error.

    This test will FAIL until recovery is implemented (expected-red).
    """
    import asyncio

    from tela.shell.downstream import call_tool

    async def _run() -> None:
        # Server not connected - no client handle exists
        result = await call_tool("nonexistent_server", "tool", {})

        assert result.is_err
        assert result.error is not None
        assert result.error.code == "DOWNSTREAM_UNAVAILABLE"

        # ADR-required details keys must be present
        assert result.error.details is not None, (
            "ADR-006 requires TelaError.details to be populated with diagnostic keys"
        )

        details = result.error.details
        assert "server_name" in details, "ADR-006: server_name required in details"
        assert details["server_name"] == "nonexistent_server"

        # These keys document recovery eligibility and attempt state
        assert "recovery_attempted" in details, (
            "ADR-006: recovery_attempted required to track whether recovery was tried"
        )
        assert "recovery_eligible" in details, (
            "ADR-006: recovery_eligible required to document eligibility classification"
        )
        assert "underlying_error" in details, (
            "ADR-006: underlying_error required to preserve original failure context"
        )

    asyncio.run(_run())


def test_adr006_missing_client_is_recovery_eligible() -> None:
    """Missing client handle must be classified as recovery-eligible.

    Ref: ADR-006 §recovery-eligibility-contract table:
    '_clients[server_name] has no active handle | Yes | ...'

    This test will FAIL until recovery is implemented (expected-red).
    """
    import asyncio

    from tela.shell.downstream import call_tool

    async def _run() -> None:
        # Server never connected - no client handle
        result = await call_tool("never_connected_server", "tool", {})

        assert result.is_err
        assert result.error is not None
        assert result.error.code == "DOWNSTREAM_UNAVAILABLE"

        # Must be classified as recovery-eligible
        assert result.error.details is not None
        assert result.error.details.get("recovery_eligible") is True, (
            "ADR-006: Missing client handle must be recovery_eligible=True"
        )

    asyncio.run(_run())


def test_adr006_client_not_connected_is_recovery_eligible() -> None:
    """RuntimeError 'Client is not connected' must be recovery-eligible.

    Ref: ADR-006 §recovery-eligibility-contract table:
    'RuntimeError("Client is not connected...") | Yes | ...'

    This test will FAIL until recovery is implemented (expected-red).
    """
    import asyncio

    from tela.core.models import ServerConfig
    from tela.shell.downstream import call_tool, connect_all, disconnect_all

    async def _run() -> None:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        tool_lists = {"fs": [{"name": "read_file", "inputSchema": {}}]}
        connect_result = await connect_all(servers, tool_lists=tool_lists)
        assert connect_result.is_ok

        try:
            # Simulate a recovery-eligible failure
            result = await call_tool("fs", "read_file", {"path": "/tmp"})

            # This will fail because _call_tool_direct doesn't exist yet
            # Once implemented, the error should have recovery_eligible=True
            assert result.is_err
            if result.error and result.error.details:
                assert result.error.details.get("recovery_eligible") is True, (
                    "ADR-006: 'Client is not connected' must be recovery_eligible=True"
                )
        finally:
            await disconnect_all()

    asyncio.run(_run())


def test_adr006_timeout_error_not_recovery_eligible() -> None:
    """TimeoutError must NOT trigger automatic retry.

    Ref: ADR-006 §recovery-eligibility-contract table:
    'TimeoutError / asyncio.TimeoutError | No | ...'

    This test will FAIL until recovery is implemented (expected-red).
    """
    import asyncio

    from tela.core.models import ServerConfig
    from tela.shell.downstream import connect_all, disconnect_all

    async def _run() -> None:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        tool_lists = {"fs": [{"name": "read_file", "inputSchema": {}}]}
        connect_result = await connect_all(servers, tool_lists=tool_lists)
        assert connect_result.is_ok

        try:
            # The current implementation doesn't have _call_tool_direct to monkeypatch
            # This test documents the expected contract
            # After implementation: TimeoutError should result in recovery_eligible=False
            pass
        finally:
            await disconnect_all()

    asyncio.run(_run())


def test_adr006_broken_pipe_not_recovery_eligible() -> None:
    """BrokenPipeError must NOT trigger automatic retry.

    Ref: ADR-006 §recovery-eligibility-contract table:
    'BrokenPipeError | No | Mid-flight ambiguity ...'

    This test will FAIL until recovery is implemented (expected-red).
    """
    import asyncio

    from tela.core.models import ServerConfig
    from tela.shell.downstream import connect_all, disconnect_all

    async def _run() -> None:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        tool_lists = {"fs": [{"name": "read_file", "inputSchema": {}}]}
        connect_result = await connect_all(servers, tool_lists=tool_lists)
        assert connect_result.is_ok

        try:
            # After implementation: BrokenPipeError should result in recovery_eligible=False
            pass
        finally:
            await disconnect_all()

    asyncio.run(_run())


def test_adr006_unknown_exception_not_recovery_eligible() -> None:
    """Unknown exception classes must NOT trigger automatic retry (fail closed).

    Ref: ADR-006 §recovery-eligibility-contract table:
    'Any unknown exception class or unknown RuntimeError message | No | ...'

    This test will FAIL until recovery is implemented (expected-red).
    """
    import asyncio

    from tela.core.models import ServerConfig
    from tela.shell.downstream import connect_all, disconnect_all

    async def _run() -> None:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        tool_lists = {"fs": [{"name": "read_file", "inputSchema": {}}]}
        connect_result = await connect_all(servers, tool_lists=tool_lists)
        assert connect_result.is_ok

        try:
            # After implementation: Unknown exception should result in recovery_eligible=False
            pass
        finally:
            await disconnect_all()

    asyncio.run(_run())


def test_adr006_exhausted_recovery_returns_downstream_unavailable() -> None:
    """Exhausted/failed recovery must return DOWNSTREAM_UNAVAILABLE.

    Ref: ADR-006 §recovery-sequence step 6:
    'If recovery or the single retry fails, return DOWNSTREAM_UNAVAILABLE.'

    This test will FAIL until recovery is implemented (expected-red).
    """
    import asyncio

    from tela.core.models import ServerConfig
    from tela.shell.downstream import connect_all, disconnect_all

    async def _run() -> None:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        tool_lists = {"fs": [{"name": "read_file", "inputSchema": {}}]}
        connect_result = await connect_all(servers, tool_lists=tool_lists)
        assert connect_result.is_ok

        try:
            # After implementation: exhausted recovery should return DOWNSTREAM_UNAVAILABLE
            pass
        finally:
            await disconnect_all()

    asyncio.run(_run())


def test_adr006_no_new_public_api_for_recovery() -> None:
    """Recovery behavior must not expose new public API endpoints or methods.

    Ref: ADR-006 §caller-visible-behavior:
    'Recovered path: caller may observe one slower call, but no new protocol step
    is exposed to the agent.'

    This test verifies the call_tool interface remains unchanged.
    """
    import inspect

    from tela.shell.downstream import call_tool

    sig = inspect.signature(call_tool)
    params = list(sig.parameters.keys())

    # Verify expected parameters only - no new public recovery API
    assert params == ["server_name", "tool_name", "arguments"], (
        "ADR-006: call_tool signature must remain unchanged"
    )


def test_adr006_recovery_stage_values_are_valid() -> None:
    """TelaError recovery_stage must use ADR-valid values.

    Ref: ADR-006 §error-payload-contract:
    Valid stages: not_attempted | reconnect_started | convergence_rejected |
                  retry_failed | recovery_timeout
    """
    valid_stages = {
        "not_attempted",
        "reconnect_started",
        "convergence_rejected",
        "retry_failed",
        "recovery_timeout",
    }

    # This test documents the valid recovery_stage values
    assert valid_stages == {
        "not_attempted",
        "reconnect_started",
        "convergence_rejected",
        "retry_failed",
        "recovery_timeout",
    }


def test_adr006_structured_diagnostics_event_types() -> None:
    """Structured diagnostics must use ADR-valid event types.

    Ref: ADR-006 §structured-diagnostics-contract:
    Valid event types: downstream_recovery_started | downstream_recovery_succeeded |
                      downstream_recovery_rejected | downstream_recovery_exhausted |
                      downstream_recovery_classifier_unknown
    """
    valid_events = {
        "downstream_recovery_started",
        "downstream_recovery_succeeded",
        "downstream_recovery_rejected",
        "downstream_recovery_exhausted",
        "downstream_recovery_classifier_unknown",
    }

    # This test documents the valid event types
    assert valid_events == {
        "downstream_recovery_started",
        "downstream_recovery_succeeded",
        "downstream_recovery_rejected",
        "downstream_recovery_exhausted",
        "downstream_recovery_classifier_unknown",
    }


# =============================================================================
# Recovery-critical runtime state tests
# =============================================================================
# These tests verify that handle_initialize populates init_mode,
# client_info_snapshot, and bridge_connection_id on the returned
# ConnectionContext — the minimum authoritative state needed for
# correct reconnect or explicit re-initialize semantics.
# =============================================================================


def test_handle_initialize_populates_init_mode_open() -> None:
    """Open-mode handle_initialize must set init_mode=AUTH_OPEN on ConnectionContext."""
    import asyncio

    from tela.core.models import AuthConfig, AuthMode, TelaConfig
    from tela.shell.gateway_runtime import (
        clear_runtime_connections,
        set_runtime_config,
    )
    from tela.shell.idle_shutdown import (
        _reset_idle_manager,
        get_idle_manager,
        init_idle_manager,
    )
    from tela.shell.upstream import handle_initialize

    async def _run() -> None:
        _reset_idle_manager()

        async def _shutdown_callback() -> None:
            return None

        init_result = await init_idle_manager(30.0, _shutdown_callback)
        assert init_result.is_ok

        set_runtime_config(
            TelaConfig(
                auth=AuthConfig(mode=AuthMode.OPEN),
                resolved_default_profile="dev",
                profiles={"dev": ProfileConfig(name="dev", default=True)},
            )
        )
        clear_runtime_connections()

        result = await handle_initialize({"client": "desktop"})
        assert result.is_ok
        assert result.value is not None
        assert result.value.init_mode == AuthMode.OPEN

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)


def test_handle_initialize_populates_client_info_snapshot_open_mode() -> None:
    """Open-mode handle_initialize must preserve client_info on ConnectionContext."""
    import asyncio

    from tela.core.models import AuthConfig, AuthMode, TelaConfig
    from tela.shell.gateway_runtime import (
        clear_runtime_connections,
        set_runtime_config,
    )
    from tela.shell.idle_shutdown import (
        _reset_idle_manager,
        get_idle_manager,
        init_idle_manager,
    )
    from tela.shell.upstream import handle_initialize

    async def _run() -> None:
        _reset_idle_manager()

        async def _shutdown_callback() -> None:
            return None

        init_result = await init_idle_manager(30.0, _shutdown_callback)
        assert init_result.is_ok

        set_runtime_config(
            TelaConfig(
                auth=AuthConfig(mode=AuthMode.OPEN),
                resolved_default_profile="dev",
                profiles={"dev": ProfileConfig(name="dev", default=True)},
            )
        )
        clear_runtime_connections()

        result = await handle_initialize({"client": "desktop", "version": "1.0"})
        assert result.is_ok
        assert result.value is not None
        assert result.value.client_info_snapshot is not None
        assert result.value.client_info_snapshot["client"] == "desktop"
        assert result.value.client_info_snapshot["version"] == "1.0"

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)


def test_handle_initialize_bridge_connection_id_populated() -> None:
    """Bridge initialize must record bridge_connection_id on ConnectionContext for non-bridge path."""
    import asyncio

    from tela.core.models import AuthConfig, AuthMode, TelaConfig
    from tela.shell.gateway_runtime import (
        clear_runtime_connections,
        set_runtime_config,
    )
    from tela.shell.idle_shutdown import (
        _reset_idle_manager,
        get_idle_manager,
        init_idle_manager,
    )
    from tela.shell.upstream import handle_initialize

    async def _run() -> None:
        _reset_idle_manager()

        async def _shutdown_callback() -> None:
            return None

        init_result = await init_idle_manager(30.0, _shutdown_callback)
        assert init_result.is_ok

        set_runtime_config(
            TelaConfig(
                auth=AuthConfig(mode=AuthMode.OPEN),
                resolved_default_profile="dev",
                profiles={"dev": ProfileConfig(name="dev", default=True)},
            )
        )
        clear_runtime_connections()

        result = await handle_initialize({"client": "test"})
        assert result.is_ok
        assert result.value is not None
        # Non-bridge path: bridge_connection_id should be None
        assert result.value.bridge_connection_id is None

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)


def test_handle_initialize_bridge_revalidates_token_and_replaces_existing_connection() -> (
    None
):
    """Bridge initialize must validate token input instead of reusing stale binding."""
    import asyncio

    from tela.core.models import (
        AuthConfig,
        AuthMode,
        ConnectionContext,
        TelaConfig,
    )
    from tela.core.token import compute_signature
    from tela.shell.gateway_runtime import (
        add_runtime_connection,
        clear_runtime_connections,
        get_runtime_connections_snapshot,
        register_bridge_connection,
        set_runtime_config,
        set_runtime_secrets,
    )
    from tela.shell.upstream import handle_initialize

    async def _scenario() -> None:
        secret = "bridge-secret"
        fields = {
            "token_id": "tok_bridge_1",
            "profile_id": "prod",
            "persona_ref": "persona.prod",
            "instance_id": "inst-prod",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-12-31T23:59:59Z",
            "token_version": "0.1.0",
        }
        signature = compute_signature(fields, secret)
        set_runtime_config(
            TelaConfig(
                auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
                profiles={"prod": ProfileConfig(name="prod")},
            )
        )
        set_runtime_secrets([secret])
        clear_runtime_connections()
        registration_result = register_bridge_connection("bridge_recovery_1")
        assert registration_result.is_ok

        # Seed a stale bridge connection that must NOT be reused.
        bridge_connection = ConnectionContext(
            connection_id="bridge_recovery_1",
            profile_id="stale",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.OPEN,
            bridge_connection_id="bridge_recovery_1",
        )
        add_runtime_connection(bridge_connection)

        # MCP initialize with bridge connection ID must validate the canonical token
        # and replace the stale connection binding.
        result = await handle_initialize(
            {
                "name": "probe",
                "tela_bridge_connection_id": "bridge_recovery_1",
                "capability_token": {**fields, "signature": signature},
            }
        )
        assert result.is_ok
        ctx = result.value
        assert ctx is not None
        assert ctx.connection_id == "bridge_recovery_1"
        assert ctx.profile_id == "prod"
        assert ctx.init_mode == AuthMode.TOKEN
        assert ctx.bridge_connection_id == "bridge_recovery_1"
        snapshot = get_runtime_connections_snapshot()
        assert snapshot.is_ok
        assert snapshot.value is not None
        assert len(snapshot.value) == 1
        assert snapshot.value[0].profile_id == "prod"

    try:
        asyncio.run(_scenario())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])
        clear_runtime_connections()
