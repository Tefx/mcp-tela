"""Tests for tela.core.config contract surfaces and authority resolution.

This module tests:
- Contract documentation presence
- Error code definitions for open-mode rejection reasons
- Precedence rules for default profile resolution
- Auth mode default resolution requirements
"""

from __future__ import annotations

import pytest
from pathlib import Path

from tela.core.config import (
    ConfigContractError,
    parse_config,
    validate_config,
    resolve_open_mode_default_profile,
    requires_open_mode_default_resolution,
)
from tela.core.models import (
    AuthConfig,
    AuthMode,
    ProfileConfig,
    TelaConfig,
    SideEffectPolicy,
)


# =============================================================================
# Contract Documentation Tests (verify contract surfaces exist)
# =============================================================================


def test_open_mode_precedence_contract_is_documented() -> None:
    """Verify precedence rules are documented in Core config.py."""
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert "CLI `--default-profile` if provided." in source
    assert "Else exactly one profile with `default=True`." in source


def test_open_mode_rejection_contract_is_documented() -> None:
    """Verify rejection error codes are documented in Core config.py."""
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert 'code="OPEN_MODE_DEFAULT_PROFILE_MISSING"' in source
    assert 'code="OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS"' in source


def test_profile_not_found_error_code_is_documented() -> None:
    """Verify PROFILE_NOT_FOUND error code is documented for CLI-selected missing profile."""
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert 'code="PROFILE_NOT_FOUND"' in source


def test_core_contract_surfaces_have_pre_post_and_doctest_placeholders() -> None:
    """Verify all Core config functions have @pre/@post contracts and doctests."""
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert "@pre(" in source
    assert "@post(" in source
    assert ">>> parse_config(" in source
    assert ">>> validate_config(" in source
    assert ">>> resolve_open_mode_default_profile(" in source
    assert ">>> requires_open_mode_default_resolution(" in source


def test_contract_surfaces_are_stubs_only() -> None:
    """Verify Core config functions are contract stubs (not yet implemented)."""
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert 'raise NotImplementedError("Contract stub: parse_config")' in source
    assert 'raise NotImplementedError("Contract stub: validate_config")' in source
    assert (
        'raise NotImplementedError("Contract stub: resolve_open_mode_default_profile")'
        in source
    )
    assert (
        'raise NotImplementedError("Contract stub: requires_open_mode_default_resolution")'
        in source
    )


# =============================================================================
# ConfigContractError Tests
# =============================================================================


def test_config_contract_error_has_code_and_message() -> None:
    """ConfigContractError must carry stable code and human message."""
    error = ConfigContractError(code="TEST_ERROR", message="Test error message")
    assert error.code == "TEST_ERROR"
    assert error.message == "Test error message"


def test_config_contract_error_is_frozen_dataclass() -> None:
    """ConfigContractError must be immutable (frozen dataclass)."""
    error = ConfigContractError(code="TEST", message="test")
    with pytest.raises(Exception):  # FrozenInstanceError is a subclass
        error.code = "MODIFIED"  # type: ignore[misc]


def test_config_contract_error_is_exception_subclass() -> None:
    """ConfigContractError must be raisable as an Exception."""
    error = ConfigContractError(code="TEST", message="test")
    assert isinstance(error, Exception)


# =============================================================================
# requires_open_mode_default_resolution Tests
# =============================================================================


class TestRequiresOpenModeDefaultResolution:
    """Tests for requires_open_mode_default_resolution contract helper.

    Contract: Returns True only when auth_mode is AuthMode.OPEN.
    """

    @pytest.mark.skip(reason="Contract stub: requires_open_mode_default_resolution")
    def test_returns_true_for_open_mode(self) -> None:
        """OPEN auth mode requires default profile resolution."""
        assert requires_open_mode_default_resolution(AuthMode.OPEN) is True

    @pytest.mark.skip(reason="Contract stub: requires_open_mode_default_resolution")
    def test_returns_false_for_token_mode(self) -> None:
        """TOKEN auth mode does not require default profile resolution."""
        assert requires_open_mode_default_resolution(AuthMode.TOKEN) is False


# =============================================================================
# resolve_open_mode_default_profile Tests (Precedence & Rejection)
# =============================================================================


class TestResolveOpenModeDefaultProfilePrecedence:
    """Tests for CLI precedence over config default in open mode.

    Precedence contract:
    1. CLI `--default-profile` if provided.
    2. Else exactly one profile with `default=True`.
    """

    @pytest.fixture
    def profiles_single_default(self) -> dict[str, ProfileConfig]:
        """Profile config with exactly one default profile."""
        return {
            "dev": ProfileConfig(name="dev", default=False),
            "prod": ProfileConfig(name="prod", default=True),
        }

    @pytest.fixture
    def profiles_no_default(self) -> dict[str, ProfileConfig]:
        """Profile config with no default profile."""
        return {
            "dev": ProfileConfig(name="dev", default=False),
            "prod": ProfileConfig(name="prod", default=False),
        }

    @pytest.fixture
    def profiles_multiple_defaults(self) -> dict[str, ProfileConfig]:
        """Profile config with multiple default profiles (ambiguous)."""
        return {
            "dev": ProfileConfig(name="dev", default=True),
            "prod": ProfileConfig(name="prod", default=True),
        }

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_cli_default_profile_wins_over_config_default(
        self, profiles_single_default: dict[str, ProfileConfig]
    ) -> None:
        """--default-profile CLI arg takes precedence over ProfileConfig.default=True."""
        # Config has prod as default, but CLI requests dev
        result = resolve_open_mode_default_profile(
            profiles_single_default, cli_default_profile="dev"
        )
        assert result == "dev"

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_config_default_used_when_no_cli_override(
        self, profiles_single_default: dict[str, ProfileConfig]
    ) -> None:
        """ProfileConfig.default=True is used when --default-profile not provided."""
        result = resolve_open_mode_default_profile(
            profiles_single_default, cli_default_profile=None
        )
        assert result == "prod"

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_cli_default_profile_resolved_even_if_not_marked_default(
        self, profiles_no_default: dict[str, ProfileConfig]
    ) -> None:
        """CLI --default-profile works even for non-default-marked profile."""
        result = resolve_open_mode_default_profile(
            profiles_no_default, cli_default_profile="dev"
        )
        assert result == "dev"


class TestResolveOpenModeDefaultProfileCliNotFound:
    """Tests for PROFILE_NOT_FOUND rejection when CLI profile missing from config."""

    @pytest.fixture
    def profiles_available(self) -> dict[str, ProfileConfig]:
        """Profile config with available profiles."""
        return {
            "dev": ProfileConfig(name="dev", default=True),
            "prod": ProfileConfig(name="prod", default=False),
        }

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_raises_profile_not_found_when_cli_profile_missing(
        self, profiles_available: dict[str, ProfileConfig]
    ) -> None:
        """CLI-selected profile missing from config raises PROFILE_NOT_FOUND."""
        with pytest.raises(ConfigContractError) as exc_info:
            resolve_open_mode_default_profile(
                profiles_available, cli_default_profile="staging"
            )
        assert exc_info.value.code == "PROFILE_NOT_FOUND"

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_profile_not_found_message_names_missing_profile(
        self, profiles_available: dict[str, ProfileConfig]
    ) -> None:
        """Error message includes the missing profile name for discoverability."""
        with pytest.raises(ConfigContractError) as exc_info:
            resolve_open_mode_default_profile(
                profiles_available, cli_default_profile="staging"
            )
        assert "staging" in exc_info.value.message


class TestResolveOpenModeDefaultProfileMissing:
    """Tests for OPEN_MODE_DEFAULT_PROFILE_MISSING rejection."""

    @pytest.fixture
    def profiles_no_default(self) -> dict[str, ProfileConfig]:
        """Profile config with no profiles marked as default."""
        return {
            "dev": ProfileConfig(name="dev", default=False),
            "prod": ProfileConfig(name="prod", default=False),
        }

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_raises_missing_when_no_default_and_no_cli(
        self, profiles_no_default: dict[str, ProfileConfig]
    ) -> None:
        """Open mode with no default profile and no CLI override raises MISSING error."""
        with pytest.raises(ConfigContractError) as exc_info:
            resolve_open_mode_default_profile(
                profiles_no_default, cli_default_profile=None
            )
        assert exc_info.value.code == "OPEN_MODE_DEFAULT_PROFILE_MISSING"

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_missing_error_message_indicates_no_default_available(
        self, profiles_no_default: dict[str, ProfileConfig]
    ) -> None:
        """Error message makes rejection reason discoverable."""
        with pytest.raises(ConfigContractError) as exc_info:
            resolve_open_mode_default_profile(
                profiles_no_default, cli_default_profile=None
            )
        assert "default" in exc_info.value.message.lower()


class TestResolveOpenModeDefaultProfileAmbiguous:
    """Tests for OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS rejection."""

    @pytest.fixture
    def profiles_multiple_defaults(self) -> dict[str, ProfileConfig]:
        """Profile config with multiple profiles marked as default."""
        return {
            "dev": ProfileConfig(name="dev", default=True),
            "staging": ProfileConfig(name="staging", default=True),
            "prod": ProfileConfig(name="prod", default=False),
        }

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_raises_ambiguous_when_multiple_defaults_and_no_cli(
        self, profiles_multiple_defaults: dict[str, ProfileConfig]
    ) -> None:
        """Open mode with multiple default profiles raises AMBIGUOUS error."""
        with pytest.raises(ConfigContractError) as exc_info:
            resolve_open_mode_default_profile(
                profiles_multiple_defaults, cli_default_profile=None
            )
        assert exc_info.value.code == "OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS"

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_ambiguous_error_message_lists_conflicting_profiles(
        self, profiles_multiple_defaults: dict[str, ProfileConfig]
    ) -> None:
        """Error message lists conflicting profiles for debugging."""
        with pytest.raises(ConfigContractError) as exc_info:
            resolve_open_mode_default_profile(
                profiles_multiple_defaults, cli_default_profile=None
            )
        message = exc_info.value.message.lower()
        assert "dev" in message or "staging" in message

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_cli_override_resolves_ambiguity(
        self, profiles_multiple_defaults: dict[str, ProfileConfig]
    ) -> None:
        """CLI --default-profile resolves ambiguity from multiple defaults."""
        result = resolve_open_mode_default_profile(
            profiles_multiple_defaults, cli_default_profile="prod"
        )
        assert result == "prod"


class TestResolveOpenModeDefaultProfileEmptyProfiles:
    """Tests for empty profile edge cases."""

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_empty_profiles_no_cli_raises_missing(self) -> None:
        """Empty profiles dict with no CLI raises MISSING error."""
        with pytest.raises(ConfigContractError) as exc_info:
            resolve_open_mode_default_profile({}, cli_default_profile=None)
        assert exc_info.value.code == "OPEN_MODE_DEFAULT_PROFILE_MISSING"

    @pytest.mark.skip(reason="Contract stub: resolve_open_mode_default_profile")
    def test_empty_profiles_with_cli_raises_not_found(self) -> None:
        """Empty profiles dict with CLI override raises PROFILE_NOT_FOUND."""
        with pytest.raises(ConfigContractError) as exc_info:
            resolve_open_mode_default_profile({}, cli_default_profile="any")
        assert exc_info.value.code == "PROFILE_NOT_FOUND"


# =============================================================================
# validate_config Tests
# =============================================================================


class TestValidateConfig:
    """Tests for validate_config cross-field constraint validation."""

    @pytest.fixture
    def valid_open_mode_config(self) -> TelaConfig:
        """Valid config for open mode with single default profile."""
        return TelaConfig(
            profiles={
                "dev": ProfileConfig(name="dev", default=True),
            },
            auth=AuthConfig(mode=AuthMode.OPEN),
        )

    @pytest.fixture
    def valid_token_mode_config(self) -> TelaConfig:
        """Valid config for token mode with secrets."""
        return TelaConfig(
            profiles={
                "dev": ProfileConfig(name="dev"),
            },
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret1"]),
        )

    @pytest.mark.skip(reason="Contract stub: validate_config")
    def test_valid_config_returns_empty_error_list(
        self, valid_open_mode_config: TelaConfig
    ) -> None:
        """Valid config should return empty list of errors."""
        result = validate_config(valid_open_mode_config)
        assert result == []

    @pytest.mark.skip(reason="Contract stub: validate_config")
    def test_open_mode_missing_default_returns_errors(
        self,
    ) -> None:
        """Open mode config without default profile should return error."""
        config = TelaConfig(
            profiles={"dev": ProfileConfig(name="dev", default=False)},
            auth=AuthConfig(mode=AuthMode.OPEN),
        )
        errors = validate_config(config)
        assert len(errors) > 0
        assert any(
            "default" in str(e).lower() or "missing" in str(e).lower() for e in errors
        )


# =============================================================================
# parse_config Tests
# =============================================================================


class TestParseConfig:
    """Tests for parse_config raw to model transformation."""

    @pytest.mark.skip(reason="Contract stub: parse_config")
    def test_parse_minimal_config(self) -> None:
        """Parse minimal valid config into TelaConfig."""
        raw: dict[str, object] = {"profiles": {}, "auth": {"mode": "token"}}
        result = parse_config(raw, {})
        assert isinstance(result, TelaConfig)

    @pytest.mark.skip(reason="Contract stub: parse_config")
    def test_parse_config_with_profiles(self) -> None:
        """Parse config with profiles into TelaConfig."""
        raw: dict[str, object] = {
            "profiles": {
                "dev": {"name": "dev", "default": True},
            },
            "auth": {"mode": "open"},
        }
        env_vars: dict[str, str] = {}
        result = parse_config(raw, env_vars)
        assert "dev" in result.profiles
        assert result.profiles["dev"].default is True
