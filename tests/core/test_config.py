"""Regression tests for core config authority behavior."""

from __future__ import annotations

import pytest

from tela.core.config import (
    ConfigContractError,
    parse_config,
    requires_open_mode_default_resolution,
    resolve_open_mode_default_profile,
    validate_config,
)
from tela.core.models import AuthConfig, AuthMode, ProfileConfig, ServerConfig, TelaConfig


def test_requires_open_mode_default_resolution() -> None:
    assert requires_open_mode_default_resolution(AuthMode.OPEN) is True
    assert requires_open_mode_default_resolution(AuthMode.TOKEN) is False


def test_parse_config_expands_env_tokens() -> None:
    config = parse_config(
        {
            "profiles": {"dev": {"name": "dev", "default": True}},
            "auth": {"mode": "token", "secrets": ["$TOKEN_A", "${TOKEN_B}"]},
        },
        {"TOKEN_A": "a", "TOKEN_B": "b"},
    )

    assert config.auth.secrets == ["a", "b"]


def test_parse_config_raises_deterministic_error_on_invalid_shape() -> None:
    with pytest.raises(ConfigContractError) as exc_info:
        parse_config({"profiles": []}, {})

    assert exc_info.value.code == "CONFIG_PARSE_ERROR"
    assert "profiles" in exc_info.value.message


def test_resolve_open_mode_default_profile_cli_wins_last_writer() -> None:
    profiles = {
        "dev": ProfileConfig(name="dev", default=False),
        "prod": ProfileConfig(name="prod", default=True),
    }
    assert (
        resolve_open_mode_default_profile(profiles, cli_default_profile="dev") == "dev"
    )


def test_resolve_open_mode_default_profile_missing_default_rejected() -> None:
    with pytest.raises(ConfigContractError) as exc_info:
        resolve_open_mode_default_profile(
            {
                "dev": ProfileConfig(name="dev", default=False),
                "prod": ProfileConfig(name="prod", default=False),
            }
        )

    assert exc_info.value.code == "OPEN_MODE_DEFAULT_PROFILE_MISSING"


def test_resolve_open_mode_default_profile_ambiguous_rejected() -> None:
    with pytest.raises(ConfigContractError) as exc_info:
        resolve_open_mode_default_profile(
            {
                "dev": ProfileConfig(name="dev", default=True),
                "prod": ProfileConfig(name="prod", default=True),
            }
        )

    assert exc_info.value.code == "OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS"


def test_resolve_open_mode_default_profile_unknown_cli_profile_rejected() -> None:
    with pytest.raises(ConfigContractError) as exc_info:
        resolve_open_mode_default_profile(
            {"dev": ProfileConfig(name="dev", default=True)},
            cli_default_profile="missing",
        )

    assert exc_info.value.code == "PROFILE_NOT_FOUND"


def test_validate_config_open_mode_reports_missing_default() -> None:
    config = TelaConfig(
        profiles={"dev": ProfileConfig(name="dev", default=False)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )

    errors = validate_config(config)
    assert len(errors) == 1
    assert errors[0].startswith("OPEN_MODE_DEFAULT_PROFILE_MISSING:")


def test_validate_config_token_mode_requires_secrets() -> None:
    config = TelaConfig(
        profiles={"dev": ProfileConfig(name="dev", default=False)},
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[]),
    )

    errors = validate_config(config)
    assert len(errors) == 1
    assert errors[0].startswith("TOKEN_MODE_SECRETS_MISSING:")


def test_validate_config_token_mode_with_secret_is_valid() -> None:
    config = TelaConfig(
        profiles={"dev": ProfileConfig(name="dev", default=False)},
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret"]),
    )
    assert validate_config(config) == []


def test_validate_config_server_missing_transport() -> None:
    """Issue 18: servers must have either command or url."""
    config = TelaConfig(
        servers={"bad": ServerConfig(name="bad")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )
    errors = validate_config(config)
    transport_errors = [e for e in errors if "SERVER_MISSING_TRANSPORT" in e]
    assert len(transport_errors) == 1
    assert "'bad'" in transport_errors[0]


def test_validate_config_server_ambiguous_transport() -> None:
    """Issue 18: servers must not have both command and url."""
    config = TelaConfig(
        servers={"amb": ServerConfig(name="amb", command="cmd", url="http://x")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )
    errors = validate_config(config)
    transport_errors = [e for e in errors if "SERVER_AMBIGUOUS_TRANSPORT" in e]
    assert len(transport_errors) == 1
    assert "'amb'" in transport_errors[0]


def test_validate_config_server_valid_command_only() -> None:
    """Issue 18: server with only command is valid."""
    config = TelaConfig(
        servers={"ok": ServerConfig(name="ok", command="cmd")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )
    errors = validate_config(config)
    assert errors == []


def test_validate_config_server_valid_url_only() -> None:
    """Issue 18: server with only url is valid."""
    config = TelaConfig(
        servers={"ok": ServerConfig(name="ok", url="http://example.com")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )
    errors = validate_config(config)
    assert errors == []


def test_parse_config_injects_name_from_dict_keys() -> None:
    """B3: YAML key IS the server/profile name per INTERFACES.md."""
    config = parse_config(
        {
            "profiles": {"dev": {"default": True}},
            "servers": {"fs": {"command": "cmd"}},
            "auth": {"mode": "open"},
        },
        {},
    )
    assert config.profiles["dev"].name == "dev"
    assert config.servers["fs"].name == "fs"


def test_parse_config_explicit_name_still_works() -> None:
    """B3: Backward compatibility -- explicit name field is not overwritten."""
    config = parse_config(
        {
            "profiles": {"dev": {"name": "dev", "default": True}},
            "servers": {"fs": {"name": "fs", "command": "cmd"}},
            "auth": {"mode": "open"},
        },
        {},
    )
    assert config.profiles["dev"].name == "dev"
    assert config.servers["fs"].name == "fs"
