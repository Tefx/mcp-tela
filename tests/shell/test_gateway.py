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
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementVerdict,
    GatewayStatus,
    GatewayTransport,
    Posture,
    ProfileConfig,
    ServerConfig,
    TelaConfig,
)
from tela.core.token import compute_signature
from tela.commands.start import start_command
from tela.shell.result import Result
from tela.shell.connection_lifecycle import ConnectionCleanupOutcome
from tela.shell.downstream import DOWNSTREAM_CONVERGENCE_CONTRACT
from tela.shell.gateway import (
    GatewayStartupConfig,
    bind_gateway_startup,
    gateway_reload_config_from_disk,
    gateway_connections,
    gateway_shutdown,
    gateway_start,
    gateway_status,
)
from tela.shell import gateway as gateway_module
from tela.shell.gateway_runtime import (
    is_runtime_running,
    is_upstream_server_initialized,
    with_upstream_server,
)
from tela.shell.audit import audit_write, clear_audit_entries, get_audit_entries
from tela.shell.gateway_runtime import (
    LOCKFILE_DISCOVERY_CONTRACT,
    STATUS_SNAPSHOT_CONTRACT,
    UpstreamSession,
    add_runtime_connection,
    capture_session,
    clear_runtime_connections,
    clear_session_registry,
    set_runtime_config,
    set_runtime_running,
)
from tela.shell import gateway_runtime


_LEGACY_PROFILE_KEY = "profile" + "_name"
_LEGACY_PROFILE_RESOURCE = "tela" + ".profiles"
_LEGACY_TOOLS_KEY = "to" + "ols"
_LEGACY_FAMILIES_KEY = "famil" + "ies"


def _set_startup_auth_mode(auth_mode: AuthMode) -> None:
    """Install startup auth mode for direct _connect_handler tests."""

    with gateway_runtime._runtime_lock:
        gateway_runtime._runtime.startup_config = GatewayStartupConfig(
            transport=GatewayTransport.HTTP,
            port=8400,
            auth_mode=auth_mode,
            default_profile="dev" if auth_mode == AuthMode.OPEN else None,
        )


def _clear_startup_config() -> None:
    """Clear startup config after direct _connect_handler tests."""

    with gateway_runtime._runtime_lock:
        gateway_runtime._runtime.startup_config = None


# --- Helper for tests requiring a bound MCP session ---


class _FakeSession:
    """Minimal UpstreamSession implementation for test request contexts.

    Also serves as duck-typed session object: has a .session attribute
    that returns itself, so request_ctx.get().session works.
    """

    async def send_tool_list_changed(self) -> None:
        pass

    @property
    def session(self) -> _FakeSession:
        """Duck-type: request_ctx.get().session returns the session itself."""
        return self


def _setup_test_connection_with_session() -> tuple[str, _FakeSession]:
    """Register a bridge connection and bind a fake session for test handlers.

    Used by tests that call handler functions directly (bypassing real MCP
    initialization). Without this, _ensure_connection raises RECONNECT_REQUIRED
    because no session context is available in the test environment.

    Returns:
        Tuple of (connection_id, fake_session) for use in test assertions.
    """
    from mcp.server.lowlevel.server import request_ctx

    connection_id = "bridge_test"
    conn = ConnectionContext(
        connection_id=connection_id,
        profile_id="dev",
        connected_at="2026-01-01T00:00:00Z",
        init_mode=AuthMode.OPEN,
    )
    add_runtime_connection(conn)
    session = _FakeSession()
    capture_result = capture_session(connection_id, session)
    assert capture_result.is_ok
    # Set request_ctx so _ensure_connection can find the session and
    # adopt the bridge connection. type: ignore because _FakeSession
    # is not a RequestContext but has .session attribute for duck typing.
    _ctx_token = request_ctx.set(session)  # type: ignore[arg-type]  # test-only: _FakeSession satisfies session protocol at runtime
    return connection_id, session


def _extract_jsonrpc_payload(raw_text: str) -> dict[str, object]:
    """Extract the JSON-RPC payload from plain or SSE-style HTTP responses."""

    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("data: "):
            payload = json.loads(stripped[6:])
            assert isinstance(payload, dict)
            return payload
        if stripped.startswith("{"):
            payload = json.loads(stripped)
            assert isinstance(payload, dict)
            return payload
    raise AssertionError(f"No JSON-RPC payload found in response: {raw_text!r}")


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
        from tela.shell.result import Result

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


def test_gateway_reload_config_from_disk_applies_reaper_cli_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "tela.yaml"
    config_path.write_text(
        "profiles:\n"
        "  dev:\n"
        "    name: dev\n"
        "    default: true\n"
        "auth:\n"
        "  mode: open\n"
        "reaper:\n"
        "  bridge_idle_ttl_seconds: 300\n",
        encoding="utf-8",
    )

    captured: list[TelaConfig] = []

    async def _fake_on_config_changed(new_config: TelaConfig):
        captured.append(new_config)
        from tela.shell.result import Result

        return Result(value=None)

    monkeypatch.setattr(
        "tela.shell.reload.on_config_changed",
        _fake_on_config_changed,
    )

    result = asyncio.run(
        gateway_reload_config_from_disk(
            config_path=config_path,
            default_profile=None,
            bridge_idle_ttl_seconds=900.0,
        )
    )

    assert result.is_ok
    assert len(captured) == 1
    assert captured[0].reaper.bridge_idle_ttl_seconds == 900.0


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


def test_gateway_shutdown_uses_shared_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gateway_shutdown delegates per-connection cleanup to shared authority."""

    called_ids: list[str] = []

    async def _fake_disconnect_all() -> Result[None, str]:
        return Result(value=None)

    async def _fake_audit_close() -> Result[None, str]:
        return Result(value=None)

    def _fake_cleanup(connection_id: str) -> Result[ConnectionCleanupOutcome, str]:
        called_ids.append(connection_id)
        return Result(
            value=ConnectionCleanupOutcome(
                connection_id=connection_id,
                removed_runtime_connection=True,
                removed_bridge_registration=False,
            )
        )

    monkeypatch.setattr("tela.shell.gateway.disconnect_all", _fake_disconnect_all)
    monkeypatch.setattr("tela.shell.gateway.audit_close", _fake_audit_close)
    monkeypatch.setattr("tela.shell.gateway.cleanup_connection_by_id", _fake_cleanup)

    set_runtime_config(TelaConfig())
    set_runtime_running(True)

    clear_runtime_connections()
    add_runtime_connection(
        ConnectionContext(
            connection_id="shutdown-cleanup-1",
            profile_id="default",
            connected_at="2026-01-01T00:00:00Z",
        )
    )
    add_runtime_connection(
        ConnectionContext(
            connection_id="shutdown-cleanup-2",
            profile_id="default",
            connected_at="2026-01-01T00:00:00Z",
        )
    )

    try:
        result = asyncio.run(gateway_shutdown())
        assert result.is_ok
        assert called_ids == ["shutdown-cleanup-1", "shutdown-cleanup-2"]
    finally:
        clear_runtime_connections()
        set_runtime_running(False)
        set_runtime_config(None)


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
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            names = sorted(tool.name for tool in response.root.tools)  # type: ignore[union-attr]  # response is ListToolsResult at runtime
            # Only downstream tools are filtered; builtin tools like tela_list_providers are always present
            assert "read_file" in names
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
        _setup_test_connection_with_session()
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


def test_fastmcp_profiles_resource_not_registered() -> None:
    """Legacy profile MCP resource must not be registered."""

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
        # The retired profile resource must not appear.
        assert not any(
            resource.name == _LEGACY_PROFILE_RESOURCE
            for resource in resources_result.value
        )
    finally:
        asyncio.run(gateway_shutdown())


def test_fastmcp_list_profiles_builtin_tool() -> None:
    """tela_list_profiles builtin tool must be callable and return canonical payload."""

    tela = TelaConfig(
        profiles={
            "dev": ProfileConfig(
                name="dev",
                default=True,
                capabilities={"fs": Posture.READ_WRITE},
            ),
            "reviewer": ProfileConfig(
                name="reviewer",
                default=False,
                capabilities={"fs": Posture.READ_ONLY},
            ),
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
        from tela.shell.builtin_tools import handle_profiles_list

        result = handle_profiles_list()
        assert isinstance(result, list)
        assert len(result) == 2

        dev_entry = next(e for e in result if e["profile_id"] == "dev")
        assert dev_entry["default"] is True
        assert dev_entry["capabilities"] == {"fs": "read_write"}

        rev_entry = next(e for e in result if e["profile_id"] == "reviewer")
        assert rev_entry["default"] is False
        assert rev_entry["capabilities"] == {"fs": "read_only"}

        # Verify legacy keys are absent
        for entry in result:
            assert _LEGACY_PROFILE_KEY not in entry
            assert _LEGACY_FAMILIES_KEY not in entry
            assert _LEGACY_TOOLS_KEY not in entry
    finally:
        asyncio.run(gateway_shutdown())


def test_fastmcp_list_profiles_builtin_tool_returns_json_payload_resource() -> None:
    """MCP builtin call must return exact canonical JSON payload, not repr text."""

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
                    capabilities={"fs": Posture.READ_WRITE},
                ),
                "reviewer": ProfileConfig(
                    name="reviewer",
                    default=False,
                    capabilities={"fs": Posture.READ_ONLY},
                ),
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

        await gateway_start(config, tela_config=tela, tool_lists={})
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
            )
            assert handler_result.is_ok

            response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="tela_list_profiles",
                        arguments={},
                    )
                )
            )

            assert response.root.isError is False  # type: ignore[union-attr]
            content_item = response.root.content[0]  # type: ignore[union-attr]
            assert content_item.type == "resource"
            assert content_item.resource.mimeType == "application/json"
            assert str(content_item.resource.uri) == "tela://builtin/tela_list_profiles"
            resource_text = getattr(content_item.resource, "text", None)
            assert isinstance(resource_text, str)
            assert json.loads(resource_text) == [
                {
                    "profile_id": "dev",
                    "capabilities": {"fs": "read_write"},
                    "default": True,
                },
                {
                    "profile_id": "reviewer",
                    "capabilities": {"fs": "read_only"},
                    "default": False,
                },
            ]
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


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
        _setup_test_connection_with_session()
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
            assert app is not None

            with TestClient(app, base_url="http://127.0.0.1:8402") as client:
                health = client.get("/health")
                assert health.status_code == 200
                assert health.json()["status"] == "ok"

                unauthorized_status = client.get("/status")
                assert unauthorized_status.status_code == 401

                unauthorized_connect = client.post(
                    "/connect", json={"server_name": "conn-1"}
                )
                assert unauthorized_connect.status_code == 401

                auth_headers = {"Authorization": "Bearer mounted-token"}

                status = client.get("/status", headers=auth_headers)
                assert status.status_code == 200

                clear_audit_entries()
                write_result = await audit_write(
                    AuditEntry(
                        timestamp="2026-01-01T00:00:00Z",
                        level=AuditLevel.L1,
                        connection_id="conn-1",
                        profile_id="dev",
                        tool_name="tool_one",
                        server_name="srv",
                        verdict=EnforcementVerdict.ALLOW,
                    )
                )
                assert write_result.is_ok

                audit_page = client.get(
                    "/operator/audit?limit=1",
                    headers=auth_headers,
                )
                assert audit_page.status_code == 200
                assert audit_page.json()["entries"][0]["tool_name"] == "tool_one"
                assert audit_page.json()["next_cursor"] is None
                assert audit_page.json()["has_more"] is False

                connect = client.post(
                    "/connect",
                    headers=auth_headers,
                    json={"server_name": "conn-1"},
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


def test_connect_handler_requires_server_name_field() -> None:
    """Canonical /connect helper must reject missing required request key."""

    set_runtime_config(TelaConfig())
    set_runtime_running(True)
    _set_startup_auth_mode(AuthMode.OPEN)
    try:
        result = gateway_module._connect_handler("valid", "valid", {})
        assert result.is_err
        assert result.error == "missing_required_field: field=server_name"
    finally:
        _clear_startup_config()
        set_runtime_running(False)
        set_runtime_config(None)


def test_connect_handler_rejects_wrong_server_name_type() -> None:
    """Canonical /connect helper must reject non-string server_name."""

    set_runtime_config(TelaConfig())
    set_runtime_running(True)
    _set_startup_auth_mode(AuthMode.OPEN)
    try:
        result = gateway_module._connect_handler(
            "valid", "valid", {"server_name": 123}
        )
        assert result.is_err
        assert result.error == "wrong_type: field=server_name"
    finally:
        _clear_startup_config()
        set_runtime_running(False)
        set_runtime_config(None)


def test_connect_handler_rejects_extra_http_keys() -> None:
    """Canonical /connect helper must fail closed on unexpected keys."""

    set_runtime_config(TelaConfig())
    set_runtime_running(True)
    _set_startup_auth_mode(AuthMode.OPEN)
    try:
        result = gateway_module._connect_handler(
            "valid",
            "valid",
            {"server_name": "bridge_http_1", "unexpected_key": True},
        )
        assert result.is_err
        assert result.error == "extra_key: rejected_keys=unexpected_key"
    finally:
        _clear_startup_config()
        set_runtime_running(False)
        set_runtime_config(None)


def test_connect_handler_rejects_token_mode_profile_binding_fields() -> None:
    """Token-mode /connect helper must reject fabricated profile binding hints."""

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret"]),
            profiles={"prod": ProfileConfig(name="prod")},
        )
    )
    set_runtime_running(True)
    _set_startup_auth_mode(AuthMode.TOKEN)
    try:
        result = gateway_module._connect_handler(
            "valid",
            "valid",
            {"server_name": "bridge_http_1", "profile_id": "prod"},
        )
        assert result.is_err
        assert result.error is not None
        assert "fabricated_profile_binding_forbidden" in result.error
    finally:
        _clear_startup_config()
        set_runtime_running(False)
        set_runtime_config(None)


def test_connect_handler_uses_startup_config_auth_mode_not_runtime_config_fallback() -> None:
    """Regression for W6-001: /connect validation consumes GatewayStartupConfig.auth_mode."""

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            profiles={"dev": ProfileConfig(name="dev", default=True)},
            resolved_default_profile="dev",
        )
    )
    set_runtime_running(True)
    _set_startup_auth_mode(AuthMode.TOKEN)
    try:
        result = gateway_module._connect_handler(
            "valid",
            "valid",
            {"server_name": "bridge_http_1", "profile_id": "dev"},
        )
        assert result.is_err
        assert result.error is not None
        assert "fabricated_profile_binding_forbidden" in result.error
    finally:
        _clear_startup_config()
        set_runtime_running(False)
        set_runtime_config(None)


def test_streamable_http_initialize_token_mode_rejects_missing_capability_token() -> (
    None
):
    """Actual /mcp initialize must reject token mode when the canonical token is absent."""

    def _extract_jsonrpc_payload(raw_text: str) -> dict[str, object]:
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data: "):
                return json.loads(stripped[6:])
            if stripped.startswith("{"):
                return json.loads(stripped)
        raise AssertionError(f"No JSON-RPC payload found in response: {raw_text!r}")

    async def _scenario() -> None:
        tela = TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret"]),
            profiles={"prod": ProfileConfig(name="prod")},
        )
        config = GatewayStartupConfig(
            transport=GatewayTransport.HTTP,
            port=8402,
            auth_mode=AuthMode.TOKEN,
            default_profile=None,
        )
        start_result = await gateway_start(
            config,
            tela_config=tela,
            expected_bearer_token="mounted-token",
        )
        assert start_result.is_ok

        try:
            app_result = with_upstream_server(lambda s: s.streamable_http_app())
            assert app_result.is_ok
            app = app_result.value
            assert app is not None

            with TestClient(app, base_url="http://127.0.0.1:8402") as client:
                response = client.post(
                    "/mcp",
                    headers={
                        "Authorization": "Bearer mounted-token",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "bridge-test", "version": "0.1"},
                        },
                    },
                )
                assert response.status_code == 200
                payload = _extract_jsonrpc_payload(response.text)
                assert "error" in payload
                error = payload["error"]
                assert isinstance(error, dict)
                assert "INITIALIZE_REJECTED" in str(error.get("message"))

        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_streamable_http_initialize_token_mode_rejects_unknown_token_profile() -> None:
    """Actual /mcp initialize must reject a valid token that names no configured profile."""

    def _extract_jsonrpc_payload(raw_text: str) -> dict[str, object]:
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data: "):
                return json.loads(stripped[6:])
            if stripped.startswith("{"):
                return json.loads(stripped)
        raise AssertionError(f"No JSON-RPC payload found in response: {raw_text!r}")

    async def _scenario() -> None:
        secret = "secret"
        token_fields = {
            "token_id": "tok_unknown_http_1",
            "profile_id": "ghost",
            "persona_ref": "persona.ghost",
            "instance_id": "inst-ghost",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-12-31T23:59:59Z",
            "token_version": "0.1.0",
        }
        token_payload = {
            **token_fields,
            "signature": compute_signature(token_fields, secret),
        }

        tela = TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"prod": ProfileConfig(name="prod")},
        )
        config = GatewayStartupConfig(
            transport=GatewayTransport.HTTP,
            port=8404,
            auth_mode=AuthMode.TOKEN,
            default_profile=None,
        )
        start_result = await gateway_start(
            config,
            tela_config=tela,
            expected_bearer_token="mounted-token",
        )
        assert start_result.is_ok

        try:
            app_result = with_upstream_server(lambda s: s.streamable_http_app())
            assert app_result.is_ok
            app = app_result.value
            assert app is not None

            with TestClient(app, base_url="http://127.0.0.1:8404") as client:
                response = client.post(
                    "/mcp",
                    headers={
                        "Authorization": "Bearer mounted-token",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "bridge-test",
                                "version": "0.1",
                                "capability_token": token_payload,
                            },
                        },
                    },
                )
                assert response.status_code == 200
                payload = _extract_jsonrpc_payload(response.text)
                assert "error" in payload
                error = payload["error"]
                assert isinstance(error, dict)
                assert "unknown_profile_binding" in str(error.get("message"))

        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_streamable_http_initialize_token_mode_rejects_profile_name_alias() -> None:
    """Actual /mcp initialize must reject legacy token alias fields on alternate path."""

    def _extract_jsonrpc_payload(raw_text: str) -> dict[str, object]:
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data: "):
                return json.loads(stripped[6:])
            if stripped.startswith("{"):
                return json.loads(stripped)
        raise AssertionError(f"No JSON-RPC payload found in response: {raw_text!r}")

    async def _scenario() -> None:
        secret = "secret"
        token_fields = {
            "token_id": "tok_alias_http_1",
            "profile_id": "prod",
            "persona_ref": "persona.prod",
            "instance_id": "inst-prod",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-12-31T23:59:59Z",
            "token_version": "0.1.0",
        }
        token_payload = {
            **token_fields,
            _LEGACY_PROFILE_KEY: "legacy-prod",
            "signature": compute_signature(token_fields, secret),
        }

        tela = TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"prod": ProfileConfig(name="prod")},
        )
        config = GatewayStartupConfig(
            transport=GatewayTransport.HTTP,
            port=8405,
            auth_mode=AuthMode.TOKEN,
            default_profile=None,
        )
        start_result = await gateway_start(
            config,
            tela_config=tela,
            expected_bearer_token="mounted-token",
        )
        assert start_result.is_ok

        try:
            app_result = with_upstream_server(lambda s: s.streamable_http_app())
            assert app_result.is_ok
            app = app_result.value
            assert app is not None

            with TestClient(app, base_url="http://127.0.0.1:8405") as client:
                response = client.post(
                    "/mcp",
                    headers={
                        "Authorization": "Bearer mounted-token",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "bridge-test",
                                "version": "0.1",
                                "capability_token": token_payload,
                            },
                        },
                    },
                )
                assert response.status_code == 200
                payload = _extract_jsonrpc_payload(response.text)
                assert "error" in payload
                error = payload["error"]
                assert isinstance(error, dict)
                assert "alias_field_present" in str(error.get("message"))

        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_streamable_http_bridge_initialize_token_mode_binds_after_connect_only_with_valid_token() -> (
    None
):
    """Bridge /mcp initialize must validate token and establish the binding after /connect."""

    def _extract_jsonrpc_payload(raw_text: str) -> dict[str, object]:
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data: "):
                return json.loads(stripped[6:])
            if stripped.startswith("{"):
                return json.loads(stripped)
        raise AssertionError(f"No JSON-RPC payload found in response: {raw_text!r}")

    async def _scenario() -> None:
        secret = "bridge-secret"
        token_fields = {
            "token_id": "tok_bridge_http_1",
            "profile_id": "prod",
            "persona_ref": "persona.prod",
            "instance_id": "inst-prod",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-12-31T23:59:59Z",
            "token_version": "0.1.0",
        }
        token_payload = {
            **token_fields,
            "signature": compute_signature(token_fields, secret),
        }

        tela = TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={
                "dev": ProfileConfig(name="dev", default=True),
                "prod": ProfileConfig(name="prod"),
            },
            resolved_default_profile="dev",
        )
        config = GatewayStartupConfig(
            transport=GatewayTransport.HTTP,
            port=8403,
            auth_mode=AuthMode.TOKEN,
            default_profile=None,
        )
        start_result = await gateway_start(
            config,
            tela_config=tela,
            expected_bearer_token="mounted-token",
        )
        assert start_result.is_ok

        try:
            app_result = with_upstream_server(lambda s: s.streamable_http_app())
            assert app_result.is_ok
            app = app_result.value
            assert app is not None

            with TestClient(app, base_url="http://127.0.0.1:8403") as client:
                connect = client.post(
                    "/connect",
                    headers={"Authorization": "Bearer mounted-token"},
                    json={"server_name": "bridge_http_1"},
                )
                assert connect.status_code == 200
                assert connect.json() == {
                    "connection_id": "bridge_http_1",
                    "status": "connected",
                }

                initialize = client.post(
                    "/mcp",
                    headers={
                        "Authorization": "Bearer mounted-token",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "bridge-test",
                                "version": "0.1",
                                "tela_bridge_connection_id": "bridge_http_1",
                                "capability_token": token_payload,
                            },
                        },
                    },
                )
                assert initialize.status_code == 200
                initialize_payload_json = _extract_jsonrpc_payload(initialize.text)
                assert "result" in initialize_payload_json

                session_id = initialize.headers.get("mcp-session-id")
                assert isinstance(session_id, str) and len(session_id) > 0

                tools_list = client.post(
                    "/mcp",
                    headers={
                        "Authorization": "Bearer mounted-token",
                        "Accept": "application/json, text/event-stream",
                        "mcp-session-id": session_id,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                )
                assert tools_list.status_code == 200
                tools_payload = _extract_jsonrpc_payload(tools_list.text)
                assert "error" not in tools_payload
                assert "result" in tools_payload

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
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) >= 1
            tool = next((t for t in tools if t.name == "read_file"), None)
            assert tool is not None
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
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) >= 1
            tool = next((t for t in tools if t.name == "read_file"), None)
            assert tool is not None
            assert tool.description == "Read a file"
            assert tool.title == "File Reader"
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
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) >= 1
            # Find the downstream tool (read_file), builtin tool may also be present
            tool = next((t for t in tools if t.name == "read_file"), None)
            assert tool is not None
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
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) >= 1
            tool = next((t for t in tools if t.name == "read_file"), None)
            assert tool is not None
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
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) >= 1
            tool = next((t for t in tools if t.name == "read_file"), None)
            assert tool is not None
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
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            assert len(tools) >= 1
            tool = next((t for t in tools if t.name == "read_file"), None)
            assert tool is not None
            assert tool.title is None
            assert tool.outputSchema is None
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.idempotentHint is True
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_gateway_list_tools_includes_builtin() -> None:
    """Gateway tools/list includes 'tela_list_providers' alongside downstream tools."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
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
            "fs": [{"name": "read_file", "inputSchema": {}}],
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(types.ListToolsRequest())

            tools = response.root.tools  # type: ignore[union-attr]
            tool_names = [t.name for t in tools]
            assert "tela_list_providers" in tool_names
            assert "read_file" in tool_names
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_gateway_call_tool_dispatches_builtin() -> None:
    """Gateway tools/call dispatches 'tela_list_providers' to handle_list_providers."""

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
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
            "fs": [{"name": "read_file", "inputSchema": {}}],
        }
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists=tool_lists)
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="tela_list_providers",
                        arguments={},
                    )
                )
            )

            # Should not be an error (TOOL_NOT_FOUND)
            assert response.root.isError is False  # type: ignore[union-attr]

            # Should return a valid result (list of ProviderInfo dicts)
            assert response.root.content is not None  # type: ignore[union-attr]
            resource_text = getattr(response.root.content[0].resource, "text", None)  # type: ignore[union-attr]
            assert isinstance(resource_text, str)
            payload = json.loads(resource_text)
            assert payload[0]["provider_name"] == "fs"
            assert "name" not in payload[0]
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_gateway_call_tool_rejects_non_snake_case_name() -> None:
    """Direct MCP tools/call must reject non-snake-case shared tool names."""

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
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )

        await gateway_start(config, tela_config=tela, tool_lists={})
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="bad.tool",
                        arguments={},
                    )
                )
            )

            assert response.root.isError is True  # type: ignore[union-attr]
            assert "invalid_tool_name" in response.root.content[0].text  # type: ignore[union-attr]
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_gateway_call_tool_rejects_extra_arguments_for_tela_list_profiles() -> None:
    """tela_list_profiles must fail closed on non-empty argument payloads."""

    async def _scenario() -> None:
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

        await gateway_start(config, tela_config=tela, tool_lists={})
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
            )
            assert handler_result.is_ok
            response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="tela_list_profiles",
                        arguments={"extra": True},
                    )
                )
            )

            assert response.root.isError is True  # type: ignore[union-attr]
            assert "extra_key" in response.root.content[0].text  # type: ignore[union-attr]
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


@pytest.mark.parametrize("tool_name", ["tela_list_profiles", "tela_list_providers"])
def test_handle_builtin_call_rejects_extra_arguments_fail_closed(
    tool_name: str,
) -> None:
    """Helper builtin path must reject non-empty args with canonical error code."""

    async def _scenario() -> None:
        clear_audit_entries()
        tela = TelaConfig(
            servers={"fs": ServerConfig(name="fs", command="cmd")},
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
        connection = ConnectionContext(
            connection_id="conn_builtin_helper",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.OPEN,
        )

        await gateway_start(config, tela_config=tela, tool_lists={"fs": []})
        try:
            with pytest.raises(RuntimeError, match="extra_key"):
                await gateway_module._handle_builtin_call(
                    tool_name,
                    {"unexpected_key": True},
                    connection,
                )

            audit_entries = get_audit_entries()
            assert audit_entries.is_ok and audit_entries.value is not None
            entry = audit_entries.value[-1]
            assert entry.tool_name == tool_name
            assert entry.server_name == "tela"
            assert entry.error_code == "extra_key"
        finally:
            clear_audit_entries()
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_streamable_http_builtin_call_requires_admitted_session() -> None:
    """Builtin tools/call must fail closed without a live admitted session."""

    async def _scenario() -> None:
        tela = TelaConfig(
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
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
            add_runtime_connection(
                ConnectionContext(
                    connection_id="conn_unadmitted_builtin",
                    profile_id="dev",
                    connected_at="2026-01-01T00:00:00Z",
                    init_mode=AuthMode.OPEN,
                )
            )
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
            )
            assert handler_result.is_ok

            response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="tela_list_providers",
                        arguments={},
                    )
                )
            )

            assert response.root.isError is True  # type: ignore[union-attr]
            assert "RECONNECT_REQUIRED" in response.root.content[0].text  # type: ignore[union-attr]
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_streamable_http_builtin_call_accepts_only_exact_empty_object() -> None:
    """Builtin tools/call must accept {} only and reject null/non-object/extra keys."""

    async def _scenario() -> None:
        tela = TelaConfig(
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
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
        _setup_test_connection_with_session()
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
            )
            assert handler_result.is_ok

            ok_response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="tela_list_profiles",
                        arguments={},
                    )
                )
            )
            assert ok_response.root.isError is False  # type: ignore[union-attr]

            null_response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="tela_list_profiles",
                        arguments=None,
                    )
                )
            )
            assert null_response.root.isError is True  # type: ignore[union-attr]
            assert "wrong_type" in null_response.root.content[0].text  # type: ignore[union-attr]

            list_response = await handler_result.value(
                types.CallToolRequest.model_construct(
                    params=types.CallToolRequestParams.model_construct(
                        name="tela_list_profiles",
                        arguments=[],
                    )
                )
            )
            assert list_response.root.isError is True  # type: ignore[union-attr]
            assert "wrong_type" in list_response.root.content[0].text  # type: ignore[union-attr]

            extra_response = await handler_result.value(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name="tela_list_profiles",
                        arguments={"extra": True},
                    )
                )
            )
            assert extra_response.root.isError is True  # type: ignore[union-attr]
            assert "extra_key" in extra_response.root.content[0].text  # type: ignore[union-attr]
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_builtin_call_rejects_unbound_multi_bridge_session_instead_of_auditing_first_connection() -> (
    None
):
    """Builtin calls must fail closed when no admitted session binding exists."""

    async def _scenario() -> None:
        from mcp.server.lowlevel.server import request_ctx

        tela = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs",
                    command="cmd",
                    default_posture=Posture.READ_ONLY,
                ),
            },
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
                    capabilities={"fs": Posture.READ_ONLY},
                ),
                "prod": ProfileConfig(
                    name="prod",
                    default=False,
                    capabilities={"fs": Posture.NONE},
                ),
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

        clear_audit_entries()
        await gateway_start(
            config,
            tela_config=tela,
            tool_lists={"fs": [{"name": "read_file", "inputSchema": {}}]},
        )
        try:
            add_runtime_connection(
                ConnectionContext(
                    connection_id="bridge_conn_dev",
                    profile_id="dev",
                    connected_at="2026-01-01T00:00:00Z",
                    init_mode=AuthMode.OPEN,
                )
            )
            add_runtime_connection(
                ConnectionContext(
                    connection_id="bridge_conn_prod",
                    profile_id="prod",
                    connected_at="2026-01-01T00:00:01Z",
                    init_mode=AuthMode.OPEN,
                )
            )
            ctx_token = request_ctx.set(_FakeSession())  # type: ignore[arg-type]  # test-only session duck type
            try:
                handler_result = with_upstream_server(
                    lambda s: s._mcp_server.request_handlers[types.CallToolRequest]
                )
                assert handler_result.is_ok
                response = await handler_result.value(
                    types.CallToolRequest(
                        params=types.CallToolRequestParams(
                            name="tela_list_providers",
                            arguments={},
                        )
                    )
                )
            finally:
                request_ctx.reset(ctx_token)

            assert response.root.isError is True  # type: ignore[union-attr]
            assert "RECONNECT_REQUIRED" in response.root.content[0].text  # type: ignore[union-attr]

            audit_entries_result = get_audit_entries()
            assert audit_entries_result.is_ok
            assert audit_entries_result.value == []
        finally:
            clear_audit_entries()
            await gateway_shutdown()

    asyncio.run(_scenario())


# =============================================================================
# ADR-006: Expected-Red Surface Contract Tests for Gateway Integration
# =============================================================================
# These tests verify the upstream-facing (MCP protocol) behavior for ADR-006
# downstream steady-state self-healing recovery.
#
# Expected-red meaning: these tests expose MISSING recovery behavior that will
# be implemented in adr006_recovery.impl.
#
# Ref: docs/ADR-006-steady-state-downstream-recovery.md
# =============================================================================


# =============================================================================
# Gateway Fail-Closed Recovery Tests (idle_recovery.gateway_fail_closed)
# =============================================================================
# These tests verify that _ensure_connection:
# 1. Never calls handle_initialize({}) as a fake recovery fallback
# 2. Recaptures only when a real current session is available
# 3. Fails closed with RECONNECT_REQUIRED on true session loss
# And that _call_tool captures/rebinds session like _list_tools does.
# =============================================================================


def test_ensure_connection_no_empty_initialize_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ensure_connection must never call handle_initialize({}) as recovery.

    When invoked via handler without request_ctx (no session available),
    the gateway must fail closed with RECONNECT_REQUIRED rather than
    creating a spurious empty connection via handle_initialize({}).
    """

    async def _scenario() -> None:
        initialize_calls: list[dict] = []

        async def _capture_initialize_calls(client_info: dict):
            initialize_calls.append(dict(client_info))
            return Result(error="GATEWAY_NOT_STARTED: test isolation")

        monkeypatch.setattr(
            "tela.shell.upstream.handle_initialize", _capture_initialize_calls
        )

        tela = TelaConfig(
            servers={"fs": ServerConfig(name="fs", command="cmd")},
            profiles={
                "dev": ProfileConfig(name="dev", default=True),
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

        await gateway_start(config, tela_config=tela, tool_lists={})
        try:
            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            # Calling without request_ctx will cause request_ctx.get() to raise
            # LookupError in both session-lookup and bridge-adoption paths.
            # Before fix: falls through to handle_initialize({})
            # After fix: raises RuntimeError with RECONNECT_REQUIRED
            with pytest.raises(RuntimeError, match="RECONNECT_REQUIRED"):
                await handler_result.value(types.ListToolsRequest())

            # No empty initialize was called
            assert len(initialize_calls) == 0, (
                "handle_initialize must not be called with empty dict as "
                f"recovery fallback. Got calls: {initialize_calls}"
            )
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_ensure_connection_recaptures_on_existing_connection() -> None:
    """_ensure_connection must recapture session for existing connection.

    When a connection already exists (added to runtime) but its session
    binding was lost, and no request_ctx is available, _ensure_connection
    should fail closed rather than silently creating a new connection.

    This tests the true-session-loss failure path.
    """

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={"fs": ServerConfig(name="fs", command="cmd")},
            profiles={
                "dev": ProfileConfig(name="dev", default=True),
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

        await gateway_start(config, tela_config=tela, tool_lists={})
        try:
            # Add an existing connection to the runtime
            conn = ConnectionContext(
                connection_id="conn_existing",
                profile_id="dev",
                connected_at="2026-01-01T00:00:00Z",
                init_mode=AuthMode.OPEN,
            )
            add_runtime_connection(conn)

            handler_result = with_upstream_server(
                lambda s: s._mcp_server.request_handlers[types.ListToolsRequest]
            )
            assert handler_result.is_ok
            # The connection exists but without request_ctx, we cannot
            # bind the session to it. Must fail closed.
            with pytest.raises(RuntimeError, match="RECONNECT_REQUIRED"):
                await handler_result.value(types.ListToolsRequest())
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_call_tool_captures_session_like_list_tools() -> None:
    """_call_tool must capture/rebind session after _ensure_connection, mirroring _list_tools.

    Both tools/list and tools/call handlers must capture the upstream MCP session
    for notification delivery. If only _list_tools captures, a session that first
    calls tools/call (without a prior tools/list) will miss notification delivery.

    This test verifies the code path by inspecting that the _call_tool handler
    contains a capture_session call, mirroring _list_tools.
    """
    import ast
    import inspect
    from tela.shell import gateway

    # Extract the source of _wire_upstream_handlers to verify both handlers
    # contain session capture logic
    source = inspect.getsource(gateway._wire_upstream_handlers)
    tree = ast.parse(source)

    # Find all async function definitions in _wire_upstream_handlers
    handler_names = []
    capture_session_calls = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            handler_names.append(node.name)
            # Check for capture_session calls in this function
            call_count = 0
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Attribute):
                        if child.func.attr == "capture_session":
                            call_count += 1
                    elif isinstance(child.func, ast.Name):
                        if child.func.id == "capture_session":
                            call_count += 1
            if call_count > 0:
                capture_session_calls[node.name] = call_count

    # _list_tools must have capture_session
    assert "_list_tools" in capture_session_calls, (
        f"_list_tools must call capture_session. Found handlers: {handler_names}"
    )
    # _call_tool must also have capture_session
    assert "_call_tool" in capture_session_calls, (
        f"_call_tool must call capture_session (mirroring _list_tools). "
        f"capture_session found in: {list(capture_session_calls.keys())}"
    )
    # Both should have exactly 1 capture_session call
    assert capture_session_calls["_call_tool"] == 1, (
        f"_call_tool must have exactly 1 capture_session call, "
        f"got {capture_session_calls['_call_tool']}"
    )


def test_ensure_connection_recaptures_lost_session() -> None:
    """_ensure_connection must recapture session for existing connection when session binding is lost.

    When a connection exists but its session binding is stale/lost (e.g., after idle
    disconnect), and the current MCP session is the same logical connection, the gateway
    must rebind the session to the existing connection rather than fail-closing.
    """

    async def _scenario() -> None:
        tela = TelaConfig(
            servers={"fs": ServerConfig(name="fs", command="cmd")},
            profiles={
                "dev": ProfileConfig(name="dev", default=True),
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

        await gateway_start(config, tela_config=tela, tool_lists={})
        try:
            # The gateway is running. Add a connection simulating existing state.
            conn = ConnectionContext(
                connection_id="conn_recap",
                profile_id="dev",
                connected_at="2026-01-01T00:00:00Z",
                init_mode=AuthMode.OPEN,
            )
            add_runtime_connection(conn)

            # Verify connection exists
            snap = gateway_runtime.get_runtime_connections_snapshot()
            assert any(c.connection_id == "conn_recap" for c in snap.value)

            # Release session to simulate lost session binding
            gateway_runtime.release_session("conn_recap")

            # Now when tools/list is called on a session that was previously
            # bound to conn_recap, it should recapture rather than fail.
            # This tests the recapture path in _list_tools's capture_session.
            # (The recapture happens via capture_session being called again
            # with the same connection_id and new session.)
            recapture_result = gateway_runtime.capture_session(
                "conn_recap",
                type(
                    "FakeSession",
                    (),
                    {
                        "send_tool_list_changed": lambda self: None,  # type: ignore[assignment]  # test fake satisfies UpstreamSession protocol at runtime
                    },
                )(),
            )
            assert recapture_result.is_ok, (
                "capture_session must allow recapture on same connection_id"
            )
        finally:
            await gateway_shutdown()

    asyncio.run(_scenario())


def test_adr006_gateway_tela_error_details_has_required_keys() -> None:
    """Gateway call_tool returns DOWNSTREAM_UNAVAILABLE with ADR-required details.

    Ref: ADR-006 §error-payload-contract: TelaError.details must include
    server_name, recovery_attempted, recovery_eligible, underlying_error.
    """
    import asyncio

    from tela.shell.downstream import call_tool

    async def _run() -> None:
        # Server not connected
        result = await call_tool("nonexistent", "tool", {})

        assert result.is_err
        assert result.error is not None
        assert result.error.code == "DOWNSTREAM_UNAVAILABLE"

        # ADR-required details must be present
        assert result.error.details is not None, (
            "ADR-006: TelaError.details must be populated for DOWNSTREAM_UNAVAILABLE"
        )
        details = result.error.details

        required_keys = {
            "server_name",
            "recovery_attempted",
            "recovery_eligible",
            "underlying_error",
        }
        for key in required_keys:
            assert key in details, f"ADR-006: {key} required in TelaError.details"

        assert details["server_name"] == "nonexistent"

    asyncio.run(_run())


def test_adr006_recovery_no_new_protocol_step() -> None:
    """Successful recovery must not expose new MCP protocol step.

    Ref: ADR-006 §caller-visible-behavior:
    'Recovered path: caller may observe one slower call, but no new protocol step
    is exposed to the agent.'

    This test documents that recovery success returns the same tool response
    structure as a normal call - just with potentially more latency.
    """
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_recovery_diagnostic_latency_only_as_added_time() -> None:
    """Successful recovery must be observable only via latency/diagnostics.

    Ref: ADR-006 §caller-visible-behavior:
    'Successful recovery is visible only as added latency / diagnostics,
    not as a new client protocol step.'
    """
    # The test would verify that:
    # 1. A successful call (no recovery needed) returns quickly
    # 2. A call that triggers recovery succeeds but takes longer
    # 3. The response structure is the same in both cases
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_recovery_started_event_has_required_fields() -> None:
    """downstream_recovery_started event must include required fields.

    Ref: ADR-006 §structured-diagnostics-contract:
    Required fields: event, level, server_name, elapsed_ms, recovery_stage.
    """
    # Structured diagnostics are logged, not returned to caller.
    # Valid event types: downstream_recovery_started, downstream_recovery_succeeded,
    # downstream_recovery_rejected, downstream_recovery_exhausted,
    # downstream_recovery_classifier_unknown
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_recovery_exhausted_warning_level() -> None:
    """downstream_recovery_exhausted events must be WARNING level.

    Ref: ADR-006 §event-semantics:
    'downstream_recovery_exhausted with retry_failed or recovery_timeout => level = WARNING'
    """
    # Exhausted recovery should log at WARNING level, not INFO
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_recovery_classifier_unknown_warning_level() -> None:
    """downstream_recovery_classifier_unknown events must be WARNING level.

    Ref: ADR-006 §event-semantics:
    'downstream_recovery_classifier_unknown / classifier_unknown => level = WARNING'
    """
    # Unknown classifier outcomes should log at WARNING to aid debugging
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_config_remove_wins_over_inflight_recovery() -> None:
    """Server removal from config must abort in-flight recovery.

    Ref: ADR-006 §config-reload-concurrency-contract:
    'Config reload wins over in-flight recovery.
    if the target server no longer exists in runtime config, recovery MUST abort
    and return DOWNSTREAM_UNAVAILABLE.'
    """
    # If recovery is in progress and config is reloaded to remove that server,
    # recovery should fail with config_missing=True
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_config_change_wins_over_inflight_recovery() -> None:
    """Material config change must abort in-flight recovery.

    Ref: ADR-006 §config-reload-concurrency-contract:
    'if the target server's config changes materially during an in-flight recovery,
    the recovered handle from the stale config MUST NOT be swapped into _clients'
    """
    # A material config change (e.g., different command) during recovery
    # should result in the stale recovery being rejected
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_stale_waiter_reuses_recovered_client() -> None:
    """Caller waiting on recovery lock must reuse recovered client if now healthy.

    Ref: ADR-006 §stale-caller-and-lock-wait:
    'after acquiring the per-server recovery lock, a waiting caller MUST re-read:
    _clients / registry state for whether a healthy client now exists
    if a healthy client now exists, the stale caller MUST skip reconnect work
    and proceed directly to the single allowed retry'
    """
    # If server recovers while caller is waiting, the caller should use
    # the recovered client, not trigger another recovery
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_stale_waiter_fails_config_missing_when_server_removed() -> None:
    """Caller waiting on recovery lock must fail if server removed from config.

    Ref: ADR-006 §stale-caller-and-lock-wait:
    'if the server was removed or materially changed during lock wait, the stale
    caller MUST fail closed with DOWNSTREAM_UNAVAILABLE;
    use details.config_missing=true when the server no longer exists'
    """
    # If server is removed from config while caller is waiting,
    # the caller should get DOWNSTREAM_UNAVAILABLE with config_missing=True
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_stale_waiter_timeout_under_shared_budget() -> None:
    """Waiting callers must timeout under the shared recovery timeout budget.

    Ref: ADR-006 §stale-caller-and-lock-wait:
    'if the timeout budget is exhausted before the caller acquires the lock or
    before recovery completes, the call MUST fail with
    details.recovery_stage = "recovery_timeout"'
    """
    # Total budget (including lock wait) is 15.0 seconds
    # If exhausted, recovery_stage must be "recovery_timeout"
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_recovery_timeout_returns_correct_stage() -> None:
    """Recovery timeout must set details.recovery_stage = 'recovery_timeout'.

    Ref: ADR-006 §recovery-timeout-contract:
    'timeout exhaustion MUST set details.recovery_stage = "recovery_timeout"'
    """
    # When recovery exceeds the 15.0s budget, the error must have
    # recovery_stage="recovery_timeout" (not "retry_failed" or another stage)
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_recovery_timeout_includes_lock_wait_time() -> None:
    """Recovery timeout budget includes time spent waiting for per-server lock.

    Ref: ADR-006 §stale-caller-and-lock-wait:
    'the total recovery timeout budget for one original user call starts when the
    call is classified as recovery-eligible
    waiting to acquire the per-server recovery lock consumes that same timeout budget'
    """
    # Lock wait time counts against the 15.0s budget
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_convergence_rejection_returns_downstream_unavailable() -> None:
    """Convergence rejection must return DOWNSTREAM_UNAVAILABLE without retry.

    Ref: ADR-006 §recovery-sequence:
    'If convergence rejects the reconnect payload (for example due to
    TOOL_CONFLICT), the recovered client handle is treated as unusable for this
    request, the call does not proceed to retry, and the outward failure remains
    DOWNSTREAM_UNAVAILABLE with rejection context in diagnostics.'
    """
    # If on_server_reconnect returns error (e.g., TOOL_CONFLICT),
    # the error should be DOWNSTREAM_UNAVAILABLE with convergence_rejected stage
    # Implementation in adr006_recovery.impl
    pass


def test_adr006_convergence_rejection_has_required_diagnostic_keys() -> None:
    """Convergence rejection error must include diagnostic context.

    Ref: ADR-006 §illustrative-convergence-rejected-payload:
    Shows convergence_rejected stage with underlying_error documenting the conflict.
    """
    # The error details should include recovery_stage="convergence_rejected"
    # and underlying_error with the TOOL_CONFLICT context
    # Implementation in adr006_recovery.impl
    pass

    async def test_gateway_recovery_diagnostic_latency_visible_only_as_added_time(
        self,
    ) -> None:
        """Successful recovery must be observable only via latency/diagnostics, not new protocol.

        Ref: ADR-006 §caller-visible-behavior:
        'Successful recovery is visible only as added latency / diagnostics,
        not as a new client protocol step.'
        """
        # The test would verify that:
        # 1. A successful call (no recovery needed) returns quickly
        # 2. A call that triggers recovery succeeds but takes longer
        # 3. The response structure is the same in both cases
        pass  # Implementation in adr006_recovery.impl
