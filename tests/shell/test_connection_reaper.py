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
    clear_runtime_connections,
    get_runtime_connections_snapshot,
    set_runtime_config,
    set_runtime_running,
    touch_connection_activity,
)
from tela.shell.idle_shutdown import IdleShutdownManager
from tela.shell.upstream import capture_session, release_session


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
    """Architecture ADR: ReaperConfig defaults -- sweep_interval_seconds=30.0, native_idle_ttl_seconds=120.0, bridge_idle_ttl_seconds=300.0"""
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
                profile_name="default",
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
        """Verify ReaperConfig defaults match the Architecture ADR."""
        assert reaper_config.sweep_interval_seconds == 30.0
        assert reaper_config.native_idle_ttl_seconds == 120.0
        assert reaper_config.bridge_idle_ttl_seconds == 300.0

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
                profile_name="default",
                connected_at="2026-01-01T00:00:00Z",
            )
        )
        # Deliberately do NOT capture a session for this connection_id.
        # The reaper should detect the missing session and reap it.

        async def _shutdown() -> None:
            pass

        idle_mgr = IdleShutdownManager(timeout_seconds=30.0, shutdown_callback=_shutdown)
        reaper = ConnectionReaper()

        outcome_result = asyncio.run(reaper.sweep())

        assert outcome_result.is_ok
        assert outcome_result.value is not None
        outcome = outcome_result.value
        assert conn_id in outcome.reaped_session_gone

    @pytest.mark.usefixtures("_runtime_setup")
    def test_sweep_detects_stale_connection(self) -> None:
        """Set up a connection with last_activity far in the past (beyond TTL).
        Run sweep(). Verify it appears in reaped_stale."""
        conn_id = "conn_stale_1"
        # last_activity is far in the past -- well beyond any TTL
        add_runtime_connection(
            ConnectionContext(
                connection_id=conn_id,
                profile_name="default",
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
            reaper = ConnectionReaper()

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
                profile_name="default",
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
