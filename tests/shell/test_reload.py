"""Tests for hot reload re-enumeration and conflict rejection.

Tests cover:
- Callback wiring reachability (set_notify_callback, on_tools_changed, on_server_reconnect)
- Re-enumeration triggers through reload path
- Negative/assertive checks for dead no-op paths
- on_config_changed behavior
"""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import ServerConfig, TelaConfig
from tela.shell.downstream import (
    connect_all,
    disconnect_all,
    get_downstream_startup_snapshot,
    get_tool_server,
)
from tela.shell.reload import (
    RECONNECT_ENUMERATION_CONTRACT,
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


def _get_config():
    from tela.shell.gateway_runtime import get_runtime_config

    return get_runtime_config().value


def _set_config(config):
    from tela.shell.gateway_runtime import set_runtime_config

    set_runtime_config(config)


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


def test_reconnect_contract_marks_fresh_payload_authoritative() -> None:
    """Declarative reconnect contract forbids duplicate enumeration."""

    assert RECONNECT_ENUMERATION_CONTRACT.authoritative_payload_name == "tool_list"
    assert RECONNECT_ENUMERATION_CONTRACT.authoritative_payload_fields == ("raw_tools",)
    assert "second enumeration" in RECONNECT_ENUMERATION_CONTRACT.forbidden_behavior


# --- on_config_changed ---


def test_on_config_changed_updates_runtime() -> None:
    r = asyncio.run(on_config_changed(TelaConfig()))
    assert r.is_ok


def test_on_config_changed_sets_runtime_config() -> None:
    """on_config_changed updates the runtime config reference."""

    old_config = _get_config()  # Save current config

    try:
        # Empty config = no servers to connect
        new_config = TelaConfig()
        # With no previous config set, it should succeed (no servers to manage)
        result = asyncio.run(on_config_changed(new_config))
        assert result.is_ok
        assert _get_config() == new_config
    finally:
        _set_config(old_config)


def test_on_config_changed_detects_server_removal() -> None:
    """on_config_changed detects removed servers and triggers disconnect."""
    from tela.shell.downstream import get_all_tools

    old_config_ref = _get_config()

    # Setup: connect with initial servers
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    asyncio.run(
        connect_all(
            servers, tool_lists={"srv": [{"name": "tool_a", "inputSchema": {}}]}
        )
    )

    # Set runtime config to match connected servers
    old_config = TelaConfig(servers=servers)
    _set_config(old_config)

    try:
        # New config removes all servers
        new_config = TelaConfig(servers={})
        result = asyncio.run(on_config_changed(new_config))
        assert result.is_ok
        assert _get_config() == new_config
        # Registry should be cleared after disconnect
        tools_result = get_all_tools()
        assert tools_result.is_ok and tools_result.value == {}
    finally:
        _set_config(old_config_ref)


def test_on_config_changed_identical_config_no_reconnect() -> None:
    """When old and new configs are identical, no disconnect/reconnect occurs."""

    old_config_ref = _get_config()

    try:
        # Setup with tool_lists injection
        servers = {"srv": ServerConfig(name="srv", command="cmd")}
        config = TelaConfig(servers=servers)
        asyncio.run(
            connect_all(
                config.servers, tool_lists={"srv": [{"name": "t", "inputSchema": {}}]}
            )
        )
        _set_config(config)

        # No server changes - on_config_changed sets config and returns
        # (removed and servers_to_reconnect are both empty sets)
        result = asyncio.run(on_config_changed(config))
        assert result.is_ok
    finally:
        _set_config(old_config_ref)
        _teardown()


def test_on_config_changed_server_change_records_reconnect_failure() -> None:
    """on_config_changed records failed providers without rejecting reload."""

    old_config_ref = _get_config()

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
    _set_config(old_config)

    try:
        # New config with different server triggers reconnect attempt
        # Since actual command doesn't exist, it will fail - this is expected
        # behavior verification for the reconnect path
        new_servers = {
            "new_srv": ServerConfig(name="new_srv", command="nonexistent_cmd")
        }
        new_config = TelaConfig(servers=new_servers)

        result = asyncio.run(on_config_changed(new_config))
        assert result.is_ok
        snapshot = get_downstream_startup_snapshot().value
        assert snapshot is not None
        assert snapshot.degraded_reason == "provider_initialize_failed:new_srv"

        # Runtime config should still be updated even when the provider failed.
        assert _get_config() == new_config
    finally:
        _set_config(old_config_ref)
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


# --- Reconnect enumeration regression tests ---
# Spec ref: docs/DESIGN.md Runtime Architecture / Connection lifecycle
# Acceptance: When downstream reconnect handling already has fresh raw_tools,
# the reload/reconnect flow MUST reuse that payload.
# Reconnect flow MUST NOT trigger a second list_tools or re_enumerate call
# for the same reconnect event.


def test_on_server_reconnect_reuses_passed_tool_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_server_reconnect must use the tool_list argument, not re-enumerate.

    Regression test for: downstream reconnect already has fresh raw_tools
    from _handle_reconnect's _enumerate_client_tools call. The reconnect
    handler MUST NOT call re_enumerate again - it must reuse the passed
    tool_list directly.
    """
    from typing import Any
    from tela.shell.result import Result

    re_enumerate_called = []

    async def _fake_re_enumerate(server_name: str) -> Result[list[Any], str]:
        re_enumerate_called.append(server_name)
        # Return empty list to avoid affecting registry in this test
        return Result(value=[])

    monkeypatch.setattr("tela.shell.downstream.re_enumerate", _fake_re_enumerate)

    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(
            servers, tool_lists={"fs": [{"name": "initial_tool", "inputSchema": {}}]}
        )
    )

    fresh_tool_list = [
        {"name": "initial_tool", "inputSchema": {}},
        {"name": "new_tool", "inputSchema": {}},
    ]

    # on_server_reconnect is called with fresh_tool_list by _handle_reconnect
    # after reconnect is established. It MUST use fresh_tool_list directly.
    result = asyncio.run(on_server_reconnect("fs", servers["fs"], fresh_tool_list))
    assert result.is_ok

    # The bug: on_server_reconnect currently calls re_enumerate(server_name)
    # which is a duplicate enumeration. After fix, re_enumerate_called should
    # be empty because the tool_list was already provided.
    assert re_enumerate_called == [], (
        f"on_server_reconnect must NOT call re_enumerate when tool_list is "
        f"provided. re_enumerate was called for: {re_enumerate_called}. "
        f"Bug: downstream already enumerated fresh tools in _handle_reconnect"
    )

    _teardown()


def test_on_server_reconnect_does_not_trigger_duplicate_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect flow must trigger exactly one enumeration, not two.

    The reconnect path (_handle_reconnect -> on_server_reconnect) must:
    1. Enumerate once in _handle_reconnect via _enumerate_client_tools
    2. Pass fresh raw_tools to on_server_reconnect
    3. on_server_reconnect must reuse the passed tools WITHOUT re-enumerating

    This test verifies that re_enumerate is NOT called during reconnect,
    proving no duplicate enumeration occurs.
    """
    from typing import Any
    from tela.shell.result import Result

    enumerate_count = []

    async def _fake_re_enumerate(server_name: str) -> Result[list[Any], str]:
        enumerate_count.append(server_name)
        from tela.shell.downstream import get_registry

        registry = get_registry()
        return Result(value=registry.get_all_tools().get(server_name, []))

    monkeypatch.setattr("tela.shell.downstream.re_enumerate", _fake_re_enumerate)

    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(servers, tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]})
    )

    reconnect_tools = [
        {"name": "tool_a", "inputSchema": {}},
        {"name": "tool_b", "inputSchema": {}},
    ]

    result = asyncio.run(on_server_reconnect("fs", servers["fs"], reconnect_tools))
    assert result.is_ok

    # Exactly zero re_enumerate calls should occur during reconnect
    # (the fresh tools were already enumerated by _handle_reconnect)
    assert len(enumerate_count) == 0, (
        f"Duplicate enumeration detected: re_enumerate was called {enumerate_count}. "
        f"Reconnect flow must not re-enumerate when fresh tools are provided."
    )

    _teardown()


def test_on_server_reconnect_registry_reflects_passed_tools() -> None:
    """Registry reflects the tools passed to on_server_reconnect, not re-enumerated ones."""
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(servers, tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]})
    )

    # Simulate reconnect with expanded tool list
    reconnect_tools = [
        {"name": "tool_a", "inputSchema": {}},
        {"name": "tool_b", "inputSchema": {}},
        {"name": "tool_c", "inputSchema": {}},
    ]

    result = asyncio.run(on_server_reconnect("fs", servers["fs"], reconnect_tools))
    assert result.is_ok

    # Registry should reflect all tools from reconnect_tools
    assert get_tool_server("tool_a").value == "fs"
    assert get_tool_server("tool_b").value == "fs"
    assert get_tool_server("tool_c").value == "fs"

    _teardown()


def test_on_server_reconnect_notify_callback_fired_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notification callback fires exactly once after successful reconnect."""
    notified: list[str] = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    set_notify_callback(capture_notify)

    # Disable re_enumerate to prevent duplicate enumeration affecting test
    async def _no_op_re_enumerate(server_name: str):
        from tela.shell.result import Result

        return Result(value=[])

    monkeypatch.setattr("tela.shell.downstream.re_enumerate", _no_op_re_enumerate)

    try:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        asyncio.run(
            connect_all(
                servers,
                tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]},
            )
        )

        reconnect_tools = [
            {"name": "tool_a", "inputSchema": {}},
            {"name": "tool_b", "inputSchema": {}},
        ]

        result = asyncio.run(on_server_reconnect("fs", servers["fs"], reconnect_tools))
        assert result.is_ok

        # Exactly one notification should fire
        assert len(notified) == 1, (
            f"Expected 1 notification callback, got {len(notified)}. "
            f"Reconnect must fire notification only via on_tools_changed delegation."
        )
        assert notified[0].startswith("sha256:")
    finally:
        set_notify_callback(None)
        _teardown()


def test_on_server_reconnect_preserves_other_servers_tools() -> None:
    """Reconnect of one server does not affect other servers' tools in registry."""
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

    # Reconnect only 'fs' server
    reconnect_tools = [
        {"name": "read_file", "inputSchema": {}},
        {"name": "write_file", "inputSchema": {}},
    ]

    result = asyncio.run(on_server_reconnect("fs", servers["fs"], reconnect_tools))
    assert result.is_ok

    # fs tools updated
    assert get_tool_server("read_file").value == "fs"
    assert get_tool_server("write_file").value == "fs"
    # git tools preserved
    assert get_tool_server("git_status").value == "git"

    _teardown()


def test_reconnect_does_not_trigger_list_tools_via_on_server_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify on_server_reconnect path does not trigger list_tools on downstream.

    This is a failure-path regression test: if on_server_reconnect incorrectly
    calls re_enumerate (which calls _enumerate_tools), this test will fail,
    detecting the duplicate re_enumerate/list_tools bug.
    """
    from typing import Any
    from tela.shell.result import Result

    downstream_list_tools_calls: list[str] = []

    async def _fake_re_enumerate(server_name: str) -> Result[list[Any], str]:
        # This tracks if re_enumerate is wrongly called during reconnect
        downstream_list_tools_calls.append(f"re_enumerate:{server_name}")
        from tela.shell.downstream import get_registry

        return Result(value=get_registry().get_all_tools().get(server_name, []))

    monkeypatch.setattr("tela.shell.downstream.re_enumerate", _fake_re_enumerate)

    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(servers, tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]})
    )

    reconnect_tools = [{"name": "tool_a", "inputSchema": {}}]

    result = asyncio.run(on_server_reconnect("fs", servers["fs"], reconnect_tools))
    assert result.is_ok

    # Assert no re_enumerate was triggered during reconnect
    # (reconnect already has fresh tools from _handle_reconnect)
    assert downstream_list_tools_calls == [], (
        f"Duplicate enumeration path detected: {downstream_list_tools_calls}. "
        f"on_server_reconnect must not call re_enumerate - it must use "
        f"the tool_list already provided by _handle_reconnect."
    )

    _teardown()


# --- Prefix-driven tool-surface change tests ---


def test_on_tools_changed_prefix_change_updates_registry_and_notifies() -> None:
    """on_tools_changed with prefix change updates registry and emits notification.

    A prefix-only change on a single server counts as a tool-surface change
    and must trigger tools/list_changed notification.
    """
    notified: list[str] = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    set_notify_callback(capture_notify)

    old_config_ref = _get_config()

    # Setup: fs with no prefix
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(
            servers,
            tool_lists={"fs": [{"name": "read_file", "inputSchema": {}}]},
        )
    )
    _set_config(TelaConfig(servers=servers))

    try:
        # Update: same raw tools but now with prefix
        updated_server_config = ServerConfig(
            name="fs", command="cmd", tool_prefix="fs."
        )
        result = asyncio.run(
            on_tools_changed(
                "fs",
                updated_server_config,
                [{"name": "read_file", "inputSchema": {}}],
            )
        )
        assert result.is_ok

        # Registry updated with prefixed exposed name
        assert get_tool_server("fs.read_file").value == "fs"

        # Notification fired
        assert len(notified) == 1
        assert notified[0].startswith("sha256:")
    finally:
        set_notify_callback(None)
        _set_config(old_config_ref)
        _teardown()


def test_prefix_change_produces_different_digest() -> None:
    """Changing prefix produces a different digest from the original.

    The digest keys off exposed names, so changing prefix changes digest.
    """
    notified: list[str] = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    set_notify_callback(capture_notify)

    old_config_ref = _get_config()

    # Setup: fs with no prefix
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(
        connect_all(
            servers,
            tool_lists={"fs": [{"name": "read_file", "inputSchema": {}}]},
        )
    )
    _set_config(TelaConfig(servers=servers))

    # Clear notifications from initial connect
    notified.clear()

    try:
        # Update: same raw tools but now with prefix
        updated_server_config = ServerConfig(
            name="fs", command="cmd", tool_prefix="fs."
        )
        result = asyncio.run(
            on_tools_changed(
                "fs",
                updated_server_config,
                [{"name": "read_file", "inputSchema": {}}],
            )
        )
        assert result.is_ok
        assert len(notified) == 1

        # The digest is based on exposed names (fs.read_file), not raw names
        import hashlib

        tool_names = sorted(["fs.read_file"])
        raw = ":".join(tool_names).encode()
        expected = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        assert notified[0] == expected
    finally:
        set_notify_callback(None)
        _set_config(old_config_ref)
        _teardown()
