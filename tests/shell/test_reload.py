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

def test_on_config_changed_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(on_config_changed(TelaConfig()))
