"""Downstream provider startup runtime helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from tela.core.classification import RuntimeEvent, RuntimeEventKind
from tela.core.downstream_startup import (
    DownstreamStartupSnapshot,
    ProviderStartupFailure,
    degraded_reason_from_failures,
    startup_failure_from_error,
)
from tela.shell.adr008_registry_events import append_runtime_event_best_effort

__all__ = [
    "DownstreamStartupSnapshot",
    "ProviderStartupFailure",
    "degraded_reason_from_failures",
    "is_external_task_cancellation",
    "provider_event",
    "startup_failure_from_error",
]


# @shell_orchestration: wraps runtime-event model construction and append side effect
def provider_event(
    kind: RuntimeEventKind,
    *,
    server_name: str,
    phase: str,
    details: dict[str, object] | None = None,
) -> None:
    """Append one best-effort downstream provider startup event."""

    payload: dict[str, object] = {"provider_name": server_name, "phase": phase}
    if details:
        payload.update(details)
    append_runtime_event_best_effort(
        RuntimeEvent(
            kind=kind,
            client_id=f"provider:{server_name}",
            client_kind="downstream_provider",
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            details=payload,
        )
    )


# @invar:allow shell_result: asyncio cancellation state is runtime-only diagnostic plumbing.
# @shell_orchestration: asyncio cancellation state is runtime diagnostic plumbing.
def is_external_task_cancellation() -> bool:
    """Return true when cancellation came from the owning asyncio task.

    Provider transports can raise ``CancelledError`` from their own internal
    cancel scopes when a downstream is unavailable. Those are provider failures,
    not operator/runtime cancellation of tela startup. When the current task is
    actually being cancelled, preserve normal cancellation propagation.
    """

    current = asyncio.current_task()
    return current is not None and current.cancelling() > 0
