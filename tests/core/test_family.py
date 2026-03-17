"""Tests for family mapping."""

from __future__ import annotations

from tela.core.family import resolve_family, resolve_tools
from tela.core.models import Posture, ServerConfig, ToolOverride


def test_resolve_family_default_is_server_name() -> None:
    cfg = ServerConfig(name="git", command="cmd")
    assert resolve_family("git_status", cfg) == "git"


def test_resolve_family_server_level_override() -> None:
    cfg = ServerConfig(name="srv", command="cmd", family="custom")
    assert resolve_family("any", cfg) == "custom"


def test_resolve_family_tool_level_override() -> None:
    cfg = ServerConfig(
        name="srv", command="cmd", family="custom",
        tool_overrides={"special": ToolOverride(family="override")},
    )
    assert resolve_family("special", cfg) == "override"
    assert resolve_family("normal", cfg) == "custom"


def test_resolve_tools_basic() -> None:
    cfg = ServerConfig(name="fs", command="cmd")
    tools = resolve_tools("fs", cfg, [
        {"name": "read_file", "inputSchema": {"type": "object"}},
        {"name": "write_file", "inputSchema": {}},
    ])
    assert len(tools) == 2
    assert tools[0].name == "read_file"
    assert tools[0].family == "fs"
    assert tools[0].server_name == "fs"


def test_resolve_tools_with_annotations() -> None:
    cfg = ServerConfig(name="srv", command="cmd")
    tools = resolve_tools("srv", cfg, [
        {"name": "reader", "inputSchema": {}, "annotations": {"readOnlyHint": True}},
    ])
    assert tools[0].posture == Posture.READ_ONLY


def test_resolve_tools_empty_list() -> None:
    cfg = ServerConfig(name="srv", command="cmd")
    assert resolve_tools("srv", cfg, []) == []
