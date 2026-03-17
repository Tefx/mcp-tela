"""Downstream server management contracts.

Defines acceptance-only interfaces for spawning/connecting to downstream MCP
servers, enumerating their tools, and forwarding tool calls. No actual process
management or network I/O is implemented in this contract step.
"""

from __future__ import annotations

from tela.core.models import ResolvedTool, ServerConfig, TelaError
from tela.shell.config_loader import Result


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def connect_all(
    servers: dict[str, ServerConfig],
) -> Result[None, str]:
    """Connect to all configured downstream servers.

    Spawns stdio servers, connects to SSE servers, enumerates tool lists,
    and runs conflict detection. Fails fast on conflict at startup.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(connect_all({}))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: connect_all pending

    Args:
        servers: Server name to configuration mapping.

    Returns:
        ``Result[None, str]`` once implemented.
    """

    raise NotImplementedError("Contract stub: connect_all pending")


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
async def disconnect_all() -> Result[None, str]:
    """Disconnect all downstream servers and kill spawned processes.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(disconnect_all())
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: disconnect_all pending

    Returns:
        ``Result[None, str]`` once implemented.
    """

    raise NotImplementedError("Contract stub: disconnect_all pending")


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def call_tool(
    server_name: str,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]:
    """Forward a tool call to a specific downstream server.

    Contract stub: raises NotImplementedError.

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

    Contract stub: raises NotImplementedError.

    Examples:
        >>> get_all_tools()
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: get_all_tools pending

    Returns:
        Server name to resolved tool list mapping.
    """

    raise NotImplementedError("Contract stub: get_all_tools pending")


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow shell_result: returns optional str per DESIGN.md spec, lookup not a failable I/O boundary.
# @invar:allow dead_param: contract stub preserves parameter signatures.
def get_tool_server(tool_name: str) -> str | None:
    """Look up which server owns a given tool name.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> get_tool_server("some_tool")
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: get_tool_server pending

    Args:
        tool_name: Tool to look up.

    Returns:
        Server name or None if not found.
    """

    raise NotImplementedError("Contract stub: get_tool_server pending")


# @invar:allow dead_export: downstream wiring is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def re_enumerate(
    server_name: str,
) -> Result[list[ResolvedTool], str]:
    """Re-enumerate tools for a specific server (hot reload).

    Contract stub: raises NotImplementedError.

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
