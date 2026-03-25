"""Integration tests for BearerAuthMiddleware wired into the MCP app.

Verifies that wrapping ``streamable_http_app()`` with ``BearerAuthMiddleware``
enforces bearer-token auth on ``/mcp`` while allowing ``/health`` through.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from tela.shell.http_auth import BearerAuthMiddleware


# ---------------------------------------------------------------------------
# ASGI test helpers
# ---------------------------------------------------------------------------

_EXPECTED_TOKEN = "test-secret-token-42"


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


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _build_wrapped_app() -> BearerAuthMiddleware:
    """Create a FastMCP app wrapped with BearerAuthMiddleware."""
    server = FastMCP("test-gateway")

    # Register a minimal /health custom route to mirror production.
    @server.custom_route("/health", methods=["GET"])
    async def _health_route(_request: Any) -> Any:
        from starlette.responses import JSONResponse

        return JSONResponse(content={"status": "ok"})

    raw_app = server.streamable_http_app()
    return BearerAuthMiddleware(
        raw_app,
        get_expected_token=lambda: _EXPECTED_TOKEN,
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_post_mcp_without_bearer_token_returns_401() -> None:
    """POST /mcp without bearer token must be rejected with 401."""
    app = _build_wrapped_app()
    send = _ResponseCollector()
    scope = _make_scope("/mcp", method="POST")
    _run(app(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
    body = send.json_body
    assert "error" in body
    assert body["error"].startswith("AUTH_INVALID_TOKEN")


def test_post_mcp_with_valid_bearer_token_not_401() -> None:
    """POST /mcp with valid bearer token must NOT return 401.

    The middleware lets the request through to the inner MCP app.  Because
    the MCP session manager is not fully initialised in this lightweight
    test, the inner app raises ``RuntimeError``.  That proves the
    middleware itself did **not** reject the request -- i.e. no 401.
    """
    app = _build_wrapped_app()
    send = _ResponseCollector()
    scope = _make_scope(
        "/mcp",
        method="POST",
        headers=_bearer_header(_EXPECTED_TOKEN),
    )

    async def _receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    passed_through = False
    try:
        _run(app(scope, _receive, send))
    except RuntimeError:
        # Inner MCP app raised because session manager isn't running.
        # This proves the middleware passed the request through.
        passed_through = True

    if not passed_through:
        # If no exception, the middleware itself responded -- must not be 401.
        assert send.status is not None
        assert send.status != 401
    else:
        # The request reached the inner app -- middleware did not block it.
        assert send.status is None or send.status != 401


def test_get_health_without_token_returns_200() -> None:
    """GET /health without any token must return 200."""
    app = _build_wrapped_app()
    send = _ResponseCollector()
    scope = _make_scope("/health", method="GET")

    async def _receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    _run(app(scope, _receive, send))
    assert send.status == 200


def test_post_mcp_with_wrong_token_returns_401() -> None:
    """POST /mcp with incorrect bearer token must be rejected with 401."""
    app = _build_wrapped_app()
    send = _ResponseCollector()
    scope = _make_scope(
        "/mcp",
        method="POST",
        headers=_bearer_header("wrong-token"),
    )
    _run(app(scope, None, send))  # type: ignore[arg-type]  # test fake: receive not used
    assert send.status == 401
