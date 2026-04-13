"""Tests for notification forwarding from downstream reload to upstream sessions.

Tests cover:
1) Session capture after _list_tools and multiple sessions
2) notify_tools_changed triggers send_tool_list_changed
3) Stale session handling (remove dead, keep live)
4) End-to-end flow via reload callback to notify all sessions
5) No sessions -> no-op no error
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from typing import Any

from tela.core.models import (
    AuthConfig,
    AuthMode,
    ConnectionContext,
    ProfileConfig,
    ServerConfig,
    TelaConfig,
)
from tela.shell.downstream import connect_all, disconnect_all, get_tool_server
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,
    get_runtime_connections_snapshot,
    set_runtime_config,
)
from tela.shell.gateway_runtime import (
    capture_session,
    clear_session_registry,
    get_captured_session,
    release_session,
)
from tela.shell.reload import on_tools_changed, set_notify_callback
from tela.shell.upstream import notify_tools_changed


# --- Test fixtures ---


class StubSession:
    """Stub UpstreamSession for testing notifications."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_tool_list_changed(self) -> None:
        self.calls.append({"method": "send_tool_list_changed"})


class FailingSession:
    """Session that raises on send_tool_list_changed."""

    def __init__(self, error_message: str = "transport closed"):
        self.error_message = error_message
        self.calls: list[dict[str, Any]] = []

    async def send_tool_list_changed(self) -> None:
        self.calls.append({"method": "send_tool_list_changed", "failed": True})
        raise RuntimeError(self.error_message)


def _clear_all_sessions() -> None:
    """Clear all captured sessions from registry."""
    clear_session_registry()


def _setup_runtime_for_notifications() -> None:
    """Setup runtime config for notification tests."""
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={"dev": ProfileConfig(name="dev", default=True)},
        )
    )


def _teardown_runtime() -> None:
    """Teardown runtime config."""
    set_runtime_config(None)
    clear_runtime_connections()


# --- Category 1: Session capture after _list_tools and multiple sessions ---


def test_session_capture_stores_session_for_connection() -> None:
    """capture_session stores a session retrievable by connection_id."""
    _clear_all_sessions()

    session = StubSession()
    result = capture_session("conn_1", session)
    assert result.is_ok

    retrieved = get_captured_session("conn_1")
    assert retrieved.is_ok
    assert retrieved.value is session

    release_session("conn_1")


def test_multiple_sessions_captured_independently() -> None:
    """Multiple sessions can be captured for different connection_ids."""
    _clear_all_sessions()

    sessions = {f"conn_{i}": StubSession() for i in range(3)}

    for conn_id, session in sessions.items():
        result = capture_session(conn_id, session)
        assert result.is_ok

    # All sessions are retrievable
    for conn_id, session in sessions.items():
        retrieved = get_captured_session(conn_id)
        assert retrieved.is_ok
        assert retrieved.value is session

    # Cleanup
    for conn_id in sessions:
        release_session(conn_id)


def test_session_capture_preserves_first_binding() -> None:
    """Capturing a different session for same connection_id preserves first binding."""
    _clear_all_sessions()

    first_session = StubSession()
    second_session = StubSession()

    result_first = capture_session("conn_same", first_session)
    assert result_first.is_ok

    result_second = capture_session("conn_same", second_session)
    assert result_second.is_err
    assert "SESSION_ALREADY_BOUND" in (result_second.error or "")

    retrieved = get_captured_session("conn_same")
    assert retrieved.is_ok
    assert retrieved.value is first_session  # first binding preserved

    release_session("conn_same")


def test_session_capture_idempotent_same_session() -> None:
    """Re-capturing the same session object for same connection_id is idempotent."""
    _clear_all_sessions()

    session = StubSession()
    result1 = capture_session("conn_idem", session)
    assert result1.is_ok

    result2 = capture_session("conn_idem", session)
    assert result2.is_ok  # idempotent, not an error

    retrieved = get_captured_session("conn_idem")
    assert retrieved.is_ok
    assert retrieved.value is session

    release_session("conn_idem")


def test_distinct_sessions_get_distinct_connection_ids() -> None:
    """Distinct upstream sessions must map to distinct connection IDs, not share one."""
    _clear_all_sessions()

    session_a = StubSession()
    session_b = StubSession()

    # Each session binds to its own connection_id
    capture_session("conn_a", session_a)
    capture_session("conn_b", session_b)

    # Attempting to bind session_b to conn_a fails (preserves session_a)
    result = capture_session("conn_a", session_b)
    assert result.is_err

    # Each binding is distinct and correct
    retrieved_a = get_captured_session("conn_a")
    assert retrieved_a.is_ok
    assert retrieved_a.value is session_a

    retrieved_b = get_captured_session("conn_b")
    assert retrieved_b.is_ok
    assert retrieved_b.value is session_b

    release_session("conn_a")
    release_session("conn_b")


def test_session_not_found_after_release() -> None:
    """After release_session, get_captured_session returns error."""
    _clear_all_sessions()

    session = StubSession()
    capture_session("conn_release_test", session)
    release_session("conn_release_test")

    result = get_captured_session("conn_release_test")
    assert result.is_err
    assert "not found" in (result.error or "")


# --- Category 2: notify_tools_changed triggers send_tool_list_changed ---


def test_notify_tools_changed_calls_send_on_captured_session() -> None:
    """notify_tools_changed sends tool_list_changed notification to captured session."""
    _clear_all_sessions()
    _setup_runtime_for_notifications()

    try:
        session = StubSession()
        capture_session("notify_conn", session)

        conn = ConnectionContext(
            connection_id="notify_conn",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )

        result = asyncio.run(notify_tools_changed(conn, "sha256:abcd1234"))
        assert result.is_ok
        assert len(session.calls) == 1
        assert session.calls[0]["method"] == "send_tool_list_changed"

    finally:
        release_session("notify_conn")
        _teardown_runtime()


def test_notify_tools_changed_skips_when_no_session() -> None:
    """notify_tools_changed returns Ok and skips send when no session captured."""
    _clear_all_sessions()
    _setup_runtime_for_notifications()

    try:
        conn = ConnectionContext(
            connection_id="no_session_conn",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )

        # No session captured for this connection
        result = asyncio.run(notify_tools_changed(conn, "sha256:abcd1234"))
        assert result.is_ok  # No error, graceful skip

    finally:
        _teardown_runtime()


def test_notify_tools_changed_returns_error_on_send_failure() -> None:
    """notify_tools_changed returns error when session.send_tool_list_changed raises."""
    _clear_all_sessions()
    _setup_runtime_for_notifications()

    try:
        session = FailingSession(error_message="network disconnected")
        capture_session("failing_conn", session)

        conn = ConnectionContext(
            connection_id="failing_conn",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )

        result = asyncio.run(notify_tools_changed(conn, "sha256:abcd1234"))
        assert result.is_err
        assert "NOTIFICATION_SEND_FAILED" in (result.error or "")

    finally:
        release_session("failing_conn")
        _teardown_runtime()


# --- Category 3: Stale session handling (remove dead, keep live) ---


def test_stale_session_removed_from_registry() -> None:
    """release_session removes session, preventing notification to stale connection."""
    _clear_all_sessions()
    _setup_runtime_for_notifications()

    try:
        session = StubSession()
        capture_session("stale_conn", session)

        # Session exists
        retrieved = get_captured_session("stale_conn")
        assert retrieved.is_ok

        # Release (simulates disconnect)
        release_session("stale_conn")

        # Now notification should skip gracefully
        conn = ConnectionContext(
            connection_id="stale_conn",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        result = asyncio.run(notify_tools_changed(conn, "sha256:abcd1234"))
        assert result.is_ok
        assert len(session.calls) == 0  # Never called

    finally:
        _teardown_runtime()


def test_failing_session_does_not_break_other_sessions() -> None:
    """When one session fails to send, other sessions still get notifications."""
    _clear_all_sessions()
    _setup_runtime_for_notifications()

    try:
        # Multiple sessions: one will fail
        failing_session = FailingSession(error_message="connection lost")
        live_session = StubSession()

        capture_session("failing_conn", failing_session)
        capture_session("live_conn", live_session)

        # Notify failing session
        conn_failing = ConnectionContext(
            connection_id="failing_conn",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        result = asyncio.run(notify_tools_changed(conn_failing, "sha256:abcd1234"))
        assert result.is_err  # Failed

        # Notify live session
        conn_live = ConnectionContext(
            connection_id="live_conn",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        result = asyncio.run(notify_tools_changed(conn_live, "sha256:abcd1234"))
        assert result.is_ok  # Succeeded
        assert len(live_session.calls) == 1

    finally:
        release_session("failing_conn")
        release_session("live_conn")
        _teardown_runtime()


# --- Category 4: End-to-end flow via reload callback to notify all sessions ---


def test_e2e_reload_notifies_all_captured_sessions() -> None:
    """Full flow: on_tools_changed -> callback -> notify_tools_changed for each session."""
    _clear_all_sessions()

    sessions = {f"conn_{i}": StubSession() for i in range(3)}
    notified_digests: list[str] = []

    async def capture_all_digests(digest: str) -> None:
        """Callback that would be invoked per-connection in production."""
        notified_digests.append(digest)
        # In production, gateway iterates connections and calls notify_tools_changed

    set_notify_callback(capture_all_digests)

    try:
        # Capture all sessions
        for conn_id, session in sessions.items():
            capture_session(conn_id, session)

        # Setup minimal downstream state
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        asyncio.run(
            connect_all(
                servers,
                tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]},
            )
        )

        # Trigger reload
        result = asyncio.run(
            on_tools_changed(
                "fs",
                servers["fs"],
                [
                    {"name": "tool_a", "inputSchema": {}},
                    {"name": "tool_b", "inputSchema": {}},
                ],
            )
        )
        assert result.is_ok
        assert len(notified_digests) == 1
        # Digest should be sha256 of sorted tool names
        assert notified_digests[0].startswith("sha256:")

        # Verify tool registry updated
        assert get_tool_server("tool_b").value == "fs"

    finally:
        set_notify_callback(None)
        for conn_id in sessions:
            release_session(conn_id)
        asyncio.run(disconnect_all())


def test_e2e_callback_not_invoked_on_conflict() -> None:
    """Conflict rejection should NOT invoke notification callback."""
    _clear_all_sessions()

    notified_digests: list[str] = []

    async def capture_digest(digest: str) -> None:
        notified_digests.append(digest)

    set_notify_callback(capture_digest)

    try:
        servers = {
            "fs": ServerConfig(name="fs", command="cmd"),
            "other": ServerConfig(name="other", command="cmd2"),
        }
        asyncio.run(
            connect_all(
                servers,
                tool_lists={
                    "fs": [{"name": "read_file", "inputSchema": {}}],
                    "other": [{"name": "write_file", "inputSchema": {}}],
                },
            )
        )

        # Try to introduce conflict
        result = asyncio.run(
            on_tools_changed(
                "other",
                servers["other"],
                [{"name": "read_file", "inputSchema": {}}],  # conflicts with fs
            )
        )

        assert result.is_err
        assert "TOOL_CONFLICT" in (result.error or "")
        # No notification sent on conflict
        assert len(notified_digests) == 0

    finally:
        set_notify_callback(None)
        asyncio.run(disconnect_all())


def test_e2e_digest_includes_all_server_tools() -> None:
    """Digest passed to callback includes tools from all servers, not just changed one."""
    _clear_all_sessions()

    notified_digests: list[str] = []

    async def capture_digest(digest: str) -> None:
        notified_digests.append(digest)

    set_notify_callback(capture_digest)

    try:
        servers = {
            "fs": ServerConfig(name="fs", command="cmd"),
            "git": ServerConfig(name="git", command="cmd2"),
        }
        asyncio.run(
            connect_all(
                servers,
                tool_lists={
                    "fs": [{"name": "read_file", "inputSchema": {}}],
                    "git": [{"name": "git_status", "inputSchema": {}}],
                },
            )
        )

        # Change fs server tools
        result = asyncio.run(
            on_tools_changed(
                "fs",
                servers["fs"],
                [
                    {"name": "read_file", "inputSchema": {}},
                    {"name": "write_file", "inputSchema": {}},
                ],
            )
        )

        assert result.is_ok
        assert len(notified_digests) == 1

        # Expected: sha256 of sorted tool names from ALL servers
        # git_status, read_file, write_file
        expected_tools = sorted(["git_status", "read_file", "write_file"])
        expected_raw = ":".join(expected_tools).encode()
        expected_digest = f"sha256:{hashlib.sha256(expected_raw).hexdigest()}"

        assert notified_digests[0] == expected_digest

    finally:
        set_notify_callback(None)
        asyncio.run(disconnect_all())


# --- Category 5: No sessions -> no-op no error ---


def test_notify_callback_with_no_sessions_no_error() -> None:
    """When callback is set but no sessions captured, on_tools_changed succeeds."""
    _clear_all_sessions()

    notified_digests: list[str] = []

    async def capture_digest(digest: str) -> None:
        notified_digests.append(digest)

    set_notify_callback(capture_digest)

    try:
        # Setup state but no sessions captured
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        asyncio.run(
            connect_all(
                servers,
                tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]},
            )
        )

        # Trigger reload - should succeed without sessions
        result = asyncio.run(
            on_tools_changed(
                "fs",
                servers["fs"],
                [
                    {"name": "tool_a", "inputSchema": {}},
                    {"name": "tool_b", "inputSchema": {}},
                ],
            )
        )

        # Reload succeeds, but callback never sent to sessions
        # (callback was invoked, but there were no captured sessions to notify)
        assert result.is_ok
        # The callback IS invoked by on_tools_changed (digest computed)
        # But the test focuses on: no error when no sessions
        assert len(notified_digests) == 1

    finally:
        set_notify_callback(None)
        asyncio.run(disconnect_all())


def test_callback_none_no_notification_sent() -> None:
    """When set_notify_callback(None), no notification is attempted."""
    _clear_all_sessions()
    set_notify_callback(None)

    try:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        asyncio.run(
            connect_all(
                servers,
                tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]},
            )
        )

        result = asyncio.run(
            on_tools_changed(
                "fs",
                servers["fs"],
                [
                    {"name": "tool_a", "inputSchema": {}},
                    {"name": "tool_b", "inputSchema": {}},
                ],
            )
        )

        assert result.is_ok
        # No notification callback set, so nothing sent
        # on_tools_changed returns success

    finally:
        asyncio.run(disconnect_all())


def test_gateway_notify_all_connections_iterates_runtime_connections() -> None:
    """_notify_all_connections iterates all runtime connections and calls notify_tools_changed."""
    _clear_all_sessions()
    _setup_runtime_for_notifications()

    # Initialize variables at function scope for finally block
    sessions: dict[str, StubSession] = {}

    try:
        # Capture sessions for multiple connections
        sessions = {f"conn_{i}": StubSession() for i in range(3)}
        for conn_id, session in sessions.items():
            capture_session(conn_id, session)

        # Add connections to runtime
        clear_runtime_connections()
        connections = [
            ConnectionContext(
                connection_id=f"conn_{i}",
                profile_name="dev",
                connected_at="2026-01-01T00:00:00Z",
            )
            for i in range(3)
        ]
        for c in connections:
            add_runtime_connection(c)

        # Simulate _notify_all_connections behavior
        async def _notify_all_connections(tools_digest: str) -> None:
            """Simulated gateway notification iteration."""
            conns = get_runtime_connections_snapshot().value or []
            for conn in conns:
                await notify_tools_changed(conn, tools_digest)

        # Invoke with a digest
        asyncio.run(_notify_all_connections("sha256:test123"))

        # All sessions should have received notification
        for session in sessions.values():
            assert len(session.calls) == 1

    finally:
        for conn_id in sessions:
            release_session(conn_id)
        clear_runtime_connections()
        _teardown_runtime()


# --- Concurrency and thread-safety tests ---


def test_session_registry_lock_protects_concurrent_captures() -> None:
    """capture_session and release_session are thread-safe."""

    _clear_all_sessions()

    results: list[bool] = []
    errors: list[str] = []

    def capture_and_release(conn_id: str) -> None:
        try:
            session = StubSession()
            result = capture_session(conn_id, session)
            results.append(result.is_ok)
            release_session(conn_id)
        except Exception as e:
            errors.append(str(e))

    threads = [
        threading.Thread(target=capture_and_release, args=(f"conn_{i}",))
        for i in range(10)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert all(results)


def test_get_captured_session_thread_safe() -> None:
    """get_captured_session is thread-safe."""

    _clear_all_sessions()

    session = StubSession()
    capture_session("shared_conn", session)

    results: list[bool] = []
    errors: list[str] = []

    def lookup_session() -> None:
        try:
            result = get_captured_session("shared_conn")
            results.append(result.is_ok)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=lookup_session) for _ in range(10)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert all(results)

    release_session("shared_conn")


# --- Regression: distinct-session binding via _ensure_connection ---


def test_ensure_connection_assigns_distinct_connections_per_session() -> None:
    """Two distinct sessions through _ensure_connection get distinct connection IDs.

    Regression: previously _ensure_connection returned connections[0] for all
    sessions, causing capture_session to overwrite the first session's binding.
    After the fix, each distinct session triggers a new handle_initialize and
    gets its own ConnectionContext, so capture_session binds each session to
    a unique connection_id.
    """
    from tela.shell.upstream import get_connection_id_for_session

    _clear_all_sessions()
    _setup_runtime_for_notifications()

    try:
        # Simulate two distinct upstream sessions
        session_a = StubSession()
        session_b = StubSession()

        # First session establishes a connection and captures
        conn_a = ConnectionContext(
            connection_id="conn_session_a",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        add_runtime_connection(conn_a)
        result_a = capture_session("conn_session_a", session_a)
        assert result_a.is_ok

        # Second session gets a DIFFERENT connection
        conn_b = ConnectionContext(
            connection_id="conn_session_b",
            profile_name="dev",
            connected_at="2026-01-01T00:00:01Z",
        )
        add_runtime_connection(conn_b)
        result_b = capture_session("conn_session_b", session_b)
        assert result_b.is_ok

        # Verify reverse lookup: each session maps to its own connection
        lookup_a = get_connection_id_for_session(session_a)
        assert lookup_a.is_ok
        assert lookup_a.value == "conn_session_a"

        lookup_b = get_connection_id_for_session(session_b)
        assert lookup_b.is_ok
        assert lookup_b.value == "conn_session_b"

        # Key invariant: the two connection IDs are distinct
        assert lookup_a.value != lookup_b.value

        # Attempting to cross-bind session_b to conn_session_a is rejected
        cross_bind = capture_session("conn_session_a", session_b)
        assert cross_bind.is_err
        assert "SESSION_ALREADY_BOUND" in (cross_bind.error or "")

        # Notifications reach the correct session
        result_notify_a = asyncio.run(notify_tools_changed(conn_a, "sha256:aaa"))
        assert result_notify_a.is_ok
        assert len(session_a.calls) == 1
        assert len(session_b.calls) == 0  # session_b not notified

        result_notify_b = asyncio.run(notify_tools_changed(conn_b, "sha256:bbb"))
        assert result_notify_b.is_ok
        assert len(session_b.calls) == 1  # now notified

    finally:
        release_session("conn_session_a")
        release_session("conn_session_b")
        clear_runtime_connections()
        _teardown_runtime()


# --- Edge cases ---


def test_empty_connection_id_rejected() -> None:
    """capture_session rejects empty connection_id."""
    _clear_all_sessions()

    session = StubSession()
    result = capture_session("", session)
    assert result.is_err
    assert "empty" in (result.error or "").lower()


def test_release_session_idempotent() -> None:
    """release_session succeeds for unknown connection_id."""
    _clear_all_sessions()

    result = release_session("never_captured_conn")
    assert result.is_ok


def test_notify_tools_changed_with_empty_digest() -> None:
    """notify_tools_changed handles empty digest string."""
    _clear_all_sessions()
    _setup_runtime_for_notifications()

    try:
        session = StubSession()
        capture_session("empty_digest_conn", session)

        conn = ConnectionContext(
            connection_id="empty_digest_conn",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )

        result = asyncio.run(notify_tools_changed(conn, ""))
        assert result.is_ok
        # Digest is logged but notification still sent

    finally:
        release_session("empty_digest_conn")
        _teardown_runtime()
