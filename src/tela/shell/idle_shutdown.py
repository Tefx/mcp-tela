"""Idle shutdown manager for HTTP gateway.

Tracks active bridge connections and triggers graceful shutdown when all
connections are closed and the idle timeout expires.

Contract: docs/INTERFACES.md §7.2

Semantics:
- Increment count on /connect, decrement on /disconnect
- Start idle timer when count reaches 0
- Expiry triggers shutdown callback
- New connection resets idle timer
- timeout_seconds=0 disables auto-shutdown
- asyncio.Lock protects concurrent connect/disconnect
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from tela.shell.result import Result


class IdleShutdownManager:
    """Manages connection tracking and idle-timer-based shutdown.

    Thread safety: All mutable state is protected by asyncio.Lock.
    Single-instance: Module-level singleton pattern.

    Attributes:
        timeout_seconds: Idle timeout in seconds. 0 disables auto-shutdown.
        shutdown_callback: Async callback invoked on idle expiry.
    """

    def __init__(
        self,
        timeout_seconds: float,
        shutdown_callback: Callable[[], Awaitable[None]],
    ) -> None:
        """Initialize idle shutdown manager.

        Args:
            timeout_seconds: Idle timeout in seconds. 0 disables auto-shutdown.
            shutdown_callback: Async callback invoked on idle expiry.
        """
        self._timeout_seconds = timeout_seconds
        self._shutdown_callback = shutdown_callback
        self._connection_count = 0
        self._lock = asyncio.Lock()
        self._idle_handle: asyncio.Task[None] | None = None

    @property
    def timeout_seconds(self) -> float:
        """Return configured idle timeout in seconds."""
        return self._timeout_seconds

    @property
    def connection_count(self) -> int:
        """Return current connection count.

        Note: This is a snapshot and may be stale immediately after access.
        Used for diagnostics and status queries.
        """
        return self._connection_count

    @property
    def is_shutdown_disabled(self) -> bool:
        """Return True if idle shutdown is disabled (timeout == 0)."""
        return self._timeout_seconds <= 0

    async def increment(self) -> Result[None, str]:
        """Increment connection count, canceling any pending idle timer.

        Returns:
            Result[None, str] on success, or error string on failure.

        Examples:
            >>> import asyncio
            >>> async def dummy_shutdown(): pass
            >>> mgr = IdleShutdownManager(30.0, dummy_shutdown)
            >>> result = asyncio.run(mgr.increment())
            >>> result.is_ok
            True
            >>> mgr.connection_count
            1
        """
        async with self._lock:
            self._connection_count += 1
            await self._cancel_idle_timer()

        return Result(value=None)

    async def decrement(self) -> Result[None, str]:
        """Decrement connection count, starting idle timer when reaching 0.

        Returns:
            Result[None, str] on success, or error string on failure.

        Raises:
            RuntimeError: If connection count would go negative (bug).

        Examples:
            >>> import asyncio
            >>> async def dummy_shutdown(): pass
            >>> mgr = IdleShutdownManager(30.0, dummy_shutdown)
            >>> _ = asyncio.run(mgr.increment())
            >>> result = asyncio.run(mgr.decrement())
            >>> result.is_ok
            True
            >>> mgr.connection_count
            0
        """
        async with self._lock:
            if self._connection_count <= 0:
                # This is a bug: more disconnects than connects
                return Result(
                    error="CONNECTION_COUNT_UNDERFLOW: disconnect called without matching connect"
                )

            self._connection_count -= 1

            if self._connection_count == 0 and not self.is_shutdown_disabled:
                await self._start_idle_timer()

        return Result(value=None)

    async def reset(self) -> Result[None, str]:
        """Reset manager state: cancel timer and reset connection count.

        Called during gateway shutdown to clear state.

        Returns:
            Result[None, str] on success.

        Examples:
            >>> import asyncio
            >>> async def dummy_shutdown(): pass
            >>> mgr = IdleShutdownManager(30.0, dummy_shutdown)
            >>> _ = asyncio.run(mgr.increment())
            >>> result = asyncio.run(mgr.reset())
            >>> result.is_ok
            True
            >>> mgr.connection_count
            0
        """
        async with self._lock:
            await self._cancel_idle_timer()
            self._connection_count = 0

        return Result(value=None)

    async def _cancel_idle_timer(self) -> None:
        """Cancel any pending idle timer. Must be called with lock held."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            try:
                await self._idle_handle
            except asyncio.CancelledError:
                pass
            self._idle_handle = None

    async def _start_idle_timer(self) -> None:
        """Start idle timer. Must be called with lock held.

        Precondition: connection_count == 0 and not is_shutdown_disabled.
        """
        # Create background task for idle expiry
        self._idle_handle = asyncio.create_task(self._idle_timer_expired())

    async def _idle_timer_expired(self) -> None:
        """Callback for idle timer expiry. Triggers shutdown callback."""
        try:
            await asyncio.sleep(self._timeout_seconds)

            # Timer expired without cancellation - trigger shutdown
            await self._shutdown_callback()
        except asyncio.CancelledError:
            # Timer was cancelled (new connection arrived) - do nothing
            pass


# Module-level singleton (lazily initialized)
_manager: IdleShutdownManager | None = None


# @invar:allow shell_result: accessor returns manager instance directly, not a failable I/O boundary.
# @shell_orchestration: module-level singleton accessor for idle shutdown runtime state.
def get_idle_manager() -> IdleShutdownManager | None:
    """Return the module-level idle shutdown manager, or None if not initialized.

    Examples:
        >>> get_idle_manager() is None
        True
    """
    return _manager


async def init_idle_manager(
    timeout_seconds: float,
    shutdown_callback: Callable[[], Awaitable[None]],
) -> Result[IdleShutdownManager, str]:
    """Initialize the module-level idle shutdown manager.

    Must be called exactly once during gateway startup.
    Subsequent calls will fail with an error.

    Args:
        timeout_seconds: Idle timeout in seconds. 0 disables auto-shutdown.
        shutdown_callback: Async callback invoked on idle expiry.

    Returns:
        Result[IdleShutdownManager, str] with manager on success.

    Examples:
        >>> import asyncio
        >>> async def dummy_shutdown(): pass
        >>> result = asyncio.run(init_idle_manager(30.0, dummy_shutdown))
        >>> result.is_ok
        True
        >>> result.value.timeout_seconds
        30.0
        >>> # Reset global state for test isolation
        >>> _ = _reset_idle_manager()
    """
    global _manager

    if _manager is not None:
        return Result(
            error="IDLE_MANAGER_ALREADY_INITIALIZED: init_idle_manager called multiple times"
        )

    _manager = IdleShutdownManager(
        timeout_seconds=timeout_seconds,
        shutdown_callback=shutdown_callback,
    )

    return Result(value=_manager)


async def shutdown_idle_manager() -> Result[None, str]:
    """Shutdown and clear the module-level idle shutdown manager.

    Called during gateway shutdown to clean up state.

    Returns:
        Result[None, str] on success.

    Examples:
        >>> import asyncio
        >>> async def dummy_shutdown(): pass
        >>> _ = _reset_idle_manager()
        >>> _ = asyncio.run(init_idle_manager(30.0, dummy_shutdown))
        >>> result = asyncio.run(shutdown_idle_manager())
        >>> result.is_ok
        True
        >>> get_idle_manager() is None
        True
    """
    global _manager

    if _manager is not None:
        await _manager.reset()
        _manager = None

    return Result(value=None)


def _reset_idle_manager() -> Result[None, str]:
    """Reset module-level manager (for test isolation only).

    This is NOT part of the public API.
    """
    global _manager
    _manager = None
    return Result(value=None)
