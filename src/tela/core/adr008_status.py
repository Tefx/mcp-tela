"""Pure ADR-008 status classification helpers."""

from __future__ import annotations

from tela.core.contracts import post, pre

ADR008_RUNTIME_STATES = frozenset(
    {"absent", "degraded", "ready", "stale", "starting", "unknown"}
)
ADR008_PROBED_STATES = frozenset(
    {
        "config_mismatch",
        "concurrent_startup_follower",
        "degraded",
        "discoverable",
        "ready",
        "starting",
        "warming",
    }
)
ADR008_RECOVERABILITY_STATES = frozenset(
    {"not_recoverable", "recoverable", "unknown"}
)


@pre(
    lambda lockfile_present, lockfile_stale, probed_state, startup_in_progress=False: isinstance(
        lockfile_present, bool
    )
    and isinstance(lockfile_stale, bool)
    and (probed_state is None or isinstance(probed_state, str))
    and isinstance(startup_in_progress, bool)
)
@post(lambda result: result in ADR008_RUNTIME_STATES)
def classify_shared_runtime_state(
    lockfile_present: bool,
    lockfile_stale: bool,
    probed_state: str | None,
    startup_in_progress: bool = False,
) -> str:
    """Classify ADR-008 shared runtime state.

    Args:
        lockfile_present: Whether discovery lockfile data exists.
        lockfile_stale: Whether the discovery PID is no longer live.
        probed_state: Optional runtime state observed from ``GET /status``.
        startup_in_progress: Whether startup is explicitly in progress.

    Returns:
        Normalized ADR-008 runtime state.

    Examples:
        >>> classify_shared_runtime_state(False, False, None)
        'absent'
        >>> classify_shared_runtime_state(True, False, 'warming')
        'starting'
        >>> classify_shared_runtime_state(True, False, 'config_mismatch')
        'degraded'
    """

    if startup_in_progress:
        return "starting"
    if not lockfile_present:
        return "absent"
    if lockfile_stale:
        return "stale"
    if probed_state == "ready":
        return "ready"
    if probed_state == "degraded":
        return "degraded"
    starting_states = {
        "warming",
        "starting",
        "discoverable",
        "concurrent_startup_follower",
    }
    if probed_state in starting_states:
        return "starting"
    if probed_state == "config_mismatch":
        return "degraded"
    return "unknown"


@pre(
    lambda runtime_state, last_error, recovery_command_available: runtime_state
    in ADR008_RUNTIME_STATES
    and (last_error is None or isinstance(last_error, str))
    and isinstance(recovery_command_available, bool)
)
@post(lambda result: result in ADR008_RECOVERABILITY_STATES)
def classify_status_recoverability(
    runtime_state: str,
    last_error: str | None,
    recovery_command_available: bool,
) -> str:
    """Classify ADR-008 runtime recoverability.

    Args:
        runtime_state: Normalized ADR-008 runtime state.
        last_error: Last observed diagnostic error, if any.
        recovery_command_available: Whether explicit recovery can be invoked.

    Returns:
        ADR-008 recoverability state.

    Examples:
        >>> classify_status_recoverability('ready', None, True)
        'not_recoverable'
        >>> classify_status_recoverability('absent', 'missing', True)
        'recoverable'
    """

    if runtime_state == "ready":
        return "not_recoverable"
    if runtime_state == "starting":
        return "unknown"
    if runtime_state in {"degraded", "stale", "absent"}:
        return "recoverable" if recovery_command_available else "not_recoverable"
    return "unknown"


@pre(
    lambda runtime_state, recoverability_state: runtime_state in ADR008_RUNTIME_STATES
    and recoverability_state in ADR008_RECOVERABILITY_STATES
)
@post(lambda result: isinstance(result, str) and len(result) > 0)
def make_status_recommendation(runtime_state: str, recoverability_state: str) -> str:
    """Build ADR-008 human recovery guidance.

    Args:
        runtime_state: Normalized ADR-008 runtime state.
        recoverability_state: ADR-008 recoverability state.

    Returns:
        Human-readable next action.

    Examples:
        >>> make_status_recommendation('ready', 'not_recoverable')
        'Runtime is ready; no recovery action is needed.'
        >>> 'tela status --probe' in make_status_recommendation('unknown', 'unknown')
        True
    """

    if runtime_state == "ready":
        return "Runtime is ready; no recovery action is needed."
    if runtime_state == "starting":
        return "Runtime is starting; wait or run tela status --probe again."
    if runtime_state == "absent" and recoverability_state == "recoverable":
        return "Run tela doctor --recover to attempt explicit recovery."
    if runtime_state == "stale" and recoverability_state == "recoverable":
        return "Run tela doctor --recover to clean stale discovery and recover."
    if runtime_state == "degraded" and recoverability_state == "recoverable":
        return (
            "Run tela doctor --recover to retry recovery; "
            "inspect degraded_reason if it persists."
        )
    if recoverability_state == "not_recoverable":
        return "No automatic recovery is available for this state."
    if runtime_state == "unknown":
        return (
            "State is ambiguous; inspect runtime-events and run "
            "tela status --probe to determine state."
        )
    return "Run tela status --probe to verify reachability."
