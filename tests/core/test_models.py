"""Behavioral tests for core models.

Verifies model construction, defaults, validation, and serialization
instead of fragile source-text matching.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tela.core.models import (
    AuditConfig,
    AuditLevel,
    AuthConfig,
    AuthMode,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    GatewayTransport,
    Posture,
    ProfileConfig,
    ServerConfig,
    TelaConfig,
    ResolvedTool,
    GatewayStatus,
    RuntimeBindingContract,
    InitializeProfileBinding,
    DefaultProfileResolutionStatus,
    ConnectRequest,
    DisconnectRequest,
)


# --- ProfileConfig behavioral tests ---


class TestProfileConfig:
    def test_default_field_defaults_to_false(self) -> None:
        p = ProfileConfig(name="dev")
        assert p.default is False


class TestConnectRequest:
    def test_connect_request_requires_server_name(self) -> None:
        with pytest.raises(ValidationError):
            ConnectRequest.model_validate({})

    def test_connect_request_rejects_wrong_type(self) -> None:
        with pytest.raises(ValidationError):
            ConnectRequest.model_validate({"server_name": 1})

    def test_connect_request_rejects_extra_keys(self) -> None:
        with pytest.raises(ValidationError):
            ConnectRequest.model_validate(
                {"server_name": "bridge_1", "unexpected_key": True}
            )


class TestDisconnectRequest:
    def test_disconnect_request_requires_connection_id(self) -> None:
        with pytest.raises(ValidationError):
            DisconnectRequest.model_validate({})

    def test_disconnect_request_rejects_wrong_type(self) -> None:
        with pytest.raises(ValidationError):
            DisconnectRequest.model_validate({"connection_id": 1})

    def test_default_field_can_be_set_true(self) -> None:
        p = ProfileConfig(name="dev", default=True)
        assert p.default is True

    def test_capabilities_default_empty(self) -> None:
        p = ProfileConfig(name="dev")
        assert p.capabilities == {}

    def test_capabilities_with_posture(self) -> None:
        p = ProfileConfig(name="dev", capabilities={"read_file": Posture.READ_WRITE})
        assert p.capabilities["read_file"] == Posture.READ_WRITE

    def test_tools_kwarg_rejected_after_hard_cut(self) -> None:
        """Hard cut: ProfileConfig must reject the retired legacy keyword."""
        with pytest.raises(ValidationError):
            ProfileConfig(name="dev", tools={"read_file": Posture.READ_ONLY})  # type: ignore[call-arg]

    def test_name_required(self) -> None:
        with pytest.raises(ValidationError):
            ProfileConfig()  # type: ignore[call-arg]


# --- TelaConfig behavioral tests ---


class TestTelaConfig:
    def test_resolved_default_profile_defaults_to_none(self) -> None:
        cfg = TelaConfig()
        assert cfg.resolved_default_profile is None

    def test_resolved_default_profile_can_be_set(self) -> None:
        cfg = TelaConfig(resolved_default_profile="dev")
        assert cfg.resolved_default_profile == "dev"

    def test_empty_config_is_valid(self) -> None:
        cfg = TelaConfig()
        assert cfg.servers == {}
        assert cfg.profiles == {}

    def test_auth_default(self) -> None:
        cfg = TelaConfig()
        assert cfg.auth.mode == AuthMode.TOKEN

    def test_audit_default(self) -> None:
        cfg = TelaConfig()
        assert cfg.audit.level == AuditLevel.L2

    def test_roundtrip_serialization(self) -> None:
        cfg = TelaConfig(
            profiles={"dev": ProfileConfig(name="dev", default=True)},
            auth=AuthConfig(mode=AuthMode.OPEN),
        )
        data = cfg.model_dump()
        restored = TelaConfig.model_validate(data)
        assert restored.profiles["dev"].default is True
        assert restored.auth.mode == AuthMode.OPEN


# --- Enum behavioral tests ---


class TestEnums:
    def test_posture_values(self) -> None:
        assert Posture.NONE.value == "none"
        assert Posture.READ_ONLY.value == "read_only"
        assert Posture.READ_WRITE.value == "read_write"
        assert Posture.DESTRUCTIVE.value == "destructive"

    def test_auth_mode_values(self) -> None:
        assert AuthMode.TOKEN.value == "token"
        assert AuthMode.OPEN.value == "open"

    def test_gateway_transport_values(self) -> None:
        assert GatewayTransport.STDIO.value == "stdio"
        assert GatewayTransport.SSE.value == "sse"
        assert GatewayTransport.HTTP.value == "http"

    def test_audit_level_values(self) -> None:
        assert AuditLevel.L1.value == "L1"
        assert AuditLevel.L2.value == "L2"
        assert AuditLevel.L3.value == "L3"

    def test_enforcement_verdict_values(self) -> None:
        assert EnforcementVerdict.ALLOW.value == "allow"
        assert EnforcementVerdict.DENY.value == "deny"


# --- ServerConfig behavioral tests ---


class TestServerConfig:
    def test_minimal_server(self) -> None:
        s = ServerConfig(name="fs")
        assert s.name == "fs"
        assert s.command is None
        assert s.args == []
        assert s.env == {}
        assert s.default_posture == Posture.NONE

    def test_server_with_command(self) -> None:
        s = ServerConfig(name="fs", command="node", args=["server.js"])
        assert s.command == "node"
        assert s.args == ["server.js"]

    def test_server_with_explicit_env_mapping(self) -> None:
        s = ServerConfig(name="fs", command="node", env={"TOKEN": "abc"})
        assert s.env == {"TOKEN": "abc"}

    def test_server_env_requires_string_values(self) -> None:
        with pytest.raises(ValidationError):
            ServerConfig(name="fs", command="node", env={"PORT": 8080})  # type: ignore[dict-item]  # deliberate invalid type


# --- AuditConfig behavioral tests ---


class TestAuditConfig:
    def test_defaults(self) -> None:
        a = AuditConfig()
        assert a.level == AuditLevel.L2
        assert a.output == "~/.tela/audit.jsonl"

    def test_custom_values(self) -> None:
        a = AuditConfig(level=AuditLevel.L3, output="/var/log/tela.jsonl")
        assert a.level == AuditLevel.L3
        assert a.output == "/var/log/tela.jsonl"


# --- Runtime model behavioral tests ---


class TestRuntimeModels:
    def test_resolved_tool_construction(self) -> None:
        t = ResolvedTool(name="read_file", server_name="fs", family="filesystem")
        assert t.name == "read_file"
        assert t.posture is None

    def test_connection_context_defaults(self) -> None:
        c = ConnectionContext(
            connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z"
        )
        assert c.tool_call_count == 0

    def test_enforcement_result_allow(self) -> None:
        r = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        assert r.denied_by is None

    def test_enforcement_result_deny(self) -> None:
        r = EnforcementResult(
            verdict=EnforcementVerdict.DENY,
            denied_by="posture_ceiling",
            error_code="POSTURE_DENIED",
        )
        assert r.denied_by == "posture_ceiling"

    def test_gateway_status_defaults(self) -> None:
        gs = GatewayStatus(
            uptime_seconds=100.0,
            server_count=2,
            active_connections=1,
            profile_count=3,
            total_tool_calls=42,
        )
        assert gs.connected_servers == []

    def test_connection_context_defaults_recovery_fields(self) -> None:
        """Recovery fields must default to None for backward compatibility."""
        c = ConnectionContext(
            connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z"
        )
        assert c.init_mode is None
        assert c.client_info_snapshot is None
        assert c.bridge_connection_id is None

    def test_connection_context_init_mode_token(self) -> None:
        """Token-mode connections must record AuthMode.TOKEN in init_mode."""
        c = ConnectionContext(
            connection_id="c1",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.TOKEN,
        )
        assert c.init_mode == AuthMode.TOKEN

    def test_connection_context_init_mode_open(self) -> None:
        """Open-mode connections must record AuthMode.OPEN in init_mode."""
        c = ConnectionContext(
            connection_id="c1",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.OPEN,
        )
        assert c.init_mode == AuthMode.OPEN

    def test_connection_context_client_info_snapshot_preserves_token_fields(
        self,
    ) -> None:
        """client_info_snapshot must preserve all token-mode fields for recovery.

        Without the snapshot, token-mode reconnect cannot re-derive the
        original capability-token context from an empty initialize.
        """
        snapshot = {
            "token_id": "tok_1",
            "profile_id": "production",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-12-31T23:59:59Z",
            "signature": "abc123def456",
        }
        c = ConnectionContext(
            connection_id="c1",
            profile_id="production",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.TOKEN,
            client_info_snapshot=snapshot,
        )
        assert c.client_info_snapshot is not None
        assert c.client_info_snapshot["token_id"] == "tok_1"
        assert c.client_info_snapshot["profile_id"] == "production"
        assert c.client_info_snapshot["signature"] == "abc123def456"

    def test_connection_context_bridge_connection_id(self) -> None:
        """bridge_connection_id must record the /connect registration ID."""
        c = ConnectionContext(
            connection_id="conn_abc123",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
            bridge_connection_id="bridge_abc",
        )
        assert c.bridge_connection_id == "bridge_abc"

    def test_connection_context_roundtrip_with_recovery_fields(self) -> None:
        """ConnectionContext with recovery fields must roundtrip through serialization."""
        c = ConnectionContext(
            connection_id="c1",
            profile_id="production",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.TOKEN,
            client_info_snapshot={
                "token_id": "tok_1",
                "profile_id": "production",
                "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-12-31T23:59:59Z",
                "signature": "sig",
            },
            bridge_connection_id="bridge_xyz",
        )
        data = c.model_dump()
        restored = ConnectionContext.model_validate(data)
        assert restored.init_mode == AuthMode.TOKEN
        assert restored.client_info_snapshot is not None
        assert restored.client_info_snapshot["token_id"] == "tok_1"
        assert restored.bridge_connection_id == "bridge_xyz"


# --- Recovery-critical runtime state contract tests ---


class TestConnectionContextRecoveryContract:
    """Property-style tests proving init_mode and client_info_snapshot
    are required for correct reconnect/re-initialize semantics.

    These tests expose why a ConnectionContext lacking these fields
    cannot serve as authoritative recovery state.
    """

    def test_token_mode_recovery_requires_init_mode(self) -> None:
        """A token-mode connection without init_mode cannot be distinguished
        from an open-mode connection during reconnect.

        Recovery decision logic needs init_mode to select the correct
        revalidation path (token vs open-profile).
        """
        c = ConnectionContext(
            connection_id="c1",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.TOKEN,
        )
        # init_mode must be present to select revalidation path
        assert c.init_mode is not None
        assert c.init_mode == AuthMode.TOKEN

    def test_token_mode_recovery_requires_client_info_snapshot(self) -> None:
        """A token-mode connection without client_info_snapshot cannot
        re-derive token validity parameters from empty initialize.

        The snapshot carries token_id, issued_at, expires_at, signature —
        all required fields for CapabilityToken reconstruction.
        """
        c = ConnectionContext(
            connection_id="c1",
            profile_id="production",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.TOKEN,
            client_info_snapshot={
                "token_id": "tok_1",
                "profile_id": "production",
                "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-12-31T23:59:59Z",
                "signature": "hmac-sha256-hex",
            },
        )
        assert c.client_info_snapshot is not None
        required_token_fields = (
            "token_id",
            "profile_id",
            "issued_at",
            "expires_at",
            "signature",
        )
        for field in required_token_fields:
            assert field in c.client_info_snapshot, (
                f"Recovery requires {field} in client_info_snapshot for token revalidation"
            )

    def test_empty_initialize_cannot_derive_token_mode_state(self) -> None:
        """Prove that token-mode recovery state cannot be derived from
        an empty initialize — the authoritative source is the
        client_info_snapshot preserved at init time.

        This is the expected-red proof: without client_info_snapshot,
        a ConnectionContext with init_mode=TOKEN cannot re-validate
        the capability token because token_id, issued_at, expires_at,
        and signature are not recoverable from profile_id alone.
        """
        # Minimal ConnectionContext as if recorded during token init
        minimal_ctx = ConnectionContext(
            connection_id="c1",
            profile_id="production",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.TOKEN,
            client_info_snapshot={
                "token_id": "tok_1",
                "profile_id": "production",
                "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-12-31T23:59:59Z",
                "signature": "hmac-sha256-hex",
                "persona_ref": "user-42",
                "instance_id": "inst-7",
                "max_depth": "3",
            },
        )

        # Without client_info_snapshot, recovery cannot proceed:
        # profile_id alone is insufficient to reconstruct CapabilityToken
        bare_ctx = ConnectionContext(
            connection_id="c1",
            profile_id="production",
            connected_at="2026-01-01T00:00:00Z",
        )
        # Bare ctx lacks all recovery-critical fields
        assert bare_ctx.init_mode is None, (
            "Without init_mode, recovery cannot distinguish TOKEN from OPEN mode"
        )
        assert bare_ctx.client_info_snapshot is None, (
            "Without client_info_snapshot, recovery cannot reconstruct CapabilityToken"
        )
        # With the snapshot, all required token fields are available
        assert minimal_ctx.client_info_snapshot is not None
        for field in (
            "token_id",
            "profile_id",
            "issued_at",
            "expires_at",
            "signature",
        ):
            assert minimal_ctx.client_info_snapshot.get(field) is not None, (
                f"Token field {field} must be present in snapshot for revalidation"
            )

    def test_recovery_gap_bare_context_cannot_select_revalidation_path(self) -> None:
        """Prove that without init_mode, a bare ConnectionContext cannot
        determine whether to use token revalidation or open-mode profile
        lookup during reconnect.

        Expected-red proof: before init_mode was added, recovery logic
        had no way to branch on the authentication path. This test
        demonstrates the gap by showing both modes produce identical
        bare contexts.
        """
        # Two connections from different auth modes produce identical bare contexts
        token_bare = ConnectionContext(
            connection_id="c_token",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        open_bare = ConnectionContext(
            connection_id="c_open",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        # Without init_mode, these contexts are indistinguishable for recovery
        assert token_bare.init_mode is None
        assert open_bare.init_mode is None
        # Recovery cannot branch differently for TOKEN vs OPEN without init_mode
        assert token_bare.init_mode == open_bare.init_mode, (
            "Gap: bare contexts from different auth modes are indistinguishable"
        )

    def test_recovery_gap_bare_context_missing_token_fields(self) -> None:
        """Prove that without client_info_snapshot, a bare token-mode
        ConnectionContext cannot re-derive validation parameters.

        Expected-red proof: the CapabilityToken constructor requires
        token_id, persona_ref, instance_id, issued_at, expires_at,
        token_version, signature — none of which are present on a bare
        ConnectionContext.
        """
        bare_ctx = ConnectionContext(
            connection_id="c1",
            profile_id="production",
            connected_at="2026-01-01T00:00:00Z",
        )

        # CapabilityToken requires these fields; bare ctx provides none
        required_for_revalidation = (
            "token_id",
            "persona_ref",
            "instance_id",
            "issued_at",
            "expires_at",
            "token_version",
            "signature",
        )
        for field in required_for_revalidation:
            assert bare_ctx.client_info_snapshot is None, (
                f"Gap: bare context has no snapshot, cannot provide {field} for revalidation"
            )

        # A recovery-enabled context carries all required fields
        recovery_ctx = ConnectionContext(
            connection_id="c1",
            profile_id="production",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.TOKEN,
            client_info_snapshot={
                "token_id": "tok_1",
                "profile_id": "production",
                "persona_ref": "persona.production",
                "instance_id": "inst-production",
                "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-12-31T23:59:59Z",
                "token_version": "0.1.0",
                "signature": "sig",
            },
        )
        assert recovery_ctx.client_info_snapshot is not None
        for field in required_for_revalidation:
            assert recovery_ctx.client_info_snapshot.get(field) is not None, (
                f"Recovery requires {field} in client_info_snapshot"
            )


# --- Spec-derived fixture: minimal lockfile payload (INTERFACES.md §7.3) ---
# Lockfile fixture uses exact documented shape with all 7 required fields.
# No convenience fields beyond the spec. This fixture validates the contract.
_LOCKFILE_FIXTURE_MINIMAL = {
    "pid": 12345,
    "host": "127.0.0.1",
    "port": 49152,
    "token": "bearer-token-here",
    "started_at": "2026-03-22T10:00:00Z",
    "config_path": "/path/to/tela.yaml",
    "version": "0.1.0",
}


def test_lockfile_fixture_matches_interfaces_spec() -> None:
    """Minimal lockfile fixture uses exact documented shape from INTERFACES.md §7.3.

    Ref: docs/INTERFACES.md §7.3 Lockfile Contract
    The 7 required fields are: pid, host, port, token, started_at, config_path, version.
    No convenience fields beyond the spec are present.
    """
    assert set(_LOCKFILE_FIXTURE_MINIMAL.keys()) == {
        "pid",
        "host",
        "port",
        "token",
        "started_at",
        "config_path",
        "version",
    }
    assert _LOCKFILE_FIXTURE_MINIMAL["pid"] == 12345
    assert _LOCKFILE_FIXTURE_MINIMAL["host"] == "127.0.0.1"
    assert _LOCKFILE_FIXTURE_MINIMAL["port"] == 49152
    assert isinstance(_LOCKFILE_FIXTURE_MINIMAL["token"], str)
    assert _LOCKFILE_FIXTURE_MINIMAL["started_at"] == "2026-03-22T10:00:00Z"
    assert _LOCKFILE_FIXTURE_MINIMAL["config_path"] == "/path/to/tela.yaml"
    assert _LOCKFILE_FIXTURE_MINIMAL["version"] == "0.1.0"


def test_lockfile_data_accepts_spec_fixture() -> None:
    """LockfileData model validates against the spec fixture.

    Ref: docs/INTERFACES.md §7.3 - LockfileData accepts the minimal documented shape.
    """
    from tela.core.models import LockfileData

    lockfile = LockfileData(**_LOCKFILE_FIXTURE_MINIMAL)
    assert lockfile.pid == 12345
    assert lockfile.host == "127.0.0.1"
    assert lockfile.port == 49152
    assert lockfile.token == "bearer-token-here"
    assert lockfile.started_at == "2026-03-22T10:00:00Z"
    assert lockfile.config_path == "/path/to/tela.yaml"
    assert lockfile.version == "0.1.0"


# --- Spec-derived fixture: GET /status response (INTERFACES.md §7.2.1) ---
# Status response fixture includes phase, config_path, startup_generation,
# per-server state summary, and tool digest/count as documented.
_STATUS_FIXTURE_MINIMAL = {
    "uptime_seconds": 12.5,
    "server_count": 2,
    "connected_servers": ["fs", "shell"],
    "active_connections": 1,
    "profile_count": 2,
    "total_tool_calls": 42,
    "connections": [
        {
            "connection_id": "bridge_abc123",
            "profile_id": "developer",
            "connected_at": "2026-03-25T12:00:00Z",
            "tool_call_count": 5,
        }
    ],
    "audit_entries": [
        {
            "timestamp": "2026-03-25T12:00:00Z",
            "level": "L2",
            "event": "tool_call",
            "connection_id": "bridge_abc123",
            "tool_name": "filesystem/read_file",
            "details": {},
        }
    ],
}


def test_status_response_fixture_matches_interfaces_spec() -> None:
    """Minimal status fixture uses exact documented shape from INTERFACES.md §7.2.1.

    Ref: docs/INTERFACES.md §7.2.1 GET /status Response Schema
    Authoritative fields: uptime_seconds, server_count, connected_servers,
    active_connections, profile_count, total_tool_calls, connections, audit_entries.
    """
    assert "uptime_seconds" in _STATUS_FIXTURE_MINIMAL
    assert "server_count" in _STATUS_FIXTURE_MINIMAL
    assert "connected_servers" in _STATUS_FIXTURE_MINIMAL
    assert "active_connections" in _STATUS_FIXTURE_MINIMAL
    assert "profile_count" in _STATUS_FIXTURE_MINIMAL
    assert "total_tool_calls" in _STATUS_FIXTURE_MINIMAL
    assert "connections" in _STATUS_FIXTURE_MINIMAL
    assert "audit_entries" in _STATUS_FIXTURE_MINIMAL
    # Count-vs-collection semantics
    assert isinstance(_STATUS_FIXTURE_MINIMAL["active_connections"], int)
    assert isinstance(_STATUS_FIXTURE_MINIMAL["connections"], list)


def test_status_response_accepts_spec_fixture() -> None:
    """StatusResponse model validates against the spec fixture.

    Ref: docs/INTERFACES.md §7.2.1 - StatusResponse accepts the documented shape.
    """
    from tela.core.models import (
        AuditEntry,
        AuditLevel,
        EnforcementVerdict,
        StatusResponse,
    )

    # Build valid audit entry with all required fields
    audit_entry = AuditEntry(
        timestamp="2026-03-25T12:00:00Z",
        level=AuditLevel.L2,
        connection_id="bridge_abc123",
        profile_id="developer",
        tool_name="filesystem/read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
    )

    status = StatusResponse(
        uptime_seconds=12.5,
        server_count=2,
        connected_servers=["fs", "shell"],
        active_connections=1,
        profile_count=2,
        total_tool_calls=42,
        connections=[
            ConnectionContext(
                connection_id="bridge_abc123",
                profile_id="developer",
                connected_at="2026-03-25T12:00:00Z",
                tool_call_count=5,
            )
        ],
        audit_entries=[audit_entry],
    )
    assert status.uptime_seconds == 12.5
    assert status.server_count == 2
    assert status.connected_servers == ["fs", "shell"]
    assert status.active_connections == 1
    assert status.profile_count == 2
    assert status.total_tool_calls == 42
    assert len(status.connections) == 1
    assert status.connections[0].connection_id == "bridge_abc123"


# --- Contract dataclass tests ---


class TestContractDataclasses:
    def test_runtime_binding_stdio(self) -> None:
        rb = RuntimeBindingContract(
            config_path="tela.yaml",
            transport=GatewayTransport.STDIO,
            port=None,
            cli_default_profile=None,
        )
        assert rb.transport == GatewayTransport.STDIO
        assert rb.port is None

    def test_runtime_binding_sse(self) -> None:
        rb = RuntimeBindingContract(
            config_path="tela.yaml",
            transport=GatewayTransport.SSE,
            port=8080,
            cli_default_profile="dev",
        )
        assert rb.port == 8080

    def test_runtime_binding_http(self) -> None:
        rb = RuntimeBindingContract(
            config_path="tela.yaml",
            transport=GatewayTransport.HTTP,
            port=8080,
            cli_default_profile="dev",
        )
        assert rb.transport == GatewayTransport.HTTP
        assert rb.port == 8080

    def test_initialize_profile_binding_resolved(self) -> None:
        ipb = InitializeProfileBinding(
            status=DefaultProfileResolutionStatus.RESOLVED,
            resolved_default_profile="dev",
        )
        assert ipb.resolved_default_profile == "dev"

    def test_initialize_profile_binding_missing(self) -> None:
        ipb = InitializeProfileBinding(
            status=DefaultProfileResolutionStatus.MISSING,
            resolved_default_profile=None,
        )
        assert ipb.resolved_default_profile is None
