"""Downstream client transport and tool-list helpers.

This module holds transport/session lifecycle primitives used by
``tela.shell.downstream`` so that orchestration logic can stay small.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp.client.session import ClientSession, MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client

from tela.core.models import ServerConfig


@dataclass
class _ClientHandle:
    """Connected downstream client session and transport lifecycle stack."""

    session: ClientSession
    stack: AsyncExitStack


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
