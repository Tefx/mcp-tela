"""Connection reaper for idle and orphaned gateway connections.

Periodically sweeps runtime connections, removing those whose upstream
session is gone or whose idle TTL has been exceeded.  All public methods
follow Shell convention: return ``Result[T, E]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tela.shell.config_loader import Result


@dataclass(frozen=True)
class ReaperConfig:
    """Configuration knobs for the connection reaper.

    Attributes:
        sweep_interval_seconds: How often the reaper runs a sweep cycle.
        native_idle_ttl_seconds: Max idle time for native (non-bridge)
            connections before they are reaped.
        bridge_idle_ttl_seconds: Max idle time for bridge connections
            before they are reaped.  Set to 0 to disable bridge reaping.
    """

    sweep_interval_seconds: float = 30.0
    native_idle_ttl_seconds: float = 120.0
    bridge_idle_ttl_seconds: float = 300.0  # 0 = disabled


@dataclass(frozen=True)
class ReaperSweepOutcome:
    """Result payload from one reaper sweep cycle.

    Attributes:
        checked: Number of connections inspected during the sweep.
        reaped_session_gone: Connection IDs removed because their
            upstream session was no longer present.
        reaped_stale: Connection IDs removed because their idle TTL
            was exceeded.
        errors: Non-fatal errors encountered during the sweep.
    """

    checked: int = 0
    reaped_session_gone: list[str] = field(default_factory=list)
    reaped_stale: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ConnectionReaper:
    """Async reaper that periodically sweeps idle/orphaned connections.

    The reaper runs as a background task, executing sweep cycles at the
    interval specified by ``ReaperConfig.sweep_interval_seconds``.  Each
    sweep inspects all runtime connections and removes those that are
    orphaned (session gone) or stale (idle TTL exceeded).
    """

    async def start(self) -> Result[None, str]:
        """Start the reaper background task.

        Returns:
            Result with None on success, or an error string if the
            reaper could not be started.
        """
        raise NotImplementedError

    async def stop(self) -> Result[None, str]:
        """Stop the reaper background task gracefully.

        Returns:
            Result with None on success, or an error string if the
            reaper could not be stopped.
        """
        raise NotImplementedError

    async def sweep(self) -> Result[ReaperSweepOutcome, str]:
        """Execute a single sweep cycle over all runtime connections.

        Inspects each connection, removing those whose upstream session
        is absent or whose idle TTL has been exceeded.

        Returns:
            Result with a ``ReaperSweepOutcome`` summarizing the sweep,
            or an error string on failure.
        """
        raise NotImplementedError
