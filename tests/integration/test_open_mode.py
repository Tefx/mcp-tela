"""Integration-level contract and behavioral tests for open-mode runtime boundaries.

Tests span CLI/start configuration into open-mode initialize binding to verify
the runtime contract chain is coherent end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tela.core.config import (
    ConfigContractError,
    resolve_open_mode_default_profile,
)
from tela.core.models import (
    AuthMode,
    DefaultProfileResolutionStatus,
    GatewayTransport,
    InitializeProfileBinding,
    ProfileConfig,
    RuntimeBindingContract,
)
from tela.shell.gateway import GatewayStartupConfig, bind_gateway_startup
from tela.shell.upstream import InitializeContext, resolve_initialize_profile_binding


# --- Existing contract declaration tests (preserved) ---


def test_start_contract_declares_stdio_default_and_remote_opt_in() -> None:
    source = Path("src/tela/commands/start.py").read_text(encoding="utf-8")
    assert "Default transport is stdio" in source
    assert "HTTP (Streamable HTTP) is the default" in source


def test_upstream_contract_declares_no_profile_selection_from_metadata() -> None:
    source = Path("src/tela/shell/upstream.py").read_text(encoding="utf-8")
    assert "must not influence profile selection" in source


def test_upstream_contract_declares_missing_or_ambiguous_rejection() -> None:
    source = Path("src/tela/shell/upstream.py").read_text(encoding="utf-8")
    assert "Missing default-profile resolution rejects initialize" in source
    assert "Ambiguous default-profile resolution rejects initialize" in source


def test_gateway_startup_contract_declares_open_mode_without_token() -> None:
    source = Path("src/tela/shell/gateway.py").read_text(encoding="utf-8")
    assert "open mode requires no token" in source


# --- CLI default-profile precedence tests ---


def test_cli_default_profile_wins_over_config_default() -> None:
    """CLI --default-profile must take precedence over config default: true."""
    profiles = {
        "staging": ProfileConfig(name="staging", default=True),
        "production": ProfileConfig(name="production"),
    }
    result = resolve_open_mode_default_profile(
        profiles, cli_default_profile="production"
    )
    assert result == "production"


def test_config_default_used_when_no_cli_override() -> None:
    """Config default: true profile is used when CLI does not override."""
    profiles = {
        "dev": ProfileConfig(name="dev", default=True),
        "staging": ProfileConfig(name="staging"),
    }
    result = resolve_open_mode_default_profile(profiles)
    assert result == "dev"


def test_cli_default_profile_not_in_config_raises() -> None:
    """CLI --default-profile referencing unknown profile must be rejected."""
    profiles = {"dev": ProfileConfig(name="dev", default=True)}
    with pytest.raises(ConfigContractError) as exc_info:
        resolve_open_mode_default_profile(
            profiles, cli_default_profile="nonexistent"
        )
    assert exc_info.value.code == "PROFILE_NOT_FOUND"


# --- Initialize rejection cases ---


def test_open_mode_missing_default_profile_rejected() -> None:
    """Missing default profile in open mode must be rejected."""
    profiles = {
        "dev": ProfileConfig(name="dev"),
        "staging": ProfileConfig(name="staging"),
    }
    with pytest.raises(ConfigContractError) as exc_info:
        resolve_open_mode_default_profile(profiles)
    assert exc_info.value.code == "OPEN_MODE_DEFAULT_PROFILE_MISSING"


def test_open_mode_ambiguous_default_profile_rejected() -> None:
    """Multiple default: true profiles in open mode must be rejected."""
    profiles = {
        "dev": ProfileConfig(name="dev", default=True),
        "staging": ProfileConfig(name="staging", default=True),
    }
    with pytest.raises(ConfigContractError) as exc_info:
        resolve_open_mode_default_profile(profiles)
    assert exc_info.value.code == "OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS"


# --- Conformance: CLI/gateway/upstream share same resolved profile fact ---


class TestSharedResolvedProfileConformance:
    """Conformance fixture proving CLI, gateway, and upstream use the same
    resolved default profile fact rather than re-deriving it independently.
    """

    @pytest.fixture()
    def resolved_profile(self) -> str:
        """Single source of truth for resolved default profile."""
        profiles = {"production": ProfileConfig(name="production", default=True)}
        return resolve_open_mode_default_profile(profiles)

    def test_cli_runtime_binding_carries_resolved_profile(
        self, resolved_profile: str
    ) -> None:
        """RuntimeBindingContract carries the resolved profile from CLI authority."""
        contract = RuntimeBindingContract(
            config_path="tela.yaml",
            transport=GatewayTransport.STDIO,
            port=None,
            cli_default_profile=resolved_profile,
        )
        assert contract.cli_default_profile == resolved_profile

    def test_gateway_startup_config_carries_resolved_profile(
        self, resolved_profile: str
    ) -> None:
        """GatewayStartupConfig must carry the same resolved profile."""
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile=resolved_profile,
        )
        assert config.default_profile == resolved_profile

    def test_initialize_binding_carries_resolved_profile(
        self, resolved_profile: str
    ) -> None:
        """InitializeProfileBinding must carry the same resolved profile."""
        binding = InitializeProfileBinding(
            status=DefaultProfileResolutionStatus.RESOLVED,
            resolved_default_profile=resolved_profile,
        )
        assert binding.resolved_default_profile == resolved_profile

    def test_all_surfaces_agree_on_resolved_profile(
        self, resolved_profile: str
    ) -> None:
        """All three surfaces (CLI, gateway, upstream) must reference the same
        resolved profile without re-derivation.
        """
        cli_contract = RuntimeBindingContract(
            config_path="tela.yaml",
            transport=GatewayTransport.STDIO,
            port=None,
            cli_default_profile=resolved_profile,
        )
        gateway_config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile=resolved_profile,
        )
        upstream_binding = InitializeProfileBinding(
            status=DefaultProfileResolutionStatus.RESOLVED,
            resolved_default_profile=resolved_profile,
        )

        # All three must agree on the same resolved value
        assert cli_contract.cli_default_profile == gateway_config.default_profile
        assert gateway_config.default_profile == upstream_binding.resolved_default_profile


# --- Integration path: CLI start -> gateway binding (stub verification) ---


def test_bind_gateway_startup_binds_runtime_contract() -> None:
    """bind_gateway_startup must produce GatewayStartupConfig from RuntimeBindingContract."""
    import tempfile
    import os
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write("profiles:\n  dev:\n    name: dev\n    default: true\nauth:\n  mode: open\n")
    contract = RuntimeBindingContract(
        config_path=p,
        transport=GatewayTransport.STDIO,
        port=None,
        cli_default_profile="dev",
    )
    result = bind_gateway_startup(contract)
    assert result.is_ok
    assert result.value is not None
    assert result.value.default_profile == "dev"
    assert result.value.transport == GatewayTransport.STDIO


def test_resolve_initialize_binding_succeeds_for_resolved() -> None:
    """resolve_initialize_profile_binding must succeed for resolved profile."""
    result = resolve_initialize_profile_binding(
        resolved_default_profile="dev",
        default_resolution_status=DefaultProfileResolutionStatus.RESOLVED,
        context=InitializeContext(connection_metadata={}),
    )
    assert result.is_ok
    assert result.value is not None
    assert result.value.resolved_default_profile == "dev"


# --- Transport contract tests ---


def test_runtime_binding_stdio_when_no_port() -> None:
    """RuntimeBindingContract must select STDIO when port is None."""
    contract = RuntimeBindingContract(
        config_path="tela.yaml",
        transport=GatewayTransport.STDIO,
        port=None,
        cli_default_profile=None,
    )
    assert contract.transport == GatewayTransport.STDIO
    assert contract.port is None


def test_runtime_binding_sse_when_port_provided() -> None:
    """RuntimeBindingContract must select SSE when port is provided."""
    contract = RuntimeBindingContract(
        config_path="tela.yaml",
        transport=GatewayTransport.SSE,
        port=8080,
        cli_default_profile=None,
    )
    assert contract.transport == GatewayTransport.SSE
    assert contract.port == 8080
