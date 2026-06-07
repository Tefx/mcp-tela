"""Executable ADR-009 contract tests.

These tests intentionally describe the requested request-lifecycle semantics
before production behavior is implemented.  They are scoped to tests only and
should be red until the ADR-009 bridge protocol, phase-aware HTTP executor, and
forwarding-loop classifier exist.
"""

from __future__ import annotations

import importlib
import io
import json
import math
from typing import Any

import pytest

from tela.commands import connect_bridge
from tela.core import bridge_protocol
from tela.shell.result import Result


def _json_lines(output: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.decode("utf-8").splitlines()]


def _request(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _bridge_http_module() -> Any:
    try:
        return importlib.import_module("tela.commands.bridge_http")
    except ModuleNotFoundError as exc:  # pragma: no cover - red contract path
        pytest.fail(
            "Missing ADR-009 production seam: tela.commands.bridge_http with "
            "BridgeHttpError, BridgeHttpResponse, and post_mcp_http is required "
            "to express phase-aware MCP HTTP executor behavior."
        )
        raise exc


@pytest.mark.parametrize(
    ("payload", "expected_policy"),
    [
        (b'{"jsonrpc":"2.0","id":1,"method":"initialize"}', "SAFE"),
        (b'{"jsonrpc":"2.0","id":2,"method":"ping"}', "SAFE"),
        (b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}', "SAFE"),
        (b'{"jsonrpc":"2.0","id":4,"method":"tools/call"}', "UNSAFE"),
        (b'{"jsonrpc":"2.0","id":5,"method":"unknown/method"}', "UNSAFE"),
        (b'{"jsonrpc":"2.0","method":"notifications/progress"}', "UNSAFE"),
        (b'[{"jsonrpc":"2.0","id":6,"method":"tools/list"}]', "UNSAFE"),
        (b'{not-json', "UNSAFE"),
        (b'null', "UNSAFE"),
        (b'[]', "UNSAFE"),
    ],
)
def test_adr009_core_replay_policy_is_deterministic_and_fail_closed(
    payload: bytes, expected_policy: str
) -> None:
    """ADR-009 replay policy: only initialize/ping/tools/list are safe."""

    policy_enum = getattr(bridge_protocol, "BridgeReplayPolicy", None)
    classify = getattr(bridge_protocol, "bridge_replay_policy", None)

    assert policy_enum is not None, "BridgeReplayPolicy enum is required by ADR-009"
    assert callable(classify), "bridge_replay_policy(payload) is required by ADR-009"
    assert classify(payload) is getattr(policy_enum, expected_policy)


def test_adr009_jsonrpc_helpers_classify_notifications_and_batch_fail_closed() -> None:
    """ADR-009 requires singular ids, notification detection, and unsafe batches."""

    request_id = getattr(bridge_protocol, "jsonrpc_request_id", None)
    is_notification = getattr(bridge_protocol, "jsonrpc_is_notification", None)
    classify = getattr(bridge_protocol, "bridge_replay_policy", None)
    policy_enum = getattr(bridge_protocol, "BridgeReplayPolicy", None)

    assert callable(request_id), "jsonrpc_request_id(payload) is required by ADR-009"
    assert callable(is_notification), "jsonrpc_is_notification(payload) is required by ADR-009"
    assert callable(classify), "bridge_replay_policy(payload) is required by ADR-009"
    assert policy_enum is not None, "BridgeReplayPolicy enum is required by ADR-009"

    assert request_id(b'{"jsonrpc":"2.0","id":"abc","method":"ping"}') == "abc"
    assert request_id(b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]') is None
    assert is_notification(b'{"jsonrpc":"2.0","method":"notifications/cancelled"}') is True
    assert is_notification(b'{"jsonrpc":"2.0","id":1,"method":"ping"}') is False
    assert classify(b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]') is policy_enum.UNSAFE


@pytest.mark.parametrize(
    "payload",
    [
        b'{"jsonrpc":"2.0","id":11,"error":{"code":-32000,"message":"RECONNECT_REQUIRED: bridge stale"}}',
        b'{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"bridge initialize requires pre-registered connection"}}',
    ],
)
def test_adr009_response_recovery_only_for_gateway_owned_error_envelopes(
    payload: bytes,
) -> None:
    assert bridge_protocol.response_requires_bridge_recovery([payload]) is True


def test_adr009_tool_result_marker_text_does_not_trigger_recovery() -> None:
    """Downstream tool-result text must not authorize bridge recovery/replay."""

    payload = (
        b'{"jsonrpc":"2.0","id":12,"result":{"isError":true,"content":'
        b'[{"type":"text","text":"RECONNECT_REQUIRED: bridge stale"}]}}'
    )

    assert bridge_protocol.response_requires_bridge_recovery([payload]) is False


def test_adr009_phase_http_executor_connect_and_write_phase_contract() -> None:
    """Phase-aware executor exposes request_sent=False vs unknown partial send."""

    module = _bridge_http_module()
    assert hasattr(module, "BridgeHttpError")
    assert hasattr(module, "BridgeHttpResponse")
    assert callable(getattr(module, "post_mcp_http", None))

    error = module.BridgeHttpError(
        phase="connect", message="Connection refused", request_sent=False, mcp_admitted=None
    )
    assert error.phase == "connect"
    assert error.request_sent is False
    assert error.mcp_admitted is None

    partial = module.BridgeHttpError(
        phase="write", message="Broken pipe", request_sent=None, mcp_admitted=None
    )
    assert partial.phase == "write"
    assert partial.request_sent is None


def test_adr009_phase_http_executor_invalid_timeout_contract() -> None:
    """Invalid timeout arguments return INVALID_TIMEOUT instead of throwing."""

    module = _bridge_http_module()
    result = module.post_mcp_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        payload=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        session_id=None,
        connect_timeout_seconds=0.0,
        write_timeout_seconds=1.0,
        response_timeout_seconds=None,
        is_503_retryable=lambda _body: False,
    )
    assert result.is_err
    assert result.error.phase == "connect"
    assert result.error.request_sent is False
    assert result.error.mcp_admitted is None
    assert result.error.message == (
        "INVALID_TIMEOUT: connect_timeout_seconds and write_timeout_seconds must be finite > 0; "
        "response_timeout_seconds must be None or finite > 0"
    )


def test_adr009_phase_http_executor_deadline_and_warming_status_contracts() -> None:
    """Executor contract has explicit timeout, warming 503, and plain-status fields."""

    module = _bridge_http_module()
    timeout_error = module.BridgeHttpError(
        phase="response_headers",
        message="MCP_REQUEST_TIMEOUT: response deadline expired",
        request_sent=True,
        mcp_admitted=None,
    )
    assert timeout_error.phase == "response_headers"
    assert timeout_error.request_sent is True
    assert timeout_error.mcp_admitted is None
    assert "MCP_REQUEST_TIMEOUT" in timeout_error.message

    warming = module.BridgeHttpError(
        phase="http_status",
        message="HTTP_503: gateway warming",
        request_sent=True,
        mcp_admitted=False,
        status_code=503,
        retryable_warming=True,
    )
    assert warming.request_sent is True
    assert warming.mcp_admitted is False
    assert warming.retryable_warming is True

    plain = module.BridgeHttpError(
        phase="http_status",
        message="MCP_FORWARD_FAILED: http 503",
        request_sent=True,
        mcp_admitted=None,
        status_code=503,
        retryable_warming=False,
    )
    assert plain.request_sent is True
    assert plain.mcp_admitted is None
    assert plain.retryable_warming is False


def test_adr009_response_timeout_none_does_not_synthesize_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic slow MCP calls must not map string timeouts to recovery exhaustion."""

    requests = _request({"jsonrpc": "2.0", "id": "slow", "method": "tools/call"})
    stdout_buffer = io.BytesIO()
    recovery_calls = {"count": 0}

    def _post_mcp_message(**_kwargs: Any) -> Result[tuple[str, bytes, str | None], str]:
        return Result(error="MCP_REQUEST_TIMEOUT: response deadline expired")

    def _recover_transport() -> Result[tuple[str, str], str]:
        recovery_calls["count"] += 1
        return Result(error="BRIDGE_RECOVERY_EXHAUSTED: should not recover slow requests")

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    assert recovery_calls["count"] == 0
    message = _json_lines(stdout_buffer.getvalue())[0]
    assert message["error"]["data"] == {"code": "MCP_REQUEST_TIMEOUT"}
    assert "BRIDGE_RECOVERY_EXHAUSTED" not in json.dumps(message)


def test_adr009_connect_bridge_uses_post_mcp_http_without_bridge_response_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default MCP forwarding must not impose the old fixed response timeout."""

    module = _bridge_http_module()
    requests = _request({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
    stdout_buffer = io.BytesIO()
    calls: list[dict[str, Any]] = []

    def _post_mcp_http(**kwargs: Any) -> Result[Any, Any]:
        calls.append(kwargs)
        return Result(
            value=module.BridgeHttpResponse(
                content_type="application/json",
                body=b'{"jsonrpc":"2.0","id":"ping","result":{}}',
                session_id="s-1",
            )
        )

    def _retry_http_request(**_kwargs: Any) -> Result[Any, str]:
        raise AssertionError("MCP data-plane must not use retry_http_request")

    monkeypatch.setattr(connect_bridge, "post_mcp_http", _post_mcp_http)
    monkeypatch.setattr(connect_bridge, "retry_http_request", _retry_http_request)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
    )

    assert result.is_ok
    assert len(calls) == 1
    assert calls[0]["connect_timeout_seconds"] == connect_bridge.HTTP_TIMEOUT_SECONDS
    assert calls[0]["write_timeout_seconds"] == connect_bridge.HTTP_TIMEOUT_SECONDS
    assert calls[0]["response_timeout_seconds"] is None
    assert _json_lines(stdout_buffer.getvalue())[0]["id"] == "ping"


@pytest.mark.parametrize(
    ("error", "payload"),
    [
        (
            "presend",
            {"jsonrpc": "2.0", "id": "call", "method": "tools/call"},
        ),
        (
            "non_admitted",
            {"jsonrpc": "2.0", "id": "call", "method": "tools/call"},
        ),
    ],
)
def test_adr009_connect_bridge_presend_and_non_admitted_recover_and_replay(
    monkeypatch: pytest.MonkeyPatch, error: str, payload: dict[str, Any]
) -> None:
    """Pre-send and gateway-proved non-admission may replay even unsafe methods."""

    module = _bridge_http_module()
    requests = _request(payload)
    stdout_buffer = io.BytesIO()
    post_calls = {"count": 0}
    recovery_calls = {"count": 0}

    def _post_mcp_http(**_kwargs: Any) -> Result[Any, Any]:
        post_calls["count"] += 1
        if post_calls["count"] == 1 and error == "presend":
            return Result(
                error=module.BridgeHttpError(
                    phase="connect",
                    message="connect refused",
                    request_sent=False,
                    mcp_admitted=None,
                )
            )
        if post_calls["count"] == 1:
            return Result(
                error=module.BridgeHttpError(
                    phase="http_status",
                    message="HTTP_503: gateway warming",
                    request_sent=True,
                    mcp_admitted=False,
                    status_code=503,
                    retryable_warming=True,
                )
            )
        return Result(
            value=module.BridgeHttpResponse(
                content_type="application/json",
                body=b'{"jsonrpc":"2.0","id":"call","result":{"ok":true}}',
                session_id=None,
            )
        )

    def _recover_transport() -> Result[tuple[str, str], str]:
        recovery_calls["count"] += 1
        return Result(value=("http://127.0.0.1:2/mcp", "token2"))

    monkeypatch.setattr(connect_bridge, "post_mcp_http", _post_mcp_http)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    assert post_calls["count"] == 2
    assert recovery_calls["count"] == 1
    assert _json_lines(stdout_buffer.getvalue())[0]["result"] == {"ok": True}


@pytest.mark.parametrize("safe_method", ["tools/list", "ping"])
def test_adr009_connect_bridge_post_mcp_http_safe_request_replays_after_unknown_admission(
    monkeypatch: pytest.MonkeyPatch, safe_method: str
) -> None:
    """Safe JSON-RPC methods may recover/replay after post-send unknown admission."""

    module = _bridge_http_module()
    requests = _request({"jsonrpc": "2.0", "id": safe_method, "method": safe_method})
    stdout_buffer = io.BytesIO()
    post_calls = {"count": 0}
    recovery_calls = {"count": 0}

    def _post_mcp_http(**_kwargs: Any) -> Result[Any, Any]:
        post_calls["count"] += 1
        if post_calls["count"] == 1:
            return Result(
                error=module.BridgeHttpError(
                    phase="response_body",
                    message="body reset",
                    request_sent=True,
                    mcp_admitted=None,
                )
            )
        body = json.dumps(
            {"jsonrpc": "2.0", "id": safe_method, "result": {"safe": True}}
        ).encode("utf-8")
        return Result(
            value=module.BridgeHttpResponse(
                content_type="application/json", body=body, session_id=None
            )
        )

    def _recover_transport() -> Result[tuple[str, str], str]:
        recovery_calls["count"] += 1
        return Result(value=("http://127.0.0.1:2/mcp", "token2"))

    monkeypatch.setattr(connect_bridge, "post_mcp_http", _post_mcp_http)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    assert post_calls["count"] == 2
    assert recovery_calls["count"] == 1
    assert _json_lines(stdout_buffer.getvalue())[0]["result"] == {"safe": True}


def test_adr009_connect_bridge_post_mcp_http_unsafe_tools_call_no_replay_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsafe sent tools/call with unknown admission emits MCP_RESPONSE_INTERRUPTED."""

    module = _bridge_http_module()
    requests = _request({"jsonrpc": "2.0", "id": "call", "method": "tools/call"})
    stdout_buffer = io.BytesIO()
    recovery_calls = {"count": 0}

    def _post_mcp_http(**_kwargs: Any) -> Result[Any, Any]:
        return Result(
            error=module.BridgeHttpError(
                phase="response_body",
                message="body reset",
                request_sent=True,
                mcp_admitted=None,
            )
        )

    def _recover_transport() -> Result[tuple[str, str], str]:
        recovery_calls["count"] += 1
        return Result(value=("http://127.0.0.1:2/mcp", "token2"))

    monkeypatch.setattr(connect_bridge, "post_mcp_http", _post_mcp_http)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    assert recovery_calls["count"] == 0
    message = _json_lines(stdout_buffer.getvalue())[0]
    assert message["id"] == "call"
    assert message["error"]["data"] == {"code": "MCP_RESPONSE_INTERRUPTED"}


def test_adr009_connect_bridge_presend_failure_recovers_and_sends_original_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _request({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    stdout_buffer = io.BytesIO()
    sent_payloads: list[bytes] = []
    recovery_calls = {"count": 0}

    def _post_mcp_message(**kwargs: Any) -> Result[tuple[str, bytes, str | None], str]:
        sent_payloads.append(kwargs["payload"])
        if len(sent_payloads) == 1:
            return Result(error="MCP_CONNECT_FAILED: Connection refused before body send")
        return Result(value=("application/json", b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}', None))

    def _recover_transport() -> Result[tuple[str, str], str]:
        recovery_calls["count"] += 1
        return Result(value=("http://127.0.0.1:2/mcp", "token2"))

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    assert recovery_calls["count"] == 1
    assert len(sent_payloads) == 2
    assert sent_payloads[0] == sent_payloads[1]
    assert _json_lines(stdout_buffer.getvalue())[0]["result"] == {"ok": True}


def test_adr009_connect_bridge_sent_unsafe_tools_call_interruption_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _request({"jsonrpc": "2.0", "id": "call", "method": "tools/call"})
    stdout_buffer = io.BytesIO()
    post_calls = {"count": 0}
    recovery_calls = {"count": 0}

    def _post_mcp_message(**_kwargs: Any) -> Result[tuple[str, bytes, str | None], str]:
        post_calls["count"] += 1
        return Result(error="MCP_RESPONSE_INTERRUPTED: response_body closed after request sent")

    def _recover_transport() -> Result[tuple[str, str], str]:
        recovery_calls["count"] += 1
        return Result(value=("http://127.0.0.1:2/mcp", "token2"))

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    assert post_calls["count"] == 1
    assert recovery_calls["count"] == 0
    message = _json_lines(stdout_buffer.getvalue())[0]
    assert message["id"] == "call"
    assert message["error"]["data"] == {"code": "MCP_RESPONSE_INTERRUPTED"}


def test_adr009_connect_bridge_postsend_notification_unknown_admission_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = (
        _request({"jsonrpc": "2.0", "method": "notifications/cancelled"})
        + _request({"jsonrpc": "2.0", "id": "next", "method": "ping"})
    )
    stdout_buffer = io.BytesIO()
    post_calls = {"count": 0}
    recovery_calls = {"count": 0}

    def _post_mcp_message(**_kwargs: Any) -> Result[tuple[str, bytes, str | None], str]:
        post_calls["count"] += 1
        if post_calls["count"] == 1:
            return Result(error="MCP_RESPONSE_INTERRUPTED: response_body closed after notification sent")
        return Result(value=("application/json", b'{"jsonrpc":"2.0","id":"next","result":{}}', None))

    def _recover_transport() -> Result[tuple[str, str], str]:
        recovery_calls["count"] += 1
        return Result(value=("http://127.0.0.1:2/mcp", "token2"))

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    assert recovery_calls["count"] == 0
    assert _json_lines(stdout_buffer.getvalue()) == [{"jsonrpc": "2.0", "id": "next", "result": {}}]
    captured = capsys.readouterr()
    assert "notification delivery unknown: response_body" in captured.err
    assert "bridge_adr009" in captured.err


@pytest.mark.parametrize("safe_method", ["tools/list", "ping"])
def test_adr009_connect_bridge_safe_request_may_recover_and_replay(
    monkeypatch: pytest.MonkeyPatch, safe_method: str
) -> None:
    requests = _request({"jsonrpc": "2.0", "id": safe_method, "method": safe_method})
    stdout_buffer = io.BytesIO()
    post_calls = {"count": 0}
    recovery_calls = {"count": 0}

    def _post_mcp_message(**_kwargs: Any) -> Result[tuple[str, bytes, str | None], str]:
        post_calls["count"] += 1
        if post_calls["count"] == 1:
            return Result(error="MCP_RESPONSE_INTERRUPTED: response_body connection reset after request sent")
        body = json.dumps({"jsonrpc": "2.0", "id": safe_method, "result": {"safe": True}}).encode("utf-8")
        return Result(value=("application/json", body, None))

    def _recover_transport() -> Result[tuple[str, str], str]:
        recovery_calls["count"] += 1
        return Result(value=("http://127.0.0.1:2/mcp", "token2"))

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    assert recovery_calls["count"] == 1
    assert post_calls["count"] == 2
    assert _json_lines(stdout_buffer.getvalue())[0]["result"] == {"safe": True}


def test_adr009_empty_2xx_notification_succeeds_without_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _request({"jsonrpc": "2.0", "method": "notifications/cancelled"})
    stdout_buffer = io.BytesIO()

    def _post_mcp_message(**_kwargs: Any) -> Result[tuple[str, bytes, str | None], str]:
        return Result(value=("application/json", b"", None))

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
    )

    assert result.is_ok
    assert stdout_buffer.getvalue() == b""


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "id": "req", "method": "ping"},
        [{"jsonrpc": "2.0", "id": "batch", "method": "ping"}],
    ],
)
def test_adr009_empty_2xx_request_or_batch_gets_forward_failed(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    requests = _request(payload)
    stdout_buffer = io.BytesIO()

    def _post_mcp_message(**_kwargs: Any) -> Result[tuple[str, bytes, str | None], str]:
        return Result(value=("application/json", b"", None))

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr009",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=stdout_buffer,
    )

    assert result.is_ok
    message = _json_lines(stdout_buffer.getvalue())[0]
    assert message["error"]["data"] == {"code": "MCP_FORWARD_FAILED"}
    assert "recovery" not in json.dumps(message).lower()


def test_adr009_invalid_timeout_inputs_are_rejected_before_network() -> None:
    module = _bridge_http_module()
    for invalid in (0.0, -1.0, math.inf, math.nan):
        result = module.post_mcp_http(
            mcp_url="http://127.0.0.1:1/mcp",
            bearer_token="token",
            payload=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
            session_id=None,
            connect_timeout_seconds=invalid,
            write_timeout_seconds=1.0,
            response_timeout_seconds=None,
            is_503_retryable=lambda _body: False,
        )
        assert result.is_err
        assert result.error.message.startswith("INVALID_TIMEOUT:")
