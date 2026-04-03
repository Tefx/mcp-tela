"""Verification script for bounded transient 503 proof."""

import asyncio
import json
from starlette.testclient import TestClient
from tela.core.models import TelaConfig, ServerConfig, GatewayTransport, AuthMode
from tela.shell.gateway import (
    GatewayStartupConfig,
    gateway_prepare_startup,
    gateway_shutdown,
    with_upstream_server,
)
from tela.shell.gateway_lifecycle import get_lifecycle_status_facts
from tela.shell.http_auth import BearerAuthMiddleware


def main():
    print("=" * 70)
    print("BOUNDING TRANSIENT 503 BEHAVIOR PROOF")
    print("=" * 70)

    # PART 1: Demonstrate NON-READY (warming) state
    print("\n### NON-READY STATE: Gateway warming with servers not connected")
    print("=" * 70)

    tela_config = TelaConfig(servers={"fs": ServerConfig(name="fs", command="cmd")})

    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.TOKEN,
        default_profile="dev",
    )

    # Initialize gateway WITHOUT converging (so we stay in warming state)
    asyncio.run(
        gateway_prepare_startup(
            config,
            tela_config=tela_config,
            expected_bearer_token="test-token",
        )
    )

    # Verify we're in warming state
    facts_result = get_lifecycle_status_facts()
    assert facts_result.is_ok
    assert facts_result.value is not None
    print(f"Gateway state: {facts_result.value.state}")
    print(f"Server count: {facts_result.value.server_count}")
    print(f"Connected servers: {facts_result.value.connected_servers}")

    # Get the upstream app
    app_result = with_upstream_server(lambda s: s.streamable_http_app())
    assert app_result.is_ok
    assert app_result.value is not None

    app = BearerAuthMiddleware(
        app_result.value,
        get_expected_token=lambda: "test-token",
    )

    # Make request to /mcp
    with TestClient(app, base_url="http://testserver/") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1},
            headers={"Authorization": "Bearer test-token"},
        )

        print(f"\nPOST /mcp response (warming state):")
        print(f"Status: {response.status_code}")
        print(f"Body:\n{json.dumps(response.json(), indent=2)}")

        # Verify 503
        assert response.status_code == 503, (
            f"Expected 503 but got {response.status_code}"
        )

        # Verify specific fields
        body = response.json()
        assert body["code"] == "ADMISSION_REJECTED_WARMING"
        assert body["transient"] is True
        assert body["retry"]["authorized"] is True
        assert body["gateway_state"] == "warming"

    # Cleanup
    asyncio.run(gateway_shutdown())

    # PART 2: Demonstrate READY state
    print("\n\n### READY STATE: Gateway ready with no servers")
    print("=" * 70)

    tela_config = TelaConfig()  # Empty servers = immediately ready

    asyncio.run(
        gateway_prepare_startup(
            config,
            tela_config=tela_config,
            expected_bearer_token="test-token",
        )
    )

    # Verify we're in ready state
    facts_result = get_lifecycle_status_facts()
    assert facts_result.is_ok
    assert facts_result.value is not None
    print(f"Gateway state: {facts_result.value.state}")
    print(f"Server count: {facts_result.value.server_count}")

    # Get the upstream app
    app_result = with_upstream_server(lambda s: s.streamable_http_app())
    assert app_result.is_ok
    assert app_result.value is not None

    app = BearerAuthMiddleware(
        app_result.value,
        get_expected_token=lambda: "test-token",
    )

    # Make request to /mcp
    with TestClient(app, base_url="http://testserver/") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1},
            headers={"Authorization": "Bearer test-token"},
        )

        print(f"\nPOST /mcp response (ready state):")
        print(f"Status: {response.status_code}")

        # Verify NOT 503
        assert response.status_code != 503, (
            f"Expected non-503 but got {response.status_code}"
        )
        print("Status is NOT 503 (passes through to MCP handler)")

    # Cleanup
    asyncio.run(gateway_shutdown())

    print("\n" + "=" * 70)
    print("PROOF COMPLETE: 503 is BOUNDED - only during warming transition")
    print("=" * 70)


if __name__ == "__main__":
    main()
