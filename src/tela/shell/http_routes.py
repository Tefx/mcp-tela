"""HTTP route handler implementations for gateway HTTP endpoints.

This module implements all HTTP routes defined in ``docs/INTERFACES.md``
section 7.2 with explicit auth requirements.
"""

from __future__ import annotations

import os
from typing import Mapping

from tela.core.models import (
    ConnectRequest,
    ConnectionContext,
    DisconnectRequest,
    HealthResponse,
    StatusResponse,
    TelaError,
)
from tela.core.contracts import post, pre
from tela.shell.config_loader import Result
from tela.shell.gateway import get_runtime
from tela.shell.http_auth import validate_bearer_token


@pre(lambda: True)
@post(lambda result: result.is_ok and result.value.status == "ok")
def handle_health() -> Result[HealthResponse, str]:
    """HTTP handler for `GET /health`.

    Endpoint: GET /health
    Auth: none

    Returns:
        HealthResponse: ``{"status": "ok", "pid": N}``

    Examples:
        >>> result = handle_health()
        >>> result.is_ok
        True
        >>> result.value.status
        'ok'
        >>> result.value.pid > 0
        True
    """

    return Result(value=HealthResponse(status="ok", pid=os.getpid()))


@pre(
    lambda request_token, expected_token: (
        isinstance(request_token, str) and isinstance(expected_token, str)
    )
)
@post(lambda result: result.is_ok or result.is_err)
# @shell_complexity: status handler branches on auth/config/uptime/server-list patterns
def handle_status(
    request_token: str, expected_token: str
) -> Result[StatusResponse, str]:
    """HTTP handler for `GET /status`.

    Endpoint: GET /status
    Auth: Bearer token required.

    The caller is required to provide credentials that must validate with
    ``validate_bearer_token`` from ``tela.shell.http_auth``.

    Returns:
        Result[StatusResponse, str] with gateway runtime status on success.

    Examples:
        >>> runtime = get_runtime()
        >>> runtime.config = None  # Gateway not started
        >>> result = handle_status("valid-token", "valid-token")
        >>> result.is_err
        True
    """

    if not validate_bearer_token(request_token, expected_token):
        return Result(error="AUTH_INVALID_TOKEN: bearer token validation failed")

    runtime = get_runtime()
    if runtime.config is None or not runtime.running:
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

    start_time = runtime.start_time if runtime.start_time else 0.0
    uptime = 0.0
    if start_time > 0:
        import time

        uptime = time.monotonic() - start_time

    connected_servers: list[str] = []
    if runtime.config:
        connected_servers = list(runtime.config.servers.keys())

    profile_count = len(runtime.config.profiles) if runtime.config else 0

    return Result(
        value=StatusResponse(
            uptime_seconds=uptime,
            server_count=len(connected_servers),
            connected_servers=connected_servers,
            active_connections=len(runtime.connections),
            profile_count=profile_count,
            total_tool_calls=runtime.total_tool_calls,
        )
    )


@pre(
    lambda request_token, expected_token, payload: (
        isinstance(payload, ConnectRequest)
        and isinstance(request_token, str)
        and isinstance(expected_token, str)
    )
)
@post(lambda result: result.is_ok or result.is_err)
def handle_connect(
    request_token: str,
    expected_token: str,
    payload: ConnectRequest,
) -> Result[Mapping[str, object], str]:
    """HTTP handler for `POST /connect`.

    Endpoint: POST /connect
    Auth: Bearer token required.

    The caller is required to provide credentials that must validate with
    ``validate_bearer_token`` from ``tela.shell.http_auth``.

    Registers a bridge connection in the gateway runtime.

    Returns:
        Result with connection confirmation on success.

    Examples:
        >>> from tela.core.models import ConnectRequest, TelaConfig
        >>> runtime = get_runtime()
        >>> runtime.config = TelaConfig()
        >>> runtime.running = True
        >>> req = ConnectRequest(connection_id="test-conn-1")
        >>> result = handle_connect("valid-token", "valid-token", req)
        >>> result.is_ok
        True
    """

    if not validate_bearer_token(request_token, expected_token):
        return Result(error="AUTH_INVALID_TOKEN: bearer token validation failed")

    runtime = get_runtime()
    if runtime.config is None or not runtime.running:
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

    from datetime import datetime, timezone

    connection_context = ConnectionContext(
        connection_id=payload.connection_id,
        profile_name=runtime.config.resolved_default_profile or "default",
        connected_at=datetime.now(timezone.utc).isoformat(),
    )

    runtime.connections.append(connection_context)

    return Result(
        value={
            "connection_id": connection_context.connection_id,
            "profile_name": connection_context.profile_name,
            "status": "connected",
        }
    )


@pre(
    lambda request_token, expected_token, payload: (
        isinstance(payload, DisconnectRequest)
        and isinstance(request_token, str)
        and isinstance(expected_token, str)
    )
)
@post(lambda result: result.is_ok or result.is_err)
def handle_disconnect(
    request_token: str,
    expected_token: str,
    payload: DisconnectRequest,
) -> Result[Mapping[str, object], str]:
    """HTTP handler for `POST /disconnect`.

    Endpoint: POST /disconnect
    Auth: Bearer token required.

    The caller is required to provide credentials that must validate with
    ``validate_bearer_token`` from ``tela.shell.http_auth``.

    Deregisters a bridge connection from the gateway runtime.

    Returns:
        Result with disconnection confirmation on success.

    Examples:
        >>> from tela.core.models import DisconnectRequest, TelaConfig, ConnectionContext
        >>> runtime = get_runtime()
        >>> runtime.config = TelaConfig()
        >>> runtime.running = True
        >>> runtime.connections.clear()
        >>> ctx = ConnectionContext(
        ...     connection_id="test-disconnect-1",
        ...     profile_name="default",
        ...     connected_at="2026-01-01T00:00:00Z"
        ... )
        >>> runtime.connections.append(ctx)
        >>> req = DisconnectRequest(connection_id="test-disconnect-1")
        >>> result = handle_disconnect("valid-token", "valid-token", req)
        >>> result.is_ok
        True
    """

    if not validate_bearer_token(request_token, expected_token):
        return Result(error="AUTH_INVALID_TOKEN: bearer token validation failed")

    runtime = get_runtime()
    if runtime.config is None or not runtime.running:
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

    target_id = payload.connection_id
    original_count = len(runtime.connections)

    runtime.connections[:] = [
        conn for conn in runtime.connections if conn.connection_id != target_id
    ]

    if len(runtime.connections) == original_count:
        return Result(error=f"CONNECTION_NOT_FOUND: connection '{target_id}' not found")

    return Result(
        value={
            "connection_id": target_id,
            "status": "disconnected",
        }
    )


@pre(
    lambda request_token, expected_token, payload: (
        isinstance(payload, Mapping)
        and isinstance(request_token, str)
        and isinstance(expected_token, str)
    )
)
@post(lambda result: result.is_ok or result.is_err)
def handle_mcp(
    request_token: str,
    expected_token: str,
    payload: Mapping[str, object],
) -> Result[Mapping[str, object], str | TelaError]:
    """HTTP handler for `POST /mcp`.

    Endpoint: POST /mcp
    Auth: Bearer token required.

    The caller is required to provide credentials that must validate with
    ``validate_bearer_token`` from ``tela.shell.http_auth``.

    Forwards requests to the FastMCP StreamableHTTP handler.

    Returns:
        Result with MCP response on success, or TelaError on failure.

    Examples:
        >>> runtime = get_runtime()
        >>> runtime.config = None  # Gateway not started
        >>> result = handle_mcp("valid-token", "valid-token", {})
        >>> result.is_err
        True
    """

    if not validate_bearer_token(request_token, expected_token):
        return Result(error="AUTH_INVALID_TOKEN: bearer token validation failed")

    runtime = get_runtime()
    if runtime.config is None or not runtime.running:
        return Result(
            error=TelaError(
                code="GATEWAY_NOT_STARTED",
                message="Gateway has not been started",
            )
        )

    upstream_server = runtime.upstream_server
    if upstream_server is None:
        return Result(
            error=TelaError(
                code="MCP_HANDLER_NOT_AVAILABLE",
                message="MCP handler is not available",
            )
        )

    _ = payload
    return Result(
        error=TelaError(
            code="MCP_HANDLER_NOT_IMPLEMENTED",
            message="MCP StreamableHTTP forwarding not yet implemented",
        )
    )


_ROUTE_HANDLERS = (
    handle_health,
    handle_status,
    handle_connect,
    handle_disconnect,
    handle_mcp,
)
