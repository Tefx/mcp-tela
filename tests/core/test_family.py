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
        name="srv",
        command="cmd",
        family="custom",
        tool_overrides={"special": ToolOverride(family="override")},
    )
    assert resolve_family("special", cfg) == "override"
    assert resolve_family("normal", cfg) == "custom"


def test_resolve_tools_basic() -> None:
    cfg = ServerConfig(name="fs", command="cmd")
    tools = resolve_tools(
        "fs",
        cfg,
        [
            {"name": "read_file", "inputSchema": {"type": "object"}},
            {"name": "write_file", "inputSchema": {}},
        ],
    )
    assert len(tools) == 2
    assert tools[0].name == "read_file"
    assert tools[0].family == "fs"
    assert tools[0].server_name == "fs"


def test_resolve_tools_with_annotations() -> None:
    cfg = ServerConfig(name="srv", command="cmd")
    tools = resolve_tools(
        "srv",
        cfg,
        [
            {
                "name": "reader",
                "inputSchema": {},
                "annotations": {"readOnlyHint": True},
            },
        ],
    )
    assert tools[0].posture == Posture.READ_ONLY


def test_resolve_tools_empty_list() -> None:
    cfg = ServerConfig(name="srv", command="cmd")
    assert resolve_tools("srv", cfg, []) == []


# --- Metadata field extraction tests ---


def test_resolve_tools_extracts_title_present() -> None:
    """resolve_tools extracts title field when present."""
    cfg = ServerConfig(name="srv", command="cmd")
    tools = resolve_tools(
        "srv",
        cfg,
        [
            {"name": "reader", "inputSchema": {}, "title": "File Reader"},
        ],
    )
    assert len(tools) == 1
    assert tools[0].title == "File Reader"


def test_resolve_tools_extracts_title_absent() -> None:
    """resolve_tools sets title to None when absent."""
    cfg = ServerConfig(name="srv", command="cmd")
    tools = resolve_tools(
        "srv",
        cfg,
        [
            {"name": "reader", "inputSchema": {}},
        ],
    )
    assert len(tools) == 1
    assert tools[0].title is None


def test_resolve_tools_extracts_output_schema_present() -> None:
    """resolve_tools extracts outputSchema field when present."""
    cfg = ServerConfig(name="srv", command="cmd")
    tools = resolve_tools(
        "srv",
        cfg,
        [
            {"name": "reader", "inputSchema": {}, "outputSchema": {"type": "object"}},
        ],
    )
    assert len(tools) == 1
    assert tools[0].output_schema == {"type": "object"}


def test_resolve_tools_extracts_output_schema_absent() -> None:
    """resolve_tools sets output_schema to None when absent."""
    cfg = ServerConfig(name="srv", command="cmd")
    tools = resolve_tools(
        "srv",
        cfg,
        [
            {"name": "reader", "inputSchema": {}},
        ],
    )
    assert len(tools) == 1
    assert tools[0].output_schema is None


def test_resolve_tools_extracts_annotations_preserves_full_dict() -> None:
    """resolve_tools preserves full annotations dict, not just posture hints."""
    cfg = ServerConfig(name="srv", command="cmd")
    tools = resolve_tools(
        "srv",
        cfg,
        [
            {
                "name": "reader",
                "inputSchema": {},
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            },
        ],
    )
    assert len(tools) == 1
    # Posture is derived from readOnlyHint for classification
    assert tools[0].posture == Posture.READ_ONLY
    # Full annotations dict should also be preserved
    assert tools[0].annotations == {"readOnlyHint": True, "destructiveHint": False}


def test_resolve_tools_extracts_all_metadata_partial() -> None:
    """resolve_tools handles partial metadata presence."""
    cfg = ServerConfig(name="srv", command="cmd")
    tools = resolve_tools(
        "srv",
        cfg,
        [
            {"name": "tool1", "inputSchema": {}, "title": "Tool One"},
            {"name": "tool2", "inputSchema": {}, "outputSchema": {"type": "object"}},
            {"name": "tool3", "inputSchema": {}, "annotations": {"readOnlyHint": True}},
            {"name": "tool4", "inputSchema": {}},
        ],
    )
    assert len(tools) == 4
    # tool1: title only
    assert tools[0].title == "Tool One"
    assert tools[0].output_schema is None
    assert tools[0].annotations is None
    # tool2: outputSchema only
    assert tools[1].title is None
    assert tools[1].output_schema == {"type": "object"}
    assert tools[1].annotations is None
    # tool3: annotations only
    assert tools[2].title is None
    assert tools[2].output_schema is None
    assert tools[2].annotations == {"readOnlyHint": True}
    # tool4: none
    assert tools[3].title is None
    assert tools[3].output_schema is None
    assert tools[3].annotations is None
