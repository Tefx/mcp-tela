"""Tests for HTTP route handler implementations."""

from __future__ import annotations

import ast
import inspect

from tela.core.models import (
    AuthConfig,
    AuthMode,
    ConnectRequest,
    ConnectionContext,
    DisconnectRequest,
    ProfileConfig,
    TelaConfig,
)
from tela.shell import http_routes
from tela.shell.result import Result
from tela.shell.connection_lifecycle import ConnectionCleanupOutcome
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,
    get_runtime_connections_snapshot,
    set_runtime_config,
    set_runtime_running,
    set_runtime_total_tool_calls,
)
from tela.shell.http_auth import validate_bearer_token


class TestHandleHealth:
    """Tests for handle_health endpoint."""

    def test_handle_health_returns_ok_status(self) -> None:
        result = http_routes.handle_health()
        assert result.is_ok
        assert result.value is not None
        assert result.value.status == "ok"

    def test_handle_health_returns_pid(self) -> None:
        result = http_routes.handle_health()
        assert result.is_ok
        assert result.value is not None
        assert result.value.pid > 0

    def test_handle_health_no_auth_required(self) -> None:
        """GET /health must work without any authentication."""
        result = http_routes.handle_health()
        assert result.is_ok


class TestHandleStatus:
    """Tests for handle_status endpoint."""

    def test_handle_status_rejects_invalid_token(self) -> None:
        result = http_routes.handle_status("wrong-token", "expected-token")
        assert result.is_err
        assert result.error is not None
        assert "AUTH_INVALID_TOKEN" in result.error

    def test_handle_status_accepts_valid_token_when_gateway_not_started(self) -> None:
        """Valid token but gateway not started should return error."""
        result = http_routes.handle_status("valid", "valid")
        assert result.is_err
        assert result.error is not None
        assert "GATEWAY_NOT_STARTED" in result.error

    def test_handle_status_returns_status_when_gateway_started(self) -> None:
        """Valid token with gateway running should return status."""
        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()
        set_runtime_total_tool_calls(0)

        try:
            result = http_routes.handle_status("valid", "valid")
            assert result.is_ok
            assert result.value is not None
            assert hasattr(result.value, "uptime_seconds")
            assert hasattr(result.value, "server_count")
            assert hasattr(result.value, "active_connections")
            assert hasattr(result.value, "profile_count")
            assert hasattr(result.value, "total_tool_calls")
        finally:
            set_runtime_config(None)
            set_runtime_running(False)


class TestHandleStatusLifecycleSnapshots:
    """Tests for lifecycle state snapshots via GET /status.

    Ref: docs/INTERFACES.md §7.2.1 GET /status Response Schema
    Tests cover starting, warming, ready, and degraded lifecycle snapshots.
    """

    def test_status_snapshot_starting_phase(self) -> None:
        """Starting phase: gateway running but no downstream connections."""
        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()
        set_runtime_total_tool_calls(0)

        try:
            result = http_routes.handle_status("valid", "valid")
            assert result.is_ok
            status = result.value
            assert status.server_count >= 0
            assert status.connected_servers == []
            assert status.active_connections == 0
            assert status.total_tool_calls == 0
        finally:
            set_runtime_config(None)
            set_runtime_running(False)

    def test_status_snapshot_ready_phase(self) -> None:
        """Ready phase: downstream servers connected."""
        tela_config = TelaConfig(
            servers={
                "fs": __import__(
                    "tela.core.models", fromlist=["ServerConfig"]
                ).ServerConfig(name="fs", command="cmd")
            }
        )
        set_runtime_config(tela_config)
        set_runtime_running(True)
        clear_runtime_connections()
        set_runtime_total_tool_calls(0)

        try:
            result = http_routes.handle_status("valid", "valid")
            assert result.is_ok
            status = result.value
            assert status.server_count == 1
            # connected_servers reflects actual downstream state
            assert isinstance(status.connected_servers, list)
            assert isinstance(status.active_connections, int)
        finally:
            set_runtime_config(None)
            set_runtime_running(False)

    def test_status_snapshot_with_active_connections(self) -> None:
        """Active connections are reflected in status snapshot."""
        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()
        set_runtime_total_tool_calls(5)

        # Add a connection
        ctx = ConnectionContext(
            connection_id="bridge-test-123",
            profile_id="dev",
            connected_at="2026-03-25T12:00:00Z",
        )
        add_runtime_connection(ctx)

        try:
            result = http_routes.handle_status("valid", "valid")
            assert result.is_ok
            status = result.value
            assert status.active_connections >= 1
            assert len(status.connections) >= 1
            assert status.total_tool_calls == 5
        finally:
            set_runtime_config(None)
            set_runtime_running(False)
            clear_runtime_connections()


class TestHandleConnect:
    """Tests for handle_connect endpoint."""

    def test_handle_connect_rejects_invalid_token(self) -> None:
        req = ConnectRequest(server_name="test-conn")
        result = http_routes.handle_connect("wrong-token", "expected-token", req)
        assert result.is_err
        assert result.error is not None
        assert "AUTH_INVALID_TOKEN" in result.error

    def test_handle_connect_rejects_when_gateway_not_started(self) -> None:
        req = ConnectRequest(server_name="test-conn")
        result = http_routes.handle_connect("valid", "valid", req)
        assert result.is_err
        assert result.error is not None
        assert "GATEWAY_NOT_STARTED" in result.error

    def test_handle_connect_is_not_readiness_gated_while_warming(self) -> None:
        """POST /connect remains outside readiness gating during warming."""
        from tela.shell.gateway_runtime import set_runtime_config, set_runtime_running
        from tela.shell.gateway_lifecycle import get_lifecycle_status_facts

        # Configure gateway with servers but no connected downstreams (warming state)
        tela_config = TelaConfig(
            servers={
                "fs": __import__(
                    "tela.core.models", fromlist=["ServerConfig"]
                ).ServerConfig(name="fs", command="cmd")
            }
        )
        set_runtime_config(tela_config)
        set_runtime_running(True)

        try:
            # Verify we're in warming state
            facts_result = get_lifecycle_status_facts()
            assert facts_result.is_ok
            assert facts_result.value is not None
            assert facts_result.value.state == "warming"

            req = ConnectRequest(server_name="test-conn-warming")
            result = http_routes.handle_connect("valid", "valid", req)
            assert result.is_ok, (
                f"/connect must remain available during warming; got: {result.error}"
            )
            assert result.value is not None
            assert result.value["connection_id"] == "test-conn-warming"
            assert result.value["status"] == "connected"
        finally:
            set_runtime_config(None)
            set_runtime_running(False)
            clear_runtime_connections()

    def test_handle_connect_accepts_when_ready(self) -> None:
        """Bridge admission succeeds once lifecycle is ready."""
        from tela.shell.gateway_runtime import set_runtime_config, set_runtime_running

        # Configure with empty servers so it's immediately ready (no convergence needed)
        set_runtime_config(TelaConfig())
        set_runtime_running(True)

        try:
            # Verify we're in ready state (no servers configured)
            req = ConnectRequest(server_name="test-conn-ready")
            result = http_routes.handle_connect("valid", "valid", req)
            assert result.is_ok, f"Expected ok but got: {result.error}"
            assert result.value is not None
            assert "profile_id" not in result.value
        finally:
            set_runtime_config(None)
            set_runtime_running(False)

    def test_handle_connect_registers_pending_bridge_without_runtime_binding(
        self,
    ) -> None:
        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()

        try:
            req = ConnectRequest(server_name="test-conn-123")
            result = http_routes.handle_connect("valid", "valid", req)
            assert result.is_ok
            assert result.value is not None
            assert result.value["connection_id"] == "test-conn-123"
            assert result.value["status"] == "connected"
            assert "profile_id" not in result.value
            snapshot = get_runtime_connections_snapshot()
            assert snapshot.is_ok
            assert snapshot.value is not None
            assert len(snapshot.value) == 0
        finally:
            set_runtime_config(None)
            set_runtime_running(False)
            clear_runtime_connections()

    def test_handle_connect_token_mode_does_not_fabricate_profile_binding(self) -> None:
        """Token-mode /connect must not fabricate a bound profile or connection."""
        set_runtime_config(
            TelaConfig(
                auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret"]),
                profiles={"dev": ProfileConfig(name="dev", default=True)},
                resolved_default_profile="dev",
            )
        )
        set_runtime_running(True)
        clear_runtime_connections()

        try:
            req = ConnectRequest(server_name="token-bridge-123")
            result = http_routes.handle_connect("valid", "valid", req)
            assert result.is_ok
            assert result.value is not None
            assert result.value == {
                "connection_id": "token-bridge-123",
                "status": "connected",
            }
            snapshot = get_runtime_connections_snapshot()
            assert snapshot.is_ok
            assert snapshot.value is not None
            assert len(snapshot.value) == 0
        finally:
            set_runtime_config(None)
            set_runtime_running(False)
            clear_runtime_connections()

    def test_handle_disconnect_succeeds_for_pending_bridge_registration(self) -> None:
        """Pending bridge registrations must be removable before initialize."""
        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()

        try:
            req = ConnectRequest(server_name="pending-only-conn")
            connect_result = http_routes.handle_connect("valid", "valid", req)
            assert connect_result.is_ok

            disconnect_result = http_routes.handle_disconnect(
                "valid",
                "valid",
                DisconnectRequest(connection_id="pending-only-conn"),
            )
            assert disconnect_result.is_ok
            assert disconnect_result.value is not None
            assert disconnect_result.value["connection_id"] == "pending-only-conn"
            assert disconnect_result.value["status"] == "disconnected"
            snapshot = get_runtime_connections_snapshot()
            assert snapshot.is_ok
            assert snapshot.value is not None
            assert len(snapshot.value) == 0
        finally:
            set_runtime_config(None)
            set_runtime_running(False)
            clear_runtime_connections()


class TestHandleDisconnect:
    """Tests for handle_disconnect endpoint."""

    def test_handle_disconnect_rejects_invalid_token(self) -> None:
        req = DisconnectRequest(connection_id="test-conn")
        result = http_routes.handle_disconnect("wrong-token", "expected-token", req)
        assert result.is_err
        assert result.error is not None
        assert "AUTH_INVALID_TOKEN" in result.error

    def test_handle_disconnect_rejects_when_gateway_not_started(self) -> None:
        req = DisconnectRequest(connection_id="test-conn")
        result = http_routes.handle_disconnect("valid", "valid", req)
        assert result.is_err
        assert result.error is not None
        assert "GATEWAY_NOT_STARTED" in result.error

    def test_handle_disconnect_removes_connection(self) -> None:
        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()
        ctx = ConnectionContext(
            connection_id="remove-me",
            profile_id="default",
            connected_at="2026-01-01T00:00:00Z",
        )
        add_runtime_connection(ctx)

        try:
            req = DisconnectRequest(connection_id="remove-me")
            result = http_routes.handle_disconnect("valid", "valid", req)
            assert result.is_ok
            assert result.value is not None
            assert result.value["connection_id"] == "remove-me"
            assert result.value["status"] == "disconnected"
            snapshot = get_runtime_connections_snapshot()
            assert snapshot.is_ok
            assert snapshot.value is not None
            assert len(snapshot.value) == 0
        finally:
            set_runtime_config(None)
            set_runtime_running(False)
            clear_runtime_connections()

    def test_handle_disconnect_fails_for_nonexistent_connection(self) -> None:
        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()

        try:
            req = DisconnectRequest(connection_id="nonexistent")
            result = http_routes.handle_disconnect("valid", "valid", req)
            assert result.is_err
            assert result.error is not None
            assert "CONNECTION_NOT_FOUND" in result.error
        finally:
            set_runtime_config(None)
            set_runtime_running(False)

    def test_handle_disconnect_uses_shared_cleanup_authority(self, monkeypatch) -> None:
        set_runtime_config(TelaConfig())
        set_runtime_running(True)
        clear_runtime_connections()

        called_ids: list[str] = []

        def _fake_cleanup(connection_id: str) -> Result[ConnectionCleanupOutcome, str]:
            called_ids.append(connection_id)
            return Result(
                value=ConnectionCleanupOutcome(
                    connection_id=connection_id,
                    removed_runtime_connection=True,
                    removed_bridge_registration=False,
                )
            )

        monkeypatch.setattr(http_routes, "cleanup_connection_by_id", _fake_cleanup)

        try:
            req = DisconnectRequest(connection_id="cleanup-authority-1")
            result = http_routes.handle_disconnect("valid", "valid", req)
            assert result.is_ok
            assert called_ids == ["cleanup-authority-1"]
        finally:
            set_runtime_config(None)
            set_runtime_running(False)


class TestBearerTokenUsage:
    """Tests to verify hmac.compare_digest is used via validate_bearer_token."""

    def test_all_handlers_use_validate_bearer_token(self) -> None:
        """Verify all handlers that need auth call validate_bearer_token."""
        source = inspect.getsource(http_routes)
        tree = ast.parse(source)

        # Find all calls to validate_bearer_token
        validate_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "validate_bearer_token"
                ):
                    validate_calls.append(node)

        # There should be calls in handle_status, handle_connect, handle_disconnect
        # handle_health has no auth requirement
        assert len(validate_calls) >= 3

    def test_validate_bearer_token_uses_hmac_compare_digest(self) -> None:
        """Verify the underlying auth uses constant-time comparison."""
        source = inspect.getsource(validate_bearer_token)
        tree = ast.parse(source)

        compare_digest_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "hmac"
            and node.func.attr == "compare_digest"
        ]
        assert len(compare_digest_calls) >= 1


class TestAllEndpointsImplemented:
    """Verify all 4 endpoints are implemented."""

    def test_handle_health_exists(self) -> None:
        assert hasattr(http_routes, "handle_health")
        assert callable(http_routes.handle_health)

    def test_handle_status_exists(self) -> None:
        assert hasattr(http_routes, "handle_status")
        assert callable(http_routes.handle_status)

    def test_handle_connect_exists(self) -> None:
        assert hasattr(http_routes, "handle_connect")
        assert callable(http_routes.handle_connect)

    def test_handle_disconnect_exists(self) -> None:
        assert hasattr(http_routes, "handle_disconnect")
        assert callable(http_routes.handle_disconnect)

    def test_route_handlers_tuple_contains_all(self) -> None:
        """Verify _ROUTE_HANDLERS tuple contains all 4 handlers."""
        assert hasattr(http_routes, "_ROUTE_HANDLERS")
        handlers = http_routes._ROUTE_HANDLERS
        assert len(handlers) == 4
        assert http_routes.handle_health in handlers
        assert http_routes.handle_status in handlers
        assert http_routes.handle_connect in handlers
        assert http_routes.handle_disconnect in handlers
