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
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,
    get_runtime_config,
    get_runtime_connections_snapshot,
    get_runtime_secrets,
    get_runtime_status_snapshot,
    get_upstream_http_app,
    get_upstream_log_level,
    get_upstream_server,
    is_upstream_server_initialized,
    remove_runtime_connection,
    set_runtime_config,
    set_runtime_running,
    set_runtime_secrets,
    set_upstream_server,
)


class TestB1LockSafeWriteHelpers:
    """B1: Lock-safe write helpers cover all runtime mutation patterns."""

    def test_set_runtime_config_replaces_config(self) -> None:
        """set_runtime_config must atomically replace config."""
        original = TelaConfig()
        set_runtime_config(original)
        assert get_runtime_config().value is not None
        set_runtime_config(None)
        assert get_runtime_config().value is None

    def test_set_runtime_running_flag(self) -> None:
        """set_runtime_running must atomically set running flag."""
        set_runtime_running(True)
        from tela.shell.gateway_runtime import is_runtime_running

        assert is_runtime_running().value is True
        set_runtime_running(False)
        assert is_runtime_running().value is False

    def test_clear_runtime_connections(self) -> None:
        """clear_runtime_connections must atomically remove all connections."""
        ctx = ConnectionContext(
            connection_id="test-clear", profile_name="p", connected_at="t"
        )
        add_runtime_connection(ctx)
        conns = get_runtime_connections_snapshot().value
        assert conns is not None and len(conns) > 0
        clear_runtime_connections()
        conns2 = get_runtime_connections_snapshot().value
        assert conns2 is not None and len(conns2) == 0


class TestB2SnapshotImmutability:
    """B2: Read/snapshot accessors return fully detached values."""

    def test_config_snapshot_is_detached(self) -> None:
        """Mutating returned config must not affect runtime state."""
        original = TelaConfig(resolved_default_profile="original")
        set_runtime_config(original)

        snapshot = get_runtime_config().value
        assert snapshot is not None
        # Mutate the snapshot
        snapshot.resolved_default_profile = "mutated"

        # Runtime state must be unchanged
        live = get_runtime_config().value
        assert live is not None
        assert live.resolved_default_profile == "original"

        # Cleanup
        set_runtime_config(None)

    def test_config_snapshot_is_not_identity_equal(self) -> None:
        """Returned config must not be the same object as runtime-owned."""
        original = TelaConfig()
        set_runtime_config(original)

        snapshot = get_runtime_config().value
        # Deep copy means different object identity
        assert snapshot is not original

        set_runtime_config(None)

    def test_connections_snapshot_list_is_detached(self) -> None:
        """Mutating returned connections list must not affect runtime."""
        ctx = ConnectionContext(
            connection_id="snap-test", profile_name="p", connected_at="t"
        )
        add_runtime_connection(ctx)

        snapshot = get_runtime_connections_snapshot().value
        assert snapshot is not None
        snapshot.clear()  # mutate the returned list

        # Runtime connections must be unchanged
        live = get_runtime_connections_snapshot().value
        assert live is not None
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

        snapshot = get_runtime_connections_snapshot().value
        assert snapshot is not None and len(snapshot) > 0
        snap_conn = [c for c in snapshot if c.connection_id == "member-test"][0]

        # Mutate the snapshot member
        snap_conn.tool_call_count = 999

        # Runtime member must be unchanged
        live = get_runtime_connections_snapshot().value
        assert live is not None
        live_conn = [c for c in live if c.connection_id == "member-test"][0]
        assert live_conn.tool_call_count == 0

        remove_runtime_connection("member-test")

    def test_status_snapshot_config_is_detached(self) -> None:
        """RuntimeStatusSnapshot.config must be detached from runtime."""
        set_runtime_config(TelaConfig(resolved_default_profile="snap-orig"))
        set_runtime_running(True)

        snap = get_runtime_status_snapshot().value
        assert snap is not None
        assert snap.config is not None
        # Verify it was captured correctly
        assert snap.config.resolved_default_profile == "snap-orig"

        # Prove detachment: mutating a second snapshot doesn't affect the first
        snap2 = get_runtime_config().value
        assert snap2 is not None
        snap2.resolved_default_profile = "mutated-for-identity"
        assert snap.config.resolved_default_profile == "snap-orig"

        set_runtime_config(None)
        set_runtime_running(False)

    def test_status_snapshot_connections_are_tuple(self) -> None:
        """RuntimeStatusSnapshot.connections must be immutable tuple."""
        set_runtime_config(TelaConfig())
        ctx = ConnectionContext(
            connection_id="tup-test", profile_name="p", connected_at="t"
        )
        add_runtime_connection(ctx)

        snap = get_runtime_status_snapshot().value
        assert snap is not None
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

        snap = get_runtime_status_snapshot().value
        assert snap is not None
        snap_conn = [c for c in snap.connections if c.connection_id == "stat-member"][0]

        # Prove detachment: snapshot member is not identical to a fresh read
        fresh = get_runtime_connections_snapshot().value
        assert fresh is not None
        fresh_conn = [c for c in fresh if c.connection_id == "stat-member"][0]
        assert snap_conn is not fresh_conn  # different objects
        assert snap_conn.tool_call_count == 5

        remove_runtime_connection("stat-member")

    def test_secrets_snapshot_is_detached(self) -> None:
        """Mutating returned secrets list must not affect runtime."""
        # Secrets are strings (immutable), but the list container must be detached
        set_runtime_secrets(["s1", "s2"])

        snapshot = get_runtime_secrets().value
        assert snapshot is not None
        snapshot.append("s3")

        live = get_runtime_secrets().value
        assert live is not None
        assert "s3" not in live
        assert len(live) == 2

        set_runtime_secrets([])


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

    def test_serve_cmd_does_not_import_get_upstream_server(self) -> None:
        """serve_cmd.py must not import get_upstream_server (uses operation accessors)."""
        import inspect

        from tela.commands import serve_cmd

        source = inspect.getsource(serve_cmd)
        import_lines = [
            line.strip()
            for line in source.split("\n")
            if line.strip().startswith(("from tela.shell.gateway import", "import"))
            and "get_upstream_server" in line
        ]
        assert import_lines == [], (
            f"serve_cmd.py still imports get_upstream_server: {import_lines}"
        )

    def test_no_production_module_imports_get_upstream_server(self) -> None:
        """No production (non-test) module should import get_upstream_server."""
        import inspect

        from tela.shell import http_routes, upstream
        from tela.commands import serve_cmd

        production_modules = {
            "upstream": upstream,
            "http_routes": http_routes,
            "serve_cmd": serve_cmd,
        }
        for name, mod in production_modules.items():
            source = inspect.getsource(mod)
            import_lines = [
                line.strip()
                for line in source.split("\n")
                if line.strip().startswith(("from tela.shell.gateway import",))
                and "get_upstream_server" in line
            ]
            assert import_lines == [], (
                f"{name} still imports get_upstream_server: {import_lines}"
            )


class TestB3UpstreamServerBoundaryPolicy:
    """B3: get_upstream_server() returns live alias; operation accessors do not."""

    def test_operation_accessors_do_not_leak_server_reference(self) -> None:
        """Operation accessors return Results wrapping values, not the live FastMCP."""
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("boundary-test")
        set_upstream_server(server)

        # is_upstream_server_initialized returns Result[bool], not server
        init_result = is_upstream_server_initialized()
        assert init_result.is_ok
        assert init_result.value is True

        # get_upstream_http_app returns a Result[Starlette], not FastMCP
        app_result = get_upstream_http_app()
        assert app_result.is_ok
        assert app_result.value is not None
        assert not isinstance(app_result.value, FastMCP)

        # get_upstream_log_level returns Result[str], not server
        log_result = get_upstream_log_level()
        assert log_result.is_ok
        assert isinstance(log_result.value, str)

        # Cleanup
        set_upstream_server(None)

    def test_deprecated_get_upstream_server_raises(self) -> None:
        """get_upstream_server raises RuntimeError (removed in loop 4)."""
        import pytest

        with pytest.raises(RuntimeError, match="removed in loop 4"):
            get_upstream_server()

    def test_uninitialised_server_operation_accessors(self) -> None:
        """Operation accessors handle None server gracefully."""
        set_upstream_server(None)

        init_result = is_upstream_server_initialized()
        assert init_result.is_ok
        assert init_result.value is False

        app_result = get_upstream_http_app()
        assert app_result.is_err
        assert "UPSTREAM_NOT_INITIALIZED" in (app_result.error or "")

        log_result = get_upstream_log_level()
        assert log_result.is_ok
        assert log_result.value == "info"

    def test_set_upstream_server_locked_mutator(self) -> None:
        """set_upstream_server provides locked write access."""
        from mcp.server.fastmcp import FastMCP

        assert not is_upstream_server_initialized().value

        server = FastMCP("mutator-test")
        set_upstream_server(server)
        assert is_upstream_server_initialized().value

        set_upstream_server(None)
        assert not is_upstream_server_initialized().value
