"""Tests for ``tela.commands.http_client`` retry skeleton.

Verifies that ``retry_http_request`` correctly centralizes the
request→retry→backoff→Result pattern and that ``_post_json_once`` delegates
to it while preserving existing error semantics.
"""

from __future__ import annotations

from email.message import Message
import http.client
import io
import json
from typing import Callable
from urllib import error as urllib_error

import pytest

from tela.commands import http_client as http_client_mod
from tela.commands.http_client import retry_http_request, _is_transient_url_error
from tela.shell.result import Result


# =============================================================================
# _is_transient_url_error classification tests
# =============================================================================


def test_is_transient_url_error_connection_refused() -> None:
    """ConnectionRefusedError must be classified as transient."""
    exc = urllib_error.URLError(ConnectionRefusedError("Connection refused"))
    assert _is_transient_url_error(exc) is True


def test_is_transient_url_error_connection_reset() -> None:
    """ConnectionResetError must be classified as transient."""
    exc = urllib_error.URLError(ConnectionResetError("Connection reset"))
    assert _is_transient_url_error(exc) is True


def test_is_transient_url_error_timeout() -> None:
    """TimeoutError must be classified as transient (gateway convergence)."""
    exc = urllib_error.URLError(TimeoutError("timed out"))
    assert _is_transient_url_error(exc) is True


def test_is_transient_url_error_broken_pipe() -> None:
    """BrokenPipeError must be classified as transient."""
    exc = urllib_error.URLError(BrokenPipeError("broken pipe"))
    assert _is_transient_url_error(exc) is True


def test_is_transient_url_error_errno_econnrefused() -> None:
    """OSError with errno=ECONNREFUSED must be classified as transient."""
    import errno

    exc = urllib_error.URLError(OSError(errno.ECONNREFUSED, "Connection refused"))
    assert _is_transient_url_error(exc) is True


def test_is_transient_url_error_errno_etimedout() -> None:
    """OSError with errno=ETIMEDOUT must be classified as transient."""
    import errno

    exc = urllib_error.URLError(OSError(errno.ETIMEDOUT, "Operation timed out"))
    assert _is_transient_url_error(exc) is True


def test_is_transient_url_error_non_transient_oserror() -> None:
    """Generic OSError with non-transient errno must NOT be classified transient."""
    exc = urllib_error.URLError(OSError(9999, "Unknown error"))
    assert _is_transient_url_error(exc) is False


def test_is_transient_url_error_string_reason_transient() -> None:
    """String reason containing transient markers must be classified transient."""
    exc = urllib_error.URLError("Connection refused")
    assert _is_transient_url_error(exc) is True


def test_is_transient_url_error_string_reason_timed_out() -> None:
    """String reason 'Timed out' must be classified as transient."""
    exc = urllib_error.URLError("Timed out")
    assert _is_transient_url_error(exc) is True


def test_is_transient_url_error_string_reason_non_transient() -> None:
    """String reason without transient markers must NOT be classified transient."""
    exc = urllib_error.URLError("SSL: CERTIFICATE_VERIFY_FAILED")
    assert _is_transient_url_error(exc) is False


def test_is_transient_url_error_unknown_reason() -> None:
    """URLError with unknown reason type must NOT be classified transient."""
    exc = urllib_error.URLError(42)  # type: ignore[arg-type]
    assert _is_transient_url_error(exc) is False


# =============================================================================
# retry_http_request: single attempt (max_retries=0)
# =============================================================================


def test_retry_http_request_success_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful request with max_retries=0 must return response."""

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def close(self) -> None:
            pass

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        return _FakeResponse()

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=0,
        timeout_seconds=5.0,
        retry_on_503=False,
        retry_on_transient=False,
    )

    assert result.is_ok
    assert result.value is not None
    body = result.value.read()
    assert json.loads(body) == {"ok": True}
    result.value.close()


def test_retry_http_request_http_error_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTPError with max_retries=0 must return error immediately."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.HTTPError(
            "http://127.0.0.1:8123/test",
            404,
            "Not Found",
            Message(),
            io.BytesIO(b"not found"),
        )

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=0,
        retry_on_503=False,
        retry_on_transient=False,
    )

    assert result.is_err
    assert "HTTP_404" in result.error
    assert calls["count"] == 1


def test_retry_http_request_url_error_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URLError with max_retries=0 and retry_on_transient=False must fail."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.URLError(ConnectionRefusedError("Connection refused"))

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=0,
        retry_on_transient=False,
    )

    assert result.is_err
    assert "HTTP_CONNECT_ERROR" in result.error
    assert calls["count"] == 1


# =============================================================================
# retry_http_request: retry on 503
# =============================================================================


def test_retry_http_request_retries_on_503_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_on_503=True must retry on 503 and succeed after transient 503."""

    calls = {"count": 0}

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def close(self) -> None:
            pass

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib_error.HTTPError(
                "http://127.0.0.1:8123/test",
                503,
                "Service Unavailable",
                Message(),
                io.BytesIO(b"unavailable"),
            )
        return _FakeResponse()

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )
    monkeypatch.setattr(http_client_mod.time, "sleep", lambda _seconds: None)

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=3,
        timeout_seconds=5.0,
        retry_on_503=True,
        retry_on_transient=False,
    )

    assert result.is_ok
    assert calls["count"] == 2


def test_retry_http_request_no_retry_on_503_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_on_503=False must NOT retry on 503."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.HTTPError(
            "http://127.0.0.1:8123/test",
            503,
            "Service Unavailable",
            Message(),
            io.BytesIO(b"unavailable"),
        )

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=3,
        retry_on_503=False,
        retry_on_transient=False,
    )

    assert result.is_err
    assert "HTTP_503" in result.error
    assert calls["count"] == 1


def test_retry_http_request_exhausts_retries_on_persistent_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent 503 with retry_on_503=True must exhaust all retries."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.HTTPError(
            "http://127.0.0.1:8123/test",
            503,
            "Service Unavailable",
            Message(),
            io.BytesIO(b"unavailable"),
        )

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )
    monkeypatch.setattr(http_client_mod.time, "sleep", lambda _seconds: None)

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=2,
        retry_on_503=True,
        retry_on_transient=False,
    )

    assert result.is_err
    assert "HTTP_503" in result.error
    # max_retries=2 means 3 total attempts (1 initial + 2 retries)
    assert calls["count"] == 3


# =============================================================================
# retry_http_request: retry on transient URLError
# =============================================================================


def test_retry_http_request_retries_on_transient_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient URLError must be retried when retry_on_transient=True."""

    calls = {"count": 0}

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def close(self) -> None:
            pass

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib_error.URLError(ConnectionRefusedError("Connection refused"))
        return _FakeResponse()

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )
    monkeypatch.setattr(http_client_mod.time, "sleep", lambda _seconds: None)

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=3,
        timeout_seconds=5.0,
        retry_on_503=True,
        retry_on_transient=True,
    )

    assert result.is_ok
    assert calls["count"] == 2


def test_retry_http_request_no_retry_on_non_transient_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-transient URLError must NOT be retried."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.URLError("SSL: CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=3,
        retry_on_503=True,
        retry_on_transient=True,
    )

    assert result.is_err
    assert "HTTP_CONNECT_ERROR" in result.error
    assert calls["count"] == 1


def test_retry_http_request_exhausts_retries_on_persistent_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent transient URLError must exhaust all retries then fail."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.URLError(ConnectionRefusedError("Connection refused"))

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )
    monkeypatch.setattr(http_client_mod.time, "sleep", lambda _seconds: None)

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        data=b"test",
        max_retries=2,
        retry_on_503=True,
        retry_on_transient=True,
    )

    assert result.is_err
    assert "HTTP_CONNECT_ERROR" in result.error
    # max_retries=2 means 3 total attempts (1 initial + 2 retries)
    assert calls["count"] == 3


# =============================================================================
# retry_http_request: backoff timing
# =============================================================================


def test_retry_http_request_backoff_increases_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backoff must be linear: backoff_seconds * (attempt + 1)."""

    sleep_calls: list[float] = []

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b"ok"

        def close(self) -> None:
            pass

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        raise urllib_error.URLError(ConnectionRefusedError("Connection refused"))

    monkeypatch.setattr(http_client_mod.urllib_request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(
        http_client_mod.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer test-token"},
        max_retries=3,
        backoff_seconds=0.5,
        retry_on_503=True,
        retry_on_transient=True,
    )

    assert result.is_err
    # 3 retries = 3 sleep calls: 0.5*1, 0.5*2, 0.5*3
    assert sleep_calls == [0.5, 1.0, 1.5]


# =============================================================================
# retry_http_request: _post_json_once migration proof
# =============================================================================


def test_post_json_once_delegates_to_retry_http_request_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_post_json_once must delegate to retry_http_request and succeed."""

    from tela.commands import connect_bridge

    calls: list[dict[str, object]] = []

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b""

        def close(self) -> None:
            pass

    def _fake_retry_http_request(  # type: ignore[no-untyped-def]
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
        max_retries: int,
        timeout_seconds: float,
        backoff_seconds: float = 0.5,
        retry_on_503: bool = True,
        retry_on_transient: bool = True,
    ) -> "Result[http.client.HTTPResponse, str]":
        from tela.shell.result import Result

        calls.append(
            {
                "url": url,
                "method": method,
                "headers": headers,
                "data": data,
                "max_retries": max_retries,
                "timeout_seconds": timeout_seconds,
                "retry_on_503": retry_on_503,
                "retry_on_transient": retry_on_transient,
            }
        )
        return Result(value=_FakeResponse())

    monkeypatch.setattr(connect_bridge, "retry_http_request", _fake_retry_http_request)

    result = connect_bridge.post_json_once(
        url="http://127.0.0.1:8123/disconnect",
        bearer_token="test-token",
        payload={"server_name": "bridge_abc123"},
        timeout_seconds=1.0,
    )

    assert result.is_ok
    assert len(calls) == 1
    assert calls[0]["url"] == "http://127.0.0.1:8123/disconnect"
    assert calls[0]["method"] == "POST"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token"
    assert calls[0]["max_retries"] == 0
    assert calls[0]["timeout_seconds"] == 1.0
    assert calls[0]["retry_on_503"] is False
    assert calls[0]["retry_on_transient"] is False


def test_post_json_once_delegates_error_from_retry_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_post_json_once must propagate retry_http_request errors."""

    from tela.commands import connect_bridge

    from tela.shell.result import Result

    def _fake_retry_http_request(  # type: ignore[no-untyped-def]
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
        max_retries: int,
        timeout_seconds: float,
        backoff_seconds: float = 0.5,
        retry_on_503: bool = True,
        retry_on_transient: bool = True,
    ) -> "Result[http.client.HTTPResponse, str]":
        return Result(error="HTTP_503: http://127.0.0.1:8123/disconnect")

    monkeypatch.setattr(connect_bridge, "retry_http_request", _fake_retry_http_request)

    result = connect_bridge.post_json_once(
        url="http://127.0.0.1:8123/disconnect",
        bearer_token="test-token",
        payload={"server_name": "bridge_abc123"},
        timeout_seconds=1.0,
    )

    assert result.is_err
    assert "HTTP_503" in result.error


# =============================================================================
# retry_http_request: preserves Bearer header semantics
# =============================================================================


def test_retry_http_request_preserves_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-constructed headers (including Bearer) must pass through unchanged."""

    captured_headers: dict[str, str] = {}

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b"ok"

        def close(self) -> None:
            pass

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        return _FakeResponse()

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/mcp",
        method="POST",
        headers={
            "Authorization": "Bearer secret-token",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        data=b'{"jsonrpc":"2.0","id":1}',
        max_retries=0,
        timeout_seconds=5.0,
        retry_on_503=False,
        retry_on_transient=False,
    )

    assert result.is_ok
    assert captured_headers.get("Authorization") == "Bearer secret-token"
    assert captured_headers.get("Content-type") == "application/json"
    assert captured_headers.get("Accept") == "application/json, text/event-stream"


# =============================================================================
# retry_http_request: caller closes response
# =============================================================================


def test_retry_http_request_caller_must_close_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returned response must be closeable by the caller."""

    close_calls = {"count": 0}

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"status": "ready"}'

        def close(self) -> None:
            close_calls["count"] += 1

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        return _FakeResponse()

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/status",
        method="GET",
        headers={"Authorization": "Bearer token"},
        max_retries=0,
        timeout_seconds=5.0,
        retry_on_503=False,
        retry_on_transient=False,
    )

    assert result.is_ok
    assert result.value is not None
    body = result.value.read()
    assert json.loads(body) == {"status": "ready"}
    result.value.close()
    assert close_calls["count"] == 1


def test_retry_http_request_zero_retries_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_retries=0 must make exactly one attempt with no retries."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.URLError(ConnectionRefusedError("Connection refused"))

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer token"},
        max_retries=0,
        retry_on_transient=True,
    )

    assert result.is_err
    assert calls["count"] == 1


# =============================================================================
# is_503_retryable callback tests
# =============================================================================


def test_retry_http_request_is_503_retryable_callback_rejects_non_contract_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When is_503_retryable returns False, 503 must NOT be retried."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.HTTPError(
            "http://127.0.0.1:8123/test",
            503,
            "Service Unavailable",
            Message(),
            io.BytesIO(b"not a contract response"),
        )

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )

    def _reject_all(exc: urllib_error.HTTPError) -> bool:
        _ = exc
        return False

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer token"},
        max_retries=3,
        retry_on_503=True,
        retry_on_transient=False,
        is_503_retryable=_reject_all,
    )

    assert result.is_err
    assert "HTTP_503" in result.error
    # Must not retry when predicate returns False
    assert calls["count"] == 1


def test_retry_http_request_is_503_retryable_callback_allows_contract_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When is_503_retryable returns True, 503 must be retried."""

    calls = {"count": 0}

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def close(self) -> None:
            pass

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib_error.HTTPError(
                "http://127.0.0.1:8123/test",
                503,
                "Service Unavailable",
                Message(),
                io.BytesIO(b"contract"),
            )
        return _FakeResponse()

    monkeypatch.setattr(
        "tela.commands.http_client.urllib_request.urlopen", _fake_urlopen
    )
    monkeypatch.setattr(http_client_mod.time, "sleep", lambda _seconds: None)

    def _accept_all(exc: urllib_error.HTTPError) -> bool:
        _ = exc
        return True

    result = retry_http_request(
        url="http://127.0.0.1:8123/test",
        method="POST",
        headers={"Authorization": "Bearer token"},
        max_retries=3,
        retry_on_503=True,
        retry_on_transient=False,
        is_503_retryable=_accept_all,
    )

    assert result.is_ok
    assert calls["count"] == 2


# =============================================================================
# Migration delegation proof tests: verify callers delegate to
# retry_http_request while preserving their own semantics.
# =============================================================================


def test_post_json_delegates_to_retry_http_request_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_post_json must delegate to retry_http_request and return None on success."""

    from tela.commands import connect_bridge

    calls: list[dict[str, object]] = []

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b""

        def close(self) -> None:
            pass

    def _fake_retry_http_request(  # type: ignore[no-untyped-def]
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
        max_retries: int,
        timeout_seconds: float,
        backoff_seconds: float = 0.5,
        retry_on_503: bool = True,
        retry_on_transient: bool = True,
        is_503_retryable: Callable | None = None,
    ) -> "Result[http.client.HTTPResponse, str]":
        from tela.shell.result import Result

        calls.append(
            {
                "url": url,
                "method": method,
                "headers": headers,
                "data": data,
                "max_retries": max_retries,
                "timeout_seconds": timeout_seconds,
                "backoff_seconds": backoff_seconds,
                "retry_on_503": retry_on_503,
                "retry_on_transient": retry_on_transient,
            }
        )
        return Result(value=_FakeResponse())

    monkeypatch.setattr(connect_bridge, "retry_http_request", _fake_retry_http_request)

    result = connect_bridge.post_json(
        url="http://127.0.0.1:8123/connect",
        bearer_token="test-token",
        payload={"server_name": "bridge_test"},
    )

    assert result.is_ok
    assert len(calls) == 1
    assert calls[0]["url"] == "http://127.0.0.1:8123/connect"
    assert calls[0]["method"] == "POST"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token"
    assert calls[0]["max_retries"] == connect_bridge.HTTP_TRANSIENT_RETRIES
    assert calls[0]["retry_on_503"] is True
    assert calls[0]["retry_on_transient"] is True


def test_post_json_delegates_error_from_retry_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_post_json must propagate retry_http_request errors."""

    from tela.commands import connect_bridge

    from tela.shell.result import Result

    def _fake_retry_http_request(  # type: ignore[no-untyped-def]
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
        max_retries: int,
        timeout_seconds: float,
        backoff_seconds: float = 0.5,
        retry_on_503: bool = True,
        retry_on_transient: bool = True,
        is_503_retryable: Callable | None = None,
    ) -> "Result[http.client.HTTPResponse, str]":
        return Result(error=f"HTTP_503: {url}")

    monkeypatch.setattr(connect_bridge, "retry_http_request", _fake_retry_http_request)

    result = connect_bridge.post_json(
        url="http://127.0.0.1:8123/connect",
        bearer_token="test-token",
        payload={"server_name": "bridge_test"},
    )

    assert result.is_err
    assert "HTTP_503" in result.error


def test_get_gateway_status_delegates_to_retry_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_get_gateway_status must delegate HTTP to retry_http_request and parse response."""

    from tela.commands import connect_bridge
    from tela.core.models import StatusResponse

    from tela.shell.result import Result

    calls: list[dict[str, object]] = []

    class _FakeResponse:
        status = 200

        def __init__(self) -> None:
            self.headers = {"Content-Type": "application/json"}

        def read(self) -> bytes:
            return json.dumps(
                {
                    "uptime_seconds": 1.0,
                    "server_count": 1,
                    "connected_servers": ["fs"],
                    "active_connections": 1,
                    "profile_count": 1,
                    "total_tool_calls": 0,
                    "state": "ready",
                    "discovery_source": "lockfile",
                    "config_path": "/tmp/tela.yaml",
                    "requested_config_path": "/tmp/tela.yaml",
                    "config_mismatch": False,
                    "degraded_reason": None,
                    "connections": [],
                    "audit_entries": [],
                }
            ).encode("utf-8")

        def close(self) -> None:
            pass

    def _fake_retry_http_request(  # type: ignore[no-untyped-def]
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
        max_retries: int,
        timeout_seconds: float,
        backoff_seconds: float = 0.5,
        retry_on_503: bool = True,
        retry_on_transient: bool = True,
        is_503_retryable: Callable | None = None,
    ) -> "Result[http.client.HTTPResponse, str]":
        calls.append(
            {
                "url": url,
                "method": method,
                "headers": headers,
                "retry_on_503": retry_on_503,
                "retry_on_transient": retry_on_transient,
            }
        )
        return Result(value=_FakeResponse())

    monkeypatch.setattr(connect_bridge, "retry_http_request", _fake_retry_http_request)

    result = connect_bridge._get_gateway_status(
        status_url="http://127.0.0.1:8123/status",
        bearer_token="test-token",
    )

    assert result.is_ok
    assert isinstance(result.value, StatusResponse)
    assert result.value.state == "ready"
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token"


def test_post_mcp_message_error_format_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_post_mcp_message maps phase-aware post_mcp_http errors to MCP format."""

    from tela.commands import connect_bridge

    from tela.shell.result import Result

    def _fake_post_mcp_http_status(
        **_kwargs: object,
    ) -> Result[connect_bridge.BridgeHttpResponse, connect_bridge.BridgeHttpError]:
        return Result(
            error=connect_bridge.BridgeHttpError(
                phase="http_status",
                message="HTTP_503: http://127.0.0.1:8123/mcp",
                request_sent=True,
                mcp_admitted=None,
                status_code=503,
            )
        )

    monkeypatch.setattr(connect_bridge, "post_mcp_http", _fake_post_mcp_http_status)

    result = connect_bridge.post_mcp_message(
        mcp_url="http://127.0.0.1:8123/mcp",
        bearer_token="token",
        payload=b'{"jsonrpc":"2.0","id":1}',
    )

    assert result.is_err
    assert result.error == "MCP_FORWARD_FAILED: http 503"

    def _fake_post_mcp_http_connect_error(
        **_kwargs: object,
    ) -> Result[connect_bridge.BridgeHttpResponse, connect_bridge.BridgeHttpError]:
        return Result(
            error=connect_bridge.BridgeHttpError(
                phase="connect",
                message="Connection refused",
                request_sent=False,
                mcp_admitted=None,
            )
        )

    monkeypatch.setattr(
        connect_bridge, "post_mcp_http", _fake_post_mcp_http_connect_error
    )

    result2 = connect_bridge.post_mcp_message(
        mcp_url="http://127.0.0.1:8123/mcp",
        bearer_token="token",
        payload=b'{"jsonrpc":"2.0","id":1}',
    )

    assert result2.is_err
    assert result2.error == "MCP_FORWARD_FAILED: Connection refused"


def test_fetch_status_payload_delegates_to_retry_http_request_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch_status_payload must delegate HTTP to retry_http_request and parse JSON."""

    from tela.commands import remote_state
    from tela.core.models import LockfileData

    from tela.shell.result import Result

    calls: list[dict[str, object]] = []

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return json.dumps(
                {
                    "uptime_seconds": 5.0,
                    "server_count": 1,
                    "connected_servers": ["fs"],
                    "active_connections": 1,
                    "profile_count": 1,
                    "total_tool_calls": 0,
                    "state": "ready",
                    "discovery_source": "lockfile",
                    "config_path": "/tmp/tela.yaml",
                    "requested_config_path": "/tmp/tela.yaml",
                    "config_mismatch": False,
                    "degraded_reason": None,
                    "connections": [],
                    "audit_entries": [],
                }
            ).encode("utf-8")

        def close(self) -> None:
            pass

    def _fake_retry_http_request(  # type: ignore[no-untyped-def]
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
        max_retries: int,
        timeout_seconds: float,
        backoff_seconds: float = 0.5,
        retry_on_503: bool = True,
        retry_on_transient: bool = True,
        is_503_retryable: Callable | None = None,
    ) -> "Result[http.client.HTTPResponse, str]":
        calls.append(
            {
                "url": url,
                "method": method,
                "headers": headers,
                "max_retries": max_retries,
                "retry_on_503": retry_on_503,
                "retry_on_transient": retry_on_transient,
            }
        )
        return Result(value=_FakeResponse())

    monkeypatch.setattr(remote_state, "retry_http_request", _fake_retry_http_request)

    lockfile = LockfileData(
        pid=1234,
        host="127.0.0.1",
        port=8123,
        token="status-token",
        started_at="2026-03-22T10:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )

    result = remote_state._fetch_status_payload(lockfile)

    assert result.is_ok
    assert isinstance(result.value, dict)
    assert result.value["state"] == "ready"
    assert len(calls) == 1
    assert calls[0]["url"] == "http://127.0.0.1:8123/status"
    assert calls[0]["method"] == "GET"
    assert calls[0]["max_retries"] == 0
    assert calls[0]["retry_on_503"] is False
    assert calls[0]["retry_on_transient"] is False


def test_fetch_status_payload_preserves_error_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch_status_payload must transform retry_http_request errors to remote_state format."""

    from tela.commands import remote_state
    from tela.core.models import LockfileData

    from tela.shell.result import Result

    def _fake_retry_http_request_http_error(  # type: ignore[no-untyped-def]
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
        max_retries: int,
        timeout_seconds: float,
        backoff_seconds: float = 0.5,
        retry_on_503: bool = True,
        retry_on_transient: bool = True,
        is_503_retryable: Callable | None = None,
    ) -> "Result[http.client.HTTPResponse, str]":
        return Result(error="HTTP_503: http://127.0.0.1:8123/status")

    monkeypatch.setattr(
        remote_state, "retry_http_request", _fake_retry_http_request_http_error
    )

    lockfile = LockfileData(
        pid=1234,
        host="127.0.0.1",
        port=8123,
        token="status-token",
        started_at="2026-03-22T10:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )

    result = remote_state._fetch_status_payload(lockfile)

    assert result.is_err
    assert "REMOTE_STATUS_QUERY_ERROR: http 503" in result.error

    # URLError case
    def _fake_retry_http_request_connect_error(  # type: ignore[no-untyped-def]
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None = None,
        max_retries: int,
        timeout_seconds: float,
        backoff_seconds: float = 0.5,
        retry_on_503: bool = True,
        retry_on_transient: bool = True,
        is_503_retryable: Callable | None = None,
    ) -> "Result[http.client.HTTPResponse, str]":
        return Result(error="HTTP_CONNECT_ERROR: Connection refused")

    monkeypatch.setattr(
        remote_state, "retry_http_request", _fake_retry_http_request_connect_error
    )

    result2 = remote_state._fetch_status_payload(lockfile)

    assert result2.is_err
    assert "NO_RUNNING_SERVER" in result2.error
    assert "Connection refused" in result2.error
