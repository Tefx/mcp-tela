"""Pure manifest-header formatting for surface instructions."""

from __future__ import annotations

from tela.core.contracts import post, pre
from tela.core.models import ResolvedTool, ServerConfig


@pre(
    lambda servers, connected_names, tools_by_server: (
        isinstance(servers, dict)
        and isinstance(connected_names, set)
        and isinstance(tools_by_server, dict)
    )
)
@post(
    lambda result: (
        isinstance(result, str) and result.startswith("Connected at startup:")
    )
)
def build_manifest_header(
    servers: dict[str, ServerConfig],
    connected_names: set[str],
    tools_by_server: dict[str, list[ResolvedTool]],
) -> str:
    """Build provider manifest header for gateway instructions.

    Examples:
        >>> servers = {"fs": ServerConfig(name="fs", command="cmd")}
        >>> tools = {"fs": [ResolvedTool(name="read_file", server_name="fs", family="fs")]}
        >>> build_manifest_header(servers, {"fs"}, tools)
        'Connected at startup: fs (1 tools)'
    """

    parts: list[str] = []
    for server_name in sorted(servers.keys()):
        if server_name in connected_names:
            tool_count = len(tools_by_server.get(server_name, []))
            parts.append(f"{server_name} ({tool_count} tools)")
    if not parts:
        return "Connected at startup: (none)"
    return "Connected at startup: " + ", ".join(parts)
