"""Tool conflict detection across downstream servers.

Detects tool name conflicts where multiple servers expose tools with the
same name. Does NOT decide what to do about conflicts (caller decides).
"""

from __future__ import annotations

from collections import defaultdict
from enum import Enum

from tela.core.contracts import pre, post
from pydantic import BaseModel

from tela.core.models import ResolvedTool


class ConflictType(str, Enum):
    """Type of tool conflict."""

    NAME_COLLISION = "name_collision"
    PREFIX_VIOLATION = "prefix_violation"


class ToolConflict(BaseModel):
    """A tool name conflict across multiple servers."""

    tool_name: str
    servers: list[str]
    conflict_type: ConflictType = ConflictType.NAME_COLLISION


RESERVED_PREFIX = "tela."
"""Prefix reserved for tela-owned surfaces."""

INTROSPECTION_TOOLS = ("tela_list_profiles",)
"""Currently supported built-in tela MCP surface names."""


@pre(lambda all_tools: isinstance(all_tools, dict))
@post(lambda result: isinstance(result, list))
def detect_conflicts(
    all_tools: dict[str, list[ResolvedTool]],
) -> list[ToolConflict]:
    """Detect tool name conflicts across servers.

    Input: dict mapping server_name -> list of ResolvedTools.
    Returns list of ToolConflict (tool_name, server_names involved).

    Detects two conflict types:
    1. NAME_COLLISION: multiple servers expose the same tool name
    2. PREFIX_VIOLATION: downstream tool uses reserved "tela." prefix

    Examples:
        >>> from tela.core.models import ResolvedTool
        >>> tools = {
        ...     "fs1": [ResolvedTool(name="read_file", server_name="fs1", family="fs1")],
        ...     "fs2": [ResolvedTool(name="read_file", server_name="fs2", family="fs2")],
        ... }
        >>> conflicts = detect_conflicts(tools)
        >>> len(conflicts)
        1
        >>> conflicts[0].tool_name
        'read_file'
        >>> conflicts[0].conflict_type
        <ConflictType.NAME_COLLISION: 'name_collision'>
        >>> sorted(conflicts[0].servers)
        ['fs1', 'fs2']
        >>> detect_conflicts({"a": [ResolvedTool(name="t1", server_name="a", family="a")]})
        []
        >>> # tela.* prefix from single server is rejected as PREFIX_VIOLATION
        >>> conflicts = detect_conflicts({
        ...     "srv": [ResolvedTool(name="tela.custom", server_name="srv", family="srv")],
        ... })
        >>> len(conflicts)
        1
        >>> conflicts[0].tool_name
        'tela.custom'
        >>> conflicts[0].conflict_type
        <ConflictType.PREFIX_VIOLATION: 'prefix_violation'>
        >>> conflicts[0].servers
        ['srv']

    Args:
        all_tools: Server name to resolved tool list mapping.

    Returns:
        List of ToolConflict instances for conflicting tool names.

    # NOTE: Conflict detection keys off each tool's final exposed upstream name
    # (``ResolvedTool.name``), not the raw downstream inventory name.
    # NOTE: Reserved-prefix rejection applies to any exposed name produced by a
    # downstream raw name, a configured tool_prefix, or their combination.
    """

    tool_owners: dict[str, list[str]] = defaultdict(list)

    for server_name, tools in all_tools.items():
        for tool in tools:
            tool_owners[tool.name].append(server_name)

    conflicts: list[ToolConflict] = []

    # Check for prefix violations first (single server can cause this)
    for tool_name, servers in sorted(tool_owners.items()):
        unique_servers = sorted(set(servers))
        if tool_name.startswith(RESERVED_PREFIX):
            conflicts.append(
                ToolConflict(
                    tool_name=tool_name,
                    servers=unique_servers,
                    conflict_type=ConflictType.PREFIX_VIOLATION,
                )
            )

    # Check for name collisions (multiple servers with same name)
    for tool_name, servers in sorted(tool_owners.items()):
        unique_servers = sorted(set(servers))
        if len(unique_servers) > 1 and not tool_name.startswith(RESERVED_PREFIX):
            # PREFIX_VIOLATION already handled above; don't double-report
            conflicts.append(
                ToolConflict(
                    tool_name=tool_name,
                    servers=unique_servers,
                    conflict_type=ConflictType.NAME_COLLISION,
                )
            )

    return conflicts
