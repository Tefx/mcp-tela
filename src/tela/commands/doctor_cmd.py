"""ADR-008 doctor CLI command surface.

The ``tela doctor`` command is the operator diagnostics surface for ADR-008
recovery. Passive doctor reads cached diagnostic artifacts only. Explicit
``--recover`` is the only mode allowed to probe, clean stale lockfiles, or
start a replacement gateway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Literal

from tela.commands.connect_cmd import _autostart_serve, _wait_for_live_lockfile
from tela.commands.http_client import retry_http_request
from tela.core.classification import (
    AttachmentRegistry,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeState,
)
from tela.core.models import LockfileData
from tela.shell import lockfile
from tela.shell.adr008_registry_events import (
    append_runtime_event,
    read_attachment_registry,
    read_runtime_events,
)
from tela.shell.result import Result

DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_RECOVER_TIMEOUT_SECONDS = 5.0
ATTACHMENT_HEARTBEAT_STALE_SECONDS = 90.0
DOCTOR_EVENT_CLIENT_ID = "doctor"
DOCTOR_EVENT_CLIENT_KIND = "tela_cli"


@dataclass(frozen=True)
class DoctorDiscovery:
    """Read-only lockfile discovery facts used by doctor."""

    lockfile_present: bool
    lockfile_stale: bool
    lockfile_data: LockfileData | None
    lockfile_status: str
    pid: int | None
    endpoint: str | None
    discovery_error: str | None = None


@dataclass(frozen=True)
class DoctorProbeObservation:
    """Result of an explicit recovery probe."""

    state: str | None
    degraded_reason: str | None
    error: str | None


@dataclass(frozen=True)
class DoctorRuntimeEventsSummary:
    """Last ADR-008 runtime events relevant to doctor diagnostics."""

    last_provider_exit: dict[str, object] | None
    last_provider_startup_event: dict[str, object] | None
    last_probe_event: dict[str, object] | None
    last_recovery_event: dict[str, object] | None
    malformed_line_count: int
    read_error: str | None = None


@dataclass(frozen=True)
class DoctorAttachmentSummary:
    """Read-only client attachment liveness summary."""

    client_attachments_alive: bool
    liveness_reason: str
    count: int
    alive_client_ids: list[str] = field(default_factory=list)
    registry_parse_error: str | None = None


@dataclass(frozen=True)
class DoctorRecoverySummary:
    """Mutation result for ``tela doctor --recover``."""

    attempted: bool
    recovery_succeeded: bool
    already_ready: bool
    stale_cleanup: bool | None
    cold_start_attempted: bool
    actions: list[str]
    events_appended: list[str]
    error: str | None


@dataclass(frozen=True)
class DoctorResult:
    """ADR-008 doctor JSON and human output model."""

    schema_version: Literal[1]
    recover_performed: bool
    probe_performed: bool
    shared_runtime: dict[str, object]
    recommendation: str
    runtime_events: dict[str, object]
    client_attachments: dict[str, object]
    recovery: dict[str, object]


# @shell_complexity: argparse-facing doctor validation branches by passive versus explicit recovery mode.
def doctor_command(
    *,
    json_output: bool = False,
    recover: bool = False,
    probe_timeout: float | None = None,
    recover_timeout: float | None = None,
) -> Result[int, str]:
    """Run ADR-008 doctor diagnostics or explicit recovery.

    Args:
        json_output: Whether to emit machine-readable JSON.
        recover: Whether to perform explicit probe/recovery mutations.
        probe_timeout: Per-probe timeout in seconds; valid only with recover.
        recover_timeout: Maximum wait for a cold-started lockfile.

    Returns:
        Result containing POSIX exit code ``0`` on command success.
    """

    if probe_timeout is not None and not recover:
        return Result(error="INVALID_ARGUMENT: --probe-timeout requires --recover")

    effective_probe_timeout = (
        probe_timeout if probe_timeout is not None else DEFAULT_PROBE_TIMEOUT_SECONDS
    )
    effective_recover_timeout = (
        recover_timeout
        if recover_timeout is not None
        else DEFAULT_RECOVER_TIMEOUT_SECONDS
    )

    run_result = _run_doctor_command(
        json_output=json_output,
        recover=recover,
        probe_timeout=effective_probe_timeout,
        recover_timeout=effective_recover_timeout,
    )
    if run_result.is_err:
        return Result(error=run_result.error)
    return Result(value=0)


# @shell_complexity: ADR-008 doctor combines passive diagnostics with explicit recovery branches.
def _run_doctor_command(
    *,
    json_output: bool,
    recover: bool,
    probe_timeout: float,
    recover_timeout: float,
) -> Result[None, str]:
    """Execute doctor and print the selected representation."""

    discovery_result = _read_doctor_discovery()
    runtime_events_result = _read_doctor_runtime_events()
    if discovery_result.is_err or discovery_result.value is None:
        return Result(error=discovery_result.error or "DOCTOR_DISCOVERY_ERROR")
    if runtime_events_result.is_err or runtime_events_result.value is None:
        return Result(error=runtime_events_result.error or "DOCTOR_RUNTIME_EVENTS_ERROR")
    discovery = discovery_result.value
    runtime_events = runtime_events_result.value
    attachments_result = _read_doctor_attachment_summary(runtime_events)
    if attachments_result.is_err or attachments_result.value is None:
        return Result(error=attachments_result.error or "DOCTOR_ATTACHMENT_SUMMARY_ERROR")
    attachments = attachments_result.value
    if recover:
        recovery_result = _recover_doctor_runtime(
            discovery=discovery,
            probe_timeout=probe_timeout,
            recover_timeout=recover_timeout,
        )
        if recovery_result.is_err or recovery_result.value is None:
            return Result(error=recovery_result.error or "DOCTOR_RECOVERY_ERROR")
        recovery = recovery_result.value
        refreshed_discovery = _read_doctor_discovery()
        if refreshed_discovery.is_err or refreshed_discovery.value is None:
            return Result(error=refreshed_discovery.error or "DOCTOR_DISCOVERY_ERROR")
        discovery = refreshed_discovery.value
        probe_performed = True
    else:
        recovery = DoctorRecoverySummary(
            attempted=False,
            recovery_succeeded=False,
            already_ready=False,
            stale_cleanup=None,
            cold_start_attempted=False,
            actions=[],
            events_appended=[],
            error=None,
        )
        probe_performed = False

    runtime_state = _runtime_state_from_discovery(discovery)
    result = DoctorResult(
        schema_version=1,
        recover_performed=recover,
        probe_performed=probe_performed,
        shared_runtime={
            "state": runtime_state,
            "pid": discovery.pid,
            "endpoint": discovery.endpoint,
            "lockfile": discovery.lockfile_status,
            "degraded_reason": recovery.error,
        },
        recommendation=_doctor_recommendation(
            runtime_state=runtime_state,
            recover=recover,
            recovery=recovery,
            attachments=attachments,
        ),
        runtime_events={
            "last_provider_exit": runtime_events.last_provider_exit,
            "last_provider_startup_event": runtime_events.last_provider_startup_event,
            "last_probe_event": runtime_events.last_probe_event,
            "last_recovery_event": runtime_events.last_recovery_event,
            "malformed_line_count": runtime_events.malformed_line_count,
            "read_error": runtime_events.read_error,
        },
        client_attachments={
            "client_attachments_alive": attachments.client_attachments_alive,
            "liveness_reason": attachments.liveness_reason,
            "count": attachments.count,
            "alive_client_ids": attachments.alive_client_ids,
            "registry_parse_error": attachments.registry_parse_error,
        },
        recovery={
            "attempted": recovery.attempted,
            "recovery_succeeded": recovery.recovery_succeeded,
            "already_ready": recovery.already_ready,
            "stale_cleanup": recovery.stale_cleanup,
            "cold_start_attempted": recovery.cold_start_attempted,
            "actions": recovery.actions,
            "events_appended": recovery.events_appended,
            "error": recovery.error,
        },
    )

    if json_output:
        _print_doctor_json(result)
    else:
        _print_doctor_human(result)
    return Result(value=None)


# @shell_orchestration: diagnostic aggregate normalizes lockfile Result into a total snapshot for JSON output.
def _read_doctor_discovery() -> Result[DoctorDiscovery, str]:
    """Read lockfile facts without probing or mutating state."""

    lockfile_result = lockfile.read_lockfile()
    if lockfile_result.is_ok and lockfile_result.value is not None:
        data = lockfile_result.value
        return Result(value=DoctorDiscovery(
            lockfile_present=True,
            lockfile_stale=False,
            lockfile_data=data,
            lockfile_status="present",
            pid=data.pid,
            endpoint=f"http://{data.host}:{data.port}",
        ))

    error = lockfile_result.error or "LOCKFILE_READ_ERROR"
    if error.startswith("LOCKFILE_STALE"):
        return Result(value=DoctorDiscovery(
            lockfile_present=True,
            lockfile_stale=True,
            lockfile_data=None,
            lockfile_status="stale",
            pid=None,
            endpoint=None,
            discovery_error=error,
        ))
    return Result(value=DoctorDiscovery(
        lockfile_present=False,
        lockfile_stale=False,
        lockfile_data=None,
        lockfile_status="missing",
        pid=None,
        endpoint=None,
        discovery_error=error,
    ))


# @shell_orchestration: diagnostic aggregate represents JSONL read failure inside the summary block.
# @shell_complexity: runtime-event summary branches by ADR-008 event kind to expose last provider/probe/recovery facts.
def _read_doctor_runtime_events() -> Result[DoctorRuntimeEventsSummary, str]:
    """Read last provider/probe/recovery events from runtime-events JSONL."""

    events_result = read_runtime_events()
    if events_result.is_err or events_result.value is None:
        return Result(value=DoctorRuntimeEventsSummary(None, None, None, None, 0, events_result.error))

    provider_startup_kinds = {
        RuntimeEventKind.PROVIDER_STARTING,
        RuntimeEventKind.PROVIDER_INITIALIZED,
        RuntimeEventKind.PROVIDER_TOOLS_LIST_STARTED,
        RuntimeEventKind.PROVIDER_TOOLS_LIST_COMPLETED,
        RuntimeEventKind.PROVIDER_FAILED,
        RuntimeEventKind.PROVIDER_TIMEOUT,
    }
    last_provider_exit: dict[str, object] | None = None
    last_provider_startup_event: dict[str, object] | None = None
    last_probe_event: dict[str, object] | None = None
    last_recovery_event: dict[str, object] | None = None
    for event in events_result.value.events:
        payload = event.model_dump(mode="json")
        if event.kind == RuntimeEventKind.CLIENT_PROVIDER_EXIT:
            last_provider_exit = payload
        elif event.kind in provider_startup_kinds:
            last_provider_startup_event = payload
        elif event.kind == RuntimeEventKind.RECOVERY_PROBE:
            last_probe_event = payload
        elif event.kind in {
            RuntimeEventKind.RECOVERY_FAILED,
            RuntimeEventKind.RECOVERY_SUCCEEDED,
        }:
            last_recovery_event = payload

    return Result(value=DoctorRuntimeEventsSummary(
        last_provider_exit=last_provider_exit,
        last_provider_startup_event=last_provider_startup_event,
        last_probe_event=last_probe_event,
        last_recovery_event=last_recovery_event,
        malformed_line_count=events_result.value.malformed_line_count,
    ))


# @shell_orchestration: diagnostic aggregate represents registry parse failure inside the liveness block.
def _read_doctor_attachment_summary(
    runtime_events: DoctorRuntimeEventsSummary,
) -> Result[DoctorAttachmentSummary, str]:
    """Summarize client attachment liveness without mutating the registry."""

    registry_result = read_attachment_registry()
    if registry_result.is_err or registry_result.value is None:
        return Result(value=DoctorAttachmentSummary(
            client_attachments_alive=False,
            liveness_reason="registry_parse_error",
            count=0,
            registry_parse_error=registry_result.error,
        ))

    registry: AttachmentRegistry = registry_result.value
    now = datetime.now(UTC)
    alive_ids = [
        attachment.client_id
        for attachment in registry.attachments
        if attachment.runtime_state
        in {RuntimeState.ACTIVE, RuntimeState.INITIALIZING, RuntimeState.RECOVERING}
        and _attachment_heartbeat_is_fresh(attachment.last_heartbeat, now)
    ]
    if alive_ids:
        reason = "client_attachments_alive"
    elif runtime_events.last_provider_exit is not None:
        reason = "last_provider_exit"
    else:
        reason = "no_live_client_attachments"
    return Result(value=DoctorAttachmentSummary(
        client_attachments_alive=bool(alive_ids),
        liveness_reason=reason,
        count=len(registry.attachments),
        alive_client_ids=alive_ids,
    ))


# @invar:allow shell_result: pure heartbeat freshness predicate used inside doctor summary
# @shell_orchestration: parses persisted shell diagnostic timestamps for liveness reporting
def _attachment_heartbeat_is_fresh(last_heartbeat: str, now: datetime) -> bool:
    """Return True only when an attachment heartbeat is inside its lease."""

    try:
        normalized = last_heartbeat.replace("Z", "+00:00")
        heartbeat = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    age_seconds = (now - heartbeat.astimezone(UTC)).total_seconds()
    return 0 <= age_seconds <= ATTACHMENT_HEARTBEAT_STALE_SECONDS


# @shell_orchestration: explicit recovery returns a complete action/event summary even for failed branches.
# @shell_complexity: Explicit recovery must preserve ADR-008 event order across ready, stale, absent, and failure paths.
def _recover_doctor_runtime(
    *,
    discovery: DoctorDiscovery,
    probe_timeout: float,
    recover_timeout: float,
) -> Result[DoctorRecoverySummary, str]:
    """Perform ADR-008 explicit recovery and append ordered runtime events."""

    actions: list[str] = []
    events_appended: list[str] = []
    first_probe_result = _probe_doctor_runtime(discovery, probe_timeout)
    if first_probe_result.is_err or first_probe_result.value is None:
        return Result(error=first_probe_result.error or "DOCTOR_PROBE_ERROR")
    first_probe = first_probe_result.value
    _append_doctor_event(
        RuntimeEventKind.RECOVERY_PROBE,
        {
            "state": first_probe.state,
            "degraded_reason": first_probe.degraded_reason,
            "error": first_probe.error,
        },
        events_appended,
    )
    actions.append("probe")
    if first_probe.state == "ready":
        return Result(value=DoctorRecoverySummary(True, False, True, None, False, actions, events_appended, None))

    stale_cleanup: bool | None = None
    if discovery.lockfile_stale:
        cleanup_result = lockfile.delete_lockfile_if_stale()
        actions.append("stale_cleanup")
        if cleanup_result.is_err:
            error = cleanup_result.error or "LOCKFILE_STALE_CLEANUP_ERROR"
            _append_doctor_event(RuntimeEventKind.RECOVERY_FAILED, {"error": error}, events_appended)
            return Result(value=DoctorRecoverySummary(True, False, False, None, False, actions, events_appended, error))
        stale_cleanup = bool(cleanup_result.value)
        if not stale_cleanup:
            refreshed_result = _read_doctor_discovery()
            if refreshed_result.is_err or refreshed_result.value is None:
                return Result(error=refreshed_result.error or "DOCTOR_DISCOVERY_ERROR")
            refreshed_probe_result = _probe_doctor_runtime(refreshed_result.value, probe_timeout)
            if refreshed_probe_result.is_err or refreshed_probe_result.value is None:
                return Result(error=refreshed_probe_result.error or "DOCTOR_PROBE_ERROR")
            refreshed_probe = refreshed_probe_result.value
            _append_doctor_event(
                RuntimeEventKind.RECOVERY_PROBE,
                {
                    "state": refreshed_probe.state,
                    "degraded_reason": refreshed_probe.degraded_reason,
                    "error": refreshed_probe.error,
                },
                events_appended,
            )
            actions.append("probe_after_stale_cleanup_false")
            if refreshed_probe.state == "ready":
                return Result(value=DoctorRecoverySummary(True, False, True, False, False, actions, events_appended, None))

    start_result = _autostart_serve(config_path="tela.yaml", default_profile=None)
    actions.append("cold_start")
    if start_result.is_err or start_result.value is None:
        error = start_result.error or "COLD_START_FAILED"
        _append_doctor_event(RuntimeEventKind.RECOVERY_FAILED, {"error": error}, events_appended)
        return Result(value=DoctorRecoverySummary(True, False, False, stale_cleanup, True, actions, events_appended, error))

    live_result = _wait_for_live_lockfile(recover_timeout, expected_pid=start_result.value)
    if live_result.is_err or live_result.value is None:
        error = live_result.error or "COLD_START_FAILED"
        _append_doctor_event(RuntimeEventKind.RECOVERY_FAILED, {"error": error}, events_appended)
        return Result(value=DoctorRecoverySummary(True, False, False, stale_cleanup, True, actions, events_appended, error))

    final_discovery = DoctorDiscovery(
        lockfile_present=True,
        lockfile_stale=False,
        lockfile_data=live_result.value,
        lockfile_status="present",
        pid=live_result.value.pid,
        endpoint=f"http://{live_result.value.host}:{live_result.value.port}",
    )
    final_probe_result = _probe_doctor_runtime(final_discovery, probe_timeout)
    if final_probe_result.is_err or final_probe_result.value is None:
        return Result(error=final_probe_result.error or "DOCTOR_PROBE_ERROR")
    final_probe = final_probe_result.value
    _append_doctor_event(
        RuntimeEventKind.RECOVERY_PROBE,
        {
            "state": final_probe.state,
            "degraded_reason": final_probe.degraded_reason,
            "error": final_probe.error,
        },
        events_appended,
    )
    actions.append("probe_after_cold_start")
    if final_probe.state == "ready":
        _append_doctor_event(RuntimeEventKind.RECOVERY_SUCCEEDED, {"state": "ready"}, events_appended)
        return Result(value=DoctorRecoverySummary(True, True, False, stale_cleanup, True, actions, events_appended, None))

    error = final_probe.error or f"RECOVERY_NOT_READY: {final_probe.state or 'unknown'}"
    _append_doctor_event(RuntimeEventKind.RECOVERY_FAILED, {"error": error}, events_appended)
    return Result(value=DoctorRecoverySummary(True, False, False, stale_cleanup, True, actions, events_appended, error))


# @shell_orchestration: explicit probe converts HTTP/parse failures into a diagnostic observation object.
# @shell_complexity: probe distinguishes absent, stale, network, parse, and success outcomes for ADR-008 JSON.
def _probe_doctor_runtime(
    discovery: DoctorDiscovery,
    probe_timeout: float,
) -> Result[DoctorProbeObservation, str]:
    """Probe the current runtime endpoint for explicit recovery only."""

    if not discovery.lockfile_present or discovery.lockfile_data is None:
        return Result(value=DoctorProbeObservation("absent", None, discovery.discovery_error))
    if discovery.lockfile_stale:
        return Result(value=DoctorProbeObservation("stale", None, discovery.discovery_error))
    lockfile_data = discovery.lockfile_data
    probe_result = retry_http_request(
        url=f"http://{lockfile_data.host}:{lockfile_data.port}/status",
        method="GET",
        headers={"Authorization": f"Bearer {lockfile_data.token}", "Accept": "application/json"},
        max_retries=0,
        timeout_seconds=probe_timeout,
        retry_on_503=False,
        retry_on_transient=False,
    )
    if probe_result.is_err or probe_result.value is None:
        return Result(value=DoctorProbeObservation("unknown", None, probe_result.error))
    try:
        payload = json.loads(probe_result.value.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Result(value=DoctorProbeObservation("unknown", None, f"PROBE_PARSE_ERROR: {exc}"))
    finally:
        probe_result.value.close()
    state = payload.get("state")
    degraded_reason = payload.get("degraded_reason")
    return Result(value=DoctorProbeObservation(
        state if isinstance(state, str) else "unknown",
        degraded_reason if isinstance(degraded_reason, str) else None,
        None,
    ))


# @shell_orchestration: helper performs indirect runtime-events append and records event ordering evidence.
def _append_doctor_event(
    kind: RuntimeEventKind,
    details: dict[str, object],
    events_appended: list[str],
) -> None:
    """Append one doctor event and record successful event ordering evidence."""

    event = RuntimeEvent(
        kind=kind,
        client_id=DOCTOR_EVENT_CLIENT_ID,
        client_kind=DOCTOR_EVENT_CLIENT_KIND,
        timestamp=datetime.now(UTC).isoformat(),
        details=details,
    )
    append_result = append_runtime_event(event)
    if append_result.is_ok:
        events_appended.append(kind.value)


# @invar:allow shell_result: pure display mapping stays local to shell-only DoctorDiscovery dataclass.
# @shell_orchestration: display mapping depends on shell-only discovery fields and does not warrant a core artifact.
def _runtime_state_from_discovery(discovery: DoctorDiscovery) -> str:
    """Return the cached runtime state visible to passive doctor."""

    if discovery.lockfile_stale:
        return "stale"
    if not discovery.lockfile_present:
        return "absent"
    return "unknown"


# @invar:allow shell_result: human recommendation is a display template for shell diagnostic output.
# @shell_orchestration: recommendation templates are CLI presentation, not domain classification.
# @shell_complexity: ADR-008 recommendation templates intentionally branch by recovery and liveness state.
def _doctor_recommendation(
    *,
    runtime_state: str,
    recover: bool,
    recovery: DoctorRecoverySummary,
    attachments: DoctorAttachmentSummary,
) -> str:
    """Build ADR-008 recommendation template output."""

    if not recover:
        return "Doctor is passive; run tela doctor --recover to probe and attempt recovery."
    if recovery.already_ready:
        return "Runtime is already ready; no recovery mutation was needed."
    if recovery.recovery_succeeded:
        return "Recovery succeeded; reconnect clients if their transports were closed."
    if attachments.client_attachments_alive:
        return "Client attachments are still alive; do not revive closed client transports."
    if runtime_state == "stale":
        return "Stale cleanup did not restore readiness; inspect runtime-events."
    if runtime_state == "absent":
        return "Cold-start recovery did not produce a ready runtime; inspect serve logs."
    return "Recovery did not establish readiness; inspect runtime-events and status output."


def _print_doctor_json(result: DoctorResult) -> None:
    """Print doctor result as ADR-008 JSON."""

    print(json.dumps(result.__dict__, indent=2))


def _print_doctor_human(result: DoctorResult) -> None:
    """Print doctor result in a human-readable form."""

    if not result.recover_performed:
        print("Doctor source: cached runtime state only.")
        print("No active probe was performed.")
        print("Run `tela doctor --recover` to probe and attempt recovery.")
        print()
    print("Shared Runtime:")
    print(f"  state: {result.shared_runtime['state']}")
    print(f"  lockfile: {result.shared_runtime['lockfile']}")
    if result.shared_runtime["endpoint"]:
        print(f"  endpoint: {result.shared_runtime['endpoint']}")
    print()
    print("Client Attachments:")
    print(f"  alive: {result.client_attachments['client_attachments_alive']}")
    print(f"  liveness_reason: {result.client_attachments['liveness_reason']}")
    print()
    print("Recovery:")
    print(f"  attempted: {result.recovery['attempted']}")
    print(f"  recovery_succeeded: {result.recovery['recovery_succeeded']}")
    if result.recovery["error"]:
        print(f"  error: {result.recovery['error']}")
    print(f"  recommendation: {result.recommendation}")
