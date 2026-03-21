"""HTTP route handler contracts for gateway HTTP endpoints.

This module is intentionally contract-only. It exposes handler signatures for all
HTTP routes defined in ``docs/INTERFACES.md`` section 7.2 with explicit auth
requirements.
"""

from __future__ import annotations

from typing import Mapping

from tela.core.contracts import post, pre
from tela.shell.config_loader import Result
from tela.core.models import (
    ConnectRequest,
    DisconnectRequest,
    HealthResponse,
    StatusResponse,
)
from tela.shell.http_auth import validate_bearer_token


@pre(lambda: True)
@post(lambda result: True)
def handle_health() -> Result[HealthResponse, str]:
    """HTTP handler contract for `GET /health`.

    Endpoint: GET /health
    Auth: none

    Returns:
        HealthResponse: ``{"status": "ok", "pid": N}``
    """

    raise NotImplementedError("HTTP route handler contract stub")


@pre(
    lambda request_token, expected_token: (
        isinstance(request_token, str) and isinstance(expected_token, str)
    )
)
@post(lambda result: True)
def handle_status(
    request_token: str, expected_token: str
) -> Result[StatusResponse, str]:
    """HTTP handler contract for `GET /status`.

    Endpoint: GET /status
    Auth: Bearer token required.

    The caller is required to provide credentials that must validate with
    ``validate_bearer_token`` from ``tela.shell.http_auth``.
    """

    _ = validate_bearer_token(request_token, expected_token)
    raise NotImplementedError("HTTP route handler contract stub")


@pre(
    lambda request_token, expected_token, payload: (
        isinstance(payload, ConnectRequest)
        and isinstance(request_token, str)
        and isinstance(expected_token, str)
    )
)
@post(lambda result: True)
def handle_connect(
    request_token: str,
    expected_token: str,
    payload: ConnectRequest,
) -> Result[Mapping[str, object], str]:
    """HTTP handler contract for `POST /connect`.

    Endpoint: POST /connect
    Auth: Bearer token required.

    The caller is required to provide credentials that must validate with
    ``validate_bearer_token`` from ``tela.shell.http_auth``.
    """

    _ = validate_bearer_token(request_token, expected_token)
    _ = payload
    raise NotImplementedError("HTTP route handler contract stub")


@pre(
    lambda request_token, expected_token, payload: (
        isinstance(payload, DisconnectRequest)
        and isinstance(request_token, str)
        and isinstance(expected_token, str)
    )
)
@post(lambda result: True)
def handle_disconnect(
    request_token: str,
    expected_token: str,
    payload: DisconnectRequest,
) -> Result[Mapping[str, object], str]:
    """HTTP handler contract for `POST /disconnect`.

    Endpoint: POST /disconnect
    Auth: Bearer token required.

    The caller is required to provide credentials that must validate with
    ``validate_bearer_token`` from ``tela.shell.http_auth``.
    """

    _ = validate_bearer_token(request_token, expected_token)
    _ = payload
    raise NotImplementedError("HTTP route handler contract stub")


@pre(
    lambda request_token, expected_token, payload: (
        isinstance(payload, Mapping)
        and isinstance(request_token, str)
        and isinstance(expected_token, str)
    )
)
@post(lambda result: True)
def handle_mcp(
    request_token: str,
    expected_token: str,
    payload: Mapping[str, object],
) -> Result[Mapping[str, object], str]:
    """HTTP handler contract for `POST /mcp`.

    Endpoint: POST /mcp
    Auth: Bearer token required.

    The caller is required to provide credentials that must validate with
    ``validate_bearer_token`` from ``tela.shell.http_auth``.
    """

    _ = validate_bearer_token(request_token, expected_token)
    _ = payload
    raise NotImplementedError("HTTP route handler contract stub")


_ROUTE_HANDLERS = (
    handle_health,
    handle_status,
    handle_connect,
    handle_disconnect,
    handle_mcp,
)
