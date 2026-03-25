"""Regression: connection count semantics must use active_connections (int).

The status JSON payload exposes two fields with similar names:
  - ``active_connections`` (int): numeric count for liveness/count assertions
  - ``connections`` (list[ConnectionContext]): structural collection

Tests that compare connection counts numerically MUST use ``active_connections``.
Using ``connections`` (a list) in numeric comparisons silently passes or fails
depending on list truthiness, not actual count semantics.

This regression covers:
  - Payload shape: both fields present with correct types
  - Divergence: collection-valued ``connections`` and numeric ``active_connections``
    can differ in kind (list vs int) even when logically consistent
  - No silent coercion: ``connections`` is never accidentally numeric
"""

from __future__ import annotations

import json

import pytest

from tela.core.models import ConnectionContext, GatewayStatus, StatusResponse


class TestStatusPayloadCountSemantics:
    """Verify active_connections is the numeric truth, connections is structural."""

    def test_gateway_status_active_connections_is_int(self) -> None:
        """GatewayStatus.active_connections must be an int, not a list."""
        status = GatewayStatus(
            uptime_seconds=10.0,
            server_count=1,
            connected_servers=["fs"],
            active_connections=2,
            profile_count=1,
            total_tool_calls=5,
        )
        assert isinstance(status.active_connections, int)
        assert status.active_connections == 2

    def test_status_response_connections_is_list(self) -> None:
        """StatusResponse.connections must be a list, not an int."""
        ctx = ConnectionContext(
            connection_id="bridge_1",
            profile_name="dev",
            connected_at="2026-03-25T00:00:00Z",
            tool_call_count=0,
        )
        response = StatusResponse(
            uptime_seconds=10.0,
            server_count=1,
            connected_servers=["fs"],
            active_connections=1,
            profile_count=1,
            total_tool_calls=0,
            connections=[ctx],
        )
        assert isinstance(response.connections, list)
        assert len(response.connections) == 1
        assert response.connections[0].connection_id == "bridge_1"

    def test_json_payload_has_both_fields_with_distinct_types(self) -> None:
        """Serialized status payload must expose both fields with correct types."""
        ctx = ConnectionContext(
            connection_id="bridge_abc",
            profile_name="default",
            connected_at="2026-03-25T12:00:00Z",
            tool_call_count=3,
        )
        response = StatusResponse(
            uptime_seconds=42.0,
            server_count=2,
            connected_servers=["fs", "git"],
            active_connections=1,
            profile_count=1,
            total_tool_calls=3,
            connections=[ctx],
        )
        payload = json.loads(response.model_dump_json())

        assert "active_connections" in payload, (
            "Payload must include 'active_connections' (int)"
        )
        assert "connections" in payload, (
            "Payload must include 'connections' (list)"
        )

        assert isinstance(payload["active_connections"], int), (
            f"active_connections must be int, got {type(payload['active_connections'])}"
        )
        assert isinstance(payload["connections"], list), (
            f"connections must be list, got {type(payload['connections'])}"
        )

        assert payload["active_connections"] == 1
        assert len(payload["connections"]) == 1

    def test_empty_connections_payload_divergence(self) -> None:
        """When no connections exist, active_connections=0 and connections=[]."""
        response = StatusResponse(
            uptime_seconds=1.0,
            server_count=0,
            connected_servers=[],
            active_connections=0,
            profile_count=0,
            total_tool_calls=0,
            connections=[],
        )
        payload = json.loads(response.model_dump_json())

        assert payload["active_connections"] == 0
        assert payload["connections"] == []
        assert payload["active_connections"] != payload["connections"], (
            "active_connections (int 0) must not equal connections (list [])"
        )

    def test_numeric_comparison_on_connections_list_is_wrong(self) -> None:
        """Demonstrate that using connections (list) as numeric count is a bug.

        This test explicitly proves the failure mode: comparing a list to
        an int produces wrong results, which is why active_connections exists.
        """
        ctx = ConnectionContext(
            connection_id="bridge_1",
            profile_name="dev",
            connected_at="2026-03-25T00:00:00Z",
            tool_call_count=0,
        )
        response = StatusResponse(
            uptime_seconds=1.0,
            server_count=0,
            connected_servers=[],
            active_connections=1,
            profile_count=0,
            total_tool_calls=0,
            connections=[ctx],
        )
        payload = json.loads(response.model_dump_json())

        connections_field = payload["connections"]
        active_field = payload["active_connections"]

        assert isinstance(active_field, int), "active_connections is the numeric truth"
        assert isinstance(connections_field, list), "connections is structural"

        with pytest.raises(TypeError):
            _ = connections_field >= 1  # type: ignore[operator]
