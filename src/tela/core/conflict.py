"""Tool conflict detection across downstream servers.

Detects tool name conflicts where multiple servers expose tools with the
same name. Does NOT decide what to do about conflicts (caller decides).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from pydantic import BaseModel

from tela.core.models import ResolvedTool

pre: Callable[[Callable[..., bool]], Callable[[Any], Any]] = lambda _predicate: (
    lambda func: func
)
post: Callable[[Callable[[Any], bool]], Callable[[Any], Any]] = lambda _predicate: (
    lambda func: func
)


class ToolConflict(BaseModel):
    """A tool name conflict across multiple servers."""

    tool_name: str
    servers: list[str]


@pre(lambda all_tools: isinstance(all_tools, dict))
@post(lambda result: isinstance(result, list))
def detect_conflicts(
    all_tools: dict[str, list[ResolvedTool]],
) -> list[ToolConflict]:
    """Detect tool name conflicts across servers.

    Input: dict mapping server_name -> list of ResolvedTools.
    Returns list of ToolConflict (tool_name, server_names involved).

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
        >>> sorted(conflicts[0].servers)
        ['fs1', 'fs2']
        >>> detect_conflicts({"a": [ResolvedTool(name="t1", server_name="a", family="a")]})
        []

    Args:
        all_tools: Server name to resolved tool list mapping.

    Returns:
        List of ToolConflict instances for conflicting tool names.
    """

    tool_owners: dict[str, list[str]] = defaultdict(list)

    for server_name, tools in all_tools.items():
        for tool in tools:
            tool_owners[tool.name].append(server_name)

    conflicts = []
    for tool_name, servers in sorted(tool_owners.items()):
        if len(servers) > 1:
            conflicts.append(ToolConflict(tool_name=tool_name, servers=servers))

    return conflicts
