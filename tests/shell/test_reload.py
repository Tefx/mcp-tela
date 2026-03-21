"""Tests for hot reload re-enumeration and conflict rejection.

Tests cover:
- Callback wiring reachability (set_notify_callback, on_tools_changed, on_server_reconnect)
- Re-enumeration triggers through reload path
- Negative/assertive checks for dead no-op paths
- on_config_changed behavior
"""

from __future__ import annotations

import asyncio

from tela.core.models import ServerConfig, TelaConfig
from tela.shell.downstream import connect_all, disconnect_all, get_tool_server
from tela.shell.reload import (
    on_config_changed,
    on_server_reconnect,
    on_tools_changed,
    set_notify_callback,
)


def _setup_single_server() -> dict[str, ServerConfig]:
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(
            servers, tool_lists={"fs": [{"name": "read_file", "inputSchema": {}}]}
        )
    )
    return servers


def _teardown() -> None:
    asyncio.run(disconnect_all())


def _runtime():
    from tela.shell.gateway import get_runtime

    return get_runtime()


# --- on_tools_changed: accepted ---


def test_on_tools_changed_adds_new_tool() -> None:
    servers = _setup_single_server()
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
    assert get_tool_server("write_file").value == "fs"
    _teardown()


def test_on_tools_changed_removes_old_tool() -> None:
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(
            servers,
            tool_lists={
                "fs": [
                    {"name": "tool_a", "inputSchema": {}},
                    {"name": "tool_b", "inputSchema": {}},
                ]
            },
        )
    )
    result = asyncio.run(
        on_tools_changed("fs", servers["fs"], [{"name": "tool_a", "inputSchema": {}}])
    )
    assert result.is_ok
    assert get_tool_server("tool_a").value == "fs"
    assert get_tool_server("tool_b").value is None
    _teardown()


# --- on_tools_changed: conflict rejected ---


def test_on_tools_changed_rejects_conflict() -> None:
    servers = {
        "fs": ServerConfig(name="fs", command="cmd"),
        "custom": ServerConfig(name="custom", command="cmd2"),
    }
    asyncio.run(
        connect_all(
            servers,
            tool_lists={
                "fs": [{"name": "read_file", "inputSchema": {}}],
                "custom": [{"name": "other_tool", "inputSchema": {}}],
            },
        )
    )
    # Try to add conflicting tool to "custom"
    result = asyncio.run(
        on_tools_changed(
            "custom",
            servers["custom"],
            [{"name": "read_file", "inputSchema": {}}],  # conflicts with fs
        )
    )
    assert result.is_err
    assert "TOOL_CONFLICT" in (result.error or "")
    # Previous tools preserved
    assert get_tool_server("other_tool").value == "custom"
    _teardown()


# --- on_server_reconnect ---


def test_on_server_reconnect_updates_tools() -> None:
    servers = _setup_single_server()
    result = asyncio.run(
        on_server_reconnect(
            "fs",
            servers["fs"],
            [
                {"name": "read_file", "inputSchema": {}},
                {"name": "new_tool", "inputSchema": {}},
            ],
        )
    )
    assert result.is_ok
    assert get_tool_server("new_tool").value == "fs"
    _teardown()


# --- on_config_changed ---


def test_on_config_changed_updates_runtime() -> None:
    r = asyncio.run(on_config_changed(TelaConfig()))
    assert r.is_ok


def test_on_config_changed_sets_runtime_config() -> None:
    """on_config_changed updates the runtime config reference."""

    runtime = _runtime()
    old_config = runtime.config  # Save current config

    try:
        # Empty config = no servers to connect
        new_config = TelaConfig()
        # With no previous config set, it should succeed (no servers to manage)
        result = asyncio.run(on_config_changed(new_config))
        assert result.is_ok
        assert runtime.config == new_config
    finally:
        runtime.config = old_config


def test_on_config_changed_detects_server_removal() -> None:
    """on_config_changed detects removed servers and triggers disconnect."""
    from tela.shell.downstream import get_all_tools

    runtime = _runtime()
    old_config_ref = runtime.config

    # Setup: connect with initial servers
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    asyncio.run(
        connect_all(
            servers, tool_lists={"srv": [{"name": "tool_a", "inputSchema": {}}]}
        )
    )

    # Set runtime config to match connected servers
    old_config = TelaConfig(servers=servers)
    runtime.config = old_config

    try:
        # New config removes all servers
        new_config = TelaConfig(servers={})
        result = asyncio.run(on_config_changed(new_config))
        assert result.is_ok
        assert runtime.config == new_config
        # Registry should be cleared after disconnect
        tools_result = get_all_tools()
        assert tools_result.is_ok and tools_result.value == {}
    finally:
        runtime.config = old_config_ref


def test_on_config_changed_identical_config_no_reconnect() -> None:
    """When old and new configs are identical, no disconnect/reconnect occurs."""

    runtime = _runtime()
    old_config_ref = runtime.config

    try:
        # Setup with tool_lists injection
        servers = {"srv": ServerConfig(name="srv", command="cmd")}
        config = TelaConfig(servers=servers)
        asyncio.run(
            connect_all(
                config.servers, tool_lists={"srv": [{"name": "t", "inputSchema": {}}]}
            )
        )
        runtime.config = config

        # No server changes - on_config_changed sets config and returns
        # (removed and servers_to_reconnect are both empty sets)
        result = asyncio.run(on_config_changed(config))
        assert result.is_ok
    finally:
        runtime.config = old_config_ref
        _teardown()


def test_on_config_changed_server_change_triggers_reconnect_error() -> None:
    """on_config_changed triggers reconnect when servers change.

    This test verifies the reconnect path is attempted. For actual connection
    tests, use the integration tests with real MCP servers.
    """

    runtime = _runtime()
    old_config_ref = runtime.config

    # Setup: initial config
    old_servers = {"old_srv": ServerConfig(name="old_srv", command="cmd")}
    old_config = TelaConfig(servers=old_servers)

    # Connect with tool_lists
    asyncio.run(
        connect_all(
            old_config.servers,
            tool_lists={"old_srv": [{"name": "t", "inputSchema": {}}]},
        )
    )
    runtime.config = old_config

    try:
        # New config with different server triggers reconnect attempt
        # Since actual command doesn't exist, it will fail - this is expected
        # behavior verification for the reconnect path
        new_servers = {
            "new_srv": ServerConfig(name="new_srv", command="nonexistent_cmd")
        }
        new_config = TelaConfig(servers=new_servers)

        result = asyncio.run(on_config_changed(new_config))
        # Fails because command doesn't exist - proves reconnect was attempted
        assert result.is_err
        assert "DOWNSTREAM_CONNECT_FAILED" in (result.error or "")

        # Runtime config should still be updated (even on failure)
        assert runtime.config == new_config
    finally:
        runtime.config = old_config_ref
        _teardown()


# --- Notification callback wiring ---


def test_on_tools_changed_calls_notify_callback() -> None:
    """Accepted reload calls the notification callback with tools digest."""
    notified = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    set_notify_callback(capture_notify)
    try:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        asyncio.run(
            connect_all(
                servers, tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]}
            )
        )
        asyncio.run(
            on_tools_changed(
                "fs",
                servers["fs"],
                [
                    {"name": "tool_a", "inputSchema": {}},
                    {"name": "tool_b", "inputSchema": {}},
                ],
            )
        )
        assert len(notified) == 1
        assert notified[0].startswith("sha256:")
        assert len(notified[0]) == len("sha256:") + 64
    finally:
        set_notify_callback(None)
        _teardown()


def test_set_notify_callback_clear_removes_callback() -> None:
    """set_notify_callback(None) clears callback so no notification is sent."""
    notified = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    # Set then clear
    set_notify_callback(capture_notify)
    set_notify_callback(None)

    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(servers, tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]})
    )
    try:
        asyncio.run(
            on_tools_changed(
                "fs",
                servers["fs"],
                [
                    {"name": "tool_a", "inputSchema": {}},
                    {"name": "tool_b", "inputSchema": {}},
                ],
            )
        )
        # No notification because callback was cleared
        assert len(notified) == 0
    finally:
        _teardown()


def test_on_tools_changed_conflict_does_not_call_notify_callback() -> None:
    """Conflict rejection does NOT call notification callback (dead path check)."""
    from tela.shell.audit import clear_audit_entries

    notified = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    set_notify_callback(capture_notify)
    clear_audit_entries()
    try:
        servers = {
            "fs": ServerConfig(name="fs", command="cmd"),
            "custom": ServerConfig(name="custom", command="cmd2"),
        }
        asyncio.run(
            connect_all(
                servers,
                tool_lists={
                    "fs": [{"name": "read_file", "inputSchema": {}}],
                    "custom": [{"name": "other_tool", "inputSchema": {}}],
                },
            )
        )
        # This triggers a conflict
        asyncio.run(
            on_tools_changed(
                "custom",
                servers["custom"],
                [{"name": "read_file", "inputSchema": {}}],
            )
        )
        # No notification because conflict was rejected
        assert len(notified) == 0
    finally:
        set_notify_callback(None)
        clear_audit_entries()
        _teardown()


def test_on_server_reconnect_calls_notify_callback_via_delegation() -> None:
    """on_server_reconnect delegates to on_tools_changed and triggers notification."""
    notified = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    set_notify_callback(capture_notify)
    try:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        asyncio.run(
            connect_all(
                servers, tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]}
            )
        )
        result = asyncio.run(
            on_server_reconnect(
                "fs",
                servers["fs"],
                [
                    {"name": "tool_a", "inputSchema": {}},
                    {"name": "tool_b", "inputSchema": {}},
                ],
            )
        )
        assert result.is_ok
        assert len(notified) == 1
        assert notified[0].startswith("sha256:")
    finally:
        set_notify_callback(None)
        _teardown()


def test_notify_callback_digest_includes_all_servers_tools() -> None:
    """Notification digest includes tools from all servers, not just changed one."""
    notified = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    set_notify_callback(capture_notify)
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
        # Change fs
        asyncio.run(
            on_tools_changed(
                "fs",
                servers["fs"],
                [
                    {"name": "read_file", "inputSchema": {}},
                    {"name": "write_file", "inputSchema": {}},
                ],
            )
        )
        # Digest should reflect tools from BOTH servers (alphabetically sorted)
        # git_status, read_file, write_file
        import hashlib

        tool_names = sorted(["git_status", "read_file", "write_file"])
        raw = ":".join(tool_names).encode()
        expected = f"sha256:{hashlib.sha256(raw).hexdigest()}"

        assert len(notified) == 1
        assert notified[0] == expected
    finally:
        set_notify_callback(None)
        _teardown()


# --- Warning emission ---


def test_on_tools_changed_conflict_emits_audit_warning() -> None:
    """Rejected reload emits TOOL_CONFLICT audit warning."""
    from tela.shell.audit import clear_audit_entries, get_audit_entries

    clear_audit_entries()
    servers = {
        "fs": ServerConfig(name="fs", command="cmd"),
        "custom": ServerConfig(name="custom", command="cmd2"),
    }
    asyncio.run(
        connect_all(
            servers,
            tool_lists={
                "fs": [{"name": "read_file", "inputSchema": {}}],
                "custom": [{"name": "other_tool", "inputSchema": {}}],
            },
        )
    )
    asyncio.run(
        on_tools_changed(
            "custom",
            servers["custom"],
            [{"name": "read_file", "inputSchema": {}}],
        )
    )
    entries_result = get_audit_entries()
    assert entries_result.is_ok and entries_result.value is not None
    assert len(entries_result.value) == 1
    assert entries_result.value[0].error_code == "TOOL_CONFLICT"
    clear_audit_entries()
    _teardown()


# --- Re-enumeration through reload path ---


def test_re_enumeration_updates_registry_via_reload() -> None:
    """Verify tool-list changes flow through on_tools_changed to registry update."""
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(servers, tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]})
    )

    # Initial state
    assert get_tool_server("tool_a").value == "fs"
    assert get_tool_server("tool_b").value is None

    # Re-enumerate via on_tools_changed
    result = asyncio.run(
        on_tools_changed(
            "fs",
            servers["fs"],
            [
                {"name": "tool_a", "inputSchema": {}},
                {"name": "tool_b", "inputSchema": {}},
                {"name": "tool_c", "inputSchema": {}},
            ],
        )
    )
    assert result.is_ok

    # Registry reflects all three tools
    assert get_tool_server("tool_a").value == "fs"
    assert get_tool_server("tool_b").value == "fs"
    assert get_tool_server("tool_c").value == "fs"

    _teardown()


def test_re_enumeration_preserves_other_servers() -> None:
    """Re-enumeration of one server preserves other servers' tool registry."""
    servers = {
        "fs": ServerConfig(name="fs", command="cmd"),
        "git": ServerConfig(name="git", command="cmd"),
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

    # Re-enumerate fs only
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

    # fs tools updated
    assert get_tool_server("read_file").value == "fs"
    assert get_tool_server("write_file").value == "fs"
    # git tools preserved
    assert get_tool_server("git_status").value == "git"

    _teardown()


def test_conflict_rollback_restores_all_servers_state() -> None:
    """On conflict, rollback restores ALL servers' tools (not just the changed one)."""
    servers = {
        "fs": ServerConfig(name="fs", command="cmd"),
        "git": ServerConfig(name="git", command="cmd"),
    }
    asyncio.run(
        connect_all(
            servers,
            tool_lists={
                "fs": [{"name": "tool_a", "inputSchema": {}}],  # One tool from fs
                "git": [
                    {"name": "tool_b", "inputSchema": {}}
                ],  # One different tool from git
            },
        )
    )

    # Try to add conflicting tool to git
    result = asyncio.run(
        on_tools_changed(
            "git",
            servers["git"],
            [{"name": "tool_a", "inputSchema": {}}],  # Conflicts with fs
        )
    )
    assert result.is_err
    assert "TOOL_CONFLICT" in (result.error or "")

    # Both servers' tools should be preserved (rollback)
    assert get_tool_server("tool_a").value == "fs"
    assert get_tool_server("tool_b").value == "git"

    _teardown()
