"""Tests for the prebuilt profile catalog."""

from __future__ import annotations

import pytest

from tela.core.catalog import (
    BUILTIN_PROFILES,
    get_builtin_profile,
    list_builtin_profiles,
    merge_with_builtins,
)
from tela.core.models import Posture, ProfileConfig


class TestBuiltinProfiles:
    """Verify the 7 builtin profiles match INTERFACES.md specification."""

    def test_catalog_has_seven_profiles(self) -> None:
        assert len(BUILTIN_PROFILES) == 7

    def test_expected_profile_names(self) -> None:
        expected = {
            "read_only",
            "fetch_external",
            "modify_local",
            "send_external",
            "orchestrate",
            "execute_safe",
            "execute_full",
        }
        assert set(BUILTIN_PROFILES.keys()) == expected

    def test_read_only_profile(self) -> None:
        p = BUILTIN_PROFILES["read_only"]
        assert p.name == "read_only"
        assert p.default is False
        # read_only: local read only, no mutation
        for posture in p.capabilities.values():
            assert posture == Posture.READ_ONLY

    def test_fetch_external_profile(self) -> None:
        p = BUILTIN_PROFILES["fetch_external"]
        assert p.name == "fetch_external"
        assert "network" in p.capabilities

    def test_modify_local_profile(self) -> None:
        p = BUILTIN_PROFILES["modify_local"]
        assert p.name == "modify_local"
        assert p.capabilities.get("filesystem") == Posture.READ_WRITE

    def test_send_external_profile(self) -> None:
        p = BUILTIN_PROFILES["send_external"]
        assert p.name == "send_external"
        assert "network" in p.capabilities

    def test_orchestrate_profile(self) -> None:
        p = BUILTIN_PROFILES["orchestrate"]
        assert p.name == "orchestrate"
        assert "orchestration" in p.capabilities

    def test_execute_safe_profile(self) -> None:
        p = BUILTIN_PROFILES["execute_safe"]
        assert p.name == "execute_safe"
        assert "execution" in p.capabilities
        # execute_safe does NOT have destructive posture
        for posture in p.capabilities.values():
            assert posture != Posture.DESTRUCTIVE

    def test_execute_full_profile(self) -> None:
        p = BUILTIN_PROFILES["execute_full"]
        assert p.name == "execute_full"
        # execute_full has destructive posture
        for posture in p.capabilities.values():
            assert posture == Posture.DESTRUCTIVE

    def test_no_builtin_is_default(self) -> None:
        """Builtins should not be marked as default."""
        for name, profile in BUILTIN_PROFILES.items():
            assert profile.default is False, f"{name} should not be default"


class TestGetBuiltinProfile:
    def test_existing_profile(self) -> None:
        p = get_builtin_profile("read_only")
        assert p is not None
        assert p.name == "read_only"

    def test_nonexistent_profile(self) -> None:
        assert get_builtin_profile("nonexistent") is None

    def test_contract_rejects_empty_name(self) -> None:
        with pytest.raises(Exception):
            get_builtin_profile("")


class TestListBuiltinProfiles:
    def test_returns_seven(self) -> None:
        names = list_builtin_profiles()
        assert len(names) == 7

    def test_sorted(self) -> None:
        names = list_builtin_profiles()
        assert names == sorted(names)


class TestMergeWithBuiltins:
    def test_empty_user_profiles_returns_builtins(self) -> None:
        result = merge_with_builtins({})
        assert len(result) == 7
        assert "read_only" in result

    def test_user_profile_overrides_builtin(self) -> None:
        custom = ProfileConfig(name="read_only", capabilities={}, default=True)
        result = merge_with_builtins({"read_only": custom})
        assert result["read_only"].default is True
        assert result["read_only"].capabilities == {}

    def test_user_custom_profile_added(self) -> None:
        custom = ProfileConfig(name="my_profile")
        result = merge_with_builtins({"my_profile": custom})
        assert len(result) == 8
        assert "my_profile" in result

    def test_mixed_override_and_add(self) -> None:
        override = ProfileConfig(name="read_only", default=True)
        new = ProfileConfig(name="custom")
        result = merge_with_builtins({"read_only": override, "custom": new})
        assert len(result) == 8
        assert result["read_only"].default is True
        assert "custom" in result
