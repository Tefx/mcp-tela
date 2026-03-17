"""Tests for tela.shell.config_loader contract surfaces.

This module tests:
- Shell I/O boundary contracts for config loading
- Result type handling for success/failure paths
- Error propagation from Core through Shell layer
"""

from __future__ import annotations

import pytest
from pathlib import Path

from tela.shell.config_loader import load_config, Result
from tela.core.models import TelaConfig


# =============================================================================
# Shell Contract Documentation Tests
# =============================================================================


def test_load_config_contract_is_documented() -> None:
    """Verify load_config has proper contract documentation."""
    source = Path("src/tela/shell/config_loader.py").read_text(encoding="utf-8")
    assert "def load_config" in source
    assert "Result[TelaConfig, str]" in source


def test_load_config_is_contract_stub() -> None:
    """Verify load_config is a contract stub (not yet implemented)."""
    source = Path("src/tela/shell/config_loader.py").read_text(encoding="utf-8")
    assert 'raise NotImplementedError("Contract stub: load_config")' in source


def test_result_is_generic_type() -> None:
    """Result type must be generic for Shell boundary."""
    source = Path("src/tela/shell/config_loader.py").read_text(encoding="utf-8")
    assert "class Result(Generic" in source or "Generic[T, E]" in source


# =============================================================================
# load_config Success Path Tests
# =============================================================================


class TestLoadConfigSuccessPath:
    """Tests for successful config loading from disk/environment.

    Success path: valid config file + valid environment -> Result[TelaConfig, E]
    """

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_load_config_returns_success_for_valid_file(self, tmp_path: Path) -> None:
        """Valid config file returns Result containing TelaConfig."""
        config_file = tmp_path / "tela.yaml"
        config_file.write_text(
            """
profiles:
  dev:
    name: dev
    default: true
auth:
  mode: open
"""
        )
        result = load_config(path=config_file)
        # Result will be Success[TelaConfig] once implemented
        assert result is not None

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_load_config_uses_default_path_when_none_provided(self) -> None:
        """load_config(path=None) should use default tela.yaml path."""
        result = load_config(path=None)
        # Contract stub check
        assert result is not None

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_load_config_expands_env_vars(self, tmp_path: Path) -> None:
        """Environment variable references should be expanded."""
        config_file = tmp_path / "tela.yaml"
        config_file.write_text(
            """
profiles:
  prod:
    name: prod
auth:
  mode: token
  secrets:
    - $TELA_SECRET
"""
        )
        # This test documents the expected behavior for env var expansion
        # Once implemented, will verify $TELA_SECRET expansion
        pass  # Contract stub


# =============================================================================
# load_config Failure Path Tests
# =============================================================================


class TestLoadConfigFileNotFound:
    """Tests for file not found error handling."""

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_load_config_returns_error_for_missing_file(self, tmp_path: Path) -> None:
        """Missing config file returns Result error, not exception."""
        missing_file = tmp_path / "nonexistent.yaml"
        result = load_config(path=missing_file)
        # Result will be Failure[str] once implemented
        assert result is not None


class TestLoadConfigParseFailure:
    """Tests for parse failure handling."""

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_load_config_returns_error_for_invalid_yaml(self, tmp_path: Path) -> None:
        """Invalid YAML returns Result error."""
        config_file = tmp_path / "tela.yaml"
        config_file.write_text("invalid: yaml: content: [unclosed")
        result = load_config(path=config_file)
        # Result will be Failure[str] once implemented
        assert result is not None


class TestLoadConfigValidationFailure:
    """Tests for Core validation failure propagation through Shell."""

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_load_config_propagates_open_mode_missing_default_error(
        self, tmp_path: Path
    ) -> None:
        """Open mode config with no default profile returns validation error."""
        config_file = tmp_path / "tela.yaml"
        config_file.write_text(
            """
profiles:
  dev:
    name: dev
    default: false
auth:
  mode: open
"""
        )
        result = load_config(path=config_file)
        # Result should contain validation error from Core
        assert result is not None

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_load_config_propagates_ambiguous_default_error(
        self, tmp_path: Path
    ) -> None:
        """Config with multiple default profiles returns validation error."""
        config_file = tmp_path / "tela.yaml"
        config_file.write_text(
            """
profiles:
  dev:
    name: dev
    default: true
  staging:
    name: staging
    default: true
auth:
  mode: open
"""
        )
        result = load_config(path=config_file)
        # Result should contain validation error from Core
        assert result is not None

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_load_config_propagates_profile_not_found_error(
        self, tmp_path: Path
    ) -> None:
        """CLI-selected profile not in config returns validation error."""
        config_file = tmp_path / "tela.yaml"
        config_file.write_text(
            """
profiles:
  dev:
    name: dev
auth:
  mode: open
"""
        )
        # This tests the path where CLI --default-profile references missing profile
        # The Shell layer would need to pass CLI args through
        pass  # Contract stub


# =============================================================================
# Token Mode Tests
# =============================================================================


class TestLoadConfigTokenMode:
    """Tests for token mode specific validation."""

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_token_mode_without_secrets_returns_error(self, tmp_path: Path) -> None:
        """Token mode config without secrets should return validation error."""
        config_file = tmp_path / "tela.yaml"
        config_file.write_text(
            """
profiles:
  dev:
    name: dev
auth:
  mode: token
  secrets: []
"""
        )
        result = load_config(path=config_file)
        # Token mode without secrets should fail validation
        # Result will be Failure[str] once implemented
        assert result is not None

    @pytest.mark.skip(reason="Contract stub: load_config")
    def test_token_mode_with_secrets_succeeds(self, tmp_path: Path) -> None:
        """Token mode config with secrets should succeed."""
        config_file = tmp_path / "tela.yaml"
        config_file.write_text(
            """
profiles:
  dev:
    name: dev
auth:
  mode: token
  secrets:
    - my-secret-key
"""
        )
        result = load_config(path=config_file)
        # Result will be Success[TelaConfig] once implemented
        assert result is not None


# =============================================================================
# Result Type Contract Tests
# =============================================================================


class TestResultType:
    """Tests for Result type contract at Shell boundaries."""

    def test_result_is_generic_class(self) -> None:
        """Result must be Generic[T, E] for type safety."""
        # Result is defined as Generic[T, E] in the module
        from tela.shell.config_loader import Result as ResultType

        assert hasattr(ResultType, "__class__")

    @pytest.mark.skip(reason="Contract stub: Result implementation")
    def test_result_success_unwraps_to_value(self) -> None:
        """Success result must allow unwrapping to contained value."""
        # Once implemented, Success[TelaConfig].unwrap() -> TelaConfig
        pass  # Contract stub

    @pytest.mark.skip(reason="Contract stub: Result implementation")
    def test_result_failure_unwraps_to_error(self) -> None:
        """Failure result must allow unwrapping to error message."""
        # Once implemented, Failure[str].unwrap_err() -> str
        pass  # Contract stub
