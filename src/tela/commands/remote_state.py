"""Shared lockfile discovery, status facts, and UX contract helpers.

Per ``docs/INTERFACES.md`` section 2, query commands discover the running
server through the lockfile and query runtime state over HTTP.

This module also carries the shell-side contract for bridge interrupts and
host-facing diagnostics so ``tela connect``, ``tela serve``, ``tela status``,
and ``GET /status`` can converge on one fact model instead of re-deriving
state independently.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Literal
from tela.commands.http_client import retry_http_request
from tela.core.models import AuditEntry, ConnectionContext, LockfileData, StatusResponse
from tela.shell.result import Result
from tela.shell.lockfile import read_lockfile


HTTP_TIMEOUT_SECONDS = 5.0


BridgeInterruptStage = Literal[
    "autostart_wait",
    "attach_loop",
    "bridge_teardown",
]
BridgeInterruptTrigger = Literal["SIGINT", "SIGTERM", "KeyboardInterrupt"]
BridgeMessageState = Literal[
    "discoverable",
    "warming",
    "ready",
    "degraded",
    "config_mismatch",
    "concurrent_startup_follower",
]
DiagnosticSurface = Literal[
    "connect.stderr",
    "serve.stderr",
    "status.human",
    "status.json",
    "http.status",
]


@dataclass(frozen=True)
class BridgeInterruptContract:
    """Normative interrupt behavior for ``tela connect`` lifecycle stages.

    Examples:
        >>> [item.stage for item in BRIDGE_INTERRUPT_CONTRACTS]
        ['autostart_wait', 'attach_loop', 'bridge_teardown']
    """

    trigger: BridgeInterruptTrigger
    stage: BridgeInterruptStage
    termination_semantics: str
    stdout_contract: str
    teardown_contract: str
    message_key: str


@dataclass(frozen=True)
class BridgeBehaviorNote:
    """Short normative note for discovery, startup, and diagnostics behavior."""

    key: str
    applies_to: tuple[str, ...]
    rule: str


@dataclass(frozen=True)
class StatusFactField:
    """One resolved status fact shared by CLI and HTTP diagnostic surfaces."""

    name: str
    type_name: str
    semantics: str


@dataclass(frozen=True)
class DiagnosticSurfaceContract:
    """Mapping from a surface to the shared fact set it may render."""

    surface: DiagnosticSurface
    fact_fields: tuple[str, ...]
    contract_note: str


@dataclass(frozen=True)
class HostMessageCatalogStub:
    """Message catalog stub keyed by fact state rather than raw implementation.

    Examples:
        >>> any(item.state == 'degraded' for item in BRIDGE_MESSAGE_CATALOG_STUBS)
        True
    """

    key: str
    state: BridgeMessageState
    surfaces: tuple[DiagnosticSurface, ...]
    severity: Literal["info", "warning", "error"]
    stream: Literal["stdout", "stderr", "json"]
    template_stub: str


BRIDGE_INTERRUPT_CONTRACTS: tuple[BridgeInterruptContract, ...] = (
    BridgeInterruptContract(
        trigger="SIGINT",
        stage="autostart_wait",
        termination_semantics=(
            "Hard interrupt terminates connect immediately without retrying or "
            "waiting for timeout expiry."
        ),
        stdout_contract="Do not write interrupt diagnostics to stdout.",
        teardown_contract=(
            "No attach or disconnect call is required when interruption happens "
            "before bridge registration completes."
        ),
        message_key="connect.interrupt.autostart_wait",
    ),
    BridgeInterruptContract(
        trigger="SIGTERM",
        stage="attach_loop",
        termination_semantics=(
            "Hard interrupt terminates the active bridge loop immediately and "
            "must not continue forwarding MCP frames after the stop signal."
        ),
        stdout_contract="Stdout remains reserved for MCP transport only.",
        teardown_contract=(
            "Best-effort disconnect is attempted exactly once after the loop stops."
        ),
        message_key="connect.interrupt.attach_loop",
    ),
    BridgeInterruptContract(
        trigger="KeyboardInterrupt",
        stage="bridge_teardown",
        termination_semantics=(
            "Hard interrupt during teardown still terminates connect immediately; "
            "cleanup remains best-effort and must not block process exit."
        ),
        stdout_contract="Stdout remains untouched during teardown interruption.",
        teardown_contract=(
            "Teardown diagnostics may be emitted to stderr, but failure to emit or "
            "finish cleanup never delays termination."
        ),
        message_key="connect.interrupt.bridge_teardown",
    ),
)


BRIDGE_BEHAVIOR_NOTES: tuple[BridgeBehaviorNote, ...] = (
    BridgeBehaviorNote(
        key="shared_fact_authority",
        applies_to=(
            "tela connect",
            "tela serve",
            "tela status",
            "GET /status",
        ),
        rule=(
            "Discovery, warming, ready, degraded, and config mismatch wording must "
            "be rendered from one resolved fact set rather than recomputed per surface."
        ),
    ),
    BridgeBehaviorNote(
        key="no_timeout_only_guidance",
        applies_to=("tela connect", "tela serve", "tela status", "GET /status"),
        rule=(
            "Degraded diagnostics must distinguish discovery, warming, and runtime "
            "degradation without suggesting timeout tuning as the sole remediation."
        ),
    ),
    BridgeBehaviorNote(
        key="host_consistency",
        applies_to=("OpenCode-like hosts", "Claude-like hosts"),
        rule=(
            "Host-facing message states use the same catalog keys and fact semantics "
            "regardless of which MCP host launches ``tela connect``."
        ),
    ),
    BridgeBehaviorNote(
        key="startup_follower",
        applies_to=("tela connect", "tela status", "GET /status"),
        rule=(
            "When another process is already starting the gateway, follower messaging "
            "must report shared-startup attachment instead of implying a private timeout."
        ),
    ),
)


BRIDGE_STATUS_FACT_FIELDS: tuple[StatusFactField, ...] = (
    StatusFactField(
        name="state",
        type_name="BridgeMessageState",
        semantics="Resolved operator-facing lifecycle state.",
    ),
    StatusFactField(
        name="discovery_source",
        type_name="Literal['lockfile', 'autostart', 'explicit_server', 'startup_follower']",
        semantics="How the current endpoint ownership was resolved.",
    ),
    StatusFactField(
        name="config_path",
        type_name="str | None",
        semantics="Config path owned by the running gateway instance when known.",
    ),
    StatusFactField(
        name="requested_config_path",
        type_name="str | None",
        semantics="Config path requested by the current CLI invocation when relevant.",
    ),
    StatusFactField(
        name="config_mismatch",
        type_name="bool",
        semantics="Whether requested config differs from the running gateway owner.",
    ),
    StatusFactField(
        name="degraded_reason",
        type_name="str | None",
        semantics="Stable machine-readable reason for degraded state.",
    ),
    StatusFactField(
        name="active_connections",
        type_name="int",
        semantics="Authoritative count of active bridge connections.",
    ),
    StatusFactField(
        name="connected_servers",
        type_name="list[str]",
        semantics="Currently connected downstream server names.",
    ),
)


BRIDGE_DIAGNOSTIC_SURFACES: tuple[DiagnosticSurfaceContract, ...] = (
    DiagnosticSurfaceContract(
        surface="connect.stderr",
        fact_fields=(
            "state",
            "discovery_source",
            "config_path",
            "requested_config_path",
            "config_mismatch",
            "degraded_reason",
        ),
        contract_note=(
            "Connect emits host-facing diagnostics on stderr only and never invents "
            "state outside the shared fact model."
        ),
    ),
    DiagnosticSurfaceContract(
        surface="serve.stderr",
        fact_fields=(
            "state",
            "config_path",
            "degraded_reason",
        ),
        contract_note=(
            "Serve startup logs announce readiness or degradation from the same fact set "
            "that query surfaces consume."
        ),
    ),
    DiagnosticSurfaceContract(
        surface="status.human",
        fact_fields=(
            "state",
            "discovery_source",
            "config_path",
            "config_mismatch",
            "degraded_reason",
            "active_connections",
            "connected_servers",
        ),
        contract_note=(
            "Human-readable status may summarize facts, but must not contradict HTTP status."
        ),
    ),
    DiagnosticSurfaceContract(
        surface="status.json",
        fact_fields=tuple(field.name for field in BRIDGE_STATUS_FACT_FIELDS),
        contract_note=(
            "JSON status is the CLI serialization of the shared fact set and remains "
            "the source for machine-parsed diagnostics."
        ),
    ),
    DiagnosticSurfaceContract(
        surface="http.status",
        fact_fields=tuple(field.name for field in BRIDGE_STATUS_FACT_FIELDS),
        contract_note=(
            "GET /status returns the same resolved fact model consumed by CLI status."
        ),
    ),
)


BRIDGE_MESSAGE_CATALOG_STUBS: tuple[HostMessageCatalogStub, ...] = (
    HostMessageCatalogStub(
        key="bridge.discoverable",
        state="discoverable",
        surfaces=("connect.stderr", "status.human", "status.json", "http.status"),
        severity="info",
        stream="stderr",
        template_stub="Existing gateway discovered; attach using published endpoint facts.",
    ),
    HostMessageCatalogStub(
        key="bridge.warming",
        state="warming",
        surfaces=(
            "connect.stderr",
            "serve.stderr",
            "status.human",
            "status.json",
            "http.status",
        ),
        severity="info",
        stream="stderr",
        template_stub="Gateway startup is in progress; distinguish warmup from failure.",
    ),
    HostMessageCatalogStub(
        key="bridge.ready",
        state="ready",
        surfaces=(
            "connect.stderr",
            "serve.stderr",
            "status.human",
            "status.json",
            "http.status",
        ),
        severity="info",
        stream="stderr",
        template_stub="Gateway is ready; include resolved endpoint and profile ownership facts when relevant.",
    ),
    HostMessageCatalogStub(
        key="bridge.degraded",
        state="degraded",
        surfaces=(
            "connect.stderr",
            "serve.stderr",
            "status.human",
            "status.json",
            "http.status",
        ),
        severity="warning",
        stream="stderr",
        template_stub="Gateway is reachable but degraded; explain degraded_reason without timeout-only advice.",
    ),
    HostMessageCatalogStub(
        key="bridge.config_mismatch",
        state="config_mismatch",
        surfaces=("connect.stderr", "status.human", "status.json", "http.status"),
        severity="warning",
        stream="stderr",
        template_stub="Running gateway uses a different config_path than the caller requested.",
    ),
    HostMessageCatalogStub(
        key="bridge.concurrent_startup_follower",
        state="concurrent_startup_follower",
        surfaces=("connect.stderr", "status.human", "status.json", "http.status"),
        severity="info",
        stream="stderr",
        template_stub="Another connector is already starting the shared gateway; this client is following that startup.",
    ),
)


@dataclass(frozen=True)
class RemoteGatewayState:
    """Remote gateway runtime snapshot fetched from ``GET /status``.

    `status` remains the authoritative runtime payload; host-facing diagnostics
    for CLI and HTTP surfaces must derive from the shared fact fields declared
    in ``BRIDGE_STATUS_FACT_FIELDS`` and ``BRIDGE_DIAGNOSTIC_SURFACES``.
    """

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
        detail = lockfile_result.error or "lockfile unavailable"
        orphaned = _find_orphaned_serve_processes()
        if orphaned:
            detail = f"{detail}; orphaned tela serve processes detected: " + ", ".join(
                str(pid) for pid in orphaned
            )
        return Result(
            error=(
                "NO_RUNNING_SERVER: no running tela server found via "
                f"~/.tela/gateway.lock ({detail})"
            )
        )
    assert lockfile_result.value is not None

    payload_result = _fetch_status_payload(lockfile_result.value)
    if payload_result.is_err:
        return Result(error=payload_result.error)
    assert payload_result.value is not None

    return _parse_remote_state(payload_result.value)


# @invar:allow shell_result: best-effort subprocess inspection returns pid list, not a failable boundary.
def _find_orphaned_serve_processes() -> list[int]:
    """Return best-effort list of live ``tela serve`` pids without a lockfile."""

    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,stat=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []

    if result.returncode != 0:
        return []

    matches: list[int] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid_raw, stat, command = parts
        if "Z" in stat.upper():
            continue
        if "tela serve" not in command:
            continue
        try:
            matches.append(int(pid_raw))
        except ValueError:
            continue
    return matches


# @shell_complexity: HTTP request for status payload — no retry (remote_state
# callers handle retry at a higher level). Delegates request construction
# to shared helper; caller retains error-format semantics and response parsing.
def _fetch_status_payload(lockfile: LockfileData) -> Result[dict[str, object], str]:
    """Fetch ``GET /status`` payload from lockfile endpoint.

    Delegates the HTTP request to ``retry_http_request`` with no retry
    (``max_retries=0``), consistent with the pre-existing single-attempt
    behavior. Error format is transformed to preserve the caller-owned
    ``REMOTE_STATUS_QUERY_ERROR`` / ``NO_RUNNING_SERVER`` semantics.
    """

    result = retry_http_request(
        url=f"http://{lockfile.host}:{lockfile.port}/status",
        method="GET",
        headers={
            "Authorization": f"Bearer {lockfile.token}",
            "Accept": "application/json",
        },
        max_retries=0,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
        retry_on_503=False,
        retry_on_transient=False,
    )
    if result.is_err:
        error = result.error or ""
        # Transform retry_http_request error format to remote_state semantics.
        # NOTE: HTTP_CONNECT_ERROR must be matched before generic HTTP_ because
        # it also starts with "HTTP_".
        if error.startswith("HTTP_CONNECT_ERROR: "):
            reason = error[len("HTTP_CONNECT_ERROR: ") :]
            return Result(
                error=(
                    "NO_RUNNING_SERVER: no running tela server found via "
                    f"~/.tela/gateway.lock (endpoint unreachable: {reason})"
                )
            )
        if error.startswith("HTTP_"):
            # HTTP_{code}: {url} → REMOTE_STATUS_QUERY_ERROR: http {code}
            code_end = error.index(":")
            code = error[len("HTTP_") : code_end]
            return Result(error=f"REMOTE_STATUS_QUERY_ERROR: http {code}")
        return Result(error=error)
    assert result.value is not None

    try:
        decoded = result.value.read().decode("utf-8")
    except Exception as exc:
        return Result(error=f"REMOTE_STATUS_QUERY_ERROR: {exc}")
    finally:
        try:
            result.value.close()
        except OSError:
            pass

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
