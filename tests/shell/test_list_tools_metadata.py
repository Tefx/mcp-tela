"""Tests for mcp_types.Tool construction in _list_tools.

Tests verify that metadata fields (title, outputSchema, annotations)
are passed from ResolvedTool through handle_tools_list and _list_tools.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp import types as mcp_types

from tela.core.models import (
    AuthConfig,
    AuthMode,
    Posture,
    ProfileConfig,
    ResolvedTool,
    TelaConfig,
)
from tela.shell.config_loader import Result
from tela.shell.downstream_registry import DownstreamRegistry
from tela.shell.gateway import get_runtime
from tela.shell.upstream import handle_tools_list, handle_initialize


# --- _list_tools mcp_types.Tool construction tests ---
# NOTE: These tests verify the _list_tools path indirectly by testing
# handle_tools_list output format, which _list_tools consumes.


def test_list_tools_output_dict_includes_all_metadata_fields() -> None:
    """handle_tools_list output dict includes all metadata fields for mcp_types.Tool."""
    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name="read_file",
                server_name="fs",
                family="fs",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
                description="Read a file",
                title="File Reader",
                output_schema={"type": "string"},
                annotations={"readOnlyHint": True},
            )
        ],
    )

    get_runtime().config = TelaConfig(
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="dev",
        profiles={
            "dev": ProfileConfig(
                name="dev", default=True, capabilities={"fs": Posture.READ_ONLY}
            )
        },
    )
    get_runtime().connections.clear()

    import tela.shell.downstream

    original_get_all_tools = tela.shell.downstream.get_all_tools
    tela.shell.downstream.get_all_tools = lambda: Result(value=registry.get_all_tools())

    try:
        result = asyncio.run(handle_initialize({"client": "test"}))
        assert result.is_ok
        conn = result.value
        assert conn is not None

        tools_result = asyncio.run(handle_tools_list(conn))
        assert tools_result.is_ok
        assert tools_result.value is not None
        tool_dict = tools_result.value[0]

        # These fields must be present for _list_tools to construct mcp_types.Tool properly
        assert tool_dict["name"] == "read_file"
        assert tool_dict["inputSchema"] == {"type": "object"}
        assert tool_dict["description"] == "Read a file"
        assert tool_dict["title"] == "File Reader"
        assert tool_dict["outputSchema"] == {"type": "string"}
        assert tool_dict["annotations"] == {"readOnlyHint": True}
    finally:
        tela.shell.downstream.get_all_tools = original_get_all_tools


def test_mcp_types_tool_accepts_metadata_fields() -> None:
    """mcp_types.Tool constructor accepts title, outputSchema, annotations."""
    # This is a contract test - verifying the MCP library accepts these fields
    tool = mcp_types.Tool(
        name="test_tool",
        inputSchema={"type": "object"},
        description="A test tool",
        title="Test Tool Title",
        outputSchema={"type": "string"},
        annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
    )
    assert tool.name == "test_tool"
    assert tool.inputSchema == {"type": "object"}
    assert tool.description == "A test tool"
    assert tool.title == "Test Tool Title"
    assert tool.outputSchema == {"type": "string"}
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True


def test_mcp_types_tool_optional_metadata_fields() -> None:
    """mcp_types.Tool works with None/missing metadata fields."""
    # title, outputSchema, annotations are optional
    tool = mcp_types.Tool(
        name="test_tool",
        inputSchema={"type": "object"},
    )
    assert tool.name == "test_tool"
    assert tool.title is None
    assert tool.outputSchema is None
    assert tool.annotations is None
