"""Runtime lifecycle tests for downstream server management.

Tests cover:
- Tool registry lookup behavior (model-level)
- ServerConfig shapes for stdio and SSE servers
- ResolvedTool model behavior and fields
- Tool conflict detection inputs
- Downstream stub contracts
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
    get_tool_server,
    re_enumerate,
)


# --- ServerConfig model tests ---


def test_server_config_stdio_shape() -> None:
    """Stdio server config carries command and args."""
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
    """SSE server config carries url, no command."""
    config = ServerConfig(
        name="remote",
        url="http://localhost:8080/sse",
    )
    assert config.url == "http://localhost:8080/sse"
    assert config.command is None


def test_server_config_with_tool_overrides() -> None:
    """Server config can carry per-tool family and posture overrides."""
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
    """Server-level family override applies to all tools from that server."""
    config = ServerConfig(
        name="srv",
        command="cmd",
        family="custom_family",
    )
    assert config.family == "custom_family"


def test_server_config_default_posture_options() -> None:
    """Default posture can be set to any Posture value."""
    for posture in Posture:
        config = ServerConfig(name="srv", command="cmd", default_posture=posture)
        assert config.default_posture == posture


# --- ResolvedTool model tests ---


def test_resolved_tool_carries_server_and_family() -> None:
    """ResolvedTool tracks which server owns the tool and its family."""
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
    assert "path" in tool.schema_["properties"]


def test_resolved_tool_unclassified_posture() -> None:
    """Unclassified tool has posture=None."""
    tool = ResolvedTool(
        name="unknown_tool",
        server_name="srv",
        family="srv",
    )
    assert tool.posture is None
    assert tool.schema_ == {}


def test_resolved_tool_conflict_detection_inputs() -> None:
    """Two tools with same name from different servers represent a conflict.

    This tests the model input shape for conflict detection. The actual
    detection logic belongs in core/conflict.py (not yet implemented).
    """
    tool_a = ResolvedTool(name="list_files", server_name="fs1", family="fs1")
    tool_b = ResolvedTool(name="list_files", server_name="fs2", family="fs2")
    # Same name, different servers: this is a conflict input
    assert tool_a.name == tool_b.name
    assert tool_a.server_name != tool_b.server_name


def test_resolved_tool_registry_grouping() -> None:
    """Tools can be grouped by server name for registry lookup.

    This validates the dict[str, list[ResolvedTool]] registry shape
    that get_all_tools will return once implemented.
    """
    tools = {
        "filesystem": [
            ResolvedTool(name="read_file", server_name="filesystem", family="filesystem"),
            ResolvedTool(name="write_file", server_name="filesystem", family="filesystem"),
        ],
        "git": [
            ResolvedTool(name="git_status", server_name="git", family="git"),
        ],
    }
    assert len(tools["filesystem"]) == 2
    assert len(tools["git"]) == 1
    # Flat lookup: find server for a tool name
    flat = {t.name: srv for srv, ts in tools.items() for t in ts}
    assert flat["read_file"] == "filesystem"
    assert flat["git_status"] == "git"


# --- Downstream stub contracts (preserved from contract phase) ---


def test_connect_all_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(connect_all({}))


def test_disconnect_all_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(disconnect_all())


def test_call_tool_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(call_tool("srv", "tool", {}))


def test_get_all_tools_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        get_all_tools()


def test_get_tool_server_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        get_tool_server("some_tool")


def test_re_enumerate_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(re_enumerate("srv"))
