"""Tests for bearer token middleware validation semantics."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from typing import Any

from tela.shell import http_auth
from tela.shell.http_auth import BearerAuthMiddleware


def _simulate_http_auth_gate(
    path: str, auth_header: str | None, expected_token: str
) -> bool:
    if path == "/health":
        return True

    if auth_header is None:
        return False

    bearer_prefix = "Bearer "
    if not auth_header.startswith(bearer_prefix):
        return False

    presented_token = auth_header[len(bearer_prefix) :]
    return http_auth.validate_bearer_token(presented_token, expected_token).is_ok


def test_validate_bearer_token_accepts_valid_token() -> None:
    result = http_auth.validate_bearer_token("correct-token", "correct-token")
    assert result.is_ok
    assert result.error is None


def test_validate_bearer_token_rejects_invalid_token() -> None:
    result = http_auth.validate_bearer_token("wrong-token", "correct-token")
    assert result.is_err
    assert result.error == "AUTH_INVALID_TOKEN: bearer token validation failed"


def test_http_auth_gate_rejects_missing_authorization_header() -> None:
    assert (
        _simulate_http_auth_gate("/status", None, expected_token="correct-token")
        is False
    )


def test_http_auth_gate_rejects_malformed_bearer_prefix() -> None:
    assert (
        _simulate_http_auth_gate(
            "/status",
            "Token correct-token",
            expected_token="correct-token",
        )
        is False
    )


def test_http_auth_gate_bypasses_health_path() -> None:
    assert (
        _simulate_http_auth_gate("/health", None, expected_token="correct-token")
        is True
    )


def test_validate_bearer_token_uses_constant_time_compare_digest() -> None:
    source = inspect.getsource(http_auth._validate_bearer_token)
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

    eq_comparisons = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, ast.Eq) for op in node.ops)
    ]
    assert not eq_comparisons


# ---------------------------------------------------------------------------
# BearerAuthMiddleware ASGI tests
# ---------------------------------------------------------------------------


def _make_scope(
    path: str,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    """Build a minimal ASGI HTTP scope."""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
    }


def _bearer_header(token: str) -> list[tuple[bytes, bytes]]:
    """Build ASGI headers list with an Authorization: Bearer header."""
    return [(b"authorization", f"Bearer {token}".encode("latin-1"))]


class _ResponseCollector:
    """Collects ASGI response events for assertion."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    @property
    def status(self) -> int | None:
        for e in self.events:
            if e["type"] == "http.response.start":
                return e["status"]
        return None

    @property
    def body(self) -> bytes:
        parts: list[bytes] = []
        for e in self.events:
            if e["type"] == "http.response.body":
                parts.append(e.get("body", b""))
        return b"".join(parts)

    @property
    def json_body(self) -> Any:
        return json.loads(self.body)


_PASSTHROUGH_CALLED = object()


async def _passthrough_app(
    scope: dict[str, Any],
    receive: Any,
    send: Any,
) -> None:
    """Dummy ASGI app that sends a 200 to prove the request passed through."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- Health bypass ----------------------------------------------------------


def test_middleware_passes_health_without_token() -> None:
    """GET /health must pass through without any token."""
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    _run(mw(_make_scope("/health", "GET"), None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 200


def test_middleware_requires_token_for_health_post() -> None:
    """POST /health is NOT exempt -- only GET /health is."""
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    _run(mw(_make_scope("/health", "POST"), None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401


# -- Rejection cases --------------------------------------------------------


def test_middleware_rejects_mcp_without_token() -> None:
    """Unauthenticated request to /mcp must get 401 + JSON error body."""
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    _run(mw(_make_scope("/mcp", "POST"), None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
    body = send.json_body
    assert "error" in body
    assert body["error"].startswith("AUTH_INVALID_TOKEN")


def test_middleware_rejects_wrong_token() -> None:
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    scope = _make_scope("/mcp", "POST", headers=_bearer_header("wrong"))
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
    assert send.json_body["error"].startswith("AUTH_INVALID_TOKEN")


def test_middleware_rejects_when_expected_token_is_none() -> None:
    """If get_expected_token returns None (startup race), reject with 401."""
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: None)
    send = _ResponseCollector()
    scope = _make_scope("/mcp", "POST", headers=_bearer_header("anything"))
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401


def test_middleware_rejects_empty_bearer_value() -> None:
    """Authorization: Bearer (with nothing after) must be rejected."""
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    scope = _make_scope("/mcp", "POST", headers=[(b"authorization", b"Bearer ")])
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401


def test_middleware_rejects_non_bearer_scheme() -> None:
    """Authorization: Basic ... must be rejected."""
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    scope = _make_scope("/mcp", "POST", headers=[(b"authorization", b"Basic c2VjcmV0")])
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401


# -- Invalid Bearer Format (RFC 7235 / RFC 6750 Negative Cases) ---------------


def test_middleware_rejects_lowercase_bearer_scheme() -> None:
    """Authorization: bearer <token> (lowercase) must be rejected.

    Ref: RFC 7235 Section 2.1 defines auth-scheme as case-insensitive.
    Ref: RFC 6750 Section 2.1 specifies "Bearer" (titlecase).
    The current implementation uses case-sensitive prefix matching ("Bearer "),
    which rejects lowercase variants. This test documents the rejection
    behavior as a regression guard.
    """
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    scope = _make_scope("/mcp", "POST", headers=[(b"authorization", b"bearer secret")])
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
    assert send.json_body["error"].startswith("AUTH_INVALID_TOKEN")


def test_middleware_rejects_uppercase_bearer_scheme() -> None:
    """Authorization: BEARER <token> (uppercase) must be rejected.

    Ref: RFC 7235 Section 2.1 defines auth-scheme as case-insensitive.
    Ref: RFC 6750 Section 2.1 specifies "Bearer" (titlecase).
    The current implementation uses case-sensitive prefix matching ("Bearer "),
    which rejects uppercase variants. This test documents the rejection
    behavior as a regression guard.
    """
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    scope = _make_scope("/mcp", "POST", headers=[(b"authorization", b"BEARER secret")])
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
    assert send.json_body["error"].startswith("AUTH_INVALID_TOKEN")


def test_middleware_rejects_bearer_without_space() -> None:
    """Authorization: BearerX <token> (no space after scheme) must be rejected.

    Ref: RFC 6750 Section 2.1 requires exactly one space between scheme and token.
    Malformed headers like "BearerX" or "Bearersecret" must be rejected.
    """
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    # No space between "Bearer" and token - invalid per RFC 6750
    scope = _make_scope("/mcp", "POST", headers=[(b"authorization", b"Bearersecret")])
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
    assert send.json_body["error"].startswith("AUTH_INVALID_TOKEN")


def test_middleware_rejects_bearer_with_tab_separator() -> None:
    """Authorization: Bearer\\t<token> (tab instead of space) must be rejected.

    Ref: RFC 6750 Section 2.1 specifies exactly one space between scheme and token.
    A tab character between "Bearer" and the token is an invalid format.
    """
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    # Tab between "Bearer" and token - invalid format
    scope = _make_scope("/mcp", "POST", headers=[(b"authorization", b"Bearer\tsecret")])
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
    assert send.json_body["error"].startswith("AUTH_INVALID_TOKEN")


# -- Pass-through cases -----------------------------------------------------


def test_middleware_passes_authenticated_request() -> None:
    """Valid bearer token lets the request through to the inner app."""
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    scope = _make_scope("/mcp", "POST", headers=_bearer_header("secret"))
    _run(mw(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 200


def test_middleware_passes_non_http_scope() -> None:
    """Non-HTTP scopes (e.g. websocket, lifespan) pass through unconditionally."""
    called = False

    async def _ws_app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    mw = BearerAuthMiddleware(_ws_app, get_expected_token=lambda: "secret")
    ws_scope: dict[str, Any] = {"type": "websocket", "path": "/ws"}
    _run(mw(ws_scope, None, None))  # type: ignore[arg-type]  # test fake: receive/send not used
    assert called


# -- Response body format ---------------------------------------------------


def test_middleware_401_response_is_valid_json() -> None:
    """401 response body must be valid JSON with 'error' key."""
    mw = BearerAuthMiddleware(_passthrough_app, get_expected_token=lambda: "secret")
    send = _ResponseCollector()
    _run(mw(_make_scope("/status", "GET"), None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
    body = send.json_body
    assert isinstance(body, dict)
    assert "error" in body
    assert body["error"] == "AUTH_INVALID_TOKEN: bearer token validation failed"
