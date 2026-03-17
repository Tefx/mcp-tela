"""Runtime lifecycle tests for gateway startup, shutdown, and status.

Tests cover:
- Startup fail-fast on config errors (missing file, invalid YAML)
- Startup configuration shapes for stdio and SSE transports
- Gateway lifecycle stub contracts
- GatewayStartupConfig immutability and field access
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from tela.commands.start import start_command
from tela.core.models import AuthMode, GatewayStatus, GatewayTransport
from tela.shell.gateway import (
    GatewayStartupConfig,
    bind_gateway_startup,
    gateway_connections,
    gateway_shutdown,
    gateway_start,
    gateway_status,
)


# --- GatewayStartupConfig model tests ---


def test_gateway_startup_config_stdio_defaults() -> None:
    """Stdio transport config carries no port."""
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    assert config.transport == GatewayTransport.STDIO
    assert config.port is None
    assert config.auth_mode == AuthMode.OPEN
    assert config.default_profile == "dev"


def test_gateway_startup_config_sse_carries_port() -> None:
    """SSE transport config carries the port."""
    config = GatewayStartupConfig(
        transport=GatewayTransport.SSE,
        port=8080,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    assert config.transport == GatewayTransport.SSE
    assert config.port == 8080


def test_gateway_startup_config_is_frozen() -> None:
    """GatewayStartupConfig must be immutable."""
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    with pytest.raises(AttributeError):
        config.transport = GatewayTransport.SSE  # type: ignore[misc]


def test_gateway_startup_config_token_mode() -> None:
    """Token mode config carries no default profile."""
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.TOKEN,
        default_profile=None,
    )
    assert config.auth_mode == AuthMode.TOKEN
    assert config.default_profile is None


# --- Startup fail-fast tests ---


def test_startup_fails_on_missing_config_file() -> None:
    """start_command must fail if the config file does not exist."""
    result = start_command(config_path="/nonexistent/tela.yaml")
    assert result.is_err
    assert result.error is not None


def test_startup_fails_on_invalid_yaml_shape() -> None:
    """start_command must fail on unparseable config shape."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write("profiles: not_a_dict\n")
    result = start_command(config_path=p)
    assert result.is_err


def test_startup_fails_on_open_mode_no_default() -> None:
    """Open mode with no default profile must fail at startup."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write("profiles:\n  dev:\n    name: dev\nauth:\n  mode: open\n")
    result = start_command(config_path=p)
    assert result.is_err
    assert "OPEN_MODE_DEFAULT_PROFILE_MISSING" in (result.error or "")


def test_startup_fails_on_open_mode_ambiguous_defaults() -> None:
    """Open mode with multiple default:true profiles must fail."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write(
            "profiles:\n"
            "  dev:\n    name: dev\n    default: true\n"
            "  staging:\n    name: staging\n    default: true\n"
            "auth:\n  mode: open\n"
        )
    result = start_command(config_path=p)
    assert result.is_err
    assert "OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS" in (result.error or "")


def test_startup_fails_on_unknown_cli_profile() -> None:
    """CLI --default-profile referencing unknown profile must fail."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write("profiles:\n  dev:\n    name: dev\n    default: true\nauth:\n  mode: open\n")
    result = start_command(config_path=p, default_profile="nonexistent")
    assert result.is_err
    assert "PROFILE_NOT_FOUND" in (result.error or "")


# --- bind_gateway_startup fail-fast ---


def test_bind_gateway_startup_fails_on_missing_config() -> None:
    """bind_gateway_startup must fail if config file is missing."""
    from tela.core.models import RuntimeBindingContract

    runtime = RuntimeBindingContract(
        config_path="/nonexistent/tela.yaml",
        transport=GatewayTransport.STDIO,
        port=None,
        cli_default_profile=None,
    )
    result = bind_gateway_startup(runtime)
    assert result.is_err


# --- GatewayStatus model tests ---


def test_gateway_status_model_fields() -> None:
    """GatewayStatus must expose all required runtime fields."""
    status = GatewayStatus(
        uptime_seconds=120.5,
        server_count=3,
        connected_servers=["srv1", "srv2", "srv3"],
        active_connections=2,
        profile_count=4,
        total_tool_calls=100,
    )
    assert status.uptime_seconds == 120.5
    assert status.server_count == 3
    assert len(status.connected_servers) == 3
    assert status.active_connections == 2
    assert status.profile_count == 4
    assert status.total_tool_calls == 100


def test_gateway_status_model_defaults() -> None:
    """GatewayStatus connected_servers defaults to empty list."""
    status = GatewayStatus(
        uptime_seconds=0,
        server_count=0,
        active_connections=0,
        profile_count=0,
        total_tool_calls=0,
    )
    assert status.connected_servers == []


# --- Lifecycle stub contracts (preserved from contract phase) ---


def test_gateway_start_is_contract_stub() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(gateway_start(config))


def test_gateway_shutdown_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(gateway_shutdown())


def test_gateway_status_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        gateway_status()


def test_gateway_connections_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        gateway_connections()
