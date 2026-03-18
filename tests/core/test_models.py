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
    SideEffectPolicy,
    TelaConfig,
    ToolOverride,
    ResolvedTool,
    GatewayStatus,
    AuditEntry,
    CapabilityToken,
    MetaField,
    TelaError,
    RuntimeBindingContract,
    InitializeProfileBinding,
    DefaultProfileResolutionStatus,
    ProfileToolOverrides,
)


# --- ProfileConfig behavioral tests ---


class TestProfileConfig:
    def test_default_field_defaults_to_false(self) -> None:
        p = ProfileConfig(name="dev")
        assert p.default is False

    def test_default_field_can_be_set_true(self) -> None:
        p = ProfileConfig(name="dev", default=True)
        assert p.default is True

    def test_side_effect_policy_default(self) -> None:
        p = ProfileConfig(name="dev")
        assert p.side_effect_policy == SideEffectPolicy.ALLOW

    def test_tools_default_empty(self) -> None:
        p = ProfileConfig(name="dev")
        assert p.tools == {}

    def test_tools_with_posture(self) -> None:
        p = ProfileConfig(name="dev", tools={"read_file": Posture.READ_ONLY})
        assert p.tools["read_file"] == Posture.READ_ONLY

    def test_capabilities_with_posture(self) -> None:
        p = ProfileConfig(name="dev", capabilities={"read_file": Posture.READ_WRITE})
        assert p.capabilities["read_file"] == Posture.READ_WRITE

    def test_matching_tools_and_capabilities_are_accepted(self) -> None:
        p = ProfileConfig(
            name="dev",
            tools={"read_file": Posture.READ_ONLY},
            capabilities={"read_file": Posture.READ_ONLY},
        )
        assert p.capabilities["read_file"] == Posture.READ_ONLY

    def test_conflicting_tools_and_capabilities_raise_value_error(self) -> None:
        with pytest.raises(
            ValidationError, match="must match when both are provided"
        ) as exc_info:
            ProfileConfig(
                name="dev",
                tools={"read_file": Posture.READ_ONLY},
                capabilities={"read_file": Posture.READ_WRITE},
            )

        root_error = exc_info.value.errors()[0]["ctx"]["error"]
        assert isinstance(root_error, ValueError)

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

    def test_side_effect_policy_values(self) -> None:
        assert SideEffectPolicy.ALLOW.value == "allow"
        assert SideEffectPolicy.READ_ONLY.value == "read_only"

    def test_gateway_transport_values(self) -> None:
        assert GatewayTransport.STDIO.value == "stdio"
        assert GatewayTransport.SSE.value == "sse"

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
        assert s.default_posture == Posture.NONE

    def test_server_with_command(self) -> None:
        s = ServerConfig(name="fs", command="node", args=["server.js"])
        assert s.command == "node"
        assert s.args == ["server.js"]


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
            connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
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
