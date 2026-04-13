"""Regression tests for shell config loading and handoff behavior."""

from __future__ import annotations

from pathlib import Path

from tela.shell.config_loader import load_config
from tela.shell.result import Result


def test_result_is_generic_type() -> None:
    assert hasattr(Result, "__class__")


def test_load_config_success_open_mode_sets_resolved_authority(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "tela.yaml"
    config_file.write_text(
        """
profiles:
  dev:
    name: dev
    default: true
auth:
  mode: open
""",
        encoding="utf-8",
    )

    result = load_config(path=config_file)
    assert result.is_ok is True
    assert result.value is not None
    assert result.value.resolved_default_profile == "dev"


def test_load_config_success_token_mode(tmp_path: Path) -> None:
    config_file = tmp_path / "tela.yaml"
    config_file.write_text(
        """
profiles:
  dev:
    name: dev
auth:
  mode: token
  secrets:
    - token-value
""",
        encoding="utf-8",
    )

    result = load_config(path=config_file)
    assert result.is_ok is True
    assert result.value is not None
    assert result.value.auth.mode.value == "token"


def test_load_config_default_path_missing_file_is_explicit_error(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    result = load_config(path=None)
    assert result.is_err is True
    assert result.error is not None
    assert result.error.startswith("CONFIG_FILE_MISSING:")


def test_load_config_unknown_cli_override_profile_returns_error(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "tela.yaml"
    config_file.write_text(
        """
profiles:
  dev:
    name: dev
    default: true
auth:
  mode: open
""",
        encoding="utf-8",
    )

    result = load_config(path=config_file, default_profile="missing")
    assert result.is_err is True
    assert result.error is not None
    assert result.error.startswith("PROFILE_NOT_FOUND:")


def test_load_config_open_mode_missing_default_returns_error(tmp_path: Path) -> None:
    config_file = tmp_path / "tela.yaml"
    config_file.write_text(
        """
profiles:
  dev:
    name: dev
    default: false
auth:
  mode: open
""",
        encoding="utf-8",
    )

    result = load_config(path=config_file)
    assert result.is_err is True
    assert result.error is not None
    assert result.error.startswith("OPEN_MODE_DEFAULT_PROFILE_MISSING:")


def test_load_config_open_mode_ambiguous_default_returns_error(tmp_path: Path) -> None:
    config_file = tmp_path / "tela.yaml"
    config_file.write_text(
        """
profiles:
  dev:
    name: dev
    default: true
  prod:
    name: prod
    default: true
auth:
  mode: open
""",
        encoding="utf-8",
    )

    result = load_config(path=config_file)
    assert result.is_err is True
    assert result.error is not None
    assert result.error.startswith("OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS:")


def test_load_config_token_mode_missing_secrets_returns_error(tmp_path: Path) -> None:
    config_file = tmp_path / "tela.yaml"
    config_file.write_text(
        """
profiles:
  dev:
    name: dev
auth:
  mode: token
  secrets: []
""",
        encoding="utf-8",
    )

    result = load_config(path=config_file)
    assert result.is_err is True
    assert result.error is not None
    assert result.error.startswith("TOKEN_MODE_SECRETS_MISSING:")


def test_load_config_env_expansion_in_token_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELA_SECRET", "expanded-secret")
    config_file = tmp_path / "tela.yaml"
    config_file.write_text(
        """
profiles:
  dev:
    name: dev
auth:
  mode: token
  secrets:
    - $TELA_SECRET
""",
        encoding="utf-8",
    )

    result = load_config(path=config_file)
    assert result.is_ok is True
    assert result.value is not None
    assert result.value.auth.secrets == ["expanded-secret"]


def test_load_config_invalid_yaml_returns_parse_error(tmp_path: Path) -> None:
    config_file = tmp_path / "tela.yaml"
    config_file.write_text("profiles: [", encoding="utf-8")

    result = load_config(path=config_file)
    assert result.is_err is True
    assert result.error is not None
    assert result.error.startswith("CONFIG_PARSE_ERROR:")
