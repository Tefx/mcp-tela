"""Regression tests for disconnect cleanup and bounded session registry growth.

Covers notifications.gate_fix_batch blockers:
1) handle_disconnect releases captured sessions (prevents stale leaks)
2) Session registry stays bounded across repeated connect/disconnect cycles
3) Notification forwarding works correctly after disconnect cleanup
4) gateway_shutdown releases all sessions via per-connection release_session
"""

from __future__ import annotations

import asyncio

from tela.core.models import (
    AuthConfig,
    AuthMode,
    ConnectionContext,
    DisconnectRequest,
    ProfileConfig,
    TelaConfig,
)
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,
    set_runtime_config,
    set_runtime_running,
)
from tela.shell.gateway_runtime import (
    capture_session,
    clear_session_registry,
    get_captured_session,
    get_session_registry_snapshot,
    release_session,
)
from tela.shell.http_routes import handle_disconnect
from tela.shell.upstream import notify_tools_changed


# --- Test fixtures ---


class StubSession:
    """Stub UpstreamSession for testing notifications."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_tool_list_changed(self) -> None:
        self.calls.append("send_tool_list_changed")


def _clear_sessions() -> None:
    clear_session_registry()


def _session_registry_size() -> int:
    return len(get_session_registry_snapshot().value or {})


def _setup_runtime() -> None:
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={"dev": ProfileConfig(name="dev", default=True)},
        )
    )
    set_runtime_running(True)
    clear_runtime_connections()


def _teardown_runtime() -> None:
    set_runtime_config(None)
    set_runtime_running(False)
    clear_runtime_connections()


# --- Category 1: handle_disconnect releases captured sessions ---


def test_handle_disconnect_releases_captured_session() -> None:
    """handle_disconnect must call release_session for the disconnected connection."""
    _clear_sessions()
    _setup_runtime()

    try:
        conn_id = "test-disc-release-1"
        ctx = ConnectionContext(
            connection_id=conn_id,
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        add_runtime_connection(ctx)

        session = StubSession()
        capture_session(conn_id, session)

        # Session exists before disconnect
        assert get_captured_session(conn_id).is_ok

        # Disconnect
        result = handle_disconnect(
            "tok", "tok", DisconnectRequest(connection_id=conn_id)
        )
        assert result.is_ok

        # Session must be released
        assert get_captured_session(conn_id).is_err
    finally:
        _teardown_runtime()


def test_handle_disconnect_without_captured_session_succeeds() -> None:
    """handle_disconnect succeeds even when no session was captured for the connection."""
    _clear_sessions()
    _setup_runtime()

    try:
        conn_id = "test-disc-no-session"
        ctx = ConnectionContext(
            connection_id=conn_id,
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        add_runtime_connection(ctx)

        # No session captured for this connection
        result = handle_disconnect(
            "tok", "tok", DisconnectRequest(connection_id=conn_id)
        )
        assert result.is_ok
    finally:
        _teardown_runtime()


# --- Category 2: Bounded session registry growth ---


def test_session_registry_bounded_across_connect_disconnect_cycles() -> None:
    """Repeated connect/disconnect cycles must not grow the session registry unboundedly."""
    _clear_sessions()
    _setup_runtime()

    try:
        cycle_count = 20

        for i in range(cycle_count):
            conn_id = f"cycle-conn-{i}"

            # Simulate connect: add to runtime + capture session
            ctx = ConnectionContext(
                connection_id=conn_id,
                profile_id="dev",
                connected_at="2026-01-01T00:00:00Z",
            )
            add_runtime_connection(ctx)
            capture_session(conn_id, StubSession())

            # Each cycle adds exactly one session (previous was already released)
            assert _session_registry_size() == 1

            # Simulate disconnect via handle_disconnect
            result = handle_disconnect(
                "tok", "tok", DisconnectRequest(connection_id=conn_id)
            )
            assert result.is_ok

        # After all cycles, registry must be empty (all sessions released)
        assert _session_registry_size() == 0
    finally:
        _teardown_runtime()


def test_concurrent_connections_bounded_after_disconnect() -> None:
    """Multiple concurrent connections followed by disconnects stay bounded."""
    _clear_sessions()
    _setup_runtime()

    try:
        conn_count = 10

        # Connect all
        for i in range(conn_count):
            conn_id = f"concurrent-{i}"
            ctx = ConnectionContext(
                connection_id=conn_id,
                profile_id="dev",
                connected_at="2026-01-01T00:00:00Z",
            )
            add_runtime_connection(ctx)
            capture_session(conn_id, StubSession())

        assert _session_registry_size() == conn_count

        # Disconnect all
        for i in range(conn_count):
            conn_id = f"concurrent-{i}"
            result = handle_disconnect(
                "tok", "tok", DisconnectRequest(connection_id=conn_id)
            )
            assert result.is_ok

        assert _session_registry_size() == 0
    finally:
        _teardown_runtime()


# --- Category 3: Notification forwarding after disconnect cleanup ---


def test_notification_skips_disconnected_session() -> None:
    """After disconnect, notify_tools_changed for that connection gracefully skips."""
    _clear_sessions()
    _setup_runtime()

    try:
        conn_id = "notif-after-disc"
        ctx = ConnectionContext(
            connection_id=conn_id,
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        add_runtime_connection(ctx)

        session = StubSession()
        capture_session(conn_id, session)

        # Disconnect
        handle_disconnect("tok", "tok", DisconnectRequest(connection_id=conn_id))

        # Attempt notification — should skip gracefully (no session found)
        result = asyncio.run(notify_tools_changed(ctx, "sha256:test"))
        assert result.is_ok
        assert len(session.calls) == 0
    finally:
        _teardown_runtime()


def test_notification_reaches_live_sessions_after_peer_disconnect() -> None:
    """Disconnecting one connection must not affect notifications to other live connections."""
    _clear_sessions()
    _setup_runtime()

    try:
        # Connect two sessions
        live_session = StubSession()
        dead_session = StubSession()

        for conn_id, session in [
            ("live-conn", live_session),
            ("dead-conn", dead_session),
        ]:
            ctx = ConnectionContext(
                connection_id=conn_id,
                profile_id="dev",
                connected_at="2026-01-01T00:00:00Z",
            )
            add_runtime_connection(ctx)
            capture_session(conn_id, session)

        # Disconnect dead-conn
        handle_disconnect("tok", "tok", DisconnectRequest(connection_id="dead-conn"))

        # Notify live-conn — should succeed
        live_ctx = ConnectionContext(
            connection_id="live-conn",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        result = asyncio.run(notify_tools_changed(live_ctx, "sha256:test"))
        assert result.is_ok
        assert len(live_session.calls) == 1

        # Notify dead-conn — should skip
        dead_ctx = ConnectionContext(
            connection_id="dead-conn",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        result = asyncio.run(notify_tools_changed(dead_ctx, "sha256:test"))
        assert result.is_ok
        assert len(dead_session.calls) == 0
    finally:
        release_session("live-conn")
        _teardown_runtime()


# --- Category 4: gateway_shutdown releases sessions via per-connection path ---


def test_gateway_shutdown_releases_all_sessions() -> None:
    """gateway_shutdown must release all captured sessions."""
    from tela.shell.gateway import gateway_shutdown

    _clear_sessions()
    _setup_runtime()

    try:
        for i in range(5):
            conn_id = f"shutdown-conn-{i}"
            ctx = ConnectionContext(
                connection_id=conn_id,
                profile_id="dev",
                connected_at="2026-01-01T00:00:00Z",
            )
            add_runtime_connection(ctx)
            capture_session(conn_id, StubSession())

        assert _session_registry_size() == 5

        asyncio.run(gateway_shutdown())

        assert _session_registry_size() == 0
    finally:
        # gateway_shutdown already clears runtime, but ensure clean state
        _teardown_runtime()
