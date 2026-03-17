"""Runtime lifecycle tests for gateway startup, shutdown, and status."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from tela.commands.start import start_command
from tela.core.models import (
    AuthMode,
    GatewayStatus,
    GatewayTransport,
    ServerConfig,
    TelaConfig,
)
from tela.shell.gateway import (
    GatewayStartupConfig,
    bind_gateway_startup,
    gateway_connections,
    gateway_shutdown,
    gateway_start,
    gateway_status,
    get_runtime,
)


# --- GatewayStartupConfig model tests ---

def test_gateway_startup_config_stdio_defaults() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO, port=None,
        auth_mode=AuthMode.OPEN, default_profile="dev",
    )
    assert config.transport == GatewayTransport.STDIO
    assert config.port is None

def test_gateway_startup_config_sse_carries_port() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.SSE, port=8080,
        auth_mode=AuthMode.OPEN, default_profile="dev",
    )
    assert config.transport == GatewayTransport.SSE
    assert config.port == 8080

def test_gateway_startup_config_is_frozen() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO, port=None,
        auth_mode=AuthMode.OPEN, default_profile="dev",
    )
    with pytest.raises(AttributeError):
        config.transport = GatewayTransport.SSE  # type: ignore[misc]

def test_gateway_startup_config_token_mode() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO, port=None,
        auth_mode=AuthMode.TOKEN, default_profile=None,
    )
    assert config.auth_mode == AuthMode.TOKEN


# --- Startup fail-fast tests ---

def test_startup_fails_on_missing_config_file() -> None:
    result = start_command(config_path="/nonexistent/tela.yaml")
    assert result.is_err

def test_startup_fails_on_invalid_yaml_shape() -> None:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write("profiles: not_a_dict\n")
    result = start_command(config_path=p)
    assert result.is_err

def test_startup_fails_on_open_mode_no_default() -> None:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write("profiles:\n  dev:\n    name: dev\nauth:\n  mode: open\n")
    result = start_command(config_path=p)
    assert result.is_err

def test_startup_fails_on_open_mode_ambiguous_defaults() -> None:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write("profiles:\n  dev:\n    name: dev\n    default: true\n  staging:\n    name: staging\n    default: true\nauth:\n  mode: open\n")
    result = start_command(config_path=p)
    assert result.is_err

def test_startup_fails_on_unknown_cli_profile() -> None:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write("profiles:\n  dev:\n    name: dev\n    default: true\nauth:\n  mode: open\n")
    result = start_command(config_path=p, default_profile="nonexistent")
    assert result.is_err

def test_bind_gateway_startup_fails_on_missing_config() -> None:
    from tela.core.models import RuntimeBindingContract
    runtime = RuntimeBindingContract(
        config_path="/nonexistent/tela.yaml",
        transport=GatewayTransport.STDIO, port=None, cli_default_profile=None,
    )
    result = bind_gateway_startup(runtime)
    assert result.is_err


# --- GatewayStatus model tests ---

def test_gateway_status_model_fields() -> None:
    status = GatewayStatus(
        uptime_seconds=120.5, server_count=3,
        connected_servers=["srv1", "srv2", "srv3"],
        active_connections=2, profile_count=4, total_tool_calls=100,
    )
    assert status.uptime_seconds == 120.5
    assert status.server_count == 3

def test_gateway_status_model_defaults() -> None:
    status = GatewayStatus(
        uptime_seconds=0, server_count=0, active_connections=0,
        profile_count=0, total_tool_calls=0,
    )
    assert status.connected_servers == []


# --- Gateway lifecycle (start/shutdown/status/connections) ---

def test_gateway_start_succeeds_with_empty_config() -> None:
    """gateway_start with no servers succeeds."""
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO, port=None,
        auth_mode=AuthMode.OPEN, default_profile="dev",
    )
    result = asyncio.run(gateway_start(config, tela_config=TelaConfig()))
    assert result.is_ok
    assert get_runtime().running is True
    # Cleanup
    asyncio.run(gateway_shutdown())

def test_gateway_start_with_servers_and_tools() -> None:
    """gateway_start connects downstreams and registers tools."""
    tela = TelaConfig(
        servers={"fs": ServerConfig(name="fs", command="cmd")},
    )
    tool_lists = {"fs": [{"name": "read_file", "inputSchema": {}}]}
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO, port=None,
        auth_mode=AuthMode.OPEN, default_profile="dev",
    )
    result = asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    assert result.is_ok

    status = asyncio.run(gateway_status())
    assert status.server_count == 1
    assert "fs" in status.connected_servers

    # Cleanup
    asyncio.run(gateway_shutdown())

def test_gateway_start_fails_on_tool_conflict() -> None:
    """gateway_start fails fast on tool name conflicts."""
    tela = TelaConfig(
        servers={
            "fs1": ServerConfig(name="fs1", command="cmd1"),
            "fs2": ServerConfig(name="fs2", command="cmd2"),
        },
    )
    tool_lists = {
        "fs1": [{"name": "read_file", "inputSchema": {}}],
        "fs2": [{"name": "read_file", "inputSchema": {}}],
    }
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO, port=None,
        auth_mode=AuthMode.OPEN, default_profile="dev",
    )
    result = asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    assert result.is_err
    assert "TOOL_CONFLICT" in (result.error or "")

def test_gateway_shutdown_clears_state() -> None:
    """gateway_shutdown clears runtime state."""
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO, port=None,
        auth_mode=AuthMode.OPEN, default_profile="dev",
    )
    asyncio.run(gateway_start(config, tela_config=TelaConfig()))
    assert get_runtime().running is True

    result = asyncio.run(gateway_shutdown())
    assert result.is_ok
    assert get_runtime().running is False

def test_gateway_status_after_start() -> None:
    """gateway_status reflects runtime state after start."""
    tela = TelaConfig(
        servers={"srv": ServerConfig(name="srv", command="cmd")},
        profiles={"dev": __import__("tela.core.models", fromlist=["ProfileConfig"]).ProfileConfig(name="dev")},
    )
    tool_lists = {"srv": [{"name": "tool1", "inputSchema": {}}]}
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO, port=None,
        auth_mode=AuthMode.OPEN, default_profile="dev",
    )
    asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))

    status = asyncio.run(gateway_status())
    assert status.server_count == 1
    assert status.profile_count == 1
    assert status.active_connections == 0
    assert status.total_tool_calls == 0

    asyncio.run(gateway_shutdown())

def test_gateway_connections_empty_initially() -> None:
    """gateway_connections returns empty list initially."""
    asyncio.run(gateway_start(
        GatewayStartupConfig(
            transport=GatewayTransport.STDIO, port=None,
            auth_mode=AuthMode.OPEN, default_profile="dev",
        ),
        tela_config=TelaConfig(),
    ))
    assert asyncio.run(gateway_connections()) == []
    asyncio.run(gateway_shutdown())
