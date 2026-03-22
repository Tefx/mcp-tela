"""Tests for idle shutdown manager.

Tests:
- Connection tracking increments/decrements correctly
- Idle timer behavior correct
- Disabled mode works (timeout_seconds=0)
- Concurrency safe (asyncio.Lock protection)
"""

from __future__ import annotations

import asyncio

from tela.shell.idle_shutdown import (
    IdleShutdownManager,
    _reset_idle_manager,
    get_idle_manager,
    init_idle_manager,
    shutdown_idle_manager,
)


class TestIdleShutdownManager:
    """Tests for IdleShutdownManager class."""

    def setup_method(self) -> None:
        """Reset module state before each test."""
        _reset_idle_manager()

    def teardown_method(self) -> None:
        """Reset module state after each test."""
        _reset_idle_manager()

    def test_connection_count_starts_at_zero(self) -> None:
        """Manager starts with connection_count == 0."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(30.0, shutdown_callback)
        assert manager.connection_count == 0

    def test_increment_increases_count(self) -> None:
        """increment() increases connection count."""

        async def shutdown_callback() -> None:
            pass

        manager = IdleShutdownManager(30.0, shutdown_callback)

        result = asyncio.run(manager.increment())
        assert result.is_ok
        assert manager.connection_count == 1

        result = asyncio.run(manager.increment())
        assert result.is_ok
        assert manager.connection_count == 2

    def test_decrement_decreases_count(self) -> None:
        """decrement() decreases connection count."""

        async def shutdown_callback() -> None:
            pass

        manager = IdleShutdownManager(30.0, shutdown_callback)

        asyncio.run(manager.increment())
        asyncio.run(manager.increment())
        assert manager.connection_count == 2

        result = asyncio.run(manager.decrement())
        assert result.is_ok
        assert manager.connection_count == 1

        result = asyncio.run(manager.decrement())
        assert result.is_ok
        assert manager.connection_count == 0

    def test_decrement_fails_when_count_is_zero(self) -> None:
        """decrement() returns error when count would go negative."""

        async def shutdown_callback() -> None:
            pass

        manager = IdleShutdownManager(30.0, shutdown_callback)

        # No connections yet
        assert manager.connection_count == 0

        result = asyncio.run(manager.decrement())
        assert result.is_err
        assert result.error is not None
        assert "CONNECTION_COUNT_UNDERFLOW" in result.error

    def test_disabled_mode_timeout_zero(self) -> None:
        """timeout_seconds=0 disables auto-shutdown."""

        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(0.0, shutdown_callback)
        assert manager.is_shutdown_disabled

        # Increment and decrement - no timer should start
        asyncio.run(manager.increment())
        asyncio.run(manager.decrement())
        assert manager.connection_count == 0

        # Wait a bit to ensure no shutdown callback
        asyncio.run(asyncio.sleep(0.1))

        assert not shutdown_called

    def test_idle_timer_starts_when_count_reaches_zero(self) -> None:
        """Timer starts when connection count reaches 0."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(0.05, shutdown_callback)  # 50ms timeout

        async def run_test() -> None:
            await manager.increment()
            assert manager.connection_count == 1

            # Decrement to zero - timer should start
            await manager.decrement()
            assert manager.connection_count == 0

            # Wait for timer to expire (within same event loop)
            await asyncio.sleep(0.15)

        asyncio.run(run_test())
        assert shutdown_called

    def test_new_connection_resets_idle_timer(self) -> None:
        """New connection cancels pending idle timer."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(0.1, shutdown_callback)  # 100ms timeout

        async def run_test() -> None:
            await manager.increment()
            await manager.decrement()
            assert manager.connection_count == 0

            # Timer started - wait 50ms (half the timeout)
            await asyncio.sleep(0.05)

            # New connection comes in before timer expires
            await manager.increment()
            assert manager.connection_count == 1

            # Wait longer than original timeout
            await asyncio.sleep(0.15)

        asyncio.run(run_test())

        # Shutdown should NOT have been called because timer was canceled
        assert not shutdown_called

    def test_concurrent_increments(self) -> None:
        """Concurrent increments are handled correctly with asyncio.Lock."""

        async def shutdown_callback() -> None:
            pass

        manager = IdleShutdownManager(30.0, shutdown_callback)

        concurrent_count = 100

        async def do_increment() -> None:
            await manager.increment()

        async def run_concurrent() -> None:
            await asyncio.gather(*[do_increment() for _ in range(concurrent_count)])

        asyncio.run(run_concurrent())

        assert manager.connection_count == concurrent_count

    def test_concurrent_increments_and_decrements(self) -> None:
        """Concurrent increments and decrements are handled correctly."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(30.0, shutdown_callback)

        # Start with 50 connections
        for _ in range(50):
            asyncio.run(manager.increment())

        # Concurrently add 50 and remove 25
        async def do_increment() -> None:
            await manager.increment()

        async def do_decrement() -> None:
            await manager.decrement()

        async def run_concurrent() -> None:
            tasks = [do_increment() for _ in range(50)] + [
                do_decrement() for _ in range(25)
            ]
            await asyncio.gather(*tasks)

        asyncio.run(run_concurrent())

        assert manager.connection_count == 75

    def test_reset_clears_state(self) -> None:
        """reset() clears connection count and cancels timer."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(30.0, shutdown_callback)

        asyncio.run(manager.increment())
        asyncio.run(manager.increment())
        assert manager.connection_count == 2

        result = asyncio.run(manager.reset())
        assert result.is_ok
        assert manager.connection_count == 0

    def test_timer_cancellation_on_reset(self) -> None:
        """reset() cancels any pending idle timer."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(0.05, shutdown_callback)

        asyncio.run(manager.increment())
        asyncio.run(manager.decrement())
        assert manager.connection_count == 0

        # Reset before timer expires
        asyncio.run(manager.reset())

        # Wait longer than timeout
        asyncio.run(asyncio.sleep(0.15))

        # Shutdown should NOT have been called
        assert not shutdown_called


class TestModuleLevelFunctions:
    """Tests for module-level init/shutdown functions."""

    def setup_method(self) -> None:
        """Reset module state before each test."""
        _reset_idle_manager()

    def teardown_method(self) -> None:
        """Reset module state after each test."""
        _reset_idle_manager()

    def test_get_idle_manager_returns_none_when_not_initialized(self) -> None:
        """get_idle_manager() returns None when not initialized."""
        assert get_idle_manager() is None

    def test_init_idle_manager_creates_manager(self) -> None:
        """init_idle_manager() creates module-level manager."""

        async def shutdown_callback() -> None:
            pass

        result = asyncio.run(init_idle_manager(30.0, shutdown_callback))
        assert result.is_ok
        assert result.value is not None
        assert result.value.timeout_seconds == 30.0

        # get_idle_manager returns same instance
        assert get_idle_manager() is result.value

    def test_init_idle_manager_fails_on_duplicate_call(self) -> None:
        """init_idle_manager() fails if called twice."""

        async def shutdown_callback() -> None:
            pass

        result1 = asyncio.run(init_idle_manager(30.0, shutdown_callback))
        assert result1.is_ok

        result2 = asyncio.run(init_idle_manager(60.0, shutdown_callback))
        assert result2.is_err
        assert result2.error is not None
        assert "IDLE_MANAGER_ALREADY_INITIALIZED" in result2.error

    def test_shutdown_idle_manager_clears_state(self) -> None:
        """shutdown_idle_manager() clears module-level manager."""

        async def shutdown_callback() -> None:
            pass

        asyncio.run(init_idle_manager(30.0, shutdown_callback))
        assert get_idle_manager() is not None

        result = asyncio.run(shutdown_idle_manager())
        assert result.is_ok
        assert get_idle_manager() is None

    def test_shutdown_idle_manager_safe_when_not_initialized(self) -> None:
        """shutdown_idle_manager() is safe when manager not initialized."""
        result = asyncio.run(shutdown_idle_manager())
        assert result.is_ok

    def test_shutdown_idle_manager_resets_connection_count(self) -> None:
        """shutdown_idle_manager() resets manager state."""

        async def shutdown_callback() -> None:
            pass

        result = asyncio.run(init_idle_manager(30.0, shutdown_callback))
        assert result.is_ok
        assert result.value is not None
        manager = result.value

        # Add some connections
        asyncio.run(manager.increment())
        asyncio.run(manager.increment())
        assert manager.connection_count == 2

        # Shutdown
        asyncio.run(shutdown_idle_manager())

        # Manager is cleared
        assert get_idle_manager() is None


class TestDisabledMode:
    """Tests for --idle-timeout 0 (disabled) behavior."""

    def test_timeout_zero_never_calls_shutdown(self) -> None:
        """timeout_seconds=0 never triggers shutdown even with idle."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(0.0, shutdown_callback)

        # Connect and disconnect
        asyncio.run(manager.increment())
        asyncio.run(manager.decrement())

        # Verify no timer started
        assert manager._idle_handle is None

        # Wait a bit - should not trigger shutdown
        asyncio.run(asyncio.sleep(0.1))
        assert not shutdown_called

    def test_negative_timeout_treated_as_disabled(self) -> None:
        """Negative timeout values are treated as disabled."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(-10.0, shutdown_callback)
        assert manager.is_shutdown_disabled

        asyncio.run(manager.increment())
        asyncio.run(manager.decrement())

        asyncio.run(asyncio.sleep(0.05))
        assert not shutdown_called

    def test_zero_timeout_multiple_connections(self) -> None:
        """Multiple connections work correctly with disabled mode."""

        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True
            raise AssertionError("shutdown should not be called")

        manager = IdleShutdownManager(0.0, shutdown_callback)

        # Multiple connections
        for _ in range(10):
            asyncio.run(manager.increment())

        for _ in range(10):
            result = asyncio.run(manager.decrement())
            assert result.is_ok

        assert manager.connection_count == 0
        assert not shutdown_called


class TestConcurrencySafety:
    """Tests for asyncio.Lock concurrency safety."""

    def test_rapid_connect_disconnect_cycle(self) -> None:
        """Rapid connect/disconnect cycles are handled safely."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(0.05, shutdown_callback)

        async def run_cycles() -> None:
            # Rapid connect/disconnect cycles
            for _ in range(100):
                await manager.increment()
                await manager.decrement()

            # Wait for potential stale timer
            await asyncio.sleep(0.15)

        asyncio.run(run_cycles())

        # Manager should be in clean state
        assert manager.connection_count == 0

    def test_concurrent_decrements_from_zero_are_handled(self) -> None:
        """Concurrent decrements from zero don't corrupt state."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(30.0, shutdown_callback)

        # Start with connections
        asyncio.run(manager.increment())
        asyncio.run(manager.increment())

        # Remove one first (count = 1)
        asyncio.run(manager.decrement())

        # Now two concurrent decrement attempts
        # First will get lock, succeed (count = 0), then second will fail
        async def run_concurrent() -> None:
            results = await asyncio.gather(manager.decrement(), manager.decrement())
            # Due to lock, they're sequential:
            # First: count=1 -> 0, success, starts timer
            # Second: count=0, can't go negative, error
            success_count = sum(1 for r in results if r.is_ok)
            error_count = sum(1 for r in results if r.is_err)
            assert success_count == 1
            assert error_count == 1
            assert manager.connection_count == 0

        asyncio.run(run_concurrent())
        assert not shutdown_called  # Timer is 30s so won't fire during test

    def test_timer_task_properly_cancelled_on_new_connection(self) -> None:
        """Timer task is properly cancelled when new connection arrives."""
        shutdown_called = False

        async def shutdown_callback() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        manager = IdleShutdownManager(0.1, shutdown_callback)

        async def run_test() -> None:
            # Connect and disconnect - timer starts
            await manager.increment()
            await manager.decrement()

            # Timer handle should exist
            assert manager._idle_handle is not None

            # New connection cancels timer
            await manager.increment()

            # Timer should now be cancelled
            # The task is cancelled on increment, not immediately cleared
            # Let the previous timer task finish cancellation
            await asyncio.sleep(0.05)

            # Wait out the original timeout
            await asyncio.sleep(0.15)

            # Shutdown was never called
            assert not shutdown_called

        asyncio.run(run_test())
