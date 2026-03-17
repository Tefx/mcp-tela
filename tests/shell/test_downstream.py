"""Runtime tests for downstream server management.

Tests cover:
- Tool registry lookup behavior (connect, enumerate, lookup, disconnect)
- ServerConfig shapes for stdio and SSE servers
- ResolvedTool model behavior and fields
- Tool conflict detection at startup
- Registry lifecycle (connect_all, disconnect_all)
"""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import Posture, ResolvedTool, ServerConfig, ToolOverride
from tela.shell.downstream import (
    call_tool,
    connect_all,
    disconnect_all,
    get_all_tools,
    get_registry,
    get_tool_server,
    re_enumerate,
)


# --- ServerConfig model tests ---


def test_server_config_stdio_shape() -> None:
    config = ServerConfig(
        name="filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )
    assert config.command == "npx"
    assert config.url is None
    assert len(config.args) == 3
    assert config.default_posture == Posture.NONE


def test_server_config_sse_shape() -> None:
    config = ServerConfig(name="remote", url="http://localhost:8080/sse")
    assert config.url == "http://localhost:8080/sse"
    assert config.command is None


def test_server_config_with_tool_overrides() -> None:
    config = ServerConfig(
        name="srv",
        command="cmd",
        tool_overrides={
            "dangerous_tool": ToolOverride(posture=Posture.DESTRUCTIVE),
            "reassigned_tool": ToolOverride(family="custom_family"),
        },
    )
    assert config.tool_overrides["dangerous_tool"].posture == Posture.DESTRUCTIVE
    assert config.tool_overrides["reassigned_tool"].family == "custom_family"


def test_server_config_explicit_family_override() -> None:
    config = ServerConfig(name="srv", command="cmd", family="custom_family")
    assert config.family == "custom_family"


def test_server_config_default_posture_options() -> None:
    for posture in Posture:
        config = ServerConfig(name="srv", command="cmd", default_posture=posture)
        assert config.default_posture == posture


# --- ResolvedTool model tests ---


def test_resolved_tool_carries_server_and_family() -> None:
    tool = ResolvedTool(
        name="read_file",
        server_name="filesystem",
        family="filesystem",
        posture=Posture.READ_ONLY,
        schema_={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    assert tool.name == "read_file"
    assert tool.server_name == "filesystem"
    assert tool.family == "filesystem"
    assert tool.posture == Posture.READ_ONLY


def test_resolved_tool_unclassified_posture() -> None:
    tool = ResolvedTool(name="unknown_tool", server_name="srv", family="srv")
    assert tool.posture is None
    assert tool.schema_ == {}


def test_resolved_tool_registry_grouping() -> None:
    tools = {
        "filesystem": [
            ResolvedTool(name="read_file", server_name="filesystem", family="filesystem"),
            ResolvedTool(name="write_file", server_name="filesystem", family="filesystem"),
        ],
        "git": [
            ResolvedTool(name="git_status", server_name="git", family="git"),
        ],
    }
    flat = {t.name: srv for srv, ts in tools.items() for t in ts}
    assert flat["read_file"] == "filesystem"
    assert flat["git_status"] == "git"


# --- connect_all / disconnect_all / registry lifecycle ---


def test_connect_all_registers_tools() -> None:
    """connect_all populates the registry with resolved tools."""
    servers = {
        "fs": ServerConfig(name="fs", command="cmd"),
        "git": ServerConfig(name="git", command="cmd"),
    }
    tool_lists = {
        "fs": [
            {"name": "read_file", "inputSchema": {"type": "object"}},
            {"name": "write_file", "inputSchema": {"type": "object"}},
        ],
        "git": [
            {"name": "git_status", "inputSchema": {}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    assert get_tool_server("read_file") == "fs"
    assert get_tool_server("write_file") == "fs"
    assert get_tool_server("git_status") == "git"
    assert get_tool_server("nonexistent") is None

    all_tools = get_all_tools()
    assert len(all_tools["fs"]) == 2
    assert len(all_tools["git"]) == 1


def test_connect_all_resolves_families() -> None:
    """connect_all uses Core family resolution for tool family assignment."""
    servers = {
        "srv": ServerConfig(
            name="srv",
            command="cmd",
            family="custom_family",
            tool_overrides={"special": ToolOverride(family="override_family")},
        ),
    }
    tool_lists = {
        "srv": [
            {"name": "normal_tool", "inputSchema": {}},
            {"name": "special", "inputSchema": {}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    normal = registry.get_tool("normal_tool")
    assert normal is not None
    assert normal.family == "custom_family"

    special = registry.get_tool("special")
    assert special is not None
    assert special.family == "override_family"


def test_connect_all_resolves_posture_from_overrides() -> None:
    """connect_all uses Core classification for posture from tool overrides."""
    servers = {
        "srv": ServerConfig(
            name="srv",
            command="cmd",
            tool_overrides={"dangerous": ToolOverride(posture=Posture.DESTRUCTIVE)},
        ),
    }
    tool_lists = {
        "srv": [
            {"name": "dangerous", "inputSchema": {}},
            {"name": "unclassified", "inputSchema": {}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    assert registry.get_tool("dangerous") is not None
    assert registry.get_tool("dangerous").posture == Posture.DESTRUCTIVE
    assert registry.get_tool("unclassified") is not None
    assert registry.get_tool("unclassified").posture is None


def test_connect_all_resolves_posture_from_annotations() -> None:
    """connect_all uses Core classification for posture from MCP annotations."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {
        "srv": [
            {"name": "reader", "inputSchema": {}, "annotations": {"readOnlyHint": True}},
            {"name": "destroyer", "inputSchema": {}, "annotations": {"destructiveHint": True}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    assert registry.get_tool("reader").posture == Posture.READ_ONLY
    assert registry.get_tool("destroyer").posture == Posture.DESTRUCTIVE


def test_connect_all_fails_on_tool_conflict() -> None:
    """connect_all fails fast when two servers expose the same tool name."""
    servers = {
        "fs1": ServerConfig(name="fs1", command="cmd1"),
        "fs2": ServerConfig(name="fs2", command="cmd2"),
    }
    tool_lists = {
        "fs1": [{"name": "read_file", "inputSchema": {}}],
        "fs2": [{"name": "read_file", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_err
    assert "TOOL_CONFLICT" in (result.error or "")
    assert "read_file" in (result.error or "")

    # Registry must be cleared on conflict
    assert get_all_tools() == {}


def test_connect_all_no_conflict_different_names() -> None:
    """connect_all succeeds when servers have unique tool names."""
    servers = {
        "fs": ServerConfig(name="fs", command="cmd1"),
        "git": ServerConfig(name="git", command="cmd2"),
    }
    tool_lists = {
        "fs": [{"name": "read_file", "inputSchema": {}}],
        "git": [{"name": "git_status", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok


def test_disconnect_all_clears_registry() -> None:
    """disconnect_all removes all tools from the registry."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {"srv": [{"name": "tool", "inputSchema": {}}]}
    asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert get_tool_server("tool") == "srv"

    result = asyncio.run(disconnect_all())
    assert result.is_ok
    assert get_tool_server("tool") is None
    assert get_all_tools() == {}


def test_connect_all_empty_servers() -> None:
    """connect_all with no servers produces empty registry."""
    result = asyncio.run(connect_all({}))
    assert result.is_ok
    assert get_all_tools() == {}


# --- Remaining stubs ---


def test_call_tool_returns_error() -> None:
    r = asyncio.run(call_tool("srv", "tool", {}))
    assert r.is_err


def test_re_enumerate_returns_error() -> None:
    r = asyncio.run(re_enumerate("srv"))
    assert r.is_err
