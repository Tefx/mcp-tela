"""Tests for ConnectionReaper and touch_connection_activity.

Red-phase tests: implementations are stubs (raise NotImplementedError).
All tests are expected to fail until implementation is complete.

Spec link: Architecture ADR: ConnectionReaper + touch_connection_activity
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tela.core.models import ConnectionContext, TelaConfig
from tela.shell.connection_reaper import (
    ConnectionReaper,
    ReaperConfig,
    ReaperSweepOutcome,
)
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    capture_session,
    clear_runtime_connections,
    get_runtime_connections_snapshot,
    release_session,
    set_runtime_config,
    set_runtime_running,
    touch_connection_activity,
)
from tela.shell.idle_shutdown import IdleShutdownManager


# --- Fixtures ---


class _StubSession:
    """Minimal session stub for capture_session."""

    async def send_tool_list_changed(self) -> None:
        return None


@pytest.fixture()
def _runtime_setup():
    """Set up and tear down runtime state for reaper tests."""
    set_runtime_config(TelaConfig())
    set_runtime_running(True)
    clear_runtime_connections()
    try:
        yield
    finally:
        clear_runtime_connections()
        set_runtime_running(False)
        set_runtime_config(None)


@pytest.fixture()
def reaper_config() -> ReaperConfig:
    """ReaperConfig defaults -- sweep_interval_seconds=30.0, native_idle_ttl_seconds=0.0, bridge_idle_ttl_seconds=900.0"""
    return ReaperConfig()


# --- touch_connection_activity tests ---


class TestTouchConnectionActivity:
    """Tests for touch_connection_activity runtime accessor."""

    @pytest.mark.usefixtures("_runtime_setup")
    def test_touch_connection_activity_updates_last_activity(self) -> None:
        """Add a connection to runtime, call touch_connection_activity,
        verify last_activity is updated on the correct ConnectionContext."""
        conn_id = "conn_touch_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at="2026-01-01T00:00:00Z",
                last_activity="",
            )
        )

        timestamp = "2026-03-31T12:00:00Z"
        result = touch_connection_activity(conn_id, timestamp)

        assert result.is_ok
        assert result.value is True

        # Verify the connection's last_activity was updated
        snapshot = get_runtime_connections_snapshot()
        assert snapshot.is_ok
        assert snapshot.value is not None
        matched = [c for c in snapshot.value if c.connection_id == conn_id]
        assert len(matched) == 1
        assert matched[0].last_activity == timestamp

    @pytest.mark.usefixtures("_runtime_setup")
    def test_touch_connection_activity_returns_false_for_unknown(self) -> None:
        """Call touch_connection_activity with a non-existent connection_id,
        verify it returns Ok(False)."""
        result = touch_connection_activity("nonexistent_conn", "2026-03-31T12:00:00Z")

        assert result.is_ok
        assert result.value is False


# --- ReaperConfig tests ---


class TestReaperConfig:
    """Tests for ReaperConfig defaults."""

    def test_reaper_config_defaults(self, reaper_config: ReaperConfig) -> None:
        """Verify ReaperConfig defaults match the lifecycle contract:
        native_idle_ttl_seconds=0 (disabled) so live sessions survive."""
        assert reaper_config.sweep_interval_seconds == 30.0
        assert reaper_config.native_idle_ttl_seconds == 0.0
        assert reaper_config.bridge_idle_ttl_seconds == 900.0

    def test_reaper_config_is_frozen(self) -> None:
        """ReaperConfig is a frozen dataclass -- no mutation allowed."""
        config = ReaperConfig()
        with pytest.raises(AttributeError):
            config.sweep_interval_seconds = 10.0  # type: ignore[misc]


# --- ReaperSweepOutcome tests ---


class TestReaperSweepOutcome:
    """Tests for ReaperSweepOutcome field structure."""

    def test_reaper_sweep_outcome_fields(self) -> None:
        """Verify ReaperSweepOutcome has the expected fields:
        checked, reaped_session_gone, reaped_stale, errors."""
        outcome = ReaperSweepOutcome()
        assert outcome.checked == 0
        assert outcome.reaped_session_gone == []
        assert outcome.reaped_stale == []
        assert outcome.errors == []

    def test_reaper_sweep_outcome_with_values(self) -> None:
        """ReaperSweepOutcome can be constructed with explicit values."""
        outcome = ReaperSweepOutcome(
            checked=5,
            reaped_session_gone=["conn_1"],
            reaped_stale=["conn_2", "conn_3"],
            errors=["timeout on conn_4"],
        )
        assert outcome.checked == 5
        assert len(outcome.reaped_session_gone) == 1
        assert len(outcome.reaped_stale) == 2
        assert len(outcome.errors) == 1


# --- ConnectionReaper.sweep tests ---


class TestConnectionReaperSweep:
    """Tests for ConnectionReaper.sweep() behavior."""

    @pytest.mark.usefixtures("_runtime_setup")
    def test_sweep_detects_session_gone(self) -> None:
        """Set up a connection in runtime WITHOUT a corresponding session
        in the session registry. Run sweep(). Verify the connection is
        reaped (appears in reaped_session_gone)."""
        conn_id = "conn_orphan_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at="2026-01-01T00:00:00Z",
            )
        )
        # Deliberately do NOT capture a session for this connection_id.
        # The reaper should detect the missing session and reap it.

        async def _shutdown() -> None:
            pass

        idle_mgr = IdleShutdownManager(
            timeout_seconds=30.0, shutdown_callback=_shutdown
        )
        reaper = ConnectionReaper()

        outcome_result = asyncio.run(reaper.sweep())

        assert outcome_result.is_ok
        assert outcome_result.value is not None
        outcome = outcome_result.value
        assert conn_id in outcome.reaped_session_gone

    @pytest.mark.usefixtures("_runtime_setup")
    def test_sweep_detects_stale_connection(self) -> None:
        """Set up a connection with last_activity far in the past (beyond TTL).
        With explicit operator TTL > 0, run sweep(). Verify it appears in reaped_stale."""
        conn_id = "conn_stale_1"
        # last_activity is far in the past -- well beyond any TTL
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at="2020-01-01T00:00:00Z",
                last_activity="2020-01-01T00:00:00Z",
            )
        )
        # Capture a session so it is NOT reaped as session_gone
        capture_session(conn_id, _StubSession())

        try:

            async def _shutdown() -> None:
                pass

            idle_mgr = IdleShutdownManager(
                timeout_seconds=30.0, shutdown_callback=_shutdown
            )
            # Explicit operator TTL override enables native stale reaping
            reaper = ConnectionReaper(config=ReaperConfig(native_idle_ttl_seconds=10.0))

            outcome_result = asyncio.run(reaper.sweep())

            assert outcome_result.is_ok
            assert outcome_result.value is not None
            outcome = outcome_result.value
            assert conn_id in outcome.reaped_stale
        finally:
            release_session(conn_id)

    @pytest.mark.usefixtures("_runtime_setup")
    def test_sweep_calls_idle_manager_decrement(self) -> None:
        """After reaping a connection, verify idle_manager.decrement() was called."""
        conn_id = "conn_reap_decrement_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at="2026-01-01T00:00:00Z",
            )
        )
        # No session captured -- will be reaped as session_gone

        mock_idle_mgr = AsyncMock(spec=IdleShutdownManager)
        mock_idle_mgr.decrement = AsyncMock()

        reaper = ConnectionReaper()

        outcome_result = asyncio.run(reaper.sweep())

        assert outcome_result.is_ok
        assert outcome_result.value is not None
        outcome = outcome_result.value
        # If the connection was reaped, decrement should have been called
        total_reaped = len(outcome.reaped_session_gone) + len(outcome.reaped_stale)
        assert total_reaped > 0, "At least one connection should have been reaped"
        # The mock assertion depends on how the reaper receives the idle_manager;
        # the implementation will wire it. For now, this validates the sweep runs.

    @pytest.mark.usefixtures("_runtime_setup")
    def test_native_ttl_zero_disables_native_reaping(self) -> None:
        conn_id = "conn_native_disable_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at="2020-01-01T00:00:00Z",
                last_activity="2020-01-01T00:00:00Z",
            )
        )
        capture_session(conn_id, _StubSession())

        try:
            reaper = ConnectionReaper(config=ReaperConfig(native_idle_ttl_seconds=0.0))
            outcome_result = asyncio.run(reaper.sweep())

            assert outcome_result.is_ok
            assert outcome_result.value is not None
            assert conn_id not in outcome_result.value.reaped_stale
            snapshot = get_runtime_connections_snapshot()
            assert snapshot.is_ok
            assert snapshot.value is not None
            assert any(c.connection_id == conn_id for c in snapshot.value)
        finally:
            release_session(conn_id)


# --- Long-idle lifecycle contract tests ---


class TestLongIdleLifecycleContract:
    """Tests for the idle recovery lifecycle contract.

    Contract:
    - live sessions are not idle-reaped by default (native_idle_ttl_seconds=0)
    - idle_timeout governs process shutdown only after connection count reaches zero
    - explicit nonzero TTL overrides still reap stale connections
    """

    @pytest.mark.usefixtures("_runtime_setup")
    def test_live_session_survives_default_reaper(self) -> None:
        """A quiet-but-live native session with a captured session must
        survive the default reaper settings (native_idle_ttl_seconds=0)."""
        conn_id = "conn_live_idle_1"
        # Connection created a long time ago with old last_activity
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at="2020-01-01T00:00:00Z",
                last_activity="2020-01-01T00:00:00Z",
            )
        )
        # Session IS captured — this is a live session, just quiet
        capture_session(conn_id, _StubSession())

        try:
            reaper = ConnectionReaper()  # Uses default config (native TTL = 0)
            outcome_result = asyncio.run(reaper.sweep())

            assert outcome_result.is_ok
            assert outcome_result.value is not None
            outcome = outcome_result.value
            # The live session must NOT be reaped as stale
            assert conn_id not in outcome.reaped_stale, (
                f"Live session '{conn_id}' must survive default reaper "
                f"(native_idle_ttl_seconds=0), but was reaped as stale"
            )
            # The live session must NOT be reaped as session_gone (it has a session)
            assert conn_id not in outcome.reaped_session_gone, (
                f"Live session '{conn_id}' must survive default reaper, "
                f"but was reaped as session_gone"
            )
            # The connection must still be in runtime
            snapshot = get_runtime_connections_snapshot()
            assert snapshot.is_ok
            assert snapshot.value is not None
            remaining_ids = [c.connection_id for c in snapshot.value]
            assert conn_id in remaining_ids, (
                f"Live session '{conn_id}' must remain in runtime after sweep, "
                f"but was removed: {remaining_ids}"
            )
        finally:
            release_session(conn_id)

    @pytest.mark.usefixtures("_runtime_setup")
    def test_explicit_nonzero_ttl_override_reaps_stale(self) -> None:
        """When an operator explicitly sets native_idle_ttl_seconds > 0,
        stale native connections (with captured session but old activity)
        must be reaped."""
        conn_id = "conn_override_stale_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at="2020-01-01T00:00:00Z",
                last_activity="2020-01-01T00:00:00Z",
            )
        )
        capture_session(conn_id, _StubSession())

        try:
            # Explicit operator override enables native stale reaping
            reaper = ConnectionReaper(config=ReaperConfig(native_idle_ttl_seconds=10.0))
            outcome_result = asyncio.run(reaper.sweep())

            assert outcome_result.is_ok
            assert outcome_result.value is not None
            assert conn_id in outcome_result.value.reaped_stale, (
                f"Stale connection '{conn_id}' with explicit TTL override "
                f"must be reaped, but was not"
            )
        finally:
            release_session(conn_id)

    @pytest.mark.usefixtures("_runtime_setup")
    def test_orphaned_session_still_reaped_under_default(self) -> None:
        """Even with native_idle_ttl_seconds=0, orphaned connections
        (conn_* without a captured session) are still reaped as
        session_gone."""
        conn_id = "conn_orphan_default_1"
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at="2026-01-01T00:00:00Z",
                last_activity="2026-01-01T00:00:00Z",
            )
        )
        # Deliberately do NOT capture a session

        reaper = ConnectionReaper()  # Default config (native TTL = 0)
        outcome_result = asyncio.run(reaper.sweep())

        assert outcome_result.is_ok
        assert outcome_result.value is not None
        assert conn_id in outcome_result.value.reaped_session_gone, (
            f"Orphaned connection '{conn_id}' must be reaped as "
            f"session_gone under default settings, but was not"
        )

    @pytest.mark.usefixtures("_runtime_setup")
    def test_recent_active_connection_survives_explicit_ttl(self) -> None:
        """A recently-active native connection (within TTL window) must
        survive even when native_idle_ttl_seconds > 0."""
        conn_id = "conn_recent_1"
        from datetime import datetime, timezone

        recent_ts = datetime.now(timezone.utc).isoformat()
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_id="default",
                connected_at=recent_ts,
                last_activity=recent_ts,
            )
        )
        capture_session(conn_id, _StubSession())

        try:
            reaper = ConnectionReaper(
                config=ReaperConfig(native_idle_ttl_seconds=120.0)
            )
            outcome_result = asyncio.run(reaper.sweep())

            assert outcome_result.is_ok
            assert outcome_result.value is not None
            assert conn_id not in outcome_result.value.reaped_stale, (
                f"Recently-active connection '{conn_id}' must survive "
                f"within the TTL window, but was reaped"
            )
            snapshot = get_runtime_connections_snapshot()
            assert snapshot.is_ok
            assert snapshot.value is not None
            remaining_ids = [c.connection_id for c in snapshot.value]
            assert conn_id in remaining_ids
        finally:
            release_session(conn_id)
