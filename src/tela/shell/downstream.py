"""Downstream server management.

Manages connections to downstream MCP servers, real MCP ``tools/list``
enumeration, resolved-tool registry construction, and tool call forwarding.
"""

from __future__ import annotations

import asyncio
import logging

from mcp import types as mcp_types
from mcp.client.session import MessageHandlerFnT
from mcp.shared.session import RequestResponder

from tela.core.conflict import detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import ResolvedTool, ServerConfig, TelaError
from tela.shell.downstream_clients import (
    _ClientHandle,
    _enumerate_tools,
    _open_client_for_server as _transport_open_client_for_server,
    _open_sse_client as _transport_open_sse_client,
    _open_stdio_client as _transport_open_stdio_client,
    _validate_transport_mode as _transport_validate_transport_mode,
)
from tela.shell.downstream_registry import DownstreamRegistry
from tela.shell.config_loader import Result

# Module-level registry instance
_registry = DownstreamRegistry()
_registry_lock = asyncio.Lock()


_clients: dict[str, _ClientHandle] = {}


async def _open_stdio_client(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> Result[_ClientHandle, str]:
    """Compatibility wrapper for stdio transport opener."""
    return await _transport_open_stdio_client(
        server_name,
        server_config,
        message_handler=message_handler,
    )


async def _open_sse_client(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> Result[_ClientHandle, str]:
    """Compatibility wrapper for SSE transport opener."""
    return await _transport_open_sse_client(
        server_name,
        server_config,
        message_handler=message_handler,
    )


def _validate_transport_mode(
    server_name: str,
    server_config: ServerConfig,
) -> Result[None, str]:
    """Validate server transport mode and return explicit error on mismatch."""
    return _transport_validate_transport_mode(server_name, server_config)


async def _open_client_for_server(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> Result[_ClientHandle, str]:
    """Open a connected client handle from a server config transport."""
    return await _transport_open_client_for_server(
        server_name,
        server_config,
        message_handler=message_handler,
    )


async def _swap_client_handle(server_name: str, new_handle: _ClientHandle) -> None:
    """Replace one client handle and close any prior handle best-effort."""

    async with _registry_lock:
        old_handle = _clients.get(server_name)
        _clients[server_name] = new_handle

    if old_handle is not None:
        try:
            await old_handle.stack.aclose()
        except Exception:
            return


async def _enumerate_client_tools(
    server_name: str,
    handle: _ClientHandle,
) -> Result[list[dict], str]:
    """Enumerate tools for one connected client handle."""

    tools_result = await _enumerate_tools(handle.session)
    if tools_result.is_err:
        return Result(
            error=(
                "DOWNSTREAM_UNAVAILABLE: "
                f"re-enumeration failed for server '{server_name}': {tools_result.error}"
            )
        )
    assert tools_result.value is not None
    return Result(value=tools_result.value)


async def _handle_tools_list_changed(
    server_name: str,
    server_config: ServerConfig,
) -> None:
    """Re-enumerate server tools after downstream list-changed notification."""

    async with _registry_lock:
        client = _clients.get(server_name)

    if client is None:
        return

    raw_tools_result = await _enumerate_client_tools(server_name, client)
    if raw_tools_result.is_err:
        logging.warning(
            "Failed downstream tool re-enumeration for %s: %s",
            server_name,
            raw_tools_result.error,
        )
        return
    assert raw_tools_result.value is not None
    raw_tools = raw_tools_result.value

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

    open_result = await _open_client_for_server(
        server_name,
        server_config,
        message_handler=_build_downstream_message_handler(server_name, server_config),
    )
    if open_result.is_err:
        logging.warning(
            "Downstream reconnect failed for %s: %s",
            server_name,
            open_result.error,
        )
        return
    assert open_result.value is not None
    new_handle = open_result.value

    await _swap_client_handle(server_name, new_handle)

    raw_tools_result = await _enumerate_client_tools(server_name, new_handle)
    if raw_tools_result.is_err:
        logging.warning(
            "Downstream reconnect enumeration failed for %s: %s",
            server_name,
            raw_tools_result.error,
        )
        return
    assert raw_tools_result.value is not None
    raw_tools = raw_tools_result.value

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


# @shell_complexity: startup path coordinates transport connection, enumeration, and conflict rollback.
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
            validation_result = _validate_transport_mode(server_name, server_config)
            if validation_result.is_err:
                await _close_all_clients_locked()
                _registry.clear()
                return Result(error=validation_result.error)

            if tool_lists is not None:
                raw_tools = tool_lists.get(server_name, [])
            else:
                open_result = await _open_client_for_server(
                    server_name,
                    server_config,
                    message_handler=_build_downstream_message_handler(
                        server_name,
                        server_config,
                    ),
                )
                if open_result.is_err:
                    await _close_all_clients_locked()
                    _registry.clear()
                    return Result(error=open_result.error)
                assert open_result.value is not None
                client_handle = open_result.value
                _clients[server_name] = client_handle
                tools_result = await _enumerate_tools(client_handle.session)
                if tools_result.is_err:
                    await _close_all_clients_locked()
                    _registry.clear()
                    return Result(
                        error=(
                            "DOWNSTREAM_CONNECT_FAILED: "
                            f"server '{server_name}' connection/enumeration failed: {tools_result.error}"
                        )
                    )
                assert tools_result.value is not None
                raw_tools = tools_result.value

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


def get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]:
    """Return all resolved tools grouped by server name.

    Examples:
        >>> get_all_tools()
        {}

    Returns:
        Server name to resolved tool list mapping.
    """

    return Result(value=_registry.get_all_tools())


def get_tool_server(tool_name: str) -> Result[str | None, str]:
    """Look up which server owns a given tool name.

    Examples:
        >>> get_tool_server("nonexistent") is None
        True

    Args:
        tool_name: Tool to look up.

    Returns:
        Server name or None if not found.
    """

    return Result(value=_registry.get_tool_server(tool_name))


# @shell_complexity: re-enumeration validates live runtime and server ownership before registry update.
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

        tools_result = await _enumerate_tools(client.session)
        if tools_result.is_err:
            return Result(
                error=(
                    "DOWNSTREAM_UNAVAILABLE: "
                    f"re-enumeration failed for server '{server_name}': {tools_result.error}"
                )
            )

        assert tools_result.value is not None
        resolved = resolve_tools(server_name, server_config, tools_result.value)
        _registry.register(server_name, resolved)
        return Result(value=resolved)
