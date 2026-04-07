"""Gateway-local HTTP auth helpers for route handlers."""

from __future__ import annotations

from starlette.requests import Request

from tela.core.errors import AUTH_INVALID_TOKEN
from tela.shell.config_loader import Result


def extract_bearer_token(request: Request) -> Result[str, str]:
    """Extract bearer token from Authorization header."""

    authorization_header = request.headers.get("authorization")
    if authorization_header is None or not authorization_header.startswith("Bearer "):
        return Result(error=f"{AUTH_INVALID_TOKEN}: bearer token validation failed")

    request_token = authorization_header[len("Bearer ") :].strip()
    if request_token == "":
        return Result(error=f"{AUTH_INVALID_TOKEN}: bearer token validation failed")

    return Result(value=request_token)
