"""Downstream server management.

Manages connections to downstream MCP servers, tool enumeration, resolved tool
registry construction, and tool call forwarding. Actual process management and
network I/O for real MCP server connections is deferred to the gateway runtime
integration step; this implementation provides the registry and lookup layer.
"""

from __future__ import annotations

from tela.core.conflict import ToolConflict, detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import ResolvedTool, ServerConfig, TelaError
from tela.shell.config_loader import Result


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
        # Remove old tools for this server first
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

    def snapshot(self) -> tuple[dict[str, list["ResolvedTool"]], dict[str, str]]:
        """Snapshot full registry state for atomic rollback.

        Returns shallow copies -- safe because ResolvedTool is immutable.
        """
        return (
            {k: list(v) for k, v in self._tools_by_server.items()},
            dict(self._tool_to_server),
        )

    def restore(self, snap: tuple[dict[str, list["ResolvedTool"]], dict[str, str]]) -> None:
        """Restore full registry state from snapshot (atomic rollback)."""
        tools_by_server, tool_to_server = snap
        self._tools_by_server = {k: list(v) for k, v in tools_by_server.items()}
        self._tool_to_server = dict(tool_to_server)

    def clear(self) -> None:
        """Clear all registry entries."""
        self._tools_by_server.clear()
        self._tool_to_server.clear()


# Module-level registry instance
_registry = DownstreamRegistry()


# @invar:allow dead_export: registry accessor used by tests and gateway integration.
# @invar:allow shell_result: returns registry object per module pattern, not a failable I/O boundary.
def get_registry() -> DownstreamRegistry:
    """Return the module-level downstream registry."""
    return _registry


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow dead_param: servers parameter used for tool enumeration.
async def connect_all(
    servers: dict[str, ServerConfig],
    tool_lists: dict[str, list[dict]] | None = None,
) -> Result[None, str]:
    """Connect to all configured downstream servers and build tool registry.

    Enumerates tools, resolves families and posture, and runs conflict detection.
    Fails fast on tool name conflicts.

    In the current implementation, actual process spawning and MCP communication
    are deferred. The ``tool_lists`` parameter allows injection of pre-enumerated
    tool lists for testing and integration.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import ServerConfig
        >>> cfg = {"fs": ServerConfig(name="fs", command="cmd")}
        >>> tools = {"fs": [{"name": "read_file", "inputSchema": {}}]}
        >>> r = asyncio.run(connect_all(cfg, tool_lists=tools))
        >>> r.is_ok
        True
        >>> get_registry().get_tool_server("read_file")
        'fs'

    Args:
        servers: Server name to configuration mapping.
        tool_lists: Optional pre-enumerated tool lists for testing.

    Returns:
        ``Result[None, str]`` on success, or error string if conflicts detected.
    """

    _registry.clear()

    all_resolved: dict[str, list[ResolvedTool]] = {}

    for server_name, server_config in servers.items():
        raw_tools = (tool_lists or {}).get(server_name, [])
        resolved = resolve_tools(server_name, server_config, raw_tools)
        all_resolved[server_name] = resolved
        _registry.register(server_name, resolved)

    conflicts = detect_conflicts(all_resolved)
    if conflicts:
        _registry.clear()
        conflict_desc = "; ".join(
            f"{c.tool_name} in [{', '.join(c.servers)}]" for c in conflicts
        )
        return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

    return Result(value=None)


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
async def disconnect_all() -> Result[None, str]:
    """Disconnect all downstream servers and clear the registry.

    Examples:
        >>> import asyncio
        >>> r = asyncio.run(disconnect_all())
        >>> r.is_ok
        True
        >>> get_registry().get_all_tools()
        {}

    Returns:
        ``Result[None, str]`` always succeeds.
    """

    _registry.clear()
    return Result(value=None)


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def call_tool(
    server_name: str,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]:
    """Forward a tool call to a specific downstream server.

    Contract stub: actual MCP communication is deferred to gateway runtime.

    Examples:
        >>> import asyncio
        >>> asyncio.run(call_tool("srv", "tool", {}))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: call_tool pending

    Args:
        server_name: Target downstream server name.
        tool_name: Tool to invoke.
        arguments: Tool arguments (with _meta already stripped).

    Returns:
        ``Result[dict, TelaError]`` once implemented.
    """

    raise NotImplementedError("Contract stub: call_tool pending")


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns dict per DESIGN.md spec, lookup not a failable I/O boundary.
def get_all_tools() -> dict[str, list[ResolvedTool]]:
    """Return all resolved tools grouped by server name.

    Examples:
        >>> get_all_tools()
        {}

    Returns:
        Server name to resolved tool list mapping.
    """

    return _registry.get_all_tools()


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns optional str per DESIGN.md spec, lookup not a failable I/O boundary.
def get_tool_server(tool_name: str) -> str | None:
    """Look up which server owns a given tool name.

    Examples:
        >>> get_tool_server("nonexistent") is None
        True

    Args:
        tool_name: Tool to look up.

    Returns:
        Server name or None if not found.
    """

    return _registry.get_tool_server(tool_name)


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def re_enumerate(
    server_name: str,
) -> Result[list[ResolvedTool], str]:
    """Re-enumerate tools for a specific server (hot reload).

    Contract stub: actual MCP communication is deferred.

    Examples:
        >>> import asyncio
        >>> asyncio.run(re_enumerate("srv"))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: re_enumerate pending

    Args:
        server_name: Server to re-enumerate.

    Returns:
        ``Result[list[ResolvedTool], str]`` once implemented.
    """

    raise NotImplementedError("Contract stub: re_enumerate pending")
