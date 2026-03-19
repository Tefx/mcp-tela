"""Downstream resolved-tool registry.

Extracted from ``tela.shell.downstream`` to keep runtime orchestration
module size below maintainability limits while preserving behavior.
"""

from __future__ import annotations

from tela.core.models import ResolvedTool


class DownstreamRegistry:
    """In-memory registry of resolved tools from downstream servers.

    Provides lookup by tool name and server name. The registry is populated
    during connect_all and can be re-enumerated for hot reload.
    """

    def __init__(self) -> None:
        self._tools_by_server: dict[str, list[ResolvedTool]] = {}
        self._tool_to_server: dict[str, str] = {}

    def register(self, server_name: str, tools: list[ResolvedTool]) -> None:
        """Register resolved tools for a server, updating the flat lookup."""
        self.unregister(server_name)
        self._tools_by_server[server_name] = tools
        for tool in tools:
            self._tool_to_server[tool.name] = server_name

    def unregister(self, server_name: str) -> None:
        """Remove all tools for a server from the registry."""
        tools = self._tools_by_server.pop(server_name, [])
        for tool in tools:
            if self._tool_to_server.get(tool.name) == server_name:
                del self._tool_to_server[tool.name]

    def get_all_tools(self) -> dict[str, list[ResolvedTool]]:
        """Return all resolved tools grouped by server name."""
        return dict(self._tools_by_server)

    def get_tool_server(self, tool_name: str) -> str | None:
        """Look up which server owns a given tool name."""
        return self._tool_to_server.get(tool_name)

    def get_tool(self, tool_name: str) -> ResolvedTool | None:
        """Look up a resolved tool by name."""
        server = self._tool_to_server.get(tool_name)
        if server is None:
            return None
        for tool in self._tools_by_server.get(server, []):
            if tool.name == tool_name:
                return tool
        return None

    def snapshot(self) -> tuple[dict[str, list[ResolvedTool]], dict[str, str]]:
        """Snapshot full registry state for atomic rollback."""
        return (
            {k: list(v) for k, v in self._tools_by_server.items()},
            dict(self._tool_to_server),
        )

    def restore(
        self, snap: tuple[dict[str, list[ResolvedTool]], dict[str, str]]
    ) -> None:
        """Restore full registry state from snapshot (atomic rollback)."""
        tools_by_server, tool_to_server = snap
        self._tools_by_server = {k: list(v) for k, v in tools_by_server.items()}
        self._tool_to_server = dict(tool_to_server)

    def clear(self) -> None:
        """Clear all registry entries."""
        self._tools_by_server.clear()
        self._tool_to_server.clear()
