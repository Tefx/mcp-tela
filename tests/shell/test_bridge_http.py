from __future__ import annotations

import socket
from typing import Any

import pytest

from tela.commands import bridge_http


class _FakeSock:
    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        body: bytes,
        reason: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self._body = body
        self._headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        for key, value in self._headers.items():
            if key.lower() == name.lower():
                return value
        return default


class _FakeConnection:
    response = _FakeResponse(status=204, body=b"")
    connect_exc: Exception | None = None
    endheaders_exc: Exception | None = None
    send_exc: Exception | None = None
    getresponse_exc: Exception | None = None
    instances: list[_FakeConnection] = []

    def __init__(self, host: str, port: int | None, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = _FakeSock()
        self.closed = False
        self.requests: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}
        self.body = b""
        type(self).instances.append(self)

    def connect(self) -> None:
        if self.connect_exc is not None:
            raise self.connect_exc

    def putrequest(self, method: str, target: str) -> None:
        self.requests.append((method, target))

    def putheader(self, name: str, value: str) -> None:
        self.headers[name] = value

    def endheaders(self) -> None:
        if self.endheaders_exc is not None:
            raise self.endheaders_exc

    def send(self, payload: bytes) -> None:
        if self.send_exc is not None:
            raise self.send_exc
        self.body += payload

    def getresponse(self) -> _FakeResponse:
        if self.getresponse_exc is not None:
            raise self.getresponse_exc
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_connection(monkeypatch: pytest.MonkeyPatch) -> type[_FakeConnection]:
    _FakeConnection.response = _FakeResponse(status=204, body=b"")
    _FakeConnection.connect_exc = None
    _FakeConnection.endheaders_exc = None
    _FakeConnection.send_exc = None
    _FakeConnection.getresponse_exc = None
    _FakeConnection.instances = []
    monkeypatch.setattr(bridge_http.http.client, "HTTPConnection", _FakeConnection)
    return _FakeConnection


def _post(**overrides: Any) -> Any:
    args = {
        "mcp_url": "http://example.test/mcp?x=1",
        "bearer_token": "token",
        "payload": b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        "session_id": "session-in",
        "connect_timeout_seconds": 1.0,
        "write_timeout_seconds": 2.0,
        "response_timeout_seconds": None,
        "is_503_retryable": lambda _body: False,
    }
    args.update(overrides)
    return bridge_http.post_mcp_http(**args)


def test_post_mcp_http_success_allows_empty_2xx_and_unbounded_response_timeout() -> None:
    _FakeConnection.response = _FakeResponse(
        status=204,
        body=b"",
        headers={"Content-Type": "application/json", "mcp-session-id": "session-out"},
    )

    result = _post()

    assert result.is_ok
    assert result.value == bridge_http.BridgeHttpResponse(
        content_type="application/json", body=b"", session_id="session-out"
    )
    connection = _FakeConnection.instances[0]
    assert connection.timeout == 1.0
    assert connection.sock.timeouts == [2.0, None]
    assert connection.requests == [("POST", "/mcp?x=1")]
    assert connection.headers["Authorization"] == "Bearer token"
    assert connection.headers["Content-Type"] == "application/json"
    assert connection.headers["Accept"] == "application/json, text/event-stream"
    assert connection.headers["Content-Length"] == "40"
    assert connection.headers["mcp-session-id"] == "session-in"
    assert connection.body == b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    assert connection.closed is True


def test_post_mcp_http_connect_failure_is_definitely_not_sent() -> None:
    _FakeConnection.connect_exc = ConnectionRefusedError("refused")

    result = _post()

    assert result.is_err
    assert result.error.phase == "connect"
    assert result.error.request_sent is False
    assert result.error.mcp_admitted is None
    assert _FakeConnection.instances[0].closed is True


def test_post_mcp_http_header_write_failure_is_before_body_send() -> None:
    _FakeConnection.endheaders_exc = BrokenPipeError("headers failed")

    result = _post()

    assert result.is_err
    assert result.error.phase == "write"
    assert result.error.request_sent is False
    assert result.error.mcp_admitted is None


def test_post_mcp_http_body_write_failure_is_unknown_partial_send() -> None:
    _FakeConnection.send_exc = BrokenPipeError("body failed")

    result = _post()
    assert result.is_err
    assert result.error.phase == "write"
    assert result.error.request_sent is None
    assert result.error.mcp_admitted is None


def test_post_mcp_http_explicit_response_deadline_maps_to_request_timeout() -> None:
    _FakeConnection.getresponse_exc = socket.timeout("timed out")

    result = _post(response_timeout_seconds=3.0)
    assert result.is_err
    assert result.error.phase == "response_headers"
    assert result.error.request_sent is True
    assert result.error.mcp_admitted is None
    assert result.error.message == "MCP_REQUEST_TIMEOUT: response deadline expired"
    assert _FakeConnection.instances[0].sock.timeouts == [2.0, 3.0]


def test_post_mcp_http_response_timeout_none_keeps_unbounded_response_wait() -> None:
    _FakeConnection.getresponse_exc = socket.timeout("unexpected socket timeout")

    result = _post(response_timeout_seconds=None)
    assert result.is_err
    assert result.error.phase == "response_headers"
    assert result.error.request_sent is True
    assert result.error.mcp_admitted is None
    assert result.error.message == "unexpected socket timeout"
    assert _FakeConnection.instances[0].sock.timeouts == [2.0, None]


def test_post_mcp_http_response_body_failure_reports_body_phase_after_send() -> None:
    class _FailingBodyResponse(_FakeResponse):
        def read(self) -> bytes:
            raise ConnectionResetError("body reset")

    _FakeConnection.response = _FailingBodyResponse(status=200, body=b"")

    result = _post()
    assert result.is_err
    assert result.error.phase == "response_body"
    assert result.error.request_sent is True
    assert result.error.mcp_admitted is None
    assert result.error.message == "body reset"


@pytest.mark.parametrize(
    "overrides",
    [
        {"connect_timeout_seconds": 0.0},
        {"connect_timeout_seconds": -1.0},
        {"connect_timeout_seconds": float("inf")},
        {"write_timeout_seconds": False},
        {"response_timeout_seconds": "1"},
    ],
)
def test_post_mcp_http_invalid_timeout_inputs_return_error_before_network(
    overrides: dict[str, Any]
) -> None:
    result = _post(**overrides)
    assert result.is_err
    assert result.error.phase == "connect"
    assert result.error.request_sent is False
    assert result.error.mcp_admitted is None
    assert result.error.message == bridge_http.INVALID_TIMEOUT_MESSAGE
    assert _FakeConnection.instances == []


def test_post_mcp_http_warming_503_sets_non_admission_and_plain_non_2xx_does_not() -> None:
    _FakeConnection.response = _FakeResponse(status=503, body=b"warming", reason="Service Unavailable")
    warming = _post(is_503_retryable=lambda body: body == b"warming")
    assert warming.is_err
    assert warming.error.phase == "http_status"
    assert warming.error.request_sent is True
    assert warming.error.mcp_admitted is False
    assert warming.error.status_code == 503
    assert warming.error.retryable_warming is True

    _FakeConnection.response = _FakeResponse(status=503, body=b"busy", reason="Service Unavailable")
    plain_503 = _post(is_503_retryable=lambda _body: False)
    assert plain_503.is_err
    assert plain_503.error.phase == "http_status"
    assert plain_503.error.request_sent is True
    assert plain_503.error.mcp_admitted is None
    assert plain_503.error.status_code == 503
    assert plain_503.error.retryable_warming is False

    _FakeConnection.response = _FakeResponse(status=400, body=b"bad", reason="Bad Request")
    plain_400 = _post(is_503_retryable=lambda _body: False)
    assert plain_400.is_err
    assert plain_400.error.phase == "http_status"
    assert plain_400.error.request_sent is True
    assert plain_400.error.mcp_admitted is None
    assert plain_400.error.status_code == 400
    assert plain_400.error.retryable_warming is False
