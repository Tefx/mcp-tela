"""Expected-red tests for hard_cut.impl_shared_surfaces + hard_cut.impl_listing_surface steps.

Verifies:
- ConnectionContext uses `profile_id` (not `profile_name`)
- AuditEntry uses `profile_id` (not `profile_name`)
- TokenInitBinding uses `profile_id` (not `profile_name`)
- Shared runtime surfaces bind canonical `profile_id` only
- Audit construction binds `profile_id` from connection
- handle_connect response uses `profile_id` key
- handle_profiles_list emits `profile_id` key (not `profile_name`)
- CLI commands display `profile_id` (not `profile_name`)
- Legacy `profile_name` field is rejected fail-closed on all shared models
- tela_list_profiles is a builtin MCP tool with canonical payload
- tela_list_profiles payload has profile_id, capabilities, default (no legacy keys)
- Shared tela://profiles / tela.profiles resource registration is removed
- Fail-closed: invalid/missing gateway state rejects list_profiles
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tela.core.models import (
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    Posture,
    TokenInitBinding,
)


# ==============================================================================
# (1) ConnectionContext uses `profile_id` as canonical identity field
# ==============================================================================


class TestConnectionContextProfileId:
    """ConnectionContext must use `profile_id` as canonical identity field."""

    def test_connection_context_has_profile_id_field(self) -> None:
        """ConnectionContext must accept and expose `profile_id`."""
        ctx = ConnectionContext(
            connection_id="c1",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        assert ctx.profile_id == "dev"

    def test_connection_context_profile_id_required(self) -> None:
        """ConnectionContext must require `profile_id`."""
        with pytest.raises(ValidationError):
            ConnectionContext(  # type: ignore[call-arg]
                connection_id="c1",
                connected_at="2026-01-01T00:00:00Z",
            )

    def test_connection_context_rejects_profile_name_fail_closed(self) -> None:
        """ConnectionContext must NOT accept `profile_name` as field name.

        Fail-closed: legacy `profile_name` must not silently work.
        """
        with pytest.raises(ValidationError):
            ConnectionContext(  # type: ignore[call-arg]
                connection_id="c1",
                profile_name="dev",
                connected_at="2026-01-01T00:00:00Z",
            )


# ==============================================================================
# (2) AuditEntry uses `profile_id` as canonical identity field
# ==============================================================================


class TestAuditEntryProfileId:
    """AuditEntry must use `profile_id` as canonical identity field."""

    def test_audit_entry_has_profile_id_field(self) -> None:
        """AuditEntry must accept and expose `profile_id`."""
        entry = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            level=AuditLevel.L1,
            connection_id="c1",
            profile_id="dev",
            tool_name="read_file",
            server_name="fs",
            verdict=EnforcementVerdict.ALLOW,
        )
        assert entry.profile_id == "dev"

    def test_audit_entry_profile_id_required(self) -> None:
        """AuditEntry must require `profile_id`."""
        with pytest.raises(ValidationError):
            AuditEntry(  # type: ignore[call-arg]
                timestamp="2026-01-01T00:00:00Z",
                level=AuditLevel.L1,
                connection_id="c1",
                tool_name="read_file",
                server_name="fs",
                verdict=EnforcementVerdict.ALLOW,
            )

    def test_audit_entry_rejects_profile_name_fail_closed(self) -> None:
        """AuditEntry must NOT accept `profile_name` as field name."""
        with pytest.raises(ValidationError):
            AuditEntry(  # type: ignore[call-arg]
                timestamp="2026-01-01T00:00:00Z",
                level=AuditLevel.L1,
                connection_id="c1",
                profile_name="dev",
                tool_name="read_file",
                server_name="fs",
                verdict=EnforcementVerdict.ALLOW,
            )


# ==============================================================================
# (3) TokenInitBinding uses `profile_id` as canonical identity field
# ==============================================================================


class TestTokenInitBindingProfileId:
    """TokenInitBinding must use `profile_id` as canonical identity field."""

    def test_binding_has_profile_id_field(self) -> None:
        """TokenInitBinding must accept and expose `profile_id`."""
        result = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        binding = TokenInitBinding(token_result=result, profile_id="dev")
        assert binding.profile_id == "dev"

    def test_binding_profile_id_required(self) -> None:
        """TokenInitBinding must require `profile_id`."""
        result = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        with pytest.raises(TypeError):
            TokenInitBinding(  # type: ignore[call-arg]
                token_result=result,
            )

    def test_binding_rejects_profile_name_fail_closed(self) -> None:
        """TokenInitBinding must NOT accept `profile_name` as field name."""
        result = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        with pytest.raises(TypeError):
            TokenInitBinding(  # type: ignore[call-arg]
                token_result=result,
                profile_name="dev",
            )


# ==============================================================================
# (4) Audit construction binds profile_id from connection context
# ==============================================================================


class TestAuditBindsProfileId:
    """Audit entry construction must bind `profile_id` from connection context."""

    def test_build_audit_entry_uses_profile_id(self) -> None:
        """build_audit_entry must create AuditEntry with `profile_id` from connection."""
        from tela.shell.audit import build_audit_entry

        result = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        conn = ConnectionContext(
            connection_id="c1",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        entry_result = build_audit_entry(
            AuditLevel.L1, conn, "read_file", "fs", result, latency_ms=5.0
        )
        assert entry_result.is_ok
        assert entry_result.value.profile_id == "dev"


# ==============================================================================
# (5) handle_connect response uses `profile_id` key (not `profile_name`)
# ==============================================================================


class TestConnectResponseUsesProfileId:
    """HTTP /connect response must use `profile_id` key."""

    def test_connect_response_contains_profile_id_key(self) -> None:
        """Connect response dict must have `profile_id` key, not `profile_name`."""
        from tela.core.models import TelaConfig, ConnectRequest
        from tela.shell.gateway_runtime import (
            add_runtime_connection,
            set_runtime_config,
            set_runtime_running,
            clear_runtime_connections,
        )
        from tela.shell.http_routes import handle_connect

        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()
        try:
            result = handle_connect(
                "valid-token", "valid-token", ConnectRequest(connection_id="test-c1")
            )
            assert result.is_ok
            assert "profile_id" in result.value
            assert "profile_name" not in result.value
        finally:
            clear_runtime_connections()
            set_runtime_config(None)
            set_runtime_running(False)


# ==============================================================================
# (6) handle_profiles_list emits `profile_id` key (not `profile_name`)
# ==============================================================================


class TestProfilesListUsesProfileId:
    """handle_profiles_list must emit `profile_id` key in profile dicts."""

    def test_profiles_list_emits_profile_id_key(self) -> None:
        """Profile list entries must use `profile_id` key, not `profile_name`."""
        from tela.core.models import ProfileConfig, TelaConfig
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.upstream import handle_profiles_list

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"filesystem": Posture.READ_WRITE},
                        default=True,
                    ),
                }
            )
        )
        try:
            result = handle_profiles_list()
            assert result.is_ok
            entry = result.value[0]
            assert "profile_id" in entry
            assert "profile_name" not in entry
        finally:
            set_runtime_config(None)


# ==============================================================================
# (7) handle_profiles_list does not emit legacy `tools` key
# ==============================================================================


class TestProfilesListNoToolsKey:
    """handle_profiles_list must not emit the legacy `tools` key."""

    def test_profiles_list_no_tools_key(self) -> None:
        """Profile list entries must not contain `tools` key (cut in impl_core_vocab)."""
        from tela.core.models import ProfileConfig, TelaConfig
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.upstream import handle_profiles_list

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"fs": Posture.READ_WRITE},
                        default=True,
                    ),
                }
            )
        )
        try:
            result = handle_profiles_list()
            assert result.is_ok
            entry = result.value[0]
            assert "tools" not in entry
        finally:
            set_runtime_config(None)


# ==============================================================================
# (8) tela_list_profiles exists as builtin MCP tool
# ==============================================================================


class TestTelaListProfilesBuiltin:
    """tela_list_profiles must be registered as a builtin MCP tool."""

    def test_builtin_tool_names_includes_tela_list_profiles(self) -> None:
        """BUILTIN_TOOL_NAMES must include 'tela_list_profiles'."""
        from tela.shell.builtin_tools import BUILTIN_TOOL_NAMES

        assert "tela_list_profiles" in BUILTIN_TOOL_NAMES

    def test_builtin_tools_has_tela_list_profiles_entry(self) -> None:
        """BUILTIN_TOOLS list must include an entry for tela_list_profiles."""
        from tela.shell.builtin_tools import BUILTIN_TOOLS

        names = [t["name"] for t in BUILTIN_TOOLS]
        assert "tela_list_profiles" in names

    def test_tela_list_profiles_has_empty_input_schema(self) -> None:
        """tela_list_profiles builtin tool entry must have empty input schema."""
        from tela.shell.builtin_tools import BUILTIN_TOOLS

        entry = next(t for t in BUILTIN_TOOLS if t["name"] == "tela_list_profiles")
        assert entry["inputSchema"] == {
            "type": "object",
            "properties": {},
            "required": [],
        }


# ==============================================================================
# (9) tela_list_profiles payload matches canonical profile-list schema
# ==============================================================================


class TestListProfilesCanonicalPayload:
    """handle_list_profiles must emit only canonical keys: profile_id, capabilities, default."""

    def test_handle_list_profiles_emits_profile_id(self) -> None:
        """Each profile entry must have a 'profile_id' key."""
        from tela.core.models import (
            AuthConfig,
            AuthMode,
            ProfileConfig,
            TelaConfig,
            Posture,
        )
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.builtin_tools import handle_list_profiles

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"fs": Posture.READ_WRITE},
                        default=True,
                    ),
                },
                auth=AuthConfig(mode=AuthMode.OPEN),
            )
        )
        try:
            result = handle_list_profiles()
            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["profile_id"] == "dev"
        finally:
            set_runtime_config(None)

    def test_handle_list_profiles_emits_capabilities(self) -> None:
        """Each profile entry must have a 'capabilities' key with posture values."""
        from tela.core.models import (
            AuthConfig,
            AuthMode,
            ProfileConfig,
            TelaConfig,
            Posture,
        )
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.builtin_tools import handle_list_profiles

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"fs": Posture.READ_WRITE},
                        default=True,
                    ),
                },
                auth=AuthConfig(mode=AuthMode.OPEN),
            )
        )
        try:
            result = handle_list_profiles()
            assert result[0]["capabilities"] == {"fs": "read_write"}
        finally:
            set_runtime_config(None)

    def test_handle_list_profiles_emits_default(self) -> None:
        """Each profile entry must have a 'default' bool key."""
        from tela.core.models import (
            AuthConfig,
            AuthMode,
            ProfileConfig,
            TelaConfig,
            Posture,
        )
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.builtin_tools import handle_list_profiles

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"fs": Posture.READ_WRITE},
                        default=True,
                    ),
                    "reviewer": ProfileConfig(
                        name="reviewer",
                        capabilities={"fs": Posture.READ_ONLY},
                        default=False,
                    ),
                },
                auth=AuthConfig(mode=AuthMode.OPEN),
            )
        )
        try:
            result = handle_list_profiles()
            assert len(result) == 2
            dev_entry = next(e for e in result if e["profile_id"] == "dev")
            rev_entry = next(e for e in result if e["profile_id"] == "reviewer")
            assert dev_entry["default"] is True
            assert rev_entry["default"] is False
        finally:
            set_runtime_config(None)

    def test_handle_list_profiles_no_legacy_profile_name_key(self) -> None:
        """Profile entries must NOT contain legacy 'profile_name' key."""
        from tela.core.models import (
            AuthConfig,
            AuthMode,
            ProfileConfig,
            TelaConfig,
            Posture,
        )
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.builtin_tools import handle_list_profiles

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"fs": Posture.READ_WRITE},
                        default=True,
                    ),
                },
                auth=AuthConfig(mode=AuthMode.OPEN),
            )
        )
        try:
            result = handle_list_profiles()
            assert "profile_name" not in result[0]
        finally:
            set_runtime_config(None)

    def test_handle_list_profiles_no_legacy_families_key(self) -> None:
        """Profile entries must NOT contain legacy 'families' key."""
        from tela.core.models import (
            AuthConfig,
            AuthMode,
            ProfileConfig,
            TelaConfig,
            Posture,
        )
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.builtin_tools import handle_list_profiles

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"fs": Posture.READ_WRITE},
                        default=True,
                    ),
                },
                auth=AuthConfig(mode=AuthMode.OPEN),
            )
        )
        try:
            result = handle_list_profiles()
            assert "families" not in result[0]
        finally:
            set_runtime_config(None)

    def test_handle_list_profiles_no_legacy_tools_key(self) -> None:
        """Profile entries must NOT contain legacy 'tools' key."""
        from tela.core.models import (
            AuthConfig,
            AuthMode,
            ProfileConfig,
            TelaConfig,
            Posture,
        )
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.builtin_tools import handle_list_profiles

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"fs": Posture.READ_WRITE},
                        default=True,
                    ),
                },
                auth=AuthConfig(mode=AuthMode.OPEN),
            )
        )
        try:
            result = handle_list_profiles()
            assert "tools" not in result[0]
        finally:
            set_runtime_config(None)


# ==============================================================================
# (10) tela://profiles / tela.profiles resource registration is removed
# ==============================================================================


class TestProfilesResourceRemoved:
    """The shared tela://profiles / tela.profiles MCP resource must be removed."""

    def test_gateway_source_has_no_profiles_resource_registration(self) -> None:
        """gateway.py must NOT contain _register_profiles_resource function call or definition."""
        from pathlib import Path

        gateway_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "tela"
            / "shell"
            / "gateway.py"
        )
        source = gateway_path.read_text()
        # The function definition and its registration call must be gone
        assert "_register_profiles_resource" not in source
        assert "tela://profiles" not in source
        assert 'name="tela.profiles"' not in source

    def test_conflict_introspection_tools_lists_tela_list_profiles(self) -> None:
        """INTROSPECTION_TOOLS must reference tela_list_profiles, not tela.profiles."""
        from tela.core.conflict import INTROSPECTION_TOOLS

        assert "tela_list_profiles" in INTROSPECTION_TOOLS
        assert "tela.profiles" not in INTROSPECTION_TOOLS


# ==============================================================================
# (11) handle_list_profiles fails closed on missing runtime config
# ==============================================================================


class TestListProfilesFailClosed:
    """handle_list_profiles must reject when gateway has no runtime config."""

    def test_handle_list_profiles_raises_on_missing_runtime_config(self) -> None:
        """handle_list_profiles must raise RuntimeError when no runtime config available."""
        import pytest
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.builtin_tools import handle_list_profiles

        set_runtime_config(None)
        with pytest.raises(
            RuntimeError, match="handle_list_profiles requires a valid runtime config"
        ):
            handle_list_profiles()
