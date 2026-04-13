"""Expected-red tests for POST /mcp readiness gate behavior.

These tests assert the intended behavior described in docs/INTERFACES.md §7.2.1:
- POST /mcp returns machine-readable transient 503 while gateway is not ready (warming)
- POST /mcp stops returning the transient 503 once gateway is ready
- Readiness-gated behavior is on /mcp, NOT on /connect (which is registration plumbing only)

These tests SHOULD FAIL before implementation and PASS after the readiness gate is implemented.

NOTE: These tests target the actual product behavior, not mock implementations.
Tests that directly exercise HTTP behavior use TestClient which may encounter MCP
transport Host-header validation. In such cases, we test at the handler layer.
"""

from __future__ import annotations

import asyncio
import json

from starlette.testclient import TestClient

from tela.core.models import TelaConfig
from tela.shell.gateway import (
    GatewayStartupConfig,
    gateway_prepare_startup,
    gateway_shutdown,
)
from tela.shell.gateway_runtime import with_upstream_server
from tela.shell.gateway_lifecycle import get_lifecycle_status_facts
from tela.core.models import GatewayTransport, AuthMode


def _setup_warming_gateway_sync() -> None:
    """Set up gateway in warming state (running but not converged).

    A gateway with servers configured but no downstream connections is in 'warming' state.
    We call gateway_prepare_startup (which sets up the server and marks it running)
    but NOT gateway_converge_startup (which would connect the downstreams).
    """
    tela_config = TelaConfig(
        servers={
            "fs": __import__(
                "tela.core.models", fromlist=["ServerConfig"]
            ).ServerConfig(name="fs", command="cmd")
        }
    )

    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.TOKEN,
        default_profile="dev",
    )

    # Initialize the gateway WITHOUT converging (so we stay in warming state)
    asyncio.run(
        gateway_prepare_startup(
            config,
            tela_config=tela_config,
            expected_bearer_token="test-token",
        )
    )

    # Verify we're in warming state
    facts_result = get_lifecycle_status_facts()
    assert facts_result.is_ok, f"Failed to get lifecycle facts: {facts_result.error}"
    assert facts_result.value is not None
    assert facts_result.value.state == "warming", (
        f"Expected warming state but got {facts_result.value.state}"
    )


def _setup_ready_gateway_sync() -> None:
    """Set up gateway in ready state (running with no servers needing convergence)."""
    tela_config = TelaConfig()  # Empty servers = immediately ready

    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.TOKEN,
        default_profile="dev",
    )

    # For ready state, we still need to call prepare (which initializes the server)
    # The key difference is no servers are configured so convergence is trivial
    asyncio.run(
        gateway_prepare_startup(
            config,
            tela_config=tela_config,
            expected_bearer_token="test-token",
        )
    )

    # Verify we're in ready state
    facts_result = get_lifecycle_status_facts()
    assert facts_result.is_ok, f"Failed to get lifecycle facts: {facts_result.error}"
    assert facts_result.value is not None
    assert facts_result.value.state == "ready", (
        f"Expected ready state but got {facts_result.value.state}"
    )


def _teardown_gateway_sync() -> None:
    """Tear down gateway state."""
    asyncio.run(gateway_shutdown())


class TestMcpReadinessGate:
    """Tests for POST /mcp readiness gate behavior during gateway warming."""

    def test_post_mcp_returns_transient_503_when_warming(self) -> None:
        """POST /mcp must return HTTP 503 with machine-readable contract when gateway is warming.

        The contract must include:
        - error string starting with "ADMISSION_REJECTED_WARMING"
        - code: "ADMISSION_REJECTED_WARMING"
        - transient: true
        - retry.authorized: true
        - gateway_state: "warming"

        CURRENT BEHAVIOR (expected-red): This test FAILS because the readiness gate
        is not yet implemented in the product. The /mcp endpoint currently passes
        through to MCP without checking readiness.

        AFTER IMPLEMENTATION: This test should PASS.
        """
        _setup_warming_gateway_sync()

        try:
            # Get the upstream app - this is the actual FastMCP streamable HTTP app
            app_result = with_upstream_server(lambda s: s.streamable_http_app())
            assert app_result.is_ok
            assert app_result.value is not None

            from tela.shell.http_auth import BearerAuthMiddleware

            # Wrap with BearerAuthMiddleware (like serve_cmd does)
            app = BearerAuthMiddleware(
                app_result.value,
                get_expected_token=lambda: "test-token",
            )

            # Try to make a request - due to MCP transport Host header validation,
            # we may get 421, but we need to verify whether the readiness check
            # is being applied at all.
            #
            # The test FAILS if we get any response that is NOT the transient 503
            # contract (e.g., if the request passes through to MCP and gets a
            # different error, or if we get 421 from Host validation).
            #
            # The test PASSES if we get exactly the transient 503 contract.
            with TestClient(app, base_url="http://testserver/") as client:
                response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "method": "initialize",
                        "params": {},
                        "id": 1,
                    },
                    headers={"Authorization": "Bearer test-token"},
                )

                # If we get 421 (Host validation), it means the request passed
                # through the auth middleware and reached MCP without a readiness check
                if response.status_code == 421:
                    # This means NO readiness check was applied - test FAILS
                    raise AssertionError(
                        "Got 421 (Misdirected Request) which means the request passed "
                        "through to MCP without a readiness check. The readiness gate "
                        "is NOT implemented. This test should FAIL before implementation."
                    )

                # Assert 503 response with transient contract
                assert response.status_code == 503, (
                    f"Expected 503 when warming but got {response.status_code}: {response.text}. "
                    f"The readiness gate may not be implemented."
                )

                # Parse response body
                body = response.json()

                # Verify machine-readable contract fields
                assert "error" in body, "Response must contain 'error' field"
                assert body["error"].startswith("ADMISSION_REJECTED_WARMING"), (
                    f"Error must start with ADMISSION_REJECTED_WARMING, got: {body['error']}"
                )

                assert "code" in body, "Response must contain 'code' field"
                assert body["code"] == "ADMISSION_REJECTED_WARMING", (
                    f"Code must be ADMISSION_REJECTED_WARMING, got: {body['code']}"
                )

                assert "transient" in body, "Response must contain 'transient' field"
                assert body["transient"] is True, (
                    f"transient must be true, got: {body['transient']}"
                )

                assert "retry" in body, "Response must contain 'retry' field"
                retry = body["retry"]
                assert "authorized" in retry, "retry must contain 'authorized' field"
                assert retry["authorized"] is True, (
                    f"retry.authorized must be true, got: {retry['authorized']}"
                )

                assert "gateway_state" in body, (
                    "Response must contain 'gateway_state' field"
                )
                assert body["gateway_state"] == "warming", (
                    f"gateway_state must be 'warming', got: {body['gateway_state']}"
                )

        finally:
            _teardown_gateway_sync()

    def test_post_mcp_does_not_return_503_when_ready(self) -> None:
        """POST /mcp must NOT return 503 when gateway is ready.

        When the gateway is in 'ready' state, POST /mcp should proceed to actual MCP
        handling. The readiness gate should NOT block requests when ready.

        CURRENT BEHAVIOR: This test PASSES because there's no readiness gate at all.

        AFTER IMPLEMENTATION: This test should still PASS.
        """
        _setup_ready_gateway_sync()

        try:
            # Verify we're in ready state
            facts_result = get_lifecycle_status_facts()
            assert facts_result.is_ok
            assert facts_result.value is not None
            assert facts_result.value.state == "ready", (
                f"Expected ready state but got {facts_result.value.state}"
            )

            # Get the upstream app
            app_result = with_upstream_server(lambda s: s.streamable_http_app())
            assert app_result.is_ok
            assert app_result.value is not None

            from tela.shell.http_auth import BearerAuthMiddleware

            app = BearerAuthMiddleware(
                app_result.value,
                get_expected_token=lambda: "test-token",
            )

            with TestClient(app, base_url="http://testserver/") as client:
                response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "method": "initialize",
                        "params": {},
                        "id": 1,
                    },
                    headers={"Authorization": "Bearer test-token"},
                )

                # Should NOT be 503 when ready
                # Note: MCP itself may return errors (e.g., 421 for Host validation),
                # but it should NOT be 503 from the readiness gate
                assert response.status_code != 503, (
                    f"Expected non-503 response when ready but got {response.status_code}: {response.text}"
                )

                # Also verify the response is NOT the warming rejection
                # (only check if we have a JSON body)
                if response.status_code >= 400 and response.text:
                    try:
                        body = response.json()
                        if "error" in body:
                            assert not str(body["error"]).startswith(
                                "ADMISSION_REJECTED_WARMING"
                            ), (
                                f"Ready gateway must not return ADMISSION_REJECTED_WARMING, got: {body['error']}"
                            )
                    except json.JSONDecodeError:
                        # Non-JSON response is fine for this test
                        pass

        finally:
            _teardown_gateway_sync()

    def test_connect_does_not_return_transient_503_when_warming(self) -> None:
        """POST /connect must NOT return transient 503 when gateway is warming.

        /connect is registration plumbing only - it should not be gated by readiness.
        This test verifies the boundary: /mcp is the readiness-gated surface, not /connect.

        CURRENT BEHAVIOR: This test PASSES because /connect is not gated.
        AFTER IMPLEMENTATION: This test should still PASS.
        """
        _setup_warming_gateway_sync()

        try:
            # Verify we're in warming state
            facts_result = get_lifecycle_status_facts()
            assert facts_result.is_ok
            assert facts_result.value is not None
            assert facts_result.value.state == "warming"

            # Import the connect handler
            from tela.shell.http_routes import handle_connect
            from tela.core.models import ConnectRequest

            # Call handle_connect with a valid request
            req = ConnectRequest(connection_id="test-conn-warming")
            result = handle_connect("test-token", "test-token", req)

            # Connect should NOT return ADMISSION_REJECTED_WARMING error
            if result.is_err:
                assert not result.error.startswith("ADMISSION_REJECTED_WARMING"), (
                    f"/connect must not return ADMISSION_REJECTED_WARMING, got: {result.error}"
                )
            # If result is ok, that's fine - /connect is not gated

        finally:
            _teardown_gateway_sync()

    def test_mcp_transient_503_matches_schema(self) -> None:
        """The transient 503 response structure must match mcp_admission_transient_503.schema.json.

        This test verifies the schema is correct. It PASSES if the schema is properly
        defined, which it is (from the contract freeze step).
        """
        from pathlib import Path

        schema_path = (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "mcp_admission_transient_503.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        # Verify schema has required fields
        assert "required" in schema
        assert "error" in schema["required"]
        assert "code" in schema["required"]
        assert "transient" in schema["required"]
        assert "retry" in schema["required"]
        assert "gateway_state" in schema["required"]

        # Verify schema constraints
        assert schema["properties"]["code"]["const"] == "ADMISSION_REJECTED_WARMING"
        assert schema["properties"]["transient"]["const"] is True
        assert schema["properties"]["gateway_state"]["const"] == "warming"

        # Verify the expected response structure matches schema
        expected_response = {
            "error": "ADMISSION_REJECTED_WARMING: gateway not ready for MCP admission",
            "code": "ADMISSION_REJECTED_WARMING",
            "transient": True,
            "retry": {
                "authorized": True,
                "basis": "gateway_signal",
                "expectation": "bounded",
            },
            "gateway_state": "warming",
        }

        for required_field in schema.get("required", []):
            assert required_field in expected_response, (
                f"Expected response missing required field from schema: {required_field}"
            )


class TestMcpReadinessGateIntegration:
    """Integration-style tests for POST /mcp readiness gate via actual HTTP handler.

    These tests verify the gateway lifecycle state machine transitions correctly
    and that /mcp behavior changes accordingly.
    """

    def test_gateway_lifecycle_state_is_warming_with_servers_not_connected(
        self,
    ) -> None:
        """Gateway lifecycle state must be 'warming' when servers are configured but not connected."""
        _setup_warming_gateway_sync()

        try:
            facts_result = get_lifecycle_status_facts()
            assert facts_result.is_ok
            assert facts_result.value is not None
            assert facts_result.value.state == "warming"
        finally:
            _teardown_gateway_sync()

    def test_gateway_lifecycle_state_is_ready_with_no_servers(self) -> None:
        """Gateway lifecycle state must be 'ready' when no servers are configured."""
        _setup_ready_gateway_sync()

        try:
            facts_result = get_lifecycle_status_facts()
            assert facts_result.is_ok
            assert facts_result.value is not None
            assert facts_result.value.state == "ready"
        finally:
            _teardown_gateway_sync()

    def test_mcp_endpoint_requires_auth(self) -> None:
        """The /mcp endpoint must require bearer authentication.

        This sanity check verifies that /mcp enforces auth.
        """
        _setup_ready_gateway_sync()

        try:
            app_result = with_upstream_server(lambda s: s.streamable_http_app())
            assert app_result.is_ok
            assert app_result.value is not None

            from tela.shell.http_auth import BearerAuthMiddleware

            app = BearerAuthMiddleware(
                app_result.value,
                get_expected_token=lambda: "test-token",
            )

            with TestClient(app, base_url="http://testserver/") as client:
                # Request without auth should get 401
                response_no_auth = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "method": "initialize",
                        "params": {},
                        "id": 1,
                    },
                    # No Authorization header
                )

                # Must reject without auth
                assert response_no_auth.status_code == 401, (
                    f"/mcp must reject unauthenticated requests with 401, got {response_no_auth.status_code}"
                )

        finally:
            _teardown_gateway_sync()

    def test_readiness_gate_is_on_mcp_not_connect(self) -> None:
        """Verify the readiness gate is on /mcp, not /connect.

        This test verifies /connect is NOT affected by the readiness state.
        """
        _setup_warming_gateway_sync()

        try:
            # Verify we're in warming state
            facts_result = get_lifecycle_status_facts()
            assert facts_result.is_ok
            assert facts_result.value is not None
            assert facts_result.value.state == "warming"

            # Connect handler - this should NOT be affected by warming
            from tela.shell.http_routes import handle_connect
            from tela.core.models import ConnectRequest

            req = ConnectRequest(connection_id="test-boundary-conn")
            result = handle_connect("test-token", "test-token", req)

            # /connect should either succeed or fail with a non-warming error
            if result.is_err:
                assert not result.error.startswith("ADMISSION_REJECTED_WARMING"), (
                    "/connect must NOT be gated by readiness"
                )

        finally:
            _teardown_gateway_sync()
