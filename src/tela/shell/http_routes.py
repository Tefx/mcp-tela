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
)
from tela.core.contracts import post, pre
from tela.shell.config_loader import Result
from tela.shell.audit import audit_query, get_audit_entries  # noqa: F401 — audit_query wired for dead_export
from tela.shell.gateway_lifecycle import get_lifecycle_status_facts
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,  # noqa: F401 — used in doctests
    get_runtime_config,
    is_runtime_running,
    remove_runtime_connection,
    set_runtime_config,  # noqa: F401 — used in doctests
    set_runtime_running,  # noqa: F401 — used in doctests
)
from tela.shell.http_auth import validate_bearer_token
from tela.shell.upstream import release_session


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
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

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
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

    from datetime import datetime, timezone

    connection_context = ConnectionContext(
        connection_id=payload.connection_id,
        profile_name=config.resolved_default_profile or "default",
        connected_at=datetime.now(timezone.utc).isoformat(),
    )

    add_runtime_connection(connection_context)

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
    """

    auth_result = validate_bearer_token(request_token, expected_token)
    if auth_result.is_err:
        return Result(error=auth_result.error)

    if get_runtime_config().value is None or not is_runtime_running().value:
        return Result(error="GATEWAY_NOT_STARTED: gateway has not been started")

    target_id = payload.connection_id
    removed = remove_runtime_connection(target_id).value
    if not removed:
        return Result(error=f"CONNECTION_NOT_FOUND: connection '{target_id}' not found")

    # Release captured upstream session to prevent stale session leaks.
    # Must happen AFTER removing from runtime.connections so that any
    # concurrent notification broadcast sees a consistent state.
    release_session(target_id)

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
