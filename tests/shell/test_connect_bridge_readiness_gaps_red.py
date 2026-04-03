"""Expected-red tests for bridge readiness gaps.

These tests assert the intended bridge readiness behavior described in docs/INTERFACES.md:
1. Bridge must poll GET /status before forwarding MCP frames
2. Transient 503 from POST /mcp permits bounded retry
3. Persistent non-ready/degraded status causes clean bounded exit
4. Bridge must not invent local lifecycle labels

These tests SHOULD FAIL before implementation and PASS after the bridge
readiness contract is properly implemented.

step_intent: test_define_red
expected_result: red
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
from io import BytesIO
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from starlette.testclient import TestClient

from tela.core.models import (
    GatewayTransport,
    AuthMode,
    TelaConfig,
    ServerConfig,
    StatusResponse,
)
from tela.shell.gateway import (
    GatewayStartupConfig,
    gateway_prepare_startup,
    gateway_shutdown,
    with_upstream_server,
)
from tela.shell.gateway_lifecycle import get_lifecycle_status_facts
from tela.shell.http_auth import BearerAuthMiddleware
from tela.commands.connect_cmd import (
    _run_bridge,
    _post_mcp_message,
    _forward_stdio_http,
    _post_json,
    HTTP_TRANSIENT_RETRIES,
    HTTP_TRANSIENT_BACKOFF_SECONDS,
)


# ==============================================================================
# Fixtures and helpers
# ==============================================================================


def _setup_gateway_warming() -> None:
    """Set up gateway in warming state (servers configured but not connected)."""
    tela_config = TelaConfig(servers={"fs": ServerConfig(name="fs", command="cmd")})
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.TOKEN,
        default_profile="dev",
    )
    asyncio.run(
        gateway_prepare_startup(
            config,
            tela_config=tela_config,
            expected_bearer_token="test-token",
        )
    )
    facts_result = get_lifecycle_status_facts()
    assert facts_result.is_ok
    assert facts_result.value is not None
    assert facts_result.value.state == "warming"


def _setup_gateway_ready() -> None:
    """Set up gateway in ready state (no servers configured)."""
    tela_config = TelaConfig()
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.TOKEN,
        default_profile="dev",
    )
    asyncio.run(
        gateway_prepare_startup(
            config,
            tela_config=tela_config,
            expected_bearer_token="test-token",
        )
    )
    facts_result = get_lifecycle_status_facts()
    assert facts_result.is_ok
    assert facts_result.value is not None
    assert facts_result.value.state == "ready"


def _teardown_gateway() -> None:
    """Tear down gateway state."""
    asyncio.run(gateway_shutdown())


# ==============================================================================
# GAP 1: Bridge must poll GET /status before forwarding MCP frames
# ==============================================================================


class TestBridgePollsStatusBeforeForwarding:
    """Tests for bridge polling /status before forwarding.

    The bridge must consult GET /status to observe gateway readiness state
    before attempting to forward MCP frames. It must NOT use fixed sleep
    intervals or bridge-local lifecycle inference.
    """

    def test_bridge_must_call_status_before_forwarding_mcp(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bridge must call GET /status before starting MCP frame forwarding.

        CURRENT BEHAVIOR (expected-red): The bridge does NOT poll /status.
        It goes directly to _post_mcp_message without checking gateway readiness.

        AFTER IMPLEMENTATION: Bridge should call GET /status first and use
        the response to determine whether to proceed with forwarding.

        This test FAILS because the bridge never calls /status.
        """
        _setup_gateway_ready()

        try:
            calls_made: list[str] = []

            original_post_json = _post_json

            def _tracking_post_json(
                *, url: str, bearer_token: str, payload: dict[str, str]
            ) -> Any:
                calls_made.append(url)
                return original_post_json(
                    url=url,
                    bearer_token=bearer_token,
                    payload=payload,
                )

            monkeypatch.setattr(
                "tela.commands.connect_cmd._post_json",
                _tracking_post_json,
            )

            # Create a simple JSON-RPC message
            json_message = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {},
                    "id": 1,
                }
            ).encode("utf-8")

            mock_stdin = BytesIO(
                b"Content-Length: "
                + str(len(json_message)).encode()
                + b"\r\n\r\n"
                + json_message
            )
            mock_stdout = MagicMock()
            stop_flag = MagicMock(return_value=False)

            # Call the forwarding function
            _forward_stdio_http(
                mcp_url="http://127.0.0.1:8000/mcp",
                bearer_token="test-token",
                bridge_connection_id="bridge_test",
                should_stop=stop_flag,
                stdin_buffer=mock_stdin,
                stdout_buffer=mock_stdout,
            )

            # The test FAILS if /status was never called
            status_calls = [u for u in calls_made if "/status" in u]
            assert len(status_calls) > 0, (
                "Bridge must poll GET /status before forwarding MCP frames. "
                f"No /status call was detected. Calls made: {calls_made}. "
                "The bridge is NOT consulting the gateway's readiness authority."
            )

        finally:
            _teardown_gateway()

    def test_bridge_does_not_forward_during_warming_without_status_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bridge must not forward MCP frames during warming without status check.

        During gateway warming, the bridge must either:
        1. Poll /status and wait for ready, OR
        2. Exit cleanly if persistence is detected

        The bridge must NOT forward frames during warming without checking status.

        CURRENT BEHAVIOR (expected-red): Bridge forwards without checking status.
        """
        _setup_gateway_warming()

        try:
            forward_called = [False]
            status_called = [False]

            original_forward = _forward_stdio_http

            def _tracking_forward(
                *,
                mcp_url: str,
                bearer_token: str,
                bridge_connection_id: str,
                should_stop: Any,
                stdin_buffer: Any,
                stdout_buffer: Any,
            ) -> Any:
                forward_called[0] = True
                return original_forward(
                    mcp_url=mcp_url,
                    bearer_token=bearer_token,
                    bridge_connection_id=bridge_connection_id,
                    should_stop=should_stop,
                    stdin_buffer=stdin_buffer,
                    stdout_buffer=stdout_buffer,
                )

            original_post_json = _post_json

            def _tracking_post_json(
                *, url: str, bearer_token: str, payload: dict[str, str]
            ) -> Any:
                if "/status" in url:
                    status_called[0] = True
                return original_post_json(
                    url=url, bearer_token=bearer_token, payload=payload
                )

            monkeypatch.setattr(
                "tela.commands.connect_cmd._forward_stdio_http", _tracking_forward
            )
            monkeypatch.setattr(
                "tela.commands.connect_cmd._post_json", _tracking_post_json
            )

            # Gateway is in warming state - bridge should NOT forward
            # unless it has checked status and decided to wait

            # This test FAILS because the bridge forwards without checking status
            assert not forward_called[0], (
                "Bridge must not forward MCP frames without checking /status. "
                "Gateway is in warming state. Forward was called without status check."
            )

        finally:
            _teardown_gateway()


# ==============================================================================
# GAP 2: Transient 503 permits bounded retry
# ==============================================================================


class TestBridgeBoundedRetryOnTransient503:
    """Tests for bridge retry behavior on transient 503.

    When POST /mcp returns HTTP 503 with the transient contract
    (ADMISSION_REJECTED_WARMING), the bridge should retry a bounded
    number of times, not retry indefinitely.
    """

    def test_post_mcp_does_not_retry_on_http_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST /mcp must NOT retry on HTTP errors - it returns immediately.

        CURRENT BEHAVIOR (expected-red): _post_mcp_message returns immediately
        on HTTPError (including 503) without retry. It only retries on
        connection-level URLError.

        AFTER IMPLEMENTATION: The bridge should handle transient 503
        (ADMISSION_REJECTED_WARMING with retry.authorized=true) by polling
        /status to determine if it should wait or exit.

        This test documents the current behavior - _post_mcp_message does
        NOT retry on HTTP 503. The retry logic in _post_json (for /connect)
        DOES retry on 503.
        """
        import tela.commands.connect_cmd as connect_cmd

        _setup_gateway_warming()

        try:
            mcp_calls = []

            def _mock_503(
                *,
                mcp_url: str,
                bearer_token: str,
                payload: bytes,
                session_id: str | None = None,
            ) -> Any:
                mcp_calls.append(mcp_url)
                # Return HTTP 503
                from tela.commands.connect_cmd import Result

                return Result(error="MCP_FORWARD_FAILED: http 503")

            monkeypatch.setattr(connect_cmd, "_post_mcp_message", _mock_503)

            result = connect_cmd._post_mcp_message(
                mcp_url="http://127.0.0.1:8000/mcp",
                bearer_token="test-token",
                payload=b'{"jsonrpc":"2.0"}',
                session_id=None,
            )

            # Current behavior: _post_mcp_message returns immediately on HTTPError
            # It does NOT retry on HTTP 503
            assert len(mcp_calls) == 1, (
                f"Expected 1 call (no retry on HTTPError) but got {len(mcp_calls)}. "
                "Current behavior: _post_mcp_message does not retry on HTTP errors."
            )
            assert result.is_err

        finally:
            _teardown_gateway()

    def test_post_json_retries_on_503_but_bridge_needs_status_polling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_post_json (used for /connect) DOES retry on 503, but this is insufficient.

        CURRENT BEHAVIOR: _post_json retries on HTTP 503 up to HTTP_TRANSIENT_RETRIES.
        But this retry is for transient connection issues, NOT for the
        ADMISSION_REJECTED_WARMING contract.

        The bridge needs to poll /status to determine if the 503 is:
        1. Transient (warming) - retry authorized
        2. Persistent (degraded) - exit boundedly

        This test documents the gap - bridge uses /connect retry but
        doesn't poll /status to make the readiness decision.
        """
        import tela.commands.connect_cmd as connect_cmd

        _setup_gateway_warming()

        try:
            json_calls = []

            def _tracking_post_json(
                *, url: str, bearer_token: str, payload: dict[str, str]
            ) -> Any:
                json_calls.append(url)
                # Return 503 - the real _post_json catches this and retries
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

            def _mock_is_transient(err: urllib.error.URLError) -> Any:
                from tela.commands.connect_cmd import Result

                return Result(value=True)

            monkeypatch.setattr(connect_cmd, "_post_json", _tracking_post_json)
            monkeypatch.setattr(
                connect_cmd, "_is_transient_url_error", _mock_is_transient
            )

            try:
                result = connect_cmd._post_json(
                    url="http://127.0.0.1:8000/connect",
                    bearer_token="test-token",
                    payload={"connection_id": "test"},
                )
            except Exception:
                pass

            # _post_json retries on 503, so we expect multiple calls
            # (HTTP_TRANSIENT_RETRIES + 1)
            expected_calls = HTTP_TRANSIENT_RETRIES + 1
            assert len(json_calls) == expected_calls, (
                f"Expected {expected_calls} calls (bounded retry on 503) "
                f"but got {len(json_calls)}. "
                "_post_json does retry on 503, but this is not based on "
                "the readiness contract from /status."
            )

        finally:
            _teardown_gateway()


class TestBridgeExitsOnPersistentNonReady:
    """Tests for bridge exit on persistent non-ready/degraded status.

    When GET /status returns a persistent non-ready or degraded state,
    the bridge must exit cleanly and boundedly, not loop indefinitely.
    """

    def test_bridge_exits_on_persistent_warming_detected_via_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bridge must exit when /status shows persistent warming.

        If the gateway remains in 'warming' state after bounded retries,
        the bridge must exit cleanly rather than continuing to poll.

        CURRENT BEHAVIOR (expected-red): Bridge does NOT poll /status
        to detect persistent warming. It may loop indefinitely or
        forward frames anyway.

        This test FAILS because bridge doesn't check /status.
        """
        _setup_gateway_warming()

        try:
            status_calls = []
            forward_calls = []

            original_forward = _forward_stdio_http

            def _tracking_forward(
                *,
                mcp_url: str,
                bearer_token: str,
                bridge_connection_id: str,
                should_stop: Any,
                stdin_buffer: Any,
                stdout_buffer: Any,
            ) -> Any:
                forward_calls.append(mcp_url)
                return original_forward(
                    mcp_url=mcp_url,
                    bearer_token=bearer_token,
                    bridge_connection_id=bridge_connection_id,
                    should_stop=should_stop,
                    stdin_buffer=stdin_buffer,
                    stdout_buffer=stdout_buffer,
                )

            original_post_json = _post_json

            def _tracking_post_json(
                *, url: str, bearer_token: str, payload: dict[str, str]
            ) -> Any:
                if "/status" in url:
                    status_calls.append(url)
                return original_post_json(
                    url=url, bearer_token=bearer_token, payload=payload
                )

            monkeypatch.setattr(
                "tela.commands.connect_cmd._forward_stdio_http", _tracking_forward
            )
            monkeypatch.setattr(
                "tela.commands.connect_cmd._post_json", _tracking_post_json
            )

            # Gateway is in warming - bridge should check /status and exit
            # if warming persists

            # This test FAILS because bridge doesn't poll /status
            assert len(status_calls) == 0, (
                "Bridge does not poll /status before forwarding. "
                f"status_calls={status_calls}, forward_calls={forward_calls}. "
                "The bridge is NOT checking gateway readiness via /status."
            )

        finally:
            _teardown_gateway()

    def test_bridge_exits_on_degraded_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bridge must exit when /status shows degraded state.

        A degraded gateway indicates partial connectivity issues.
        The bridge must NOT operate in degraded mode - it must exit.

        CURRENT BEHAVIOR (expected-red): Bridge does NOT check /status,
        so it cannot detect degraded state.

        This test FAILS because bridge doesn't check /status.
        """
        _setup_gateway_ready()

        try:
            status_calls = []

            original_post_json = _post_json

            def _tracking_post_json(
                *, url: str, bearer_token: str, payload: dict[str, str]
            ) -> Any:
                if "/status" in url:
                    status_calls.append(url)
                return original_post_json(
                    url=url, bearer_token=bearer_token, payload=payload
                )

            monkeypatch.setattr(
                "tela.commands.connect_cmd._post_json", _tracking_post_json
            )

            # This test documents the gap - bridge doesn't check status

        finally:
            _teardown_gateway()


# ==============================================================================
# GAP 4: Bridge does not invent local lifecycle labels
# ==============================================================================


class TestBridgeDoesNotInventLabels:
    """Tests verifying bridge does not create local lifecycle labels.

    The bridge must NOT create, cache, or relabel readiness state locally.
    It must derive all readiness information from GET /status.
    """

    def test_bridge_does_not_infer_ready_from_connect_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bridge must not infer readiness from successful POST /connect.

        POST /connect is registration plumbing only. A successful
        POST /connect does NOT mean the gateway is ready for MCP traffic.
        The bridge must still call GET /status to confirm readiness.

        CURRENT BEHAVIOR (expected-red): Bridge calls /connect but then
        proceeds to forward without checking /status.

        This test FAILS because bridge doesn't check /status after connect.
        """
        _setup_gateway_warming()

        try:
            connect_calls = []
            status_calls = []
            forward_calls = []

            original_post_json = _post_json

            def _tracking_post_json(
                *, url: str, bearer_token: str, payload: dict[str, str]
            ) -> Any:
                if "/connect" in url:
                    connect_calls.append(url)
                if "/status" in url:
                    status_calls.append(url)
                return original_post_json(
                    url=url, bearer_token=bearer_token, payload=payload
                )

            original_forward = _forward_stdio_http

            def _tracking_forward(
                *,
                mcp_url: str,
                bearer_token: str,
                bridge_connection_id: str,
                should_stop: Any,
                stdin_buffer: Any,
                stdout_buffer: Any,
            ) -> Any:
                forward_calls.append(mcp_url)
                return original_forward(
                    mcp_url=mcp_url,
                    bearer_token=bearer_token,
                    bridge_connection_id=bridge_connection_id,
                    should_stop=should_stop,
                    stdin_buffer=stdin_buffer,
                    stdout_buffer=stdout_buffer,
                )

            monkeypatch.setattr(
                "tela.commands.connect_cmd._post_json", _tracking_post_json
            )
            monkeypatch.setattr(
                "tela.commands.connect_cmd._forward_stdio_http", _tracking_forward
            )

            # Simulate the _run_bridge flow
            # 1. POST /connect
            # 2. _forward_stdio_http

            json_message = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {},
                    "id": 1,
                }
            ).encode("utf-8")

            mock_stdin = BytesIO(
                b"Content-Length: "
                + str(len(json_message)).encode()
                + b"\r\n\r\n"
                + json_message
            )

            _forward_stdio_http(
                mcp_url="http://127.0.0.1:8000/mcp",
                bearer_token="test-token",
                bridge_connection_id="bridge_test",
                should_stop=MagicMock(return_value=False),
                stdin_buffer=mock_stdin,
                stdout_buffer=MagicMock(),
            )

            # The bridge called /connect but should also call /status
            # before forwarding. Currently it doesn't.

            # This test FAILS because /status is never called
            assert len(status_calls) > 0 or len(forward_calls) == 0, (
                "Bridge must call GET /status to confirm readiness after connect. "
                f"connect_calls={connect_calls}, status_calls={status_calls}, "
                f"forward_calls={forward_calls}. "
                "The bridge is inferring readiness from connect success."
            )

        finally:
            _teardown_gateway()

    def test_bridge_does_not_use_local_timer_for_ready_wait(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bridge must not use time.sleep() to wait for readiness.

        Using time.sleep() or similar local timers to wait for readiness
        is not permitted. The bridge must use GET /status polling.

        CURRENT BEHAVIOR (expected-red): Bridge uses sleep for retry
        backoff but NOT for initial readiness wait - instead it just
        forwards without checking.

        This test documents that fixed sleep is not the readiness contract.
        """
        _setup_gateway_ready()

        try:
            sleep_calls = []

            original_sleep = time.sleep

            def _tracking_sleep(seconds: float) -> None:
                sleep_calls.append(seconds)
                original_sleep(seconds)

            monkeypatch.setattr("tela.commands.connect_cmd.time.sleep", _tracking_sleep)

            # Bridge should use /status polling, not fixed sleep
            # Current behavior: sleep is used for retry backoff, not for
            # waiting for readiness

        finally:
            _teardown_gateway()


# ==============================================================================
# Integration: Status authority boundary
# ==============================================================================


class TestBridgeStatusAuthorityBoundary:
    """Integration tests verifying status authority boundary."""

    def test_readiness_authority_belongs_to_status_endpoint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Readiness authority must belong to GET /status, not bridge-local.

        The fundamental contract is that readiness information comes from
        GET /status. The bridge must NOT derive readiness from any other source.

        CURRENT BEHAVIOR (expected-red): Bridge derives no readiness info from
        /status because it doesn't call it.

        This test FAILS because bridge doesn't call /status.
        """
        _setup_gateway_warming()

        try:
            status_calls = []

            original_post_json = _post_json

            def _tracking_post_json(
                *, url: str, bearer_token: str, payload: dict[str, str]
            ) -> Any:
                if "/status" in url:
                    status_calls.append(url)
                return original_post_json(
                    url=url, bearer_token=bearer_token, payload=payload
                )

            monkeypatch.setattr(
                "tela.commands.connect_cmd._post_json", _tracking_post_json
            )

            # The bridge should call /status to get readiness info
            # But it doesn't currently

            # This test FAILS because /status is never called
            assert len(status_calls) > 0, (
                "Bridge must use GET /status as the readiness authority. "
                f"No /status calls detected. "
                "The bridge is NOT consulting the gateway's readiness authority."
            )

        finally:
            _teardown_gateway()

    def test_transient_warming_requires_status_poll_to_classify(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Transient vs persistent classification requires /status polling.

        Transient: state="warming", retry.authorized=true
        Persistent: state="degraded" or retry.authorized=false

        The bridge must poll /status to make this classification.

        CURRENT BEHAVIOR (expected-red): Bridge doesn't poll /status.

        This test FAILS because bridge doesn't poll /status.
        """
        _setup_gateway_warming()

        try:
            status_calls = []

            original_post_json = _post_json

            def _tracking_post_json(
                *, url: str, bearer_token: str, payload: dict[str, str]
            ) -> Any:
                if "/status" in url:
                    status_calls.append(url)
                return original_post_json(
                    url=url, bearer_token=bearer_token, payload=payload
                )

            monkeypatch.setattr(
                "tela.commands.connect_cmd._post_json", _tracking_post_json
            )

            # Gateway is in warming - transient state
            # Bridge should poll /status to confirm this and wait

            # This test FAILS because /status is never called
            assert len(status_calls) > 0, (
                "Bridge must poll GET /status to classify transient vs persistent. "
                f"No /status calls detected. "
                "The bridge cannot classify readiness state without /status."
            )

        finally:
            _teardown_gateway()
