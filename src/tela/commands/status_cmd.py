"""Status CLI command surface.

Provides the ``tela status`` command for displaying gateway runtime status.

ADR-008 status surfaces:
- tela status remains passive and prints required observation cue
- tela status --probe actively checks current lockfile endpoint only
- tela status --clients lists attachment registry with display_state derived
- --probe and --clients are mutually exclusive
- --probe-timeout is valid only with --probe
- --json outputs ADR-008 schema blocks and registry_parse_error
- Status reads must not rewrite attachment registry, cold-start, recover,
  or delete stale candidates
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Literal

from tela.shell.result import Result
from tela.shell import lockfile
from tela.shell.adr008_registry_events import (
    read_attachment_registry,
)
from tela.core.classification import (
    classify_attachment_display_state,
)
from tela.core.adr008_status import (
    classify_shared_runtime_state,
    classify_status_recoverability,
    make_status_recommendation,
)
from tela.commands.http_client import retry_http_request
from tela.core.models import LockfileData

# ADR-008 default probe timeout
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class StatusDiscovery:
    """Read-only discovery facts from the current lockfile."""

    lockfile_present: bool
    lockfile_stale: bool
    lockfile_data: LockfileData | None
    lockfile_status: str
    pid: int | None
    endpoint: str | None


@dataclass(frozen=True)
class ProbeObservation:
    """Read-only result of an explicit ADR-008 runtime probe."""

    probed_state: str | None
    degraded_reason: str | None
    probe_error: str | None


@dataclass(frozen=True)
class ADR008StatusResult:
    """ADR-008 status result with all required schema blocks."""

    schema_version: Literal[1] = 1
    probe_performed: bool = False
    client_attachments: list[dict[str, object]] = field(default_factory=list)
    registry_parse_error: str | None = None
    shared_runtime: dict[str, object] = field(
        default_factory=lambda: {
            "state": "unknown",
            "pid": None,
            "endpoint": None,
            "lockfile": "missing",
            "degraded_reason": None,
        }
    )
    recoverability: dict[str, object] = field(
        default_factory=lambda: {
            "state": "unknown",
            "last_event": None,
            "last_error": None,
            "recommendation": "Run tela status --probe to verify reachability.",
        }
    )


# @shell_complexity: ADR-008 argument validation has mutually exclusive modes.
def status_command(
    json_output: bool = False,
    probe: bool = False,
    clients: bool = False,
    probe_timeout: float | None = None,
) -> Result[int, str]:
    """Display gateway runtime status.

    ADR-008 behavior:
    - Passive status (default): shows observation cue, no active probe
    - status --probe: actively checks lockfile endpoint, no cold-start
    - status --clients: lists attachment registry, display_state derived

    Args:
        json_output: Whether to output JSON.
        probe: Whether to actively probe the runtime endpoint.
        clients: Whether to list client attachments.
        probe_timeout: Timeout for probe in seconds (only valid with probe).

    Returns:
        Result with process exit code.
    """
    # Validate mutual exclusivity
    if probe and clients:
        return Result(
            error="MUTUALLY_EXCLUSIVE: --probe and --clients cannot be used together"
        )

    # Validate --probe-timeout requires --probe
    if probe_timeout is not None and not probe:
        return Result(error="INVALID_ARGUMENT: --probe-timeout requires --probe")

    timeout = probe_timeout if probe_timeout is not None else DEFAULT_PROBE_TIMEOUT_SECONDS

    run_result = _run_status_command(
        json_output=json_output,
        probe=probe,
        clients=clients,
        probe_timeout=timeout,
    )
    if run_result.is_err:
        return Result(error=run_result.error)
    return Result(value=0)


# @shell_complexity: Lifecycle event handlers with inherently branching behavior — routes/priorities/status modes are mutually exclusive by design.
def _run_status_command(
    json_output: bool,
    probe: bool,
    clients: bool,
    probe_timeout: float,
) -> Result[None, str]:
    """Execute status command and print output."""

    discovery_result = _read_status_discovery()
    if discovery_result.is_err or discovery_result.value is None:
        return Result(error=discovery_result.error or "STATUS_DISCOVERY_ERROR")
    discovery = discovery_result.value
    attachments_result = _read_status_attachments()
    if attachments_result.is_err:
        registry_error = attachments_result.error
        attachments: list[dict[str, object]] = []
    else:
        registry_error = None
        attachments = attachments_result.value or []

    observation_result = _probe_status_runtime(discovery, probe, probe_timeout)
    if observation_result.is_err or observation_result.value is None:
        return Result(error=observation_result.error or "STATUS_PROBE_ERROR")
    observation = observation_result.value

    # Classify runtime state
    runtime_state = classify_shared_runtime_state(
        lockfile_present=discovery.lockfile_present,
        lockfile_stale=discovery.lockfile_stale,
        probed_state=observation.probed_state,
        startup_in_progress=False,
    )

    # Build recoverability
    last_error = observation.probe_error
    if registry_error:
        last_error = registry_error if last_error is None else last_error

    recoverability_state = classify_status_recoverability(
        runtime_state=runtime_state,
        last_error=last_error,
        recovery_command_available=True,
    )
    recommendation = make_status_recommendation(runtime_state, recoverability_state)

    # Build ADR-008 schema output
    status_result = ADR008StatusResult(
        schema_version=1,
        probe_performed=probe,
        client_attachments=attachments if clients else [],
        registry_parse_error=registry_error,
        shared_runtime={
            "state": runtime_state,
            "pid": discovery.pid,
            "endpoint": discovery.endpoint,
            "lockfile": discovery.lockfile_status,
            "degraded_reason": observation.degraded_reason,
        },
        recoverability={
            "state": recoverability_state,
            "last_event": (
                "runtime_probe_succeeded"
                if probe and observation.probed_state == "ready"
                else "runtime_probe_failed"
                if probe and observation.probe_error
                else None
            ),
            "last_error": last_error,
            "recommendation": recommendation,
        },
    )

    if json_output:
        _print_status_json(status_result)
        return Result(value=None)

    _print_status_human(
        status_result=status_result,
        probe=probe,
        clients=clients,
        runtime_state=runtime_state,
        recoverability_state=recoverability_state,
        last_error=last_error,
        recommendation=recommendation,
    )
    return Result(value=None)


# @shell_complexity: Discovery must classify missing, present, stale, and PID probe outcomes without mutating state.
def _read_status_discovery() -> Result[StatusDiscovery, str]:
    """Read lockfile discovery facts without cold-starting or recovering."""

    lockfile_result = lockfile.read_lockfile()
    lockfile_present = lockfile_result.is_ok
    lockfile_data = lockfile_result.value if lockfile_present else None
    lockfile_stale = False
    if lockfile_data is not None:
        try:
            os.kill(lockfile_data.pid, 0)
        except (OSError, ProcessLookupError):
            lockfile_stale = True

    if lockfile_data is None:
        return Result(value=StatusDiscovery(False, False, None, "missing", None, None))

    endpoint = f"http://{lockfile_data.host}:{lockfile_data.port}"
    lockfile_status = "stale" if lockfile_stale else "present"
    return Result(
        value=StatusDiscovery(
            lockfile_present,
            lockfile_stale,
            lockfile_data,
            lockfile_status,
            lockfile_data.pid,
            endpoint,
        )
    )


def _read_status_attachments() -> Result[list[dict[str, object]], str]:
    """Read client attachments and derive display state without writing registry."""

    registry_result = read_attachment_registry()
    if registry_result.is_err:
        return Result(error=registry_result.error)
    if registry_result.value is None:
        return Result(value=[])

    attachments: list[dict[str, object]] = []
    for att in registry_result.value.attachments:
        display_state = classify_attachment_display_state(
            att.runtime_state,
            att.recoverability,
            att.stale_candidate,
            att.unknown_state,
        )
        attachments.append(
            {
                "client_id": att.client_id,
                "client_kind": att.client_kind,
                "runtime_state": att.runtime_state.value,
                "recoverability": att.recoverability.value,
                "display_state": display_state.value,
                "connected_at": att.connected_at,
                "last_heartbeat": att.last_heartbeat,
                "stale_candidate": att.stale_candidate,
                "unknown_state": att.unknown_state,
            }
        )
    return Result(value=attachments)


# @shell_complexity: Probe path must distinguish passive, absent, stale, network, parse, and success outcomes.
def _probe_status_runtime(
    discovery: StatusDiscovery,
    probe: bool,
    probe_timeout: float,
) -> Result[ProbeObservation, str]:
    """Probe the current lockfile endpoint only when explicitly requested."""

    if not probe:
        return Result(value=ProbeObservation(None, None, None))
    if not discovery.lockfile_present or discovery.lockfile_data is None:
        return Result(value=ProbeObservation("absent", None, None))
    if discovery.lockfile_stale:
        return Result(value=ProbeObservation("stale", None, None))

    lockfile_data = discovery.lockfile_data
    probe_url = f"http://{lockfile_data.host}:{lockfile_data.port}/status"
    probe_result = retry_http_request(
        url=probe_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {lockfile_data.token}",
            "Accept": "application/json",
        },
        max_retries=0,
        timeout_seconds=probe_timeout,
        retry_on_503=False,
        retry_on_transient=False,
    )
    if probe_result.is_err:
        return Result(value=ProbeObservation("stale", None, probe_result.error))
    assert probe_result.value is not None
    try:
        raw = probe_result.value.read().decode("utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return Result(value=ProbeObservation("unknown", None, None))
    finally:
        probe_result.value.close()
    return Result(
        value=ProbeObservation(
            payload.get("state", "unknown"),
            payload.get("degraded_reason"),
            None,
        )
    )


def _print_status_json(status_result: ADR008StatusResult) -> None:
    """Print ADR-008 JSON status blocks."""

    output_data = {
        "schema_version": status_result.schema_version,
        "probe_performed": status_result.probe_performed,
        "client_attachments": status_result.client_attachments,
        "registry_parse_error": status_result.registry_parse_error,
        "shared_runtime": status_result.shared_runtime,
        "recoverability": status_result.recoverability,
    }
    print(json.dumps(output_data, indent=2))


def _print_status_human(
    status_result: ADR008StatusResult,
    probe: bool,
    clients: bool,
    runtime_state: str,
    recoverability_state: str,
    last_error: str | None,
    recommendation: str,
) -> None:
    """Print human-readable ADR-008 status diagnostics."""

    # Human-readable output
    if not probe and not clients:
        # Passive status - show observation cue
        print("Status source: cached runtime state only.")
        print("No active probe was performed.")
        print("Run `tela status --probe` to verify reachability.")
        print("Run `tela doctor --recover` to attempt recovery.")
        print()

    print("Shared Runtime:")
    print(f"  state: {runtime_state}")
    print(f"  lockfile: {status_result.shared_runtime['lockfile']}")
    if status_result.shared_runtime["pid"]:
        print(f"  pid: {status_result.shared_runtime['pid']}")
    if status_result.shared_runtime["endpoint"]:
        print(f"  endpoint: {status_result.shared_runtime['endpoint']}")
    if status_result.shared_runtime["degraded_reason"]:
        print(f"  degraded_reason: {status_result.shared_runtime['degraded_reason']}")
    print()

    print("Recoverability:")
    print(f"  state: {recoverability_state}")
    if last_error:
        print(f"  last_error: {last_error}")
    print(f"  recommendation: {recommendation}")

    if clients:
        print()
        print("Client Attachments:")
        if not status_result.client_attachments:
            print("  (none)")
        else:
            header = (
                f"{'CLIENT_ID':<12} {'KIND':<10} {'RUNTIME':<15} "
                f"{'RECOVERABILITY':<15} {'DISPLAY':<15} {'LAST_HEARTBEAT'}"
            )
            print(f"  {header}")
            for att in status_result.client_attachments:
                print(
                    f"  {att['client_id']:<12} {att['client_kind']:<10} "
                    f"{att['runtime_state']:<15} {att['recoverability']:<15} "
                    f"{att['display_state']:<15} {att['last_heartbeat']}"
                )
