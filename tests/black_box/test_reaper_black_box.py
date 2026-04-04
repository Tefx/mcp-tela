"""Black-box verification: ConnectionReaper and connection lifecycle.

Spec source: docs/DESIGN.md (Sweep behavior, Configuration defaults, Lifecycle wiring)
Tester: blind-tester (L3 independence -- no implementation source read)

Expected behavior per spec:
  - ReaperConfig defaults: sweep_interval=30.0, native_idle_ttl=120.0, bridge_idle_ttl=900.0
  - ReaperConfig is frozen (immutable dataclass)
  - ConnectionReaper.start() and stop() are idempotent
  - sweep() on empty runtime returns ReaperSweepOutcome with checked=0
  - touch_connection_activity() returns Ok(False) for nonexistent connection
  - sweep() detects session-gone for conn_* connections without a captured session
  - bridge_idle_ttl_seconds=0 disables bridge idle reaping
  - native_idle_ttl_seconds=0 disables native idle reaping
"""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import ConnectionContext, TelaConfig
from tela.shell.connection_reaper import (
    ConnectionReaper,
    ReaperConfig,
    ReaperSweepOutcome,
)
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,
    get_runtime_connections_snapshot,
    set_runtime_config,
    set_runtime_running,
    touch_connection_activity,
)
from tela.shell.upstream import capture_session, release_session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubSession:
    """Minimal session stub satisfying UpstreamSession protocol."""

    async def send_tool_list_changed(self) -> None:
        return None


@pytest.fixture(autouse=False)
def runtime_env():
    """Set up minimal runtime state for reaper tests, tear down after."""
    set_runtime_config(TelaConfig())
    set_runtime_running(True)
    clear_runtime_connections()
    try:
        yield
    finally:
        clear_runtime_connections()
        set_runtime_running(False)
        set_runtime_config(None)


# ---------------------------------------------------------------------------
# 1. ReaperConfig defaults match spec
# ---------------------------------------------------------------------------


class TestReaperConfigDefaults:
    """Spec: DESIGN.md Configuration defaults table."""

    def test_sweep_interval_default(self) -> None:
        """sweep_interval_seconds defaults to 30.0 per spec."""
        config = ReaperConfig()
        assert config.sweep_interval_seconds == 30.0, (
            f"Expected sweep_interval_seconds=30.0, got {config.sweep_interval_seconds}"
        )

    def test_native_idle_ttl_default(self) -> None:
        """native_idle_ttl_seconds defaults to 120.0 per spec."""
        config = ReaperConfig()
        assert config.native_idle_ttl_seconds == 120.0, (
            f"Expected native_idle_ttl_seconds=120.0, got {config.native_idle_ttl_seconds}"
        )

    def test_bridge_idle_ttl_default(self) -> None:
        """bridge_idle_ttl_seconds defaults to 900.0 per spec."""
        config = ReaperConfig()
        assert config.bridge_idle_ttl_seconds == 900.0, (
            f"Expected bridge_idle_ttl_seconds=900.0, got {config.bridge_idle_ttl_seconds}"
        )


# ---------------------------------------------------------------------------
# 2. ReaperConfig is immutable (frozen)
# ---------------------------------------------------------------------------


class TestReaperConfigImmutable:
    """Spec: ReaperConfig is a frozen dataclass."""

    def test_cannot_mutate_sweep_interval(self) -> None:
        """Assigning to a frozen field must raise an error."""
        config = ReaperConfig()
        with pytest.raises((AttributeError, TypeError)):
            config.sweep_interval_seconds = 99.0  # type: ignore[misc]

    def test_cannot_mutate_native_idle_ttl(self) -> None:
        config = ReaperConfig()
        with pytest.raises((AttributeError, TypeError)):
            config.native_idle_ttl_seconds = 99.0  # type: ignore[misc]

    def test_cannot_mutate_bridge_idle_ttl(self) -> None:
        config = ReaperConfig()
        with pytest.raises((AttributeError, TypeError)):
            config.bridge_idle_ttl_seconds = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. ConnectionReaper start/stop idempotent
# ---------------------------------------------------------------------------


class TestReaperStartStopIdempotent:
    """Spec: Both start() and stop() are idempotent."""

    def test_start_twice_no_error(self) -> None:
        """Calling start() twice must succeed both times (Ok result)."""
        reaper = ConnectionReaper()

        async def _run() -> None:
            r1 = await reaper.start()
            assert r1.is_ok, f"First start failed: {r1}"
            r2 = await reaper.start()
            assert r2.is_ok, f"Second start failed (not idempotent): {r2}"
            await reaper.stop()

        asyncio.run(_run())

    def test_stop_twice_no_error(self) -> None:
        """Calling stop() twice must succeed both times (Ok result)."""
        reaper = ConnectionReaper()

        async def _run() -> None:
            r1 = await reaper.stop()
            assert r1.is_ok, f"First stop failed: {r1}"
            r2 = await reaper.stop()
            assert r2.is_ok, f"Second stop failed (not idempotent): {r2}"

        asyncio.run(_run())

    def test_start_stop_start_stop(self) -> None:
        """Full lifecycle: start -> stop -> start -> stop all succeed."""
        reaper = ConnectionReaper()

        async def _run() -> None:
            assert (await reaper.start()).is_ok
            assert (await reaper.stop()).is_ok
            assert (await reaper.start()).is_ok
            assert (await reaper.stop()).is_ok

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. Sweep on empty runtime
# ---------------------------------------------------------------------------


class TestSweepEmptyRuntime:
    """Spec: sweep() inspects all runtime connections. With none, checked=0."""

    @pytest.mark.usefixtures("runtime_env")
    def test_sweep_empty_returns_zero_checked(self) -> None:
        """Sweep with no connections should report checked=0."""
        reaper = ConnectionReaper()
        result = asyncio.run(reaper.sweep())

        assert result.is_ok, f"sweep() failed: {result}"
        outcome = result.value
        assert outcome.checked == 0, (
            f"Expected checked=0 on empty runtime, got {outcome.checked}"
        )

    @pytest.mark.usefixtures("runtime_env")
    def test_sweep_empty_returns_empty_lists(self) -> None:
        """Sweep with no connections should have empty reaped lists."""
        reaper = ConnectionReaper()
        result = asyncio.run(reaper.sweep())

        assert result.is_ok
        outcome = result.value
        assert outcome.reaped_session_gone == [], (
            f"Expected empty reaped_session_gone, got {outcome.reaped_session_gone}"
        )
        assert outcome.reaped_stale == [], (
            f"Expected empty reaped_stale, got {outcome.reaped_stale}"
        )
        assert outcome.errors == [], f"Expected empty errors, got {outcome.errors}"


# ---------------------------------------------------------------------------
# 5. touch_connection_activity on nonexistent connection
# ---------------------------------------------------------------------------


class TestTouchActivityNonexistent:
    """Spec: touch_connection_activity returns Ok(False) for unknown ID."""

    @pytest.mark.usefixtures("runtime_env")
    def test_returns_ok_false(self) -> None:
        """Touching a nonexistent connection must return Ok(False)."""
        result = touch_connection_activity(
            "nonexistent_conn_xyz", "2026-03-31T12:00:00Z"
        )
        assert result.is_ok, (
            f"Expected Ok result for nonexistent connection, got error: {result}"
        )
        assert result.value is False, (
            f"Expected value=False for nonexistent connection, got {result.value!r}"
        )


# ---------------------------------------------------------------------------
# 6. Sweep detects session-gone
# ---------------------------------------------------------------------------


class TestSweepSessionGone:
    """Spec: Session probe (conn_* connections only) -- if session is gone,
    the connection is reaped immediately."""

    @pytest.mark.usefixtures("runtime_env")
    def test_conn_without_session_is_reaped(self) -> None:
        """Register a conn_* connection WITHOUT a captured session.
        Sweep should detect the missing session and reap it."""
        conn_id = "conn_bb_orphan_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_name="default",
                connected_at="2026-01-01T00:00:00Z",
            )
        )
        # Deliberately do NOT capture a session for this connection.

        reaper = ConnectionReaper()
        result = asyncio.run(reaper.sweep())

        assert result.is_ok, f"sweep() failed: {result}"
        outcome = result.value
        assert conn_id in outcome.reaped_session_gone, (
            f"Expected '{conn_id}' in reaped_session_gone, "
            f"got {outcome.reaped_session_gone}. "
            f"Full outcome: checked={outcome.checked}, "
            f"reaped_stale={outcome.reaped_stale}, "
            f"errors={outcome.errors}"
        )

        # Verify the connection was actually removed from runtime
        snap = get_runtime_connections_snapshot()
        assert snap.is_ok
        remaining_ids = [c.connection_id for c in snap.value]
        assert conn_id not in remaining_ids, (
            f"Connection '{conn_id}' should have been removed from runtime "
            f"after being reaped, but is still present: {remaining_ids}"
        )


# ---------------------------------------------------------------------------
# 7. Sweep respects bridge TTL disable (bridge_idle_ttl_seconds=0)
# ---------------------------------------------------------------------------


class TestSweepBridgeTTLDisable:
    """Spec: bridge_idle_ttl_seconds=0 disables bridge reaping."""

    @pytest.mark.usefixtures("runtime_env")
    def test_stale_bridge_not_reaped_when_ttl_zero(self) -> None:
        """Create a bridge connection with very old last_activity.
        Set bridge_idle_ttl_seconds=0. Sweep should NOT reap it."""
        conn_id = "bridge_bb_stale_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_name="default",
                connected_at="2020-01-01T00:00:00Z",
                last_activity="2020-01-01T00:00:00Z",
            )
        )
        # Capture a session so it is NOT reaped as session-gone.
        # (Session probe is for conn_* only per spec, but capture anyway
        # to be safe and isolate the TTL-disable behavior.)
        capture_session(conn_id, _StubSession())

        try:
            config = ReaperConfig(bridge_idle_ttl_seconds=0)
            reaper = ConnectionReaper(config=config)
            result = asyncio.run(reaper.sweep())

            assert result.is_ok, f"sweep() failed: {result}"
            outcome = result.value
            assert conn_id not in outcome.reaped_stale, (
                f"Bridge connection '{conn_id}' should NOT be reaped when "
                f"bridge_idle_ttl_seconds=0, but appeared in reaped_stale: "
                f"{outcome.reaped_stale}"
            )
            assert conn_id not in outcome.reaped_session_gone, (
                f"Bridge connection '{conn_id}' should NOT appear in "
                f"reaped_session_gone: {outcome.reaped_session_gone}"
            )

            # Verify the connection is still in runtime
            snap = get_runtime_connections_snapshot()
            assert snap.is_ok
            remaining_ids = [c.connection_id for c in snap.value]
            assert conn_id in remaining_ids, (
                f"Bridge connection '{conn_id}' should still be in runtime "
                f"when bridge TTL is disabled, but was removed: {remaining_ids}"
            )
        finally:
            release_session(conn_id)


class TestSweepNativeTTLDisable:
    """Spec: native_idle_ttl_seconds=0 disables native reaping."""

    @pytest.mark.usefixtures("runtime_env")
    def test_stale_native_not_reaped_when_ttl_zero(self) -> None:
        conn_id = "conn_bb_native_stale_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_name="default",
                connected_at="2020-01-01T00:00:00Z",
                last_activity="2020-01-01T00:00:00Z",
            )
        )
        capture_session(conn_id, _StubSession())

        try:
            config = ReaperConfig(native_idle_ttl_seconds=0)
            reaper = ConnectionReaper(config=config)
            result = asyncio.run(reaper.sweep())

            assert result.is_ok, f"sweep() failed: {result}"
            outcome = result.value
            assert conn_id not in outcome.reaped_stale, (
                f"Native connection '{conn_id}' should NOT be reaped when "
                f"native_idle_ttl_seconds=0, but appeared in reaped_stale: "
                f"{outcome.reaped_stale}"
            )

            snap = get_runtime_connections_snapshot()
            assert snap.is_ok
            remaining_ids = [c.connection_id for c in snap.value]
            assert conn_id in remaining_ids, (
                f"Native connection '{conn_id}' should still be in runtime "
                f"when native TTL is disabled, but was removed: {remaining_ids}"
            )
        finally:
            release_session(conn_id)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
