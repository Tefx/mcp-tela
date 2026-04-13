"""Gateway-local HTTP auth helpers for route handlers."""

from __future__ import annotations

from starlette.requests import Request

from tela.core.errors import AUTH_INVALID_TOKEN
from tela.shell.result import Result
from tela.shell.http_auth import extract_bearer_from_header_value


def extract_bearer_token(request: Request) -> Result[str, str]:
    """Extract bearer token from Authorization header."""

    authorization_header = request.headers.get("authorization")
    if authorization_header is None:
        return Result(error=f"{AUTH_INVALID_TOKEN}: bearer token validation failed")

    token = extract_bearer_from_header_value(authorization_header)
    if token is None:
        return Result(error=f"{AUTH_INVALID_TOKEN}: bearer token validation failed")

    return Result(value=token)
