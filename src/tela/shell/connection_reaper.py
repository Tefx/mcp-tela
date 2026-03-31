"""Connection reaper for idle and orphaned gateway connections.

Periodically sweeps runtime connections, removing those whose upstream
session is gone or whose idle TTL has been exceeded.  All public methods
follow Shell convention: return ``Result[T, E]``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tela.shell.config_loader import Result
from tela.shell.connection_lifecycle import cleanup_connection_by_id
from tela.shell.gateway_runtime import get_runtime_connections_snapshot
from tela.shell.idle_shutdown import get_idle_manager
from tela.shell.upstream import get_captured_session

logger = logging.getLogger(__name__)


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

    Examples:
        >>> r = ConnectionReaper()
        >>> r._config.sweep_interval_seconds
        30.0
    """

    def __init__(self, config: ReaperConfig | None = None) -> None:
        """Initialize the connection reaper.

        Args:
            config: Reaper configuration. Defaults to ``ReaperConfig()``
                if not provided.
        """
        self._config = config if config is not None else ReaperConfig()
        self._task: asyncio.Task[None] | None = None
        self._running: bool = False

    async def start(self) -> Result[None, str]:
        """Start the reaper background task.

        Idempotent: if already running, returns Ok(None).

        Returns:
            Result with None on success, or an error string if the
            reaper could not be started.

        Examples:
            >>> import asyncio
            >>> reaper = ConnectionReaper()
            >>> async def _test():
            ...     r = await reaper.start()
            ...     assert r.is_ok
            ...     await reaper.stop()
            >>> asyncio.run(_test())
        """
        if self._running:
            logger.debug("Reaper already running, start() is idempotent")
            return Result(value=None)

        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())
        return Result(value=None)

    async def stop(self) -> Result[None, str]:
        """Stop the reaper background task gracefully.

        Idempotent: if not running, returns Ok(None).

        Returns:
            Result with None on success, or an error string if the
            reaper could not be stopped.

        Examples:
            >>> import asyncio
            >>> reaper = ConnectionReaper()
            >>> r = asyncio.run(reaper.stop())
            >>> r.is_ok
            True
        """
        if not self._running:
            return Result(value=None)

        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        return Result(value=None)

    async def sweep(self) -> Result[ReaperSweepOutcome, str]:
        """Execute a single sweep cycle over all runtime connections.

        Inspects each connection, removing those whose upstream session
        is absent or whose idle TTL has been exceeded.

        Returns:
            Result with a ``ReaperSweepOutcome`` summarizing the sweep,
            or an error string on failure.

        Examples:
            >>> import asyncio
            >>> r = asyncio.run(ConnectionReaper().sweep())
            >>> r.is_ok
            True
            >>> r.value.checked
            0
        """
        connections_result = get_runtime_connections_snapshot()
        if connections_result.is_err:
            return Result(error=connections_result.error)

        assert connections_result.value is not None
        connections = connections_result.value
        now = datetime.now(timezone.utc)

        reaped_session_gone: list[str] = []
        reaped_stale: list[str] = []
        errors: list[str] = []

        for conn in connections:
            cid = conn.connection_id
            already_reaped = False

            # Session probe (conn_* only): check if session registry has a session
            if cid.startswith("conn_"):
                session_result = get_captured_session(cid)
                if session_result.is_err:
                    # Session is gone — reap this connection
                    cleanup_result = cleanup_connection_by_id(cid)
                    if cleanup_result.is_err:
                        errors.append(
                            f"cleanup failed for {cid}: {cleanup_result.error}"
                        )
                    else:
                        reaped_session_gone.append(cid)
                        await self._decrement_idle_manager(cid, errors)
                    already_reaped = True

            # Staleness check (all types, skip if already reaped)
            if not already_reaped:
                # Determine appropriate TTL
                if cid.startswith("bridge_"):
                    ttl = self._config.bridge_idle_ttl_seconds
                    if ttl == 0:
                        continue  # bridge reaping disabled
                else:
                    ttl = self._config.native_idle_ttl_seconds

                # Parse last activity timestamp, fall back to connected_at
                ts_str = conn.last_activity if conn.last_activity else conn.connected_at
                try:
                    last_ts = datetime.fromisoformat(ts_str)
                except (ValueError, TypeError) as exc:
                    errors.append(
                        f"bad timestamp for {cid}: {exc}"
                    )
                    continue

                # Ensure timezone-aware comparison
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)

                idle_seconds = (now - last_ts).total_seconds()
                if idle_seconds > ttl:
                    cleanup_result = cleanup_connection_by_id(cid)
                    if cleanup_result.is_err:
                        errors.append(
                            f"cleanup failed for {cid}: {cleanup_result.error}"
                        )
                    else:
                        reaped_stale.append(cid)
                        await self._decrement_idle_manager(cid, errors)

        return Result(
            value=ReaperSweepOutcome(
                checked=len(connections),
                reaped_session_gone=reaped_session_gone,
                reaped_stale=reaped_stale,
                errors=errors,
            )
        )

    async def _sweep_loop(self) -> None:
        """Background loop that runs sweep at the configured interval."""
        try:
            while self._running:
                await asyncio.sleep(self._config.sweep_interval_seconds)
                if self._running:
                    result = await self.sweep()
                    if result.is_err:
                        logger.warning("Reaper sweep failed: %s", result.error)
                    elif result.value is not None:
                        outcome = result.value
                        total = len(outcome.reaped_session_gone) + len(outcome.reaped_stale)
                        if total > 0:
                            logger.info(
                                "Reaper sweep: checked=%d, reaped_session_gone=%d, reaped_stale=%d, errors=%d",
                                outcome.checked,
                                len(outcome.reaped_session_gone),
                                len(outcome.reaped_stale),
                                len(outcome.errors),
                            )
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _decrement_idle_manager(cid: str, errors: list[str]) -> None:
        """Decrement idle manager after a successful reap.

        Args:
            cid: Connection ID (for error reporting).
            errors: Mutable error collector.
        """
        idle_manager = get_idle_manager()
        if idle_manager is not None:
            dec_result = await idle_manager.decrement()
            if dec_result.is_err:
                errors.append(
                    f"idle_manager.decrement failed for {cid}: {dec_result.error}"
                )
