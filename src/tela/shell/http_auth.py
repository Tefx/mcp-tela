"""HTTP bearer authentication helpers.

Contracts-only module for gateway HTTP auth middleware.
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Callable

from tela.shell.gateway_lifecycle import get_lifecycle_status_facts
from tela.shell.mcp_admission_contract import McpAdmissionTransient503
from tela.shell.result import Result

# ASGI type aliases for readability.
Scope = dict[str, Any]
Receive = Callable[..., Any]
Send = Callable[..., Any]
ASGIApp = Callable[[Scope, Receive, Send], Any]


def validate_bearer_token(request_token: str, expected_token: str) -> Result[None, str]:
    """Validate bearer token using constant-time comparison.

    Implementations must use ``hmac.compare_digest`` for constant-time token
    comparison.
    """

    if hmac.compare_digest(request_token, expected_token):
        return Result(value=None)
    return Result(error="AUTH_INVALID_TOKEN: bearer token validation failed")


# Internal compatibility symbol used by current tests and call-sites.
_validate_bearer_token = validate_bearer_token

_AUTH_ERROR_BODY: bytes = json.dumps(
    {"error": "AUTH_INVALID_TOKEN: bearer token validation failed"}
).encode("utf-8")

_MCP_WARMING_ERROR_PAYLOAD: McpAdmissionTransient503 = {
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
_MCP_WARMING_ERROR_BODY: bytes = json.dumps(_MCP_WARMING_ERROR_PAYLOAD).encode("utf-8")


class BearerAuthMiddleware:
    """Raw ASGI middleware enforcing bearer-token auth on all routes except GET /health.

    Uses raw ASGI protocol (not ``BaseHTTPMiddleware``) for streaming
    compatibility with SSE / Streamable HTTP transports.

    ``get_expected_token`` is a callable returning the current expected token
    at request time.  If it returns ``None`` (e.g. startup race), the request
    is rejected with 401.
    """

    def __init__(
        self,
        app: ASGIApp,
        get_expected_token: Callable[[], str | None],
    ) -> None:
        self.app = app
        self.get_expected_token = get_expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Exempt GET /health from authentication.
        if scope.get("method") == "GET" and scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return

        # Reject if expected token is unavailable (defensive against startup race).
        expected_token = self.get_expected_token()
        if expected_token is None:
            await self._send_401(send)
            return

        # Extract bearer token from headers.
        request_token = self._extract_bearer_token(scope)
        if request_token is None:
            await self._send_401(send)
            return

        # Validate via constant-time comparison.
        result = validate_bearer_token(request_token, expected_token)
        if result.is_err:
            await self._send_401(send)
            return

        if scope.get("method") == "POST" and scope.get("path") == "/mcp":
            lifecycle_result = get_lifecycle_status_facts()
            if (
                lifecycle_result.is_ok
                and lifecycle_result.value is not None
                and lifecycle_result.value.state == "warming"
            ):
                await self._send_503_mcp_warming(send)
                return

        await self.app(scope, receive, send)

    @staticmethod
    def _extract_bearer_token(scope: Scope) -> str | None:
        """Extract bearer token from ASGI scope headers."""
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for name, value in headers:
            if name == b"authorization":
                decoded = value.decode("latin-1")
                if decoded.startswith("Bearer "):
                    token = decoded[len("Bearer ") :].strip()
                    if token:
                        return token
                return None
        return None

    @staticmethod
    async def _send_401(send: Send) -> None:
        """Send a 401 JSON error response via raw ASGI."""
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(_AUTH_ERROR_BODY)).encode("ascii")],
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": _AUTH_ERROR_BODY,
            }
        )

    @staticmethod
    async def _send_503_mcp_warming(send: Send) -> None:
        """Send machine-readable transient warming 503 for POST /mcp."""
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    [b"content-type", b"application/json"],
                    [
                        b"content-length",
                        str(len(_MCP_WARMING_ERROR_BODY)).encode("ascii"),
                    ],
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": _MCP_WARMING_ERROR_BODY,
            }
        )
