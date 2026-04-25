"""HTTP route handler implementations for gateway HTTP endpoints.

This module implements all HTTP routes defined in ``docs/INTERFACES.md``
section 7.2 with explicit auth requirements.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
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
    DisconnectRequest,
    HealthResponse,
    StatusResponse,
)
from tela.core.classification import (
    ClientAttachment,
    Recoverability,
    classify_attachment_display_state,
)
from tela.core.contracts import post, pre
from tela.shell.result import Result
from tela.shell.audit import (  # noqa: F401 — audit query surfaces are route-wired exports
    audit_query,
    audit_query_paginated,
    get_recent_audit_entries,
)
from tela.shell.connection_lifecycle import cleanup_connection_by_id
from tela.shell.gateway_lifecycle import get_lifecycle_status_facts
from tela.shell.gateway_runtime import (
    clear_runtime_connections,  # noqa: F401 — used in doctests
    get_runtime_config,
    get_runtime_status_snapshot,
    is_runtime_running,
    register_bridge_connection,
    set_runtime_config,  # noqa: F401 — used in doctests
    set_runtime_running,  # noqa: F401 — used in doctests
)
from tela.shell.http_auth import validate_bearer_token
from tela.shell.adr008_registry_events import read_attachment_registry
from tela.shell.authorization_explain import handle_authorization_explain

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperatorProbeSnapshot:
    """Read-only operator probe snapshot for remote diagnostics.

    Attributes:
        running: Whether the current runtime snapshot is running.
        state: Lifecycle state observed from the current runtime snapshot.
        degraded_reason: Optional diagnostic reason when the snapshot is not
            fully healthy or discovery metadata is incomplete.
        active_connections: Count of active runtime connections.
        connections: Structural connection snapshot; distinct from the count.
    """

    running: bool
    state: str
    degraded_reason: str | None
    active_connections: int
    connections: list[Mapping[str, object]]


def operator_probe_payload(snapshot: OperatorProbeSnapshot) -> Result[dict[str, object], str]:
    """Return a JSON-serializable probe payload.

    Args:
        snapshot: Operator probe snapshot to serialize.

    Returns:
        Result containing a mapping suitable for ``JSONResponse``.
    """

    return Result(value=asdict(snapshot))


def client_attachment_payload(
    attachment: ClientAttachment,
) -> Result[dict[str, object], str]:
    """Return a JSON-serializable client attachment payload.

    Args:
        attachment: Client attachment model read from the registry.

    Returns:
        Result containing a JSON-mode model dump preserving ADR-008 field names.
    """

    return Result(value=attachment.model_dump(mode="json"))


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

    audit_entries_result = get_recent_audit_entries()
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


@pre(lambda timeout_seconds=5.0: isinstance(timeout_seconds, int | float) and timeout_seconds > 0)
@post(
    lambda result: (
        (
            result.is_ok
            and result.value is not None
            and result.value.active_connections == len(result.value.connections)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_gateway_not_started_error(result.error)
        )
    )
)
def handle_operator_probe(
    timeout_seconds: float = 5.0,
) -> Result[OperatorProbeSnapshot, str]:
    """HTTP-equivalent handler for ``tela status --probe`` diagnostics.

    The handler is intentionally read-only: it observes the current runtime
    snapshot and never invokes startup, recovery, registration, admission,
    disconnect, or session-release paths. ``timeout_seconds`` is accepted to
    mirror the CLI probe surface; this in-process runtime observation does not
    perform a retry loop or mutate state.

    Args:
        timeout_seconds: Positive probe timeout supplied by the caller.

    Returns:
        Result containing the current operator probe snapshot or a not-started
        error when no runtime is active.

    Examples:
        >>> set_runtime_config(None)
        >>> set_runtime_running(False)
        >>> result = handle_operator_probe(timeout_seconds=0.1)
        >>> result.is_err
        True
    """

    snapshot_result = get_runtime_status_snapshot()
    if snapshot_result.is_err:
        return Result(error=snapshot_result.error)
    assert snapshot_result.value is not None
    snapshot = snapshot_result.value
    if snapshot.config is None or not snapshot.running:
        return Result(error=f"{GATEWAY_NOT_STARTED}: gateway has not been started")

    lifecycle_result = get_lifecycle_status_facts()
    if lifecycle_result.is_err:
        return Result(error=lifecycle_result.error)
    assert lifecycle_result.value is not None
    facts = lifecycle_result.value

    degraded_reason = facts.degraded_reason
    if degraded_reason is None:
        degraded_reason = "lockfile_endpoint_not_verified"

    return Result(
        value=OperatorProbeSnapshot(
            running=snapshot.running,
            state=facts.state,
            degraded_reason=degraded_reason,
            active_connections=len(snapshot.connections),
            connections=[conn.model_dump(mode="json") for conn in snapshot.connections],
        )
    )


@pre(lambda: True)
@post(
    lambda result: (
        (result.is_ok and result.value is not None and isinstance(result.value, list))
        or (
            result.is_err
            and isinstance(result.error, str)
            and is_gateway_not_started_error(result.error)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and result.error.startswith("ATTACHMENT_REGISTRY_")
        )
    )
)
def handle_operator_clients() -> Result[list[ClientAttachment], str]:
    """HTTP-equivalent handler for ``tela status --clients`` diagnostics.

    The handler reads the current runtime snapshot and ADR-008 attachment
    registry only. It does not register clients, admit sessions, disconnect,
    release sessions, recover, or rewrite registry contents.

    Returns:
        Result containing the current client attachments. If the runtime is not
        active, an empty list is returned to avoid fabricating remote client
        state for an absent endpoint.

    Examples:
        >>> set_runtime_config(None)
        >>> set_runtime_running(False)
        >>> result = handle_operator_clients()
        >>> result.is_ok
        True
        >>> result.value
        []
    """

    snapshot_result = get_runtime_status_snapshot()
    if snapshot_result.is_err:
        return Result(error=snapshot_result.error)
    assert snapshot_result.value is not None
    snapshot = snapshot_result.value
    if snapshot.config is None or not snapshot.running:
        return Result(value=[])

    registry_result = read_attachment_registry()
    if registry_result.is_err:
        return Result(error=registry_result.error)
    if registry_result.value is None:
        return Result(value=[])
    clients: list[ClientAttachment] = []
    for attachment in registry_result.value.attachments:
        stale_candidate = (
            attachment.stale_candidate
            or attachment.recoverability == Recoverability.STALE
        )
        display_state = classify_attachment_display_state(
            attachment.runtime_state,
            attachment.recoverability,
            stale_candidate,
            attachment.unknown_state,
        )
        clients.append(
            attachment.model_copy(
                update={
                    "display_state": display_state,
                    "stale_candidate": stale_candidate,
                }
            )
        )
    return Result(value=clients)


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
# @shell_complexity: connect endpoint must branch across auth, runtime availability, lifecycle facts, and bridge registration results.
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

    Registers a pending bridge connection identifier in the gateway runtime.

    Profile binding is not established here. Canonical open-mode/token-mode
    admission still occurs at MCP initialize.

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
        >>> req = ConnectRequest(server_name="test-conn-1")
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

    registration_result = register_bridge_connection(payload.server_name)
    if registration_result.is_err:
        return Result(error=registration_result.error)

    return Result(
        value={
            "connection_id": payload.server_name,
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
# @shell_complexity: disconnect endpoint must branch across auth, runtime state, cleanup outcomes, and not-found semantics.
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
        >>> from tela.shell.gateway_runtime import add_runtime_connection
        >>> set_runtime_config(TelaConfig())
        >>> set_runtime_running(True)
        >>> clear_runtime_connections()
        >>> ctx = ConnectionContext(
        ...     connection_id="test-disconnect-1",
        ...     profile_id="default",
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

    if not (
        cleanup_result.value.removed_runtime_connection
        or cleanup_result.value.removed_bridge_registration
    ):
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
