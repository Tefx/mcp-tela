"""HTTP route handler implementations for gateway HTTP endpoints.

This module implements all HTTP routes defined in ``docs/INTERFACES.md``
section 7.2 with explicit auth requirements.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

from tela.core.errors import (
    CONNECTION_NOT_FOUND,
    GATEWAY_NOT_STARTED,
    is_auth_error,
    is_connection_not_found_error,
    is_gateway_not_started_error,
)
from tela.core.models import (
    ConnectRequest,
    ConnectionContext,
    DisconnectRequest,
    HealthResponse,
    StatusResponse,
)
from tela.core.contracts import post, pre
from tela.shell.result import Result
from tela.shell.audit import audit_query, get_audit_entries  # noqa: F401 — audit_query wired for dead_export
from tela.shell.connection_lifecycle import cleanup_connection_by_id
from tela.shell.gateway_lifecycle import get_lifecycle_status_facts
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,  # noqa: F401 — used in doctests
    get_runtime_config,
    is_runtime_running,
    set_runtime_config,  # noqa: F401 — used in doctests
    set_runtime_running,  # noqa: F401 — used in doctests
    touch_connection_activity,
)
from tela.shell.http_auth import validate_bearer_token

logger = logging.getLogger(__name__)


@pre(lambda: True)
@post(
    lambda result: (
        result.is_ok
        and result.value is not None
        and result.value.status == "ok"
        and result.value.pid > 0
    )
)
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
@post(
    lambda result: (
        (
            result.is_ok
            and result.value is not None
            and result.value.active_connections == len(result.value.connections)
            and isinstance(result.value.connected_servers, list)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_auth_error(result.error)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_gateway_not_started_error(result.error)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and result.error.startswith("AUDIT_QUERY_ERROR")
        )
    )
)
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

    Contract note:
        ``GET /status`` is the HTTP authority for the shared diagnostic fact set
        consumed by ``tela status`` and host-facing bridge messaging. HTTP and
        CLI surfaces must align to the same resolved facts instead of deriving
        parallel state labels independently. This handler delegates to
        ``get_lifecycle_status_facts()`` for the authoritative lifecycle
        snapshot rather than re-deriving readiness or connectivity independently.

    Examples:
        >>> set_runtime_config(None)  # Gateway not started
        >>> result = handle_status("valid-token", "valid-token")
        >>> result.is_err
        True
        >>> result.error.startswith("GATEWAY_NOT_STARTED")
        True
    """

    auth_result = validate_bearer_token(request_token, expected_token)
    if auth_result.is_err:
        return Result(error=auth_result.error)

    lifecycle_result = get_lifecycle_status_facts()
    if lifecycle_result.is_err:
        return Result(error=lifecycle_result.error)
    assert lifecycle_result.value is not None
    facts = lifecycle_result.value

    snap = facts.snapshot
    if snap.config is None or not snap.running:
        return Result(error=f"{GATEWAY_NOT_STARTED}: gateway has not been started")

    start_time = snap.start_time if snap.start_time else 0.0
    uptime = 0.0
    if start_time > 0:
        import time

        uptime = time.monotonic() - start_time

    profile_count = facts.profile_count

    audit_entries_result = get_audit_entries()
    if audit_entries_result.is_err:
        return Result(error=f"AUDIT_QUERY_ERROR: {audit_entries_result.error}")
    assert audit_entries_result.value is not None

    connected_servers_list = list(facts.connected_servers)

    # config_path is not stored in TelaConfig; it's in the lockfile.
    # For HTTP status, we don't have the requested config path context,
    # so we set these to None and let CLI status command handle mismatch detection.
    config_path: str | None = None

    return Result(
        value=StatusResponse(
            uptime_seconds=uptime,
            server_count=facts.server_count,
            connected_servers=connected_servers_list,
            active_connections=facts.active_connections,
            profile_count=profile_count,
            total_tool_calls=facts.total_tool_calls,
            connections=list(snap.connections),
            audit_entries=audit_entries_result.value,
            state=facts.state,
            degraded_reason=facts.degraded_reason,
            config_path=config_path,
            discovery_source=None,
            requested_config_path=None,
            config_mismatch=False,
        )
    )


@pre(
    lambda request_token, expected_token, payload: (
        isinstance(payload, ConnectRequest)
        and isinstance(request_token, str)
        and isinstance(expected_token, str)
    )
)
@post(
    lambda result: (
        (
            result.is_ok
            and result.value is not None
            and result.value.get("status") == "connected"
            and isinstance(result.value.get("connection_id"), str)
            and isinstance(result.value.get("profile_name"), str)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_auth_error(result.error)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_gateway_not_started_error(result.error)
        )
    )
)
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

    This endpoint is lifecycle plumbing only and is not a readiness-gated
    admission surface for MCP traffic. Readiness-gated admission is enforced
    by ``POST /mcp``, while lifecycle readiness authority remains the gateway
    runtime snapshot exposed by ``GET /status``.

    Returns:
        Result with connection confirmation on success.

    Examples:
        >>> from tela.core.models import ConnectRequest, TelaConfig
        >>> set_runtime_config(TelaConfig())
        >>> set_runtime_running(True)
        >>> req = ConnectRequest(connection_id="test-conn-1")
        >>> result = handle_connect("valid-token", "valid-token", req)
        >>> result.is_ok
        True
    """

    auth_result = validate_bearer_token(request_token, expected_token)
    if auth_result.is_err:
        return Result(error=auth_result.error)

    config = get_runtime_config().value
    if config is None or not is_runtime_running().value:
        return Result(error=f"{GATEWAY_NOT_STARTED}: gateway has not been started")

    lifecycle_result = get_lifecycle_status_facts()
    if lifecycle_result.is_err:
        return Result(error=lifecycle_result.error)

    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    connection_context = ConnectionContext(
        connection_id=payload.connection_id,
        profile_name=config.resolved_default_profile or "default",
        connected_at=now_iso,
        init_mode=config.auth.mode,
        bridge_connection_id=payload.connection_id,
    )

    add_runtime_connection(connection_context)

    touch_r = touch_connection_activity(payload.connection_id, now_iso)
    if touch_r.is_err:
        logger.warning(
            "Failed to touch connection activity for %s: %s",
            payload.connection_id,
            touch_r.error,
        )

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
@post(
    lambda result: (
        (
            result.is_ok
            and result.value is not None
            and result.value.get("status") == "disconnected"
            and isinstance(result.value.get("connection_id"), str)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_auth_error(result.error)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_gateway_not_started_error(result.error)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_connection_not_found_error(result.error)
        )
    )
)
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
        >>> set_runtime_config(TelaConfig())
        >>> set_runtime_running(True)
        >>> clear_runtime_connections()
        >>> ctx = ConnectionContext(
        ...     connection_id="test-disconnect-1",
        ...     profile_name="default",
        ...     connected_at="2026-01-01T00:00:00Z"
        ... )
        >>> add_runtime_connection(ctx)
        >>> req = DisconnectRequest(connection_id="test-disconnect-1")
        >>> result = handle_disconnect("valid-token", "valid-token", req)
        >>> result.is_ok
        True
        >>> missing = handle_disconnect(
        ...     "valid-token",
        ...     "valid-token",
        ...     DisconnectRequest(connection_id="does-not-exist"),
        ... )
        >>> missing.is_err
        True
        >>> missing.error.startswith("CONNECTION_NOT_FOUND")
        True
    """

    auth_result = validate_bearer_token(request_token, expected_token)
    if auth_result.is_err:
        return Result(error=auth_result.error)

    if get_runtime_config().value is None or not is_runtime_running().value:
        return Result(error=f"{GATEWAY_NOT_STARTED}: gateway has not been started")

    target_id = payload.connection_id
    cleanup_result = cleanup_connection_by_id(target_id)
    if cleanup_result.is_err:
        return Result(error=cleanup_result.error)
    assert cleanup_result.value is not None

    if not cleanup_result.value.removed_runtime_connection:
        return Result(
            error=f"{CONNECTION_NOT_FOUND}: connection '{target_id}' not found"
        )

    return Result(
        value={
            "connection_id": target_id,
            "status": "disconnected",
        }
    )


_ROUTE_HANDLERS = (
    handle_health,
    handle_status,
    handle_connect,
    handle_disconnect,
)
