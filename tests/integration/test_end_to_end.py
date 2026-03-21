"""End-to-end integration tests for CLI -> gateway startup wiring.

Tests verify the full path: CLI entrypoint -> start_command -> bind_gateway_startup
-> GatewayStartupConfig with the resolved default profile from shared authority.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from tela.commands.start import start_command
from tela.core.models import AuthMode, GatewayTransport
from tela.shell.config_loader import load_config
from tela.shell.gateway import bind_gateway_startup, gateway_shutdown, gateway_start
from tela.shell.upstream import handle_initialize, handle_tools_call, handle_tools_list


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
    config_path, _ = _write_open_mode_config({"staging": True, "production": False})

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
    assert (
        gateway_result.value.default_profile == runtime_result.value.cli_default_profile
    )


# --- Runtime readiness integration tests ---


def test_runtime_readiness_open_mode_stdio() -> None:
    """Full path from CLI to GatewayStartupConfig proves runtime readiness.

    This exercises: CLI args -> start_command -> bind_gateway_startup
    -> GatewayStartupConfig with resolved profile and transport.
    """
    config_path, _ = _write_open_mode_config({"production": True})

    # Step 1: CLI -> start_command
    runtime_result = start_command(config_path=config_path)
    assert runtime_result.is_ok and runtime_result.value is not None

    # Step 2: start_command -> bind_gateway_startup
    gateway_result = bind_gateway_startup(runtime_result.value)
    assert gateway_result.is_ok and gateway_result.value is not None

    # Runtime readiness assertions
    gw = gateway_result.value
    assert gw.transport == GatewayTransport.STDIO
    assert gw.port is None
    assert gw.auth_mode == AuthMode.OPEN
    assert gw.default_profile == "production"


def test_runtime_readiness_open_mode_sse() -> None:
    """SSE transport runtime readiness with explicit port."""
    config_path, _ = _write_open_mode_config({"dev": True})

    runtime_result = start_command(config_path=config_path, port=3000)
    assert runtime_result.is_ok and runtime_result.value is not None

    gateway_result = bind_gateway_startup(runtime_result.value)
    assert gateway_result.is_ok and gateway_result.value is not None

    gw = gateway_result.value
    assert gw.transport == GatewayTransport.SSE
    assert gw.port == 3000
    assert gw.default_profile == "dev"


def test_runtime_readiness_cli_profile_override() -> None:
    """CLI --default-profile overrides config default in runtime readiness."""
    config_path, _ = _write_open_mode_config({"staging": True, "production": False})

    runtime_result = start_command(
        config_path=config_path, default_profile="production"
    )
    assert runtime_result.is_ok and runtime_result.value is not None

    gateway_result = bind_gateway_startup(runtime_result.value)
    assert gateway_result.is_ok and gateway_result.value is not None

    assert gateway_result.value.default_profile == "production"


def test_runtime_readiness_fail_fast_chain() -> None:
    """Config errors propagate through the full CLI -> gateway chain.

    When start_command fails, bind_gateway_startup is never reached.
    This is the fail-fast behavior specified in DESIGN.md 8.1.
    """
    config_path, _ = _write_open_mode_config({"dev": False, "staging": False})

    # start_command fails — no default profile
    runtime_result = start_command(config_path=config_path)
    assert runtime_result.is_err

    # Gateway is never reached — this is fail-fast at startup
    # (We cannot call bind_gateway_startup because runtime_result has no value)
    assert runtime_result.value is None


def test_end_to_end_real_stdio_server_enumerate_and_call() -> None:
    """Start real FastMCP downstream process and verify tela forwards tools/call."""
    server_script = (
        Path(__file__).resolve().parents[1] / "fixtures" / "fastmcp_stdio_server.py"
    )

    config_content = "\n".join(
        [
            "servers:",
            "  local_stdio:",
            "    name: local_stdio",
            f"    command: {sys.executable}",
            "    args:",
            f"      - {server_script}",
            "    default_posture: read_only",
            "profiles:",
            "  dev:",
            "    name: dev",
            "    default: true",
            "    capabilities:",
            "      local_stdio: read_only",
            "auth:",
            "  mode: open",
        ]
    )

    config_dir = tempfile.mkdtemp()
    config_path = os.path.join(config_dir, "tela.yaml")
    with open(config_path, "w") as config_file:
        config_file.write(config_content)

    runtime_result = start_command(config_path=config_path)
    assert runtime_result.is_ok
    assert runtime_result.value is not None

    loaded_config_result = load_config(
        Path(config_path),
        default_profile=runtime_result.value.cli_default_profile,
    )
    assert loaded_config_result.is_ok
    assert loaded_config_result.value is not None

    startup_result = bind_gateway_startup(
        runtime_result.value,
        config=loaded_config_result.value,
    )
    assert startup_result.is_ok
    assert startup_result.value is not None

    async def _run() -> None:
        start_result = await gateway_start(
            startup_result.value,
            tela_config=loaded_config_result.value,
        )
        assert start_result.is_ok

        try:
            initialize_result = await handle_initialize({})
            assert initialize_result.is_ok
            assert initialize_result.value is not None

            tools_result = await handle_tools_list(initialize_result.value)
            assert tools_result.is_ok
            tool_names = sorted(tool["name"] for tool in tools_result.value)
            assert "echo" in tool_names
            assert "ping" in tool_names

            call_result = await handle_tools_call(
                initialize_result.value,
                "echo",
                {"value": "forwarded through tela"},
            )
            assert call_result.is_ok
            assert call_result.value is not None
            payload = call_result.value
            assert payload["content"][0]["type"] == "text"
            assert payload["content"][0]["text"] == "forwarded through tela"
        finally:
            shutdown_result = await gateway_shutdown()
            assert shutdown_result.is_ok

    asyncio.run(_run())
