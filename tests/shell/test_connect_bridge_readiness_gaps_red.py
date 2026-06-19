"""Bridge readiness compatibility tests.

This file previously contained expected-red assertions that encoded pre-readiness
behavior. Those assertions are obsolete now that ``_run_bridge`` calls
``_wait_for_gateway_readiness`` and ``_wait_for_gateway_readiness`` polls
``GET /status`` via ``_get_gateway_status`` before forwarding MCP traffic.
"""

from __future__ import annotations

from typing import Any

import pytest

import tela.commands.connect_cmd as connect_cmd
import tela.commands.connect_bridge as connect_bridge
from tela.core.models import StatusResponse
from tela.shell.result import Result


# Mapping: connect_cmd alias -> connect_bridge public name for dual monkeypatching.
_BRIDGE_ALIAS_MAP: dict[str, str] = {
    "_post_json": "post_json",
    "_post_json_once": "post_json_once",
    "_post_mcp_message": "post_mcp_message",
    "_forward_stdio_http": "forward_stdio_http",
    "_get_gateway_status": "_get_gateway_status",
    "_wait_for_gateway_readiness": "_wait_for_gateway_readiness",
    "_run_bridge": "run_bridge",
}


def _patch_bridge(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    """Monkeypatch both connect_cmd alias and connect_bridge definition."""
    monkeypatch.setattr(connect_cmd, name, value)
    bridge_name = _BRIDGE_ALIAS_MAP.get(name)
    if bridge_name is not None:
        monkeypatch.setattr(connect_bridge, bridge_name, value)


def _status(*, state: str, degraded_reason: str | None = None) -> StatusResponse:
    """Build a minimal valid ``StatusResponse`` for readiness tests."""
    return StatusResponse(
        uptime_seconds=1.0,
        server_count=0,
        connected_servers=[],
        active_connections=0,
        profile_count=0,
        total_tool_calls=0,
        state=state,
        degraded_reason=degraded_reason,
        connections=[],
        audit_entries=[],
    )


class TestBridgeReadinessBehavior:
    """Readiness behavior validations for connect bridge lifecycle."""

    def test_run_bridge_waits_for_status_before_forwarding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bridge must perform readiness wait before entering forwarding loop."""
        call_order: list[str] = []

        def _fake_post_json(
            *, url: str, bearer_token: str, payload: dict[str, str]
        ) -> Result[None, str]:
            if url.endswith("/connect"):
                call_order.append("connect")
            elif url.endswith("/disconnect"):
                call_order.append("disconnect")
            return Result(value=None)

        def _fake_wait_for_gateway_readiness(
            *, status_url: str, bearer_token: str, max_polls: int
        ) -> Result[None, str]:
            assert status_url.endswith("/status")
            assert max_polls == connect_cmd.BRIDGE_READINESS_MAX_POLLS
            call_order.append("wait")
            return Result(value=None)

        def _fake_forward_stdio_http(
            *,
            mcp_url: str,
            bearer_token: str,
            bridge_connection_id: str,
            should_stop: Any,
            stdin_buffer: Any,
            stdout_buffer: Any,
            max_recovery_attempts: int = 3,
            recover_transport: Any = None,
            reset_recovery_attempts: Any = None,
        ) -> Result[None, str]:
            assert mcp_url.endswith("/mcp")
            _ = reset_recovery_attempts
            call_order.append("forward")
            return Result(value=None)

        _patch_bridge(monkeypatch, "_post_json", _fake_post_json)
        _patch_bridge(
            monkeypatch, "_wait_for_gateway_readiness", _fake_wait_for_gateway_readiness
        )
        _patch_bridge(monkeypatch, "_forward_stdio_http", _fake_forward_stdio_http)

        result = connect_cmd._run_bridge(
            host="127.0.0.1", port=8000, bearer_token="test-token"
        )

        assert result.is_ok
        assert call_order == ["connect", "wait", "forward", "disconnect"]

    def test_wait_for_gateway_readiness_exits_boundedly_on_persistent_warming(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Persistent warming must return bounded non-ready error."""
        poll_count = 0

        def _fake_get_gateway_status(
            *, status_url: str, bearer_token: str
        ) -> Result[StatusResponse, str]:
            nonlocal poll_count
            poll_count += 1
            return Result(value=_status(state="warming"))

        _patch_bridge(monkeypatch, "_get_gateway_status", _fake_get_gateway_status)
        monkeypatch.setattr(connect_bridge.time, "sleep", lambda _seconds: None)

        result = connect_cmd._wait_for_gateway_readiness(
            status_url="http://127.0.0.1:8000/status",
            bearer_token="test-token",
            max_polls=3,
        )

        assert result.is_err
        assert result.error is not None
        assert "bounded readiness wait exhausted" in result.error
        assert "state=warming" in result.error
        assert poll_count == 3

    def test_wait_for_gateway_readiness_accepts_degraded_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Degraded state is serveable when partial providers are published."""

        def _fake_get_gateway_status(
            *, status_url: str, bearer_token: str
        ) -> Result[StatusResponse, str]:
            return Result(
                value=_status(
                    state="degraded",
                    degraded_reason="provider_tools_list_timeout:slow",
                )
            )

        _patch_bridge(monkeypatch, "_get_gateway_status", _fake_get_gateway_status)

        result = connect_cmd._wait_for_gateway_readiness(
            status_url="http://127.0.0.1:8000/status",
            bearer_token="test-token",
            max_polls=4,
        )

        assert result.is_ok

    def test_post_mcp_message_delegates_transient_503_to_phase_aware_executor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MCP data-plane no longer owns an internal transient-503 retry loop."""
        request_count = 0

        def _fake_post_mcp_http(**_kwargs: Any) -> Result[
            connect_bridge.BridgeHttpResponse, connect_bridge.BridgeHttpError
        ]:
            nonlocal request_count
            request_count += 1
            return Result(
                error=connect_bridge.BridgeHttpError(
                    phase="http_status",
                    message="HTTP_503: gateway warming",
                    request_sent=True,
                    mcp_admitted=False,
                    status_code=503,
                    retryable_warming=True,
                )
            )

        monkeypatch.setattr(connect_bridge, "post_mcp_http", _fake_post_mcp_http)

        result = connect_bridge.post_mcp_message(
            mcp_url="http://127.0.0.1:8000/mcp",
            bearer_token="test-token",
            payload=b'{"jsonrpc":"2.0","id":1}',
            session_id=None,
        )

        assert result.is_err
        assert result.error == "MCP_FORWARD_FAILED: http 503"
        assert request_count == 1

    def test_readiness_authority_uses_get_gateway_status_not_post_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Status authority should come from ``_get_gateway_status`` polling."""
        call_order: list[str] = []

        def _fake_get_gateway_status(
            *, status_url: str, bearer_token: str
        ) -> Result[StatusResponse, str]:
            call_order.append("get_status")
            return Result(value=_status(state="ready"))

        def _fake_post_json(
            *, url: str, bearer_token: str, payload: dict[str, str]
        ) -> Result[None, str]:
            if url.endswith("/connect"):
                call_order.append("connect")
            elif url.endswith("/disconnect"):
                call_order.append("disconnect")
            return Result(value=None)

        def _fake_forward_stdio_http(
            *,
            mcp_url: str,
            bearer_token: str,
            bridge_connection_id: str,
            should_stop: Any,
            stdin_buffer: Any,
            stdout_buffer: Any,
            max_recovery_attempts: int = 3,
            recover_transport: Any = None,
            reset_recovery_attempts: Any = None,
        ) -> Result[None, str]:
            _ = reset_recovery_attempts
            call_order.append("forward")
            return Result(value=None)

        _patch_bridge(monkeypatch, "_get_gateway_status", _fake_get_gateway_status)
        _patch_bridge(monkeypatch, "_post_json", _fake_post_json)
        _patch_bridge(monkeypatch, "_forward_stdio_http", _fake_forward_stdio_http)
        monkeypatch.setattr(connect_bridge.time, "sleep", lambda _seconds: None)

        result = connect_cmd._run_bridge(
            host="127.0.0.1", port=8000, bearer_token="test-token"
        )

        assert result.is_ok
        assert call_order == ["connect", "get_status", "forward", "disconnect"]
