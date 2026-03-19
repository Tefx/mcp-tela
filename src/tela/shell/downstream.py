"""Downstream server management.

Manages connections to downstream MCP servers, real MCP ``tools/list``
enumeration, resolved-tool registry construction, and tool call forwarding.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import types as mcp_types
from mcp.client.session import ClientSession, MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.session import RequestResponder

from tela.core.conflict import detect_conflicts
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

    def restore(
        self, snap: tuple[dict[str, list["ResolvedTool"]], dict[str, str]]
    ) -> None:
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
_registry_lock = asyncio.Lock()


@dataclass
class _ClientHandle:
    """Connected downstream client session and transport lifecycle stack."""

    session: ClientSession
    stack: AsyncExitStack


_clients: dict[str, _ClientHandle] = {}


# @invar:allow shell_result: returns optional error string, validation helper.
def _validate_transport_mode(
    server_name: str, server_config: ServerConfig
) -> str | None:
    """Validate server transport shape and return an error if invalid."""

    has_command = bool(server_config.command)
    has_url = bool(server_config.url)
    if has_command == has_url:
        return (
            "DOWNSTREAM_CONNECT_FAILED: "
            f"server '{server_name}' must set exactly one transport: command or url"
        )
    return None


# @invar:allow shell_result: returns _ClientHandle, raises on I/O failure.
async def _open_stdio_client(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> _ClientHandle:
    """Open and initialize an MCP stdio client session for one server."""

    command = server_config.command
    if command is None:
        raise ValueError(f"server '{server_name}' is missing command")

    params = StdioServerParameters(
        command=command,
        args=list(server_config.args),
        env=dict(server_config.env),
    )
    stack = AsyncExitStack()
    try:
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(params)
        )
        session = await stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                message_handler=message_handler,
            )
        )
        await session.initialize()
        return _ClientHandle(session=session, stack=stack)
    except Exception:
        await stack.aclose()
        raise


# @invar:allow shell_result: returns _ClientHandle, raises on I/O failure.
async def _open_sse_client(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> _ClientHandle:
    """Open and initialize an MCP SSE client session for one server."""

    url = server_config.url
    if url is None:
        raise ValueError(f"server '{server_name}' is missing url")

    stack = AsyncExitStack()
    try:
        read_stream, write_stream = await stack.enter_async_context(sse_client(url=url))
        session = await stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                message_handler=message_handler,
            )
        )
        await session.initialize()
        return _ClientHandle(session=session, stack=stack)
    except Exception:
        await stack.aclose()
        raise


# @invar:allow shell_result: returns _ClientHandle, raises on I/O failure.
async def _open_client_for_server(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> _ClientHandle:
    """Open a connected client handle from a server config transport."""

    if server_config.command is not None:
        return await _open_stdio_client(
            server_name,
            server_config,
            message_handler=message_handler,
        )
    return await _open_sse_client(
        server_name,
        server_config,
        message_handler=message_handler,
    )


async def _handle_tools_list_changed(
    server_name: str,
    server_config: ServerConfig,
) -> None:
    """Re-enumerate server tools after downstream list-changed notification."""

    async with _registry_lock:
        client = _clients.get(server_name)

    if client is None:
        return

    try:
        raw_tools = await _enumerate_tools(client.session)
    except Exception as exc:
        logging.warning(
            "Failed downstream tool re-enumeration for %s: %s",
            server_name,
            exc,
        )
        return

    from tela.shell.reload import on_tools_changed

    result = await on_tools_changed(server_name, server_config, raw_tools)
    if result.is_err:
        logging.warning(
            "Rejected downstream tool-list update for %s: %s",
            server_name,
            result.error,
        )


async def _handle_reconnect(
    server_name: str,
    server_config: ServerConfig,
) -> None:
    """Attempt downstream reconnect and route updated tools into reload flow."""

    try:
        new_handle = await _open_client_for_server(
            server_name,
            server_config,
            message_handler=_build_downstream_message_handler(
                server_name, server_config
            ),
        )
    except Exception as exc:
        logging.warning("Downstream reconnect failed for %s: %s", server_name, exc)
        return

    async with _registry_lock:
        old_handle = _clients.get(server_name)
        _clients[server_name] = new_handle

    if old_handle is not None:
        try:
            await old_handle.stack.aclose()
        except Exception:
            pass

    try:
        raw_tools = await _enumerate_tools(new_handle.session)
    except Exception as exc:
        logging.warning(
            "Downstream reconnect enumeration failed for %s: %s",
            server_name,
            exc,
        )
        return

    from tela.shell.reload import on_server_reconnect

    result = await on_server_reconnect(server_name, server_config, raw_tools)
    if result.is_err:
        logging.warning(
            "Rejected downstream reconnect update for %s: %s",
            server_name,
            result.error,
        )


def _build_downstream_message_handler(
    server_name: str,
    server_config: ServerConfig,
):
    """Build per-server message handler for downstream notifications/events."""

    async def _message_handler(
        message: (
            RequestResponder[mcp_types.ServerRequest, mcp_types.ClientResult]
            | mcp_types.ServerNotification
            | Exception
        ),
    ) -> None:
        if isinstance(message, Exception):
            await _handle_reconnect(server_name, server_config)
            return

        if isinstance(message, mcp_types.ServerNotification):
            if isinstance(message.root, mcp_types.ToolListChangedNotification):
                await _handle_tools_list_changed(server_name, server_config)

    return _message_handler


# @invar:allow shell_result: returns list[dict], raises on I/O failure.
async def _enumerate_tools(session: ClientSession) -> list[dict[str, Any]]:
    """Enumerate all tools from a downstream session via MCP ``tools/list``."""

    list_result = await session.list_tools()
    tools = list(list_result.tools)
    cursor = list_result.nextCursor

    while cursor is not None:
        list_result = await session.list_tools(cursor=cursor)
        tools.extend(list_result.tools)
        cursor = list_result.nextCursor

    return [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools]


async def _close_all_clients_locked() -> None:
    """Close all connected downstream sessions/processes best-effort."""

    handles = list(_clients.values())
    _clients.clear()
    for handle in handles:
        try:
            await handle.stack.aclose()
        except Exception:
            continue


# @invar:allow shell_result: returns registry object, simple accessor not failable I/O.
def get_registry() -> DownstreamRegistry:
    """Return the module-level downstream registry."""
    return _registry


# @invar:allow dead_param: servers parameter unused in test-scaffold mode (tool_lists provided).
async def connect_all(
    servers: dict[str, ServerConfig],
    tool_lists: dict[str, list[dict]] | None = None,
) -> Result[None, str]:
    """Connect to all configured downstream servers and build tool registry.

    Enumerates tools, resolves families and posture, and runs conflict detection.
    Fails fast on tool name conflicts.

    When ``tool_lists`` is provided, it is treated as test-only scaffolding and
    bypasses transport/session setup. Production runtime leaves ``tool_lists``
    unset and enumerates tools from real downstream MCP sessions.

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
        tool_lists: Optional pre-enumerated tool lists for test scaffolding.

    Returns:
        ``Result[None, str]`` on success, or error string if conflicts detected.
    """

    async with _registry_lock:
        await _close_all_clients_locked()
        _registry.clear()

        all_resolved: dict[str, list[ResolvedTool]] = {}

        for server_name, server_config in servers.items():
            validation_error = _validate_transport_mode(server_name, server_config)
            if validation_error is not None:
                await _close_all_clients_locked()
                _registry.clear()
                return Result(error=validation_error)

            try:
                if tool_lists is not None:
                    raw_tools = tool_lists.get(server_name, [])
                else:
                    client_handle = await _open_client_for_server(
                        server_name,
                        server_config,
                        message_handler=_build_downstream_message_handler(
                            server_name,
                            server_config,
                        ),
                    )
                    _clients[server_name] = client_handle
                    raw_tools = await _enumerate_tools(client_handle.session)
            except Exception as exc:
                await _close_all_clients_locked()
                _registry.clear()
                return Result(
                    error=(
                        "DOWNSTREAM_CONNECT_FAILED: "
                        f"server '{server_name}' connection/enumeration failed: {exc}"
                    )
                )

            resolved = resolve_tools(server_name, server_config, raw_tools)
            all_resolved[server_name] = resolved
            _registry.register(server_name, resolved)

        conflicts = detect_conflicts(all_resolved)
        if conflicts:
            await _close_all_clients_locked()
            _registry.clear()
            conflict_desc = "; ".join(
                f"{c.tool_name} in [{', '.join(c.servers)}]" for c in conflicts
            )
            return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

    return Result(value=None)


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

    async with _registry_lock:
        await _close_all_clients_locked()
        _registry.clear()
    return Result(value=None)


async def call_tool(
    server_name: str,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]:
    """Forward a tool call to a specific downstream server.

    Uses a connected downstream MCP client session for the target server.

    Examples:
        >>> import asyncio
        >>> r = asyncio.run(call_tool("srv", "tool", {}))
        >>> isinstance(r.is_ok, bool)
        True

    Args:
        server_name: Target downstream server name.
        tool_name: Tool to invoke.
        arguments: Tool arguments (with _meta already stripped).

    Returns:
        ``Result[dict, TelaError]`` with downstream call payload or TelaError.
    """

    async with _registry_lock:
        client = _clients.get(server_name)

    if client is None:
        return Result(
            error=TelaError(
                code="DOWNSTREAM_UNAVAILABLE",
                message=f"Downstream server '{server_name}' is not connected",
            )
        )

    try:
        downstream_result = await client.session.call_tool(
            tool_name, arguments=arguments
        )
    except Exception as exc:
        return Result(
            error=TelaError(
                code="DOWNSTREAM_UNAVAILABLE",
                message=(
                    f"Downstream server '{server_name}' call failed before response"
                ),
                details={
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error": str(exc),
                },
            )
        )

    payload = downstream_result.model_dump(by_alias=True, exclude_none=True)
    if downstream_result.isError:
        return Result(
            error=TelaError(
                code="DOWNSTREAM_ERROR",
                message=(
                    f"Downstream server '{server_name}' returned tool error for '{tool_name}'"
                ),
                details={
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "downstream": payload,
                },
            )
        )

    return Result(value=payload)


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


# @invar:allow dead_export: hot-reload entrypoint for downstream tool re-enumeration.
async def re_enumerate(
    server_name: str,
) -> Result[list[ResolvedTool], str]:
    """Re-enumerate tools for a specific server (hot reload).

    Re-lists tools over the connected downstream session and refreshes the
    resolved tool registry for that server.

    Examples:
        >>> import asyncio
        >>> r = asyncio.run(re_enumerate("srv"))
        >>> isinstance(r.is_ok, bool)
        True

    Args:
        server_name: Server to re-enumerate.

    Returns:
        ``Result[list[ResolvedTool], str]`` with updated resolved tool list.
    """

    from tela.shell.gateway import get_runtime

    async with _registry_lock:
        client = _clients.get(server_name)
        if client is None:
            return Result(
                error=(
                    f"DOWNSTREAM_UNAVAILABLE: downstream server '{server_name}' is not connected"
                )
            )

        runtime = get_runtime()
        if runtime.config is None:
            return Result(
                error="DOWNSTREAM_UNAVAILABLE: gateway runtime config is not loaded"
            )

        server_config = runtime.config.servers.get(server_name)
        if server_config is None:
            return Result(
                error=(
                    f"DOWNSTREAM_UNAVAILABLE: server '{server_name}' not found in runtime config"
                )
            )

        try:
            raw_tools = await _enumerate_tools(client.session)
        except Exception as exc:
            return Result(
                error=(
                    "DOWNSTREAM_UNAVAILABLE: "
                    f"re-enumeration failed for server '{server_name}': {exc}"
                )
            )

        resolved = resolve_tools(server_name, server_config, raw_tools)
        _registry.register(server_name, resolved)
        return Result(value=resolved)
