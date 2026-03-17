"""End-to-end integration tests for CLI -> gateway startup wiring.

Tests verify the full path: CLI entrypoint -> start_command -> bind_gateway_startup
-> GatewayStartupConfig with the resolved default profile from shared authority.
"""

from __future__ import annotations

import os
import tempfile

from tela.cli import _handle_start
from tela.commands.start import start_command
from tela.core.models import AuthMode, GatewayTransport
from tela.shell.gateway import bind_gateway_startup


def _write_open_mode_config(
    profiles: dict[str, bool] | None = None,
    cli_default: str | None = None,
) -> tuple[str, str | None]:
    """Write a temporary open-mode config and return (path, cli_default)."""
    if profiles is None:
        profiles = {"dev": True}

    profile_lines = []
    for name, is_default in profiles.items():
        profile_lines.append(f"    {name}:")
        profile_lines.append(f"      name: {name}")
        if is_default:
            profile_lines.append("      default: true")

    content = "profiles:\n" + "\n".join(profile_lines) + "\nauth:\n  mode: open\n"
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write(content)
    return p, cli_default


def test_full_path_cli_to_gateway_with_config_default() -> None:
    """Config default:true flows through to gateway startup config."""
    config_path, _ = _write_open_mode_config({"production": True})

    runtime_result = start_command(config_path=config_path)
    assert runtime_result.is_ok
    assert runtime_result.value is not None
    assert runtime_result.value.cli_default_profile == "production"

    gateway_result = bind_gateway_startup(runtime_result.value)
    assert gateway_result.is_ok
    assert gateway_result.value is not None
    assert gateway_result.value.default_profile == "production"
    assert gateway_result.value.transport == GatewayTransport.STDIO
    assert gateway_result.value.auth_mode == AuthMode.OPEN


def test_full_path_cli_to_gateway_with_cli_override() -> None:
    """CLI --default-profile overrides config default:true."""
    config_path, _ = _write_open_mode_config(
        {"staging": True, "production": False}
    )

    runtime_result = start_command(
        config_path=config_path, default_profile="production"
    )
    assert runtime_result.is_ok
    assert runtime_result.value is not None
    assert runtime_result.value.cli_default_profile == "production"

    gateway_result = bind_gateway_startup(runtime_result.value)
    assert gateway_result.is_ok
    assert gateway_result.value is not None
    assert gateway_result.value.default_profile == "production"


def test_full_path_cli_to_gateway_with_sse_port() -> None:
    """SSE transport when --port is provided."""
    config_path, _ = _write_open_mode_config({"dev": True})

    runtime_result = start_command(config_path=config_path, port=8080)
    assert runtime_result.is_ok
    assert runtime_result.value is not None
    assert runtime_result.value.transport == GatewayTransport.SSE
    assert runtime_result.value.port == 8080

    gateway_result = bind_gateway_startup(runtime_result.value)
    assert gateway_result.is_ok
    assert gateway_result.value is not None
    assert gateway_result.value.transport == GatewayTransport.SSE
    assert gateway_result.value.port == 8080


def test_full_path_rejects_missing_default() -> None:
    """Missing default profile is rejected at start_command level."""
    config_path, _ = _write_open_mode_config({"dev": False, "staging": False})

    runtime_result = start_command(config_path=config_path)
    assert runtime_result.is_err
    assert "OPEN_MODE_DEFAULT_PROFILE_MISSING" in (runtime_result.error or "")


def test_full_path_rejects_ambiguous_defaults() -> None:
    """Multiple default:true profiles are rejected at start_command level."""
    config_path, _ = _write_open_mode_config({"dev": True, "staging": True})

    runtime_result = start_command(config_path=config_path)
    assert runtime_result.is_err
    assert "OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS" in (runtime_result.error or "")


def test_full_path_rejects_unknown_cli_profile() -> None:
    """Unknown CLI --default-profile is rejected at start_command level."""
    config_path, _ = _write_open_mode_config({"dev": True})

    runtime_result = start_command(
        config_path=config_path, default_profile="nonexistent"
    )
    assert runtime_result.is_err
    assert "PROFILE_NOT_FOUND" in (runtime_result.error or "")


def test_gateway_binds_same_profile_as_cli_authority() -> None:
    """Gateway startup config must carry the same profile as CLI authority.

    This is the integration proof that no sibling path re-derives profile
    selection independently.
    """
    config_path, _ = _write_open_mode_config({"myprofile": True})

    runtime_result = start_command(config_path=config_path)
    assert runtime_result.is_ok and runtime_result.value is not None

    gateway_result = bind_gateway_startup(runtime_result.value)
    assert gateway_result.is_ok and gateway_result.value is not None

    # The profile in gateway config must be identical to what CLI resolved
    assert gateway_result.value.default_profile == runtime_result.value.cli_default_profile
