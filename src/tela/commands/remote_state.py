"""Shared lockfile discovery and remote status query helpers.

Per ``docs/INTERFACES.md`` section 2, query commands discover the running
server through the lockfile and query runtime state over HTTP.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error as urllib_error
from urllib import request as urllib_request

from tela.core.models import AuditEntry, ConnectionContext, LockfileData, StatusResponse
from tela.shell.config_loader import Result
from tela.shell.lockfile import read_lockfile


HTTP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RemoteGatewayState:
    """Remote gateway runtime snapshot fetched from ``GET /status``."""

    status: StatusResponse
    connections: list[ConnectionContext]
    audit_entries: list[AuditEntry]


def query_remote_state() -> Result[RemoteGatewayState, str]:
    """Resolve lockfile endpoint and fetch remote runtime state.

    Returns:
        Result containing parsed remote runtime snapshot.
    """

    lockfile_result = read_lockfile()
    if lockfile_result.is_err:
        return Result(error=_map_lockfile_error(lockfile_result.error))
    assert lockfile_result.value is not None

    payload_result = _fetch_status_payload(lockfile_result.value)
    if payload_result.is_err:
        return Result(error=payload_result.error)
    assert payload_result.value is not None

    return _parse_remote_state(payload_result.value)


# @invar:allow shell_result: helper only maps internal error text to user-facing message.
def _map_lockfile_error(error: str | None) -> str:
    """Map lockfile failures to a clear user-facing no-server error."""

    detail = error or "lockfile unavailable"
    return (
        "NO_RUNNING_SERVER: no running tela server found via "
        f"~/.tela/gateway.lock ({detail})"
    )


def _fetch_status_payload(lockfile: LockfileData) -> Result[dict[str, object], str]:
    """Fetch ``GET /status`` payload from lockfile endpoint."""

    request = urllib_request.Request(
        f"http://{lockfile.host}:{lockfile.port}/status",
        method="GET",
        headers={
            "Authorization": f"Bearer {lockfile.token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            decoded = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return Result(error=f"REMOTE_STATUS_QUERY_ERROR: http {exc.code}")
    except urllib_error.URLError as exc:
        return Result(
            error=(
                "NO_RUNNING_SERVER: no running tela server found via "
                f"~/.tela/gateway.lock (endpoint unreachable: {exc.reason})"
            )
        )

    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        return Result(error=f"REMOTE_STATUS_QUERY_ERROR: invalid JSON response: {exc}")

    if not isinstance(parsed, dict):
        return Result(error="REMOTE_STATUS_QUERY_ERROR: expected JSON object payload")

    return Result(value=parsed)


def _parse_remote_state(payload: dict[str, object]) -> Result[RemoteGatewayState, str]:
    """Parse status payload into typed runtime snapshot models."""

    try:
        status = StatusResponse.model_validate(payload)
        raw_connections = payload.get("connections", [])
        if not isinstance(raw_connections, list):
            return Result(
                error="REMOTE_STATUS_QUERY_ERROR: expected list for 'connections'"
            )

        raw_audit_entries = payload.get("audit_entries", [])
        if not isinstance(raw_audit_entries, list):
            return Result(
                error="REMOTE_STATUS_QUERY_ERROR: expected list for 'audit_entries'"
            )

        connections = [
            ConnectionContext.model_validate(item) for item in raw_connections
        ]
        audit_entries = [AuditEntry.model_validate(item) for item in raw_audit_entries]
    except Exception as exc:
        return Result(error=f"REMOTE_STATUS_QUERY_ERROR: {exc}")

    return Result(
        value=RemoteGatewayState(
            status=status,
            connections=connections,
            audit_entries=audit_entries,
        )
    )
