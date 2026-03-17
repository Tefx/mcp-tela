"""Tests for hot reload re-enumeration and conflict rejection."""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import ServerConfig, TelaConfig
from tela.shell.downstream import connect_all, disconnect_all, get_tool_server
from tela.shell.reload import on_config_changed, on_server_reconnect, on_tools_changed


def _setup_single_server() -> dict[str, ServerConfig]:
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(connect_all(servers, tool_lists={"fs": [{"name": "read_file", "inputSchema": {}}]}))
    return servers


def _teardown() -> None:
    asyncio.run(disconnect_all())


# --- on_tools_changed: accepted ---

def test_on_tools_changed_adds_new_tool() -> None:
    servers = _setup_single_server()
    result = asyncio.run(on_tools_changed(
        "fs", servers["fs"],
        [{"name": "read_file", "inputSchema": {}}, {"name": "write_file", "inputSchema": {}}],
    ))
    assert result.is_ok
    assert get_tool_server("write_file") == "fs"
    _teardown()


def test_on_tools_changed_removes_old_tool() -> None:
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(connect_all(servers, tool_lists={
        "fs": [{"name": "tool_a", "inputSchema": {}}, {"name": "tool_b", "inputSchema": {}}]
    }))
    result = asyncio.run(on_tools_changed("fs", servers["fs"], [{"name": "tool_a", "inputSchema": {}}]))
    assert result.is_ok
    assert get_tool_server("tool_a") == "fs"
    assert get_tool_server("tool_b") is None
    _teardown()


# --- on_tools_changed: conflict rejected ---

def test_on_tools_changed_rejects_conflict() -> None:
    servers = {
        "fs": ServerConfig(name="fs", command="cmd"),
        "custom": ServerConfig(name="custom", command="cmd2"),
    }
    asyncio.run(connect_all(servers, tool_lists={
        "fs": [{"name": "read_file", "inputSchema": {}}],
        "custom": [{"name": "other_tool", "inputSchema": {}}],
    }))
    # Try to add conflicting tool to "custom"
    result = asyncio.run(on_tools_changed(
        "custom", servers["custom"],
        [{"name": "read_file", "inputSchema": {}}],  # conflicts with fs
    ))
    assert result.is_err
    assert "TOOL_CONFLICT" in (result.error or "")
    # Previous tools preserved
    assert get_tool_server("other_tool") == "custom"
    _teardown()


# --- on_server_reconnect ---

def test_on_server_reconnect_updates_tools() -> None:
    servers = _setup_single_server()
    result = asyncio.run(on_server_reconnect(
        "fs", servers["fs"],
        [{"name": "read_file", "inputSchema": {}}, {"name": "new_tool", "inputSchema": {}}],
    ))
    assert result.is_ok
    assert get_tool_server("new_tool") == "fs"
    _teardown()


# --- on_config_changed: stub ---

def test_on_config_changed_updates_runtime() -> None:
    r = asyncio.run(on_config_changed(TelaConfig()))
    assert r.is_ok


# --- Notification callback ---

def test_on_tools_changed_calls_notify_callback() -> None:
    """Accepted reload calls the notification callback with tools digest."""
    from tela.shell.reload import set_notify_callback

    notified = []

    async def capture_notify(digest: str) -> None:
        notified.append(digest)

    set_notify_callback(capture_notify)
    try:
        servers = {"fs": ServerConfig(name="fs", command="cmd")}
        asyncio.run(connect_all(servers, tool_lists={"fs": [{"name": "tool_a", "inputSchema": {}}]}))
        asyncio.run(on_tools_changed("fs", servers["fs"], [
            {"name": "tool_a", "inputSchema": {}}, {"name": "tool_b", "inputSchema": {}}
        ]))
        assert len(notified) == 1
        assert notified[0].startswith("sha256:")
        assert len(notified[0]) == len("sha256:") + 64
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
    asyncio.run(connect_all(servers, tool_lists={
        "fs": [{"name": "read_file", "inputSchema": {}}],
        "custom": [{"name": "other_tool", "inputSchema": {}}],
    }))
    asyncio.run(on_tools_changed(
        "custom", servers["custom"],
        [{"name": "read_file", "inputSchema": {}}],
    ))
    entries = get_audit_entries()
    assert len(entries) == 1
    assert entries[0].error_code == "TOOL_CONFLICT"
    clear_audit_entries()
    _teardown()
