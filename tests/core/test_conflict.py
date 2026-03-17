"""Tests for tool conflict detection."""

from __future__ import annotations

from tela.core.conflict import ToolConflict, detect_conflicts
from tela.core.models import ResolvedTool


def test_detect_conflicts_finds_duplicates() -> None:
    tools = {
        "fs1": [ResolvedTool(name="read_file", server_name="fs1", family="fs1")],
        "fs2": [ResolvedTool(name="read_file", server_name="fs2", family="fs2")],
    }
    conflicts = detect_conflicts(tools)
    assert len(conflicts) == 1
    assert conflicts[0].tool_name == "read_file"
    assert sorted(conflicts[0].servers) == ["fs1", "fs2"]


def test_detect_conflicts_no_duplicates() -> None:
    tools = {
        "fs": [ResolvedTool(name="read_file", server_name="fs", family="fs")],
        "git": [ResolvedTool(name="git_status", server_name="git", family="git")],
    }
    assert detect_conflicts(tools) == []


def test_detect_conflicts_empty_input() -> None:
    assert detect_conflicts({}) == []


def test_detect_conflicts_multiple_conflicts() -> None:
    tools = {
        "a": [
            ResolvedTool(name="tool1", server_name="a", family="a"),
            ResolvedTool(name="tool2", server_name="a", family="a"),
        ],
        "b": [
            ResolvedTool(name="tool1", server_name="b", family="b"),
            ResolvedTool(name="tool2", server_name="b", family="b"),
        ],
    }
    conflicts = detect_conflicts(tools)
    assert len(conflicts) == 2
    names = {c.tool_name for c in conflicts}
    assert names == {"tool1", "tool2"}


def test_tool_conflict_model() -> None:
    c = ToolConflict(tool_name="t", servers=["a", "b"])
    assert c.tool_name == "t"
    assert c.servers == ["a", "b"]
