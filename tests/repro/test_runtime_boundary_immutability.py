"""Regression tests for runtime boundary immutability (B1 + B2).

B1: get_runtime() must not be used in production code; lock-safe helpers
    must cover all write mutations.
B2: Snapshot/read accessors must return detached (deep-copied) values so
    callers cannot mutate runtime-owned state through returned references.

These tests verify root-cause hardening: mutable alias leakage is removed
rather than merely wrapped.
"""

from __future__ import annotations

from tela.core.models import ConnectionContext, TelaConfig
from tela.shell.gateway import (
    add_runtime_connection,
    clear_runtime_connections,
    get_runtime,
    get_runtime_config,
    get_runtime_connections_snapshot,
    get_runtime_secrets,
    get_runtime_status_snapshot,
    remove_runtime_connection,
    set_runtime_config,
    set_runtime_running,
)


class TestB1LockSafeWriteHelpers:
    """B1: Lock-safe write helpers cover all runtime mutation patterns."""

    def test_set_runtime_config_replaces_config(self) -> None:
        """set_runtime_config must atomically replace config."""
        original = TelaConfig()
        set_runtime_config(original)
        assert get_runtime_config() is not None
        set_runtime_config(None)
        assert get_runtime_config() is None

    def test_set_runtime_running_flag(self) -> None:
        """set_runtime_running must atomically set running flag."""
        set_runtime_running(True)
        from tela.shell.gateway import is_runtime_running

        assert is_runtime_running() is True
        set_runtime_running(False)
        assert is_runtime_running() is False

    def test_clear_runtime_connections(self) -> None:
        """clear_runtime_connections must atomically remove all connections."""
        ctx = ConnectionContext(
            connection_id="test-clear", profile_name="p", connected_at="t"
        )
        add_runtime_connection(ctx)
        assert len(get_runtime_connections_snapshot()) > 0
        clear_runtime_connections()
        assert len(get_runtime_connections_snapshot()) == 0


class TestB2SnapshotImmutability:
    """B2: Read/snapshot accessors return fully detached values."""

    def test_config_snapshot_is_detached(self) -> None:
        """Mutating returned config must not affect runtime state."""
        original = TelaConfig(resolved_default_profile="original")
        set_runtime_config(original)

        snapshot = get_runtime_config()
        assert snapshot is not None
        # Mutate the snapshot
        snapshot.resolved_default_profile = "mutated"

        # Runtime state must be unchanged
        live = get_runtime_config()
        assert live is not None
        assert live.resolved_default_profile == "original"

        # Cleanup
        set_runtime_config(None)

    def test_config_snapshot_is_not_identity_equal(self) -> None:
        """Returned config must not be the same object as runtime-owned."""
        original = TelaConfig()
        set_runtime_config(original)

        snapshot = get_runtime_config()
        # Deep copy means different object identity
        assert snapshot is not original

        set_runtime_config(None)

    def test_connections_snapshot_list_is_detached(self) -> None:
        """Mutating returned connections list must not affect runtime."""
        ctx = ConnectionContext(
            connection_id="snap-test", profile_name="p", connected_at="t"
        )
        add_runtime_connection(ctx)

        snapshot = get_runtime_connections_snapshot()
        snapshot.clear()  # mutate the returned list

        # Runtime connections must be unchanged
        live = get_runtime_connections_snapshot()
        assert len(live) > 0
        assert any(c.connection_id == "snap-test" for c in live)

        remove_runtime_connection("snap-test")

    def test_connections_snapshot_members_are_detached(self) -> None:
        """ConnectionContext objects in snapshot must be deep copies."""
        ctx = ConnectionContext(
            connection_id="member-test",
            profile_name="p",
            connected_at="t",
            tool_call_count=0,
        )
        add_runtime_connection(ctx)

        snapshot = get_runtime_connections_snapshot()
        assert len(snapshot) > 0
        snap_conn = [c for c in snapshot if c.connection_id == "member-test"][0]

        # Mutate the snapshot member
        snap_conn.tool_call_count = 999

        # Runtime member must be unchanged
        live = get_runtime_connections_snapshot()
        live_conn = [c for c in live if c.connection_id == "member-test"][0]
        assert live_conn.tool_call_count == 0

        remove_runtime_connection("member-test")

    def test_status_snapshot_config_is_detached(self) -> None:
        """RuntimeStatusSnapshot.config must be detached from runtime."""
        set_runtime_config(TelaConfig(resolved_default_profile="snap-orig"))
        set_runtime_running(True)

        snap = get_runtime_status_snapshot()
        assert snap.config is not None
        # Verify it was captured correctly
        assert snap.config.resolved_default_profile == "snap-orig"

        # Mutate would require unfreezing the dataclass, but config is a
        # Pydantic model - verify it's not the live reference
        runtime = get_runtime()
        assert snap.config is not runtime.config

        set_runtime_config(None)
        set_runtime_running(False)

    def test_status_snapshot_connections_are_tuple(self) -> None:
        """RuntimeStatusSnapshot.connections must be immutable tuple."""
        set_runtime_config(TelaConfig())
        ctx = ConnectionContext(
            connection_id="tup-test", profile_name="p", connected_at="t"
        )
        add_runtime_connection(ctx)

        snap = get_runtime_status_snapshot()
        assert isinstance(snap.connections, tuple)
        assert len(snap.connections) > 0

        remove_runtime_connection("tup-test")
        set_runtime_config(None)

    def test_status_snapshot_connection_members_are_detached(self) -> None:
        """ConnectionContext members in status snapshot are deep copies."""
        ctx = ConnectionContext(
            connection_id="stat-member",
            profile_name="p",
            connected_at="t",
            tool_call_count=5,
        )
        add_runtime_connection(ctx)

        snap = get_runtime_status_snapshot()
        snap_conn = [c for c in snap.connections if c.connection_id == "stat-member"][0]

        # Verify detachment: not same object as runtime-owned
        runtime = get_runtime()
        runtime_conn = [
            c for c in runtime.connections if c.connection_id == "stat-member"
        ][0]
        assert snap_conn is not runtime_conn
        assert snap_conn.tool_call_count == 5

        remove_runtime_connection("stat-member")

    def test_secrets_snapshot_is_detached(self) -> None:
        """Mutating returned secrets list must not affect runtime."""
        # Secrets are strings (immutable), but the list container must be detached
        runtime = get_runtime()
        import threading

        lock = threading.RLock()
        # Use the runtime directly for setup (test-only pattern)
        with lock:
            runtime.secrets = ["s1", "s2"]

        snapshot = get_runtime_secrets()
        snapshot.append("s3")

        live = get_runtime_secrets()
        assert "s3" not in live
        assert len(live) == 2

        runtime.secrets = []


class TestB1B2NoProductionGetRuntime:
    """Verify production modules no longer import get_runtime."""

    def test_upstream_does_not_import_get_runtime(self) -> None:
        """upstream.py must not import get_runtime (uses lock-safe helpers)."""
        import inspect

        from tela.shell import upstream

        source = inspect.getsource(upstream)
        # The module-level import block should not contain get_runtime
        # (doctests may reference set_runtime_config instead)
        import_lines = [
            line.strip()
            for line in source.split("\n")
            if line.strip().startswith(("from tela.shell.gateway import", "import"))
            and "get_runtime," in line
            and "get_runtime_config" not in line
            and "get_runtime_secrets" not in line
            and "get_runtime_connections" not in line
            and "get_runtime_status" not in line
        ]
        assert import_lines == [], (
            f"upstream.py still imports bare get_runtime: {import_lines}"
        )

    def test_http_routes_does_not_import_get_runtime(self) -> None:
        """http_routes.py must not import get_runtime (uses lock-safe helpers)."""
        import inspect

        from tela.shell import http_routes

        source = inspect.getsource(http_routes)
        import_lines = [
            line.strip()
            for line in source.split("\n")
            if line.strip().startswith(("from tela.shell.gateway import", "import"))
            and "get_runtime," in line
            and "get_runtime_config" not in line
            and "get_runtime_status" not in line
        ]
        assert import_lines == [], (
            f"http_routes.py still imports bare get_runtime: {import_lines}"
        )
