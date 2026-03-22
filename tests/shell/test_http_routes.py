"""Tests for HTTP route handler implementations."""

from __future__ import annotations

import ast
import inspect

from tela.core.models import (
    ConnectRequest,
    ConnectionContext,
    DisconnectRequest,
    TelaConfig,
)
from tela.shell import http_routes
from tela.shell.gateway import get_runtime
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
        assert "AUTH_INVALID_TOKEN" in result.error

    def test_handle_status_accepts_valid_token_when_gateway_not_started(self) -> None:
        """Valid token but gateway not started should return error."""
        runtime = get_runtime()
        result = http_routes.handle_status("valid", "valid")
        assert result.is_err
        assert "GATEWAY_NOT_STARTED" in result.error

    def test_handle_status_returns_status_when_gateway_started(self) -> None:
        """Valid token with gateway running should return status."""
        runtime = get_runtime()
        runtime.config = TelaConfig()
        runtime.running = True
        runtime.connections.clear()
        runtime.total_tool_calls = 0

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
            runtime.config = None
            runtime.running = False


class TestHandleConnect:
    """Tests for handle_connect endpoint."""

    def test_handle_connect_rejects_invalid_token(self) -> None:
        req = ConnectRequest(connection_id="test-conn")
        result = http_routes.handle_connect("wrong-token", "expected-token", req)
        assert result.is_err
        assert "AUTH_INVALID_TOKEN" in result.error

    def test_handle_connect_rejects_when_gateway_not_started(self) -> None:
        req = ConnectRequest(connection_id="test-conn")
        result = http_routes.handle_connect("valid", "valid", req)
        assert result.is_err
        assert "GATEWAY_NOT_STARTED" in result.error

    def test_handle_connect_registers_connection(self) -> None:
        runtime = get_runtime()
        runtime.config = TelaConfig()
        runtime.running = True
        runtime.connections.clear()

        try:
            req = ConnectRequest(connection_id="test-conn-123")
            result = http_routes.handle_connect("valid", "valid", req)
            assert result.is_ok
            assert result.value is not None
            assert result.value["connection_id"] == "test-conn-123"
            assert result.value["status"] == "connected"
            assert len(runtime.connections) == 1
        finally:
            runtime.config = None
            runtime.running = False
            runtime.connections.clear()


class TestHandleDisconnect:
    """Tests for handle_disconnect endpoint."""

    def test_handle_disconnect_rejects_invalid_token(self) -> None:
        req = DisconnectRequest(connection_id="test-conn")
        result = http_routes.handle_disconnect("wrong-token", "expected-token", req)
        assert result.is_err
        assert "AUTH_INVALID_TOKEN" in result.error

    def test_handle_disconnect_rejects_when_gateway_not_started(self) -> None:
        req = DisconnectRequest(connection_id="test-conn")
        result = http_routes.handle_disconnect("valid", "valid", req)
        assert result.is_err
        assert "GATEWAY_NOT_STARTED" in result.error

    def test_handle_disconnect_removes_connection(self) -> None:
        runtime = get_runtime()
        runtime.config = TelaConfig()
        runtime.running = True
        runtime.connections.clear()
        ctx = ConnectionContext(
            connection_id="remove-me",
            profile_name="default",
            connected_at="2026-01-01T00:00:00Z",
        )
        runtime.connections.append(ctx)

        try:
            req = DisconnectRequest(connection_id="remove-me")
            result = http_routes.handle_disconnect("valid", "valid", req)
            assert result.is_ok
            assert result.value is not None
            assert result.value["connection_id"] == "remove-me"
            assert result.value["status"] == "disconnected"
            assert len(runtime.connections) == 0
        finally:
            runtime.config = None
            runtime.running = False
            runtime.connections.clear()

    def test_handle_disconnect_fails_for_nonexistent_connection(self) -> None:
        runtime = get_runtime()
        runtime.config = TelaConfig()
        runtime.running = True
        runtime.connections.clear()

        try:
            req = DisconnectRequest(connection_id="nonexistent")
            result = http_routes.handle_disconnect("valid", "valid", req)
            assert result.is_err
            assert "CONNECTION_NOT_FOUND" in result.error
        finally:
            runtime.config = None
            runtime.running = False


class TestHandleMcp:
    """Tests for handle_mcp endpoint."""

    def test_handle_mcp_rejects_invalid_token(self) -> None:
        result = http_routes.handle_mcp("wrong-token", "expected-token", {})
        assert result.is_err
        assert "AUTH_INVALID_TOKEN" in result.error

    def test_handle_mcp_rejects_when_gateway_not_started(self) -> None:
        result = http_routes.handle_mcp("valid", "valid", {})
        assert result.is_err

    def test_handle_mcp_validates_token_before_gateway_check(self) -> None:
        """Auth should be validated first, before gateway state check."""
        # Note: The implementation checks auth before gateway state
        result = http_routes.handle_mcp("wrong", "different", {})
        assert result.is_err
        assert "AUTH_INVALID_TOKEN" in result.error


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

        # There should be calls in handle_status, handle_connect, handle_disconnect, handle_mcp
        # handle_health has no auth requirement
        assert len(validate_calls) >= 4

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
    """Verify all 5 endpoints are implemented."""

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

    def test_handle_mcp_exists(self) -> None:
        assert hasattr(http_routes, "handle_mcp")
        assert callable(http_routes.handle_mcp)

    def test_route_handlers_tuple_contains_all(self) -> None:
        """Verify _ROUTE_HANDLERS tuple contains all 5 handlers."""
        assert hasattr(http_routes, "_ROUTE_HANDLERS")
        handlers = http_routes._ROUTE_HANDLERS
        assert len(handlers) == 5
        assert http_routes.handle_health in handlers
        assert http_routes.handle_status in handlers
        assert http_routes.handle_connect in handlers
        assert http_routes.handle_disconnect in handlers
        assert http_routes.handle_mcp in handlers
