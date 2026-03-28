"""Downstream server management.

Manages connections to downstream MCP servers, real MCP ``tools/list``
enumeration, resolved-tool registry construction, and tool call forwarding.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

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
    _validate_transport_mode as _transport_validate_transport_mode,
)
from tela.shell.downstream_registry import DownstreamRegistry
from tela.shell.config_loader import Result

# Module-level registry instance
_registry = DownstreamRegistry()
_registry_lock = asyncio.Lock()


_clients: dict[str, _ClientHandle] = {}
_server_instructions: dict[str, str] = {}


DownstreamSyncTruth = Literal["registry", "reconnect_payload", "live_reenumeration"]


@dataclass(frozen=True)
class DownstreamConvergenceContract:
    """Declarative contract for downstream synchronization truth.

    This module owns downstream convergence state: connected sessions, resolved
    tool registry contents, and reconnect/reload update application.
    """

    authoritative_sources: tuple[DownstreamSyncTruth, ...]
    not_authoritative_sources: tuple[str, ...]
    consumer_rule: str


DOWNSTREAM_CONVERGENCE_CONTRACT = DownstreamConvergenceContract(
    authoritative_sources=("registry", "reconnect_payload", "live_reenumeration"),
    not_authoritative_sources=("~/.tela/gateway.lock",),
    consumer_rule=(
        "Treat downstream registry state and accepted reconnect/reload payloads as sync truth. "
        "Do not infer downstream readiness or tool convergence from lockfile discovery alone."
    ),
)


DOWNSTREAM_CONVERGENCE_BEHAVIORAL_NOTES: tuple[str, ...] = (
    "Downstream convergence is established by successful connect_all, reload acceptance, or reconnect payload application.",
    "Lockfile discovery proves endpoint discoverability only; it does not prove downstream sync.",
)


@dataclass(frozen=True)
class _ConnectedServerData:
    """Temporary successful downstream startup result before registry publish."""

    server_name: str
    raw_tools: list[dict]
    client_handle: _ClientHandle | None = None
    instructions: str | None = None


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


# @shell_orchestration: swaps client handle under lock and closes prior session via aclose().
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


# @shell_orchestration: temporary handle cleanup closes transport stacks before registry publish.
async def _close_client_handles(handles: list[_ClientHandle]) -> None:
    """Close temporary client handles best-effort before registry publish."""

    for handle in handles:
        try:
            await handle.stack.aclose()
        except Exception:
            continue


async def _connect_server(
    server_name: str,
    server_config: ServerConfig,
) -> Result[_ConnectedServerData, str]:
    """Open one downstream client and enumerate its tools."""

    open_result = await _open_client_for_server(
        server_name,
        server_config,
        message_handler=_build_downstream_message_handler(server_name, server_config),
    )
    if open_result.is_err:
        return Result(error=open_result.error)
    assert open_result.value is not None
    client_handle = open_result.value

    tools_result = await _enumerate_tools(client_handle.session)
    if tools_result.is_err:
        try:
            await client_handle.stack.aclose()
        except Exception:
            pass
        return Result(
            error=(
                "DOWNSTREAM_CONNECT_FAILED: "
                f"server '{server_name}' connection/enumeration failed: {tools_result.error}"
            )
        )
    assert tools_result.value is not None
    return Result(
        value=_ConnectedServerData(
            server_name=server_name,
            raw_tools=tools_result.value,
            client_handle=client_handle,
            instructions=client_handle.instructions,
        )
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


# @shell_orchestration: builds closure that dispatches reconnect and tool-list-changed I/O.
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


# @shell_orchestration: iterates client handles and closes each session via aclose().
async def _close_all_clients_locked() -> None:
    """Close all connected downstream sessions/processes best-effort."""

    handles = list(_clients.values())
    _clients.clear()
    for handle in handles:
        try:
            await handle.stack.aclose()
        except Exception:
            continue


# @invar:allow shell_result: returns registry object directly, not a failable I/O boundary.
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
        _server_instructions.clear()

        all_resolved: dict[str, list[ResolvedTool]] = {}
        connected: dict[str, _ConnectedServerData] = {}

        for server_name, server_config in servers.items():
            validation_result = _validate_transport_mode(server_name, server_config)
            if validation_result.is_err:
                await _close_all_clients_locked()
                _registry.clear()
                return Result(error=validation_result.error)

        if tool_lists is not None:
            for server_name in servers:
                connected[server_name] = _ConnectedServerData(
                    server_name=server_name,
                    raw_tools=tool_lists.get(server_name, []),
                )
        else:
            startup_results = await asyncio.gather(
                *[
                    _connect_server(server_name, server_config)
                    for server_name, server_config in servers.items()
                ]
            )
            temporary_handles: list[_ClientHandle] = []
            for startup_result in startup_results:
                if startup_result.is_err:
                    await _close_client_handles(temporary_handles)
                    _registry.clear()
                    return Result(error=startup_result.error)
                assert startup_result.value is not None
                startup = startup_result.value
                connected[startup.server_name] = startup
                if startup.client_handle is not None:
                    temporary_handles.append(startup.client_handle)

        for server_name, server_config in servers.items():
            startup = connected[server_name]
            if startup.client_handle is not None:
                _clients[server_name] = startup.client_handle
            if startup.instructions:
                _server_instructions[server_name] = startup.instructions
            resolved = resolve_tools(server_name, server_config, startup.raw_tools)
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


def get_server_instructions() -> Result[dict[str, str], str]:
    """Return collected server instructions from downstream MCP servers.

    Each key is the server name, each value is the instructions string
    returned by the server during MCP initialize.

    Examples:
        >>> get_server_instructions().value
        {}

    Returns:
        Result with server name to instructions mapping (only servers that provided instructions).
    """

    return Result(value=dict(_server_instructions))


def get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]:
    """Return all resolved tools grouped by server name.

    Examples:
        >>> get_all_tools().value
        {}

    Returns:
        Server name to resolved tool list mapping.
    """

    return Result(value=_registry.get_all_tools())


def get_tool_server(tool_name: str) -> Result[str | None, str]:
    """Look up which server owns a given tool name.

    Examples:
        >>> get_tool_server("nonexistent").value is None
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

    from tela.shell.gateway_runtime import get_runtime_config

    async with _registry_lock:
        client = _clients.get(server_name)
        if client is None:
            return Result(
                error=(
                    f"DOWNSTREAM_UNAVAILABLE: downstream server '{server_name}' is not connected"
                )
            )

        config = get_runtime_config().value
        if config is None:
            return Result(
                error="DOWNSTREAM_UNAVAILABLE: gateway runtime config is not loaded"
            )

        server_config = config.servers.get(server_name)
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
