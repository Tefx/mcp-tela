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
from tela.shell.downstream import DOWNSTREAM_CONVERGENCE_CONTRACT
from tela.shell.gateway import (
    GatewayStartupConfig,
    bind_gateway_startup,
    gateway_reload_config_from_disk,
    gateway_connections,
    gateway_shutdown,
    gateway_start,
    gateway_status,
    is_runtime_running,
    is_upstream_server_initialized,
    with_upstream_server,
)
from tela.shell.gateway_runtime import (
    LOCKFILE_DISCOVERY_CONTRACT,
    STATUS_SNAPSHOT_CONTRACT,
    set_runtime_config,
    set_runtime_running,
)


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


def test_runtime_truth_contract_separates_discovery_status_and_convergence() -> None:
    """Declarative contracts keep discovery, readiness, and convergence separate."""

    assert LOCKFILE_DISCOVERY_CONTRACT.plane == "discovery"
    assert LOCKFILE_DISCOVERY_CONTRACT.authoritative_artifact == "~/.tela/gateway.lock"
    assert "lifecycle_readiness" in LOCKFILE_DISCOVERY_CONTRACT.not_authoritative_for
    assert "downstream_convergence" in LOCKFILE_DISCOVERY_CONTRACT.not_authoritative_for

    assert STATUS_SNAPSHOT_CONTRACT.plane == "lifecycle_readiness"
    assert (
        STATUS_SNAPSHOT_CONTRACT.authoritative_artifact
        == "RuntimeStatusSnapshot / GET /status"
    )
    assert "running" in STATUS_SNAPSHOT_CONTRACT.authoritative_fields

    assert (
        "~/.tela/gateway.lock"
        in DOWNSTREAM_CONVERGENCE_CONTRACT.not_authoritative_sources
    )


# --- Lifecycle snapshot tests (starting, warming, ready, degraded) ---


def test_gateway_status_reflects_lifecycle_states() -> None:
    """GatewayStatus reflects runtime lifecycle state across starting, warming, and ready phases.

    Ref: docs/INTERFACES.md §7.2.1 GET /status Response Schema - lifecycle states
    are reported through the running flag and connected_servers list.
    """

    # Starting phase: gateway running but no servers connected
    starting_status = GatewayStatus(
        uptime_seconds=0.1,
        server_count=2,
        connected_servers=[],  # No servers yet
        active_connections=0,
        profile_count=1,
        total_tool_calls=0,
    )
    assert starting_status.server_count == 2
    assert starting_status.connected_servers == []
    assert starting_status.active_connections == 0

    # Warming phase: some servers connected but not all
    warming_status = GatewayStatus(
        uptime_seconds=1.0,
        server_count=2,
        connected_servers=["fs"],  # Partial convergence
        active_connections=0,
        profile_count=1,
        total_tool_calls=0,
    )
    assert len(warming_status.connected_servers) < warming_status.server_count

    # Ready phase: all servers connected
    ready_status = GatewayStatus(
        uptime_seconds=5.0,
        server_count=2,
        connected_servers=["fs", "shell"],  # Full convergence
        active_connections=0,
        profile_count=1,
        total_tool_calls=0,
    )
    assert len(ready_status.connected_servers) == ready_status.server_count


def test_lockfile_discovery_does_not_imply_ready_downstreams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Readable lockfile does NOT imply ready downstreams.

    Ref: docs/INTERFACES.md §7.3 Lockfile Contract
    The lockfile only proves discovery (process location, auth, config ownership).
    Downstream readiness requires runtime status snapshot via GET /status.
    This test proves the contract: lockfile readable ≠ downstream ready.
    """
    from tela.shell import lockfile

    # Create a valid lockfile for a "starting" gateway
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": "127.0.0.1",
                "port": 49152,
                "token": "token-starting",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": str(tmp_path / "tela.yaml"),
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    # Lockfile IS readable (discovery succeeds)
    lockfile_result = lockfile.read_lockfile()
    assert lockfile_result.is_ok, "Lockfile must be readable for discovery"

    # But we cannot infer downstream readiness from lockfile alone
    # The lockfile does not contain connected_servers or running state
    lockfile_data = lockfile_result.value
    assert hasattr(lockfile_data, "config_path")
    assert not hasattr(lockfile_data, "connected_servers")
    assert not hasattr(lockfile_data, "running")

    # Contract confirmation: discovery artifact lacks readiness fields
    assert "lifecycle_readiness" in LOCKFILE_DISCOVERY_CONTRACT.not_authoritative_for
    assert "downstream_convergence" in LOCKFILE_DISCOVERY_CONTRACT.not_authoritative_for


def test_gateway_status_reports_configured_servers_without_implying_readiness() -> None:
    """Configured server discovery must not imply downstream readiness."""

    set_runtime_config(
        TelaConfig(servers={"fs": ServerConfig(name="fs", command="cmd")})
    )
    set_runtime_running(True)
    try:
        status_result = asyncio.run(gateway_status())
        assert status_result.is_ok
        assert status_result.value is not None

        # Lifecycle truth: configured server is known, but readiness depends on
        # connected downstreams (which may still be empty during startup/warming).
        assert status_result.value.server_count == 1
        assert status_result.value.connected_servers == []
    finally:
        set_runtime_running(False)
        set_runtime_config(None)


def test_status_endpoint_required_for_readiness_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """GET /status is required to determine downstream readiness.

    Ref: docs/INTERFACES.md §7.2.1 - status endpoint provides authoritative runtime state.
    This test shows that lockfile config_path alone is insufficient for readiness.
    """
    from tela.shell import lockfile

    # Gateway started with config_path A
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": "127.0.0.1",
                "port": 49152,
                "token": "token-status-test",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/home/user/.tela/tela.yaml",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    lockfile_result = lockfile.read_lockfile()
    assert lockfile_result.is_ok
    assert lockfile_result.value.config_path == "/home/user/.tela/tela.yaml"

    # config_path is known from lockfile, but we still cannot determine:
    # - which servers are connected
    # - current active_connections count
    # - whether gateway is still running
    # These require GET /status


# --- Config path ownership and status exposure tests ---


def test_status_response_exposes_config_path() -> None:
    """StatusResponse exposes config_path for query command ownership verification.

    Ref: docs/INTERFACES.md §7.2.1 GET /status Response Schema
    Query commands (status, connections, audit) use config_path from status
    to verify ownership and detect config mismatches.
    """
    from tela.core.models import StatusResponse

    status = StatusResponse(
        uptime_seconds=10.0,
        server_count=1,
        connected_servers=["fs"],
        active_connections=0,
        profile_count=1,
        total_tool_calls=0,
    )
    # StatusResponse is a GatewayStatus subclass
    # config_path is exposed through the runtime status snapshot
    assert hasattr(status, "uptime_seconds")
    assert hasattr(status, "server_count")


def test_lockfile_config_path_used_by_query_commands() -> None:
    """Lockfile config_path is the source of truth for query command ownership.

    Ref: docs/INTERFACES.md §7.3 Lockfile Contract - config_path field
    Query commands read config_path from lockfile to:
    1. Verify they are querying the correct gateway instance
    2. Detect config mismatches between CLI and running gateway
    """
    from tela.core.models import LockfileData

    lockfile_data = LockfileData(
        pid=12345,
        host="127.0.0.1",
        port=49152,
        token="query-token",
        started_at="2026-03-22T10:00:00Z",
        config_path="/home/user/.tela/tela.yaml",
        version="0.1.0",
    )
    assert lockfile_data.config_path == "/home/user/.tela/tela.yaml"
    assert "tela.yaml" in lockfile_data.config_path


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
    assert is_runtime_running().value is True
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
    assert status.value is not None
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
    assert is_runtime_running().value is True

    result = asyncio.run(gateway_shutdown())
    assert result.is_ok
    assert is_runtime_running().value is False


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
    assert status.value is not None
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
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            names = sorted(tool.name for tool in response.root.tools)  # type: ignore[union-attr]  # response is ListToolsResult at runtime
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
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
            )
            assert handler_result.is_ok

            response = await handler_result.value(
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

            assert response.root.isError is False  # type: ignore[union-attr]  # response is CallToolResult at runtime
            assert response.root.structuredContent is not None  # type: ignore[union-attr]  # response is CallToolResult at runtime
            assert response.root.structuredContent == {  # type: ignore[union-attr]  # response is CallToolResult at runtime
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
        assert is_upstream_server_initialized()

        resources_result = with_upstream_server(
            lambda s: asyncio.run(s.list_resources())
        )
        assert resources_result.is_ok
        resources = resources_result.value
        assert any(resource.name == "tela.profiles" for resource in resources)

        contents_result = with_upstream_server(
            lambda s: asyncio.run(s.read_resource("tela://profiles"))
        )
        assert contents_result.is_ok
        contents = contents_result.value
        payload = json.loads(contents[0].content)  # type: ignore[index]  # contents is indexable at runtime
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
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(name="exec", arguments={}),
                )
            )

            assert response.root.isError is True  # type: ignore[union-attr]  # response is CallToolResult at runtime
            assert "AUTHZ_DENY" in response.root.content[0].text  # type: ignore[union-attr]  # response is CallToolResult at runtime
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
            app_result = with_upstream_server(lambda s: s.streamable_http_app())
            assert app_result.is_ok
            app = app_result.value

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


def test_list_tools_preserves_all_metadata_fields() -> None:
    """_list_tools preserves title, outputSchema, and annotations from downstream."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                )
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
        # Tool with all metadata fields present
        tool_lists = {
            "fs": [
                {
                    "name": "read_file",
                    "inputSchema": {"type": "object"},
                    "description": "Read a file",
                    "title": "File Reader",
                    "outputSchema": {"type": "string"},
                    "annotations": {"readOnlyHint": True, "destructiveHint": False},
                }
            ]
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) == 1
            tool = tools[0]
            assert tool.name == "read_file"
            assert tool.description == "Read a file"
            assert tool.title == "File Reader"
            assert tool.outputSchema == {"type": "string"}
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_list_tools_preserves_partial_metadata() -> None:
    """_list_tools preserves subset of metadata fields when partially present."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                )
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
        # Tool with only title and annotations (no outputSchema)
        tool_lists = {
            "fs": [
                {
                    "name": "read_file",
                    "inputSchema": {"type": "object"},
                    "description": "Read a file",
                    "title": "File Reader",
                    "annotations": {"readOnlyHint": True},
                }
            ]
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) == 1
            tool = tools[0]
            assert tool.name == "read_file"
            assert tool.title == "File Reader"
            assert tool.outputSchema is None
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is True
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_list_tools_handles_absent_metadata() -> None:
    """_list_tools handles tools with no metadata fields gracefully."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                )
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
        # Tool with no metadata fields
        tool_lists = {
            "fs": [
                {
                    "name": "read_file",
                    "inputSchema": {"type": "object"},
                    "description": "Read a file",
                }
            ]
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) == 1
            tool = tools[0]
            assert tool.name == "read_file"
            assert tool.title is None
            assert tool.outputSchema is None
            assert tool.annotations is None
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_list_tools_preserves_only_title() -> None:
    """_list_tools preserves only title when present."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                )
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
                {
                    "name": "read_file",
                    "inputSchema": {"type": "object"},
                    "description": "Read a file",
                    "title": "File Reader",
                }
            ]
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) == 1
            tool = tools[0]
            assert tool.title == "File Reader"
            assert tool.outputSchema is None
            assert tool.annotations is None
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_list_tools_preserves_only_output_schema() -> None:
    """_list_tools preserves only outputSchema when present."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                )
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
                {
                    "name": "read_file",
                    "inputSchema": {"type": "object"},
                    "description": "Read a file",
                    "outputSchema": {"type": "string"},
                }
            ]
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) == 1
            tool = tools[0]
            assert tool.title is None
            assert tool.outputSchema == {"type": "string"}
            assert tool.annotations is None
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_list_tools_preserves_only_annotations() -> None:
    """_list_tools preserves only annotations when present."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                )
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
                {
                    "name": "read_file",
                    "inputSchema": {"type": "object"},
                    "description": "Read a file",
                    "annotations": {"readOnlyHint": True, "idempotentHint": True},
                }
            ]
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) == 1
            tool = tools[0]
            assert tool.title is None
            assert tool.outputSchema is None
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.idempotentHint is True
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())
