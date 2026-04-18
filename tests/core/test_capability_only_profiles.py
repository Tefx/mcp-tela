"""Tests for capability-only profile format (ADR-003).

Tests verify the tools→capabilities alias migration and enforcement chain.
The side_effect_policy migration has been removed - legacy configs with
side_effect_policy will now fail validation (unknown field).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tela.core.models import (
    EnforcementResult,
    EnforcementVerdict,
    Posture,
    ProfileConfig,
    ProfileToolOverrides,
)
from tela.core.enforcement import enforce
from tela.core.config import parse_config
from tela.core.errors import ConfigContractError
from tela.core.catalog import BUILTIN_PROFILES, get_builtin_profile


# ==============================================================================
# (a) ProfileConfig accepts capabilities= only (tools= is hard-cut removed)
# ==============================================================================


class TestCapabilitiesOnly:
    """Tests that ProfileConfig accepts only capabilities= (tools= is rejected)."""

    def test_capabilities_kwarg_sets_capabilities(self) -> None:
        """ProfileConfig(capabilities={...}) should set capabilities field."""
        p = ProfileConfig(name="dev", capabilities={"filesystem": Posture.READ_ONLY})
        assert p.capabilities["filesystem"] == Posture.READ_ONLY

    def test_tools_kwarg_rejected(self) -> None:
        """ProfileConfig(tools={...}) must raise ValidationError (hard cut)."""
        with pytest.raises(ValidationError):
            ProfileConfig(name="dev", tools={"filesystem": Posture.READ_WRITE})  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        ("posture_text", "expected"),
        [
            ("none", Posture.NONE),
            ("read_only", Posture.READ_ONLY),
            ("read_write", Posture.READ_WRITE),
            ("destructive", Posture.DESTRUCTIVE),
        ],
    )
    def test_profile_config_accepts_canonical_posture_strings(
        self, posture_text: str, expected: Posture
    ) -> None:
        """Canonical posture strings must remain usable on the local config surface."""
        p = ProfileConfig.model_validate(
            {"name": "dev", "capabilities": {"filesystem": posture_text}}
        )
        assert p.capabilities["filesystem"] == expected

    def test_profile_config_rejects_noncanonical_posture_strings(self) -> None:
        """Only canonical posture strings are accepted for capabilities."""
        with pytest.raises(ValidationError):
            ProfileConfig.model_validate(
                {"name": "dev", "capabilities": {"filesystem": "readonly"}}
            )


# ==============================================================================
# (b) capabilities= is canonical in serialized output
# ==============================================================================


class TestCanonicalOutput:
    """Tests that capabilities (not tools) is canonical in model output."""

    def test_model_dump_includes_capabilities(self) -> None:
        """model_dump() should include 'capabilities'."""
        p = ProfileConfig(name="dev", capabilities={"filesystem": Posture.READ_WRITE})
        data = p.model_dump()
        assert "capabilities" in data
        assert data.get("capabilities", {}).get("filesystem") == "read_write"

    def test_model_dump_with_capabilities_input(self) -> None:
        """Profile created with capabilities= must output capabilities in model_dump()."""
        p = ProfileConfig(name="dev", capabilities={"filesystem": Posture.READ_ONLY})
        data = p.model_dump()
        assert "capabilities" in data
        assert data["capabilities"]["filesystem"] == "read_only"

    def test_json_dump_uses_capabilities_key(self) -> None:
        """model_dump_json or similar should use 'capabilities' key."""
        p = ProfileConfig(name="dev", capabilities={"filesystem": Posture.READ_WRITE})
        json_data = p.model_dump(mode="json")
        assert "capabilities" in json_data
        assert json_data["capabilities"]["filesystem"] == "read_write"

    def test_parse_config_accepts_canonical_capabilities_yaml_strings(self) -> None:
        """parse_config must accept canonical posture strings from YAML/object input."""
        config = parse_config(
            {
                "profiles": {
                    "dev": {
                        "capabilities": {
                            "filesystem": "read_only",
                            "git": "read_write",
                        }
                    }
                },
                "auth": {"mode": "open"},
            },
            {},
        )
        assert config.profiles["dev"].capabilities == {
            "filesystem": Posture.READ_ONLY,
            "git": Posture.READ_WRITE,
        }


# ==============================================================================
# (c) side_effect_policy field is removed - legacy config should error
# ==============================================================================
# NOTE: side_effect_policy migration support has been removed.
# Legacy configs containing side_effect_policy will now raise an error
# during validation (unknown field). Users must migrate to capabilities format.


# ==============================================================================
# (e) new 3-step per-call enforcement chain produces correct ALLOW/DENY results
# ==============================================================================


class TestThreeStepEnforcement:
    """Tests for the new 3-step enforcement chain."""

    def test_enforce_chain_step1_family_admission(self) -> None:
        """Step 1: Family not in capabilities -> DENY."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="shell_exec",
            server_name="shell",
            family="shell",
            posture=Posture.READ_ONLY,
        )
        profile = ProfileConfig(
            name="dev",
            capabilities={"filesystem": Posture.READ_WRITE},
        )
        result = enforce(
            "shell_exec",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        assert result.verdict == EnforcementVerdict.DENY
        assert result.denied_by == "family_admission"

    def test_enforce_chain_step2_tool_override_deny(self) -> None:
        """Step 2: Tool override deny -> DENY."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="delete_file",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_WRITE,
        )
        profile = ProfileConfig(
            name="dev",
            capabilities={"filesystem": Posture.READ_WRITE},
            tool_overrides={
                "filesystem": ProfileToolOverrides(
                    overrides={"delete_file": EnforcementVerdict.DENY}
                )
            },
        )
        result = enforce(
            "delete_file",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        assert result.verdict == EnforcementVerdict.DENY
        assert result.denied_by == "tool_override"

    def test_enforce_chain_step2_tool_override_allow(self) -> None:
        """Step 2: Tool override allow (within ceiling) -> ALLOW."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="read_file",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_ONLY,
        )
        profile = ProfileConfig(
            name="dev",
            capabilities={"filesystem": Posture.READ_WRITE},
            tool_overrides={
                "filesystem": ProfileToolOverrides(
                    overrides={"read_file": EnforcementVerdict.ALLOW}
                )
            },
        )
        result = enforce(
            "read_file",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        assert result.verdict == EnforcementVerdict.ALLOW

    def test_enforce_chain_step3_posture_ceiling_allow(self) -> None:
        """Step 3: Posture within ceiling -> ALLOW."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="read_file",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_ONLY,
        )
        profile = ProfileConfig(
            name="dev",
            capabilities={"filesystem": Posture.READ_WRITE},
        )
        result = enforce(
            "read_file",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        assert result.verdict == EnforcementVerdict.ALLOW

    def test_enforce_chain_step3_posture_ceiling_deny(self) -> None:
        """Step 3: Posture exceeds ceiling -> DENY."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="write_file",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_WRITE,
        )
        profile = ProfileConfig(
            name="viewer",
            capabilities={"filesystem": Posture.READ_ONLY},
        )
        result = enforce(
            "write_file",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        assert result.verdict == EnforcementVerdict.DENY
        assert result.denied_by == "posture_ceiling"

    def test_enforce_all_steps_pass_allows(self) -> None:
        """All 3 steps pass -> ALLOW."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="read_file",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_ONLY,
        )
        profile = ProfileConfig(
            name="dev",
            capabilities={"filesystem": Posture.READ_WRITE},
        )
        result = enforce(
            "read_file",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        assert result.verdict == EnforcementVerdict.ALLOW


# ==============================================================================
# (f) builtin profiles use capabilities format
# ==============================================================================


class TestBuiltinProfilesCapabilities:
    """Tests that builtin profiles use capabilities format."""

    def test_all_builtins_have_capabilities_field(self) -> None:
        """Every builtin profile must have capabilities field populated."""
        for name, profile in BUILTIN_PROFILES.items():
            assert hasattr(profile, "capabilities"), f"{name} missing capabilities"
            assert len(profile.capabilities) > 0, f"{name} has empty capabilities"

    def test_read_only_builtin_has_read_only_capabilities(self) -> None:
        """read_only profile must have all read_only capabilities."""
        profile = get_builtin_profile("read_only")
        assert profile is not None
        for family, posture in profile.capabilities.items():
            assert posture == Posture.READ_ONLY, (
                f"read_only profile: {family} should be read_only, got {posture}"
            )

    def test_execute_full_has_destructive_capabilities(self) -> None:
        """execute_full profile must have all destructive capabilities."""
        profile = get_builtin_profile("execute_full")
        assert profile is not None
        for family, posture in profile.capabilities.items():
            assert posture == Posture.DESTRUCTIVE, (
                f"execute_full profile: {family} should be destructive, got {posture}"
            )

    def test_builtins_use_capabilities_only(self) -> None:
        """Builtin profiles must express authorization via capabilities."""
        # After migration removal, builtin profiles express full authorization
        # via capabilities field alone.
        for name, profile in BUILTIN_PROFILES.items():
            _ = name
            # The key test is that capabilities is populated correctly
            assert len(profile.capabilities) > 0, f"{name} has empty capabilities"


# ==============================================================================
# (g) config parser accepts capabilities only (tools key is hard-cut rejected)
# ==============================================================================


class TestConfigParserCanonicalFormat:
    """Tests for config parser: capabilities only, tools key is rejected."""

    def test_new_format_capabilities_only(self) -> None:
        """New format with capabilities only should parse cleanly."""
        raw_config = {
            "profiles": {
                "dev": {
                    "name": "dev",
                    "capabilities": {"filesystem": "read_write"},
                }
            },
            "auth": {"mode": "open"},
        }
        config = parse_config(raw_config, {})
        assert "dev" in config.profiles
        assert config.profiles["dev"].capabilities["filesystem"] == Posture.READ_WRITE

    def test_legacy_format_tools_key_rejected(self) -> None:
        """Legacy format with tools key must be rejected (hard cut)."""
        raw_config = {
            "profiles": {
                "dev": {
                    "name": "dev",
                    "tools": {"filesystem": "read_only"},
                }
            },
            "auth": {"mode": "open"},
        }
        with pytest.raises((ConfigContractError, ValidationError)):
            parse_config(raw_config, {})

    def test_tools_and_capabilities_together_rejected(self) -> None:
        """Both tools and capabilities keys must be rejected (tools is forbidden)."""
        raw_config = {
            "profiles": {
                "dev": {
                    "name": "dev",
                    "tools": {"filesystem": "read_write"},
                    "capabilities": {"filesystem": "read_write"},
                }
            },
            "auth": {"mode": "open"},
        }
        with pytest.raises((ConfigContractError, ValidationError)):
            parse_config(raw_config, {})

    def test_capabilities_mismatch_with_self_raises(self) -> None:
        """Capabilities-only configs with invalid posture values should still raise."""
        raw_config = {
            "profiles": {
                "dev": {
                    "name": "dev",
                    "capabilities": {"filesystem": "invalid_posture"},
                }
            },
            "auth": {"mode": "open"},
        }
        with pytest.raises((ConfigContractError, ValidationError)):
            parse_config(raw_config, {})


# ==============================================================================
# (h) override-ceiling invariant: tool override CANNOT elevate beyond capabilities[family]
# ==============================================================================


class TestOverrideCeilingInvariant:
    """Tests that tool overrides cannot elevate access beyond family ceiling."""

    def test_override_cannot_elevate_beyond_read_only_ceiling(self) -> None:
        """Allow override for destructive tool when ceiling is read_only -> must fail."""
        from tela.core.models import ResolvedTool

        # Profile has read_only ceiling for filesystem
        # Override tries to allow a destructive operation
        tool = ResolvedTool(
            name="rm_rf",
            server_name="fs",
            family="filesystem",
            posture=Posture.DESTRUCTIVE,
        )
        profile = ProfileConfig(
            name="reviewer",
            capabilities={"filesystem": Posture.READ_ONLY},
            tool_overrides={
                "filesystem": ProfileToolOverrides(
                    overrides={"rm_rf": EnforcementVerdict.ALLOW}
                )
            },
        )
        result = enforce(
            "rm_rf",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        # Override ALLOW for destructive operation in read_only family must be DENIED
        assert result.verdict == EnforcementVerdict.DENY, (
            "Override must not elevate beyond family ceiling"
        )

    def test_override_cannot_elevate_beyond_read_write_ceiling(self) -> None:
        """Allow override for destructive tool when ceiling is read_write -> must fail."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="nuke",
            server_name="fs",
            family="filesystem",
            posture=Posture.DESTRUCTIVE,
        )
        profile = ProfileConfig(
            name="editor",
            capabilities={"filesystem": Posture.READ_WRITE},
            tool_overrides={
                "filesystem": ProfileToolOverrides(
                    overrides={"nuke": EnforcementVerdict.ALLOW}
                )
            },
        )
        result = enforce(
            "nuke",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        # Override cannot grant destructive when ceiling is read_write
        assert result.verdict == EnforcementVerdict.DENY

    def test_override_allow_within_ceiling_permitted(self) -> None:
        """Allow override within ceiling boundaries is permitted."""
        from tela.core.models import ResolvedTool

        # read_write tool with capability ceiling read_write - override allow is fine
        tool = ResolvedTool(
            name="write_config",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_WRITE,
        )
        profile = ProfileConfig(
            name="dev",
            capabilities={"filesystem": Posture.READ_WRITE},
            tool_overrides={
                "filesystem": ProfileToolOverrides(
                    overrides={"write_config": EnforcementVerdict.ALLOW}
                )
            },
        )
        result = enforce(
            "write_config",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        assert result.verdict == EnforcementVerdict.ALLOW

    def test_override_deny_always_respected(self) -> None:
        """Override deny must always be respected."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="delete_file",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_ONLY,
        )
        profile = ProfileConfig(
            name="dev",
            capabilities={"filesystem": Posture.READ_WRITE},
            tool_overrides={
                "filesystem": ProfileToolOverrides(
                    overrides={"delete_file": EnforcementVerdict.DENY}
                )
            },
        )
        result = enforce(
            "delete_file",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        # Explicit deny override must result in DENY
        assert result.verdict == EnforcementVerdict.DENY
        assert result.denied_by == "tool_override"


# ==============================================================================
# (i) empty capabilities map denies all
# ==============================================================================


class TestEmptyCapabilities:
    """Tests that empty capabilities map denies all tool access."""

    def test_empty_capabilities_denies_all(self) -> None:
        """Profile with empty capabilities must deny all tool calls."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="read_file",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_ONLY,
        )
        profile = ProfileConfig(
            name="empty",
            capabilities={},
        )
        result = enforce(
            "read_file",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        # Empty capabilities must deny via family_admission (step 1)
        assert result.verdict == EnforcementVerdict.DENY
        assert result.denied_by == "family_admission"

    def test_empty_capabilities_with_tool_overrides_still_denies(self) -> None:
        """Empty capabilities with tool overrides still denies (no capability group admitted)."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="special",
            server_name="fs",
            family="filesystem",
            posture=Posture.READ_ONLY,
        )
        profile = ProfileConfig(
            name="empty_with_override",
            capabilities={},
            tool_overrides={
                "filesystem": ProfileToolOverrides(
                    overrides={"special": EnforcementVerdict.ALLOW}
                )
            },
        )
        result = enforce(
            "special",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        # Family not admitted -> deny before even checking overrides
        assert result.verdict == EnforcementVerdict.DENY

    def test_empty_capabilities_multiple_tools_denied(self) -> None:
        """All tools denied when capabilities empty."""
        from tela.core.models import ResolvedTool

        profile = ProfileConfig(name="no_access", capabilities={})

        tools = [
            ("read_file", "filesystem", Posture.READ_ONLY),
            ("write_file", "filesystem", Posture.READ_WRITE),
            ("shell_exec", "execution", Posture.DESTRUCTIVE),
        ]

        for tool_name, family, posture in tools:
            tool = ResolvedTool(
                name=tool_name, server_name="srv", family=family, posture=posture
            )
            result = enforce(
                tool_name,
                tool,
                profile,
                EnforcementResult(verdict=EnforcementVerdict.ALLOW),
                Posture.NONE,
            )
            assert result.verdict == EnforcementVerdict.DENY, (
                f"{tool_name} should be denied when capabilities are empty"
            )


# ==============================================================================
# Additional integration-style tests
# ==============================================================================


class TestEnforcementChainOrder:
    """Verify enforcement chain applies checks in correct order."""

    def test_family_admission_before_posture_check(self) -> None:
        """Family admission check must come before posture ceiling check."""
        from tela.core.models import ResolvedTool

        # Tool with destructive posture, but family not admitted
        tool = ResolvedTool(
            name="destroy",
            server_name="fs",
            family="destructive_tools",
            posture=Posture.DESTRUCTIVE,
        )
        # Profile has read_only for filesystem only, destructive_tools not admitted
        profile = ProfileConfig(
            name="limited",
            capabilities={"filesystem": Posture.READ_ONLY},
        )
        result = enforce(
            "destroy",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        # Must be denied by family_admission, not posture_ceiling
        assert result.verdict == EnforcementVerdict.DENY
        assert result.denied_by == "family_admission"

    def test_tool_override_before_posture_check(self) -> None:
        """Tool override check must come before posture ceiling check."""
        from tela.core.models import ResolvedTool

        tool = ResolvedTool(
            name="danger",
            server_name="fs",
            family="filesystem",
            posture=Posture.DESTRUCTIVE,
        )
        profile = ProfileConfig(
            name="dev",
            capabilities={"filesystem": Posture.READ_WRITE},
            tool_overrides={
                "filesystem": ProfileToolOverrides(
                    overrides={"danger": EnforcementVerdict.DENY}
                )
            },
        )
        result = enforce(
            "danger",
            tool,
            profile,
            EnforcementResult(verdict=EnforcementVerdict.ALLOW),
            Posture.NONE,
        )
        # Must be denied by tool_override, not posture_ceiling
        assert result.verdict == EnforcementVerdict.DENY
        assert result.denied_by == "tool_override"
