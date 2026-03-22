"""Runtime lifecycle tests for gateway startup, shutdown, and status."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import types
from starlette.testclient import TestClient

from tela.core.models import (
    AuthConfig,
    AuthMode,
    GatewayStatus,
    GatewayTransport,
    Posture,
    ProfileConfig,
    ServerConfig,
    TelaConfig,
)
from tela.commands.start import start_command
from tela.shell.gateway import (
    GatewayStartupConfig,
    bind_gateway_startup,
    gateway_reload_config_from_disk,
    gateway_connections,
    gateway_shutdown,
    gateway_start,
    gateway_status,
    get_runtime,
)
from tela.shell.config_loader import Result


# --- GatewayStartupConfig model tests ---


def test_gateway_startup_config_stdio_defaults() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    assert config.transport == GatewayTransport.STDIO
    assert config.port is None


def test_gateway_startup_config_sse_carries_port() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.SSE,
        port=8080,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    assert config.transport == GatewayTransport.SSE
    assert config.port == 8080


def test_gateway_startup_config_http_carries_port() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.HTTP,
        port=8080,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    assert config.transport == GatewayTransport.HTTP
    assert config.port == 8080


def test_gateway_startup_config_is_frozen() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    with pytest.raises(AttributeError):
        config.transport = GatewayTransport.SSE  # type: ignore[misc]


def test_gateway_startup_config_token_mode() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.TOKEN,
        default_profile=None,
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
        f.write(
            "profiles:\n  dev:\n    name: dev\n    default: true\n  staging:\n    name: staging\n    default: true\nauth:\n  mode: open\n"
        )
    result = start_command(config_path=p)
    assert result.is_err


def test_startup_fails_on_unknown_cli_profile() -> None:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "tela.yaml")
    with open(p, "w") as f:
        f.write(
            "profiles:\n  dev:\n    name: dev\n    default: true\nauth:\n  mode: open\n"
        )
    result = start_command(config_path=p, default_profile="nonexistent")
    assert result.is_err


def test_bind_gateway_startup_fails_on_missing_config() -> None:
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


def test_gateway_status_model_defaults() -> None:
    status = GatewayStatus(
        uptime_seconds=0,
        server_count=0,
        active_connections=0,
        profile_count=0,
        total_tool_calls=0,
    )
    assert status.connected_servers == []


# --- Gateway lifecycle (start/shutdown/status/connections) ---


def test_gateway_start_succeeds_with_empty_config() -> None:
    """gateway_start with no servers succeeds."""
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    result = asyncio.run(gateway_start(config, tela_config=TelaConfig()))
    assert result.is_ok
    assert get_runtime().running is True
    # Cleanup
    asyncio.run(gateway_shutdown())


def test_gateway_start_sets_and_clears_reload_notify_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gateway_start wires reload notify callback, gateway_shutdown clears it."""

    callbacks: list[object | None] = []

    def _capture_set_notify_callback(callback: object | None) -> None:
        callbacks.append(callback)

    monkeypatch.setattr(
        "tela.shell.gateway._set_reload_notify_callback",
        _capture_set_notify_callback,
    )

    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )

    start_result = asyncio.run(gateway_start(config, tela_config=TelaConfig()))
    assert start_result.is_ok
    assert len(callbacks) >= 1
    assert callable(callbacks[0])

    shutdown_result = asyncio.run(gateway_shutdown())
    assert shutdown_result.is_ok
    assert callbacks[-1] is None


def test_gateway_reload_config_from_disk_routes_through_reload_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Production reload entrypoint loads file and forwards to on_config_changed."""

    config_path = tmp_path / "tela.yaml"
    config_path.write_text(
        "profiles:\n  dev:\n    name: dev\n    default: true\nauth:\n  mode: open\n",
        encoding="utf-8",
    )

    captured: list[TelaConfig] = []

    async def _fake_on_config_changed(new_config: TelaConfig):
        captured.append(new_config)
        from tela.shell.config_loader import Result

        return Result(value=None)

    monkeypatch.setattr(
        "tela.shell.reload.on_config_changed",
        _fake_on_config_changed,
    )

    result = asyncio.run(
        gateway_reload_config_from_disk(
            config_path=config_path,
            default_profile=None,
        )
    )

    assert result.is_ok
    assert len(captured) == 1
    assert captured[0].resolved_default_profile == "dev"


def test_gateway_start_with_servers_and_tools() -> None:
    """gateway_start connects downstreams and registers tools."""
    tela = TelaConfig(
        servers={"fs": ServerConfig(name="fs", command="cmd")},
    )
    tool_lists = {"fs": [{"name": "read_file", "inputSchema": {}}]}
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    result = asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    assert result.is_ok

    status = asyncio.run(gateway_status())
    assert status.value.server_count == 1
    assert "fs" in status.value.connected_servers

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
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    result = asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    assert result.is_err
    assert "TOOL_CONFLICT" in (result.error or "")


def test_gateway_shutdown_clears_state() -> None:
    """gateway_shutdown clears runtime state."""
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
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
        profiles={
            "dev": __import__(
                "tela.core.models", fromlist=["ProfileConfig"]
            ).ProfileConfig(name="dev")
        },
    )
    tool_lists = {"srv": [{"name": "tool1", "inputSchema": {}}]}
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))

    status = asyncio.run(gateway_status())
    assert status.value.server_count == 1
    assert status.value.profile_count == 1
    assert status.value.active_connections == 0
    assert status.value.total_tool_calls == 0

    asyncio.run(gateway_shutdown())


def test_gateway_connections_empty_initially() -> None:
    """gateway_connections returns empty list initially."""
    asyncio.run(
        gateway_start(
            GatewayStartupConfig(
                transport=GatewayTransport.STDIO,
                port=None,
                auth_mode=AuthMode.OPEN,
                default_profile="dev",
            ),
            tela_config=TelaConfig(),
        )
    )
    assert asyncio.run(gateway_connections()).value == []
    asyncio.run(gateway_shutdown())


def test_fastmcp_tools_list_returns_filtered_tools() -> None:
    """Low-level tools/list handler returns profile-filtered tools."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                ),
                "shell": ServerConfig(
                    name="shell",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                ),
            },
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
                    capabilities={"fs": Posture.READ_ONLY},
                )
            },
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
        )
        tool_lists = {
            "fs": [
                {"name": "read_file", "inputSchema": {}},
            ],
            "shell": [{"name": "exec", "inputSchema": {}}],
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        try:
            server = get_runtime().upstream_server
            assert server is not None
            handler = server._mcp_server.request_handlers[types.ListToolsRequest]
            response = await handler(types.ListToolsRequest())

            names = sorted(tool.name for tool in response.root.tools)
            assert names == ["read_file"]
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_fastmcp_tools_call_enforces_and_strips_meta_real_downstream() -> None:
    """tools/call handler enforces through upstream and strips _meta before forwarding."""

    async def _scenario() -> None:
        fixture_server = (
            Path(__file__).resolve().parents[1] / "fixtures" / "fastmcp_stdio_server.py"
        )
        tela = TelaConfig(
            servers={
                "stdio": ServerConfig(
                    name="stdio",
                    command=sys.executable,
                    args=[str(fixture_server)],
                    default_posture=Posture.READ_WRITE,
                )
            },
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
                    capabilities={"stdio": Posture.READ_WRITE},
                )
            },
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
        )
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        start_result = await gateway_start(config, tela_config=tela)
        assert start_result.is_ok
        try:
            server = get_runtime().upstream_server
            assert server is not None

            call_handler = server._mcp_server.request_handlers[types.CallToolRequest]
            response = await call_handler(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="echo",
                        arguments={
                            "value": "hello",
                            "_meta": {"trace_id": "tr-1"},
                        },
                    )
                )
            )

            assert response.root.isError is False
            assert response.root.structuredContent is not None
            assert response.root.structuredContent["structuredContent"] == {
                "result": "hello"
            }
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_fastmcp_profiles_resource_registered() -> None:
    """tela.profiles MCP resource is registered and readable."""

    tela = TelaConfig(
        profiles={
            "dev": ProfileConfig(
                name="dev",
                default=True,
                capabilities={"fs": Posture.READ_ONLY},
            )
        },
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="dev",
    )
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists={}))
    try:
        server = get_runtime().upstream_server
        assert server is not None

        resources = asyncio.run(server.list_resources())
        assert any(resource.name == "tela.profiles" for resource in resources)

        contents = asyncio.run(server.read_resource("tela://profiles"))
        payload = json.loads(contents[0].content)
        assert payload[0]["profile_name"] == "dev"
    finally:
        asyncio.run(gateway_shutdown())


def test_fastmcp_tools_call_denies_unadmitted_family() -> None:
    """tools/call denial comes from enforcement chain before forwarding."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={"shell": ServerConfig(name="shell", command="cmd")},
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
                    capabilities={"fs": Posture.READ_WRITE},
                )
            },
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
        )
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(
            config,
            tela_config=tela,
            tool_lists={"shell": [{"name": "exec", "inputSchema": {}}]},
        )
        try:
            server = get_runtime().upstream_server
            assert server is not None
            call_handler = server._mcp_server.request_handlers[types.CallToolRequest]
            response = await call_handler(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(name="exec", arguments={}),
                )
            )

            assert response.root.isError is True
            assert "AUTHZ_DENY" in response.root.content[0].text
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_streamable_http_surface_mounts_liveness_routes_and_auth_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mounted HTTP surface serves liveness endpoints with bearer boundary."""
    _ = monkeypatch

    async def _scenario() -> None:
        config = GatewayStartupConfig(
            transport=GatewayTransport.HTTP,
            port=8401,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )
        start_result = await gateway_start(
            config,
            tela_config=TelaConfig(),
            expected_bearer_token="mounted-token",
        )
        assert start_result.is_ok

        try:
            server = get_runtime().upstream_server
            assert server is not None
            app = server.streamable_http_app()

            with TestClient(app) as client:
                health = client.get("/health")
                assert health.status_code == 200
                assert health.json()["status"] == "ok"

                unauthorized_status = client.get("/status")
                assert unauthorized_status.status_code == 401

                unauthorized_connect = client.post(
                    "/connect", json={"connection_id": "conn-1"}
                )
                assert unauthorized_connect.status_code == 401

                auth_headers = {"Authorization": "Bearer mounted-token"}

                status = client.get("/status", headers=auth_headers)
                assert status.status_code == 200

                connect = client.post(
                    "/connect",
                    headers=auth_headers,
                    json={"connection_id": "conn-1"},
                )
                assert connect.status_code == 200

                disconnect = client.post(
                    "/disconnect",
                    headers=auth_headers,
                    json={"connection_id": "conn-1"},
                )
                assert disconnect.status_code == 200
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())
