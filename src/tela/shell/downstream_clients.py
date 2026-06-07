"""Downstream client transport and tool-list helpers.

This module holds transport/session lifecycle primitives used by
``tela.shell.downstream`` so that orchestration logic can stay small.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx

from mcp.client.session import ClientSession, MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from tela.core.errors import DOWNSTREAM_CONNECT_FAILED
from tela.core.models import ServerConfig
from tela.shell.result import Result


@dataclass
class _ClientHandle:
    """Connected downstream client session and transport lifecycle stack."""

    session: ClientSession
    stack: AsyncExitStack
    instructions: str | None = None


# @invar:allow shell_result: pure local redaction helper; callers return Result
# @shell_orchestration: transport-error redaction stays at the SDK exception boundary
def _redact_header_values(message: str, headers: dict[str, str]) -> str:
    """Remove configured header values from downstream error text."""

    redacted = message
    for value in headers.values():
        if value:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def _validate_transport_mode(
    server_name: str, server_config: ServerConfig
) -> Result[None, str]:
    """Validate server transport shape and return an error if invalid."""

    has_command = bool(server_config.command)
    has_url = bool(server_config.url)
    if has_command == has_url:
        return Result(
            error=(
                f"{DOWNSTREAM_CONNECT_FAILED}: "
                f"server '{server_name}' must set exactly one transport: command or url"
            )
        )
    return Result(value=None)


async def _open_stdio_client(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> Result[_ClientHandle, str]:
    """Open and initialize an MCP stdio client session for one server."""

    command = server_config.command
    if command is None:
        return Result(
            error=f"{DOWNSTREAM_CONNECT_FAILED}: server '{server_name}' is missing command"
        )

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
        init_result = await session.initialize()
        return Result(
            value=_ClientHandle(
                session=session,
                stack=stack,
                instructions=init_result.instructions,
            )
        )
    except Exception as exc:
        await stack.aclose()
        return Result(
            error=(
                f"{DOWNSTREAM_CONNECT_FAILED}: "
                f"server '{server_name}' stdio connect failed: {exc}"
            )
        )


async def _open_sse_client(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> Result[_ClientHandle, str]:
    """Open and initialize an MCP SSE client session for one server."""

    url = server_config.url
    if url is None:
        return Result(
            error=f"{DOWNSTREAM_CONNECT_FAILED}: server '{server_name}' is missing url"
        )

    stack = AsyncExitStack()
    try:
        if server_config.headers:
            transport_context = sse_client(
                url=url,
                headers=dict(server_config.headers),
            )
        else:
            transport_context = sse_client(url=url)
        read_stream, write_stream = await stack.enter_async_context(transport_context)
        session = await stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                message_handler=message_handler,
            )
        )
        init_result = await session.initialize()
        return Result(
            value=_ClientHandle(
                session=session,
                stack=stack,
                instructions=init_result.instructions,
            )
        )
    except Exception as exc:
        await stack.aclose()
        detail = _redact_header_values(str(exc), server_config.headers)
        return Result(
            error=(
                f"{DOWNSTREAM_CONNECT_FAILED}: "
                f"server '{server_name}' sse connect failed: {detail}"
            )
        )


async def _open_streamable_http_client(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> Result[_ClientHandle, str]:
    """Open and initialize an MCP Streamable HTTP client session for one server."""

    url = server_config.url
    if url is None:
        return Result(
            error=f"{DOWNSTREAM_CONNECT_FAILED}: server '{server_name}' is missing url"
        )

    stack = AsyncExitStack()
    try:
        if server_config.headers:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=dict(server_config.headers))
            )
            transport_context = streamable_http_client(
                url=url,
                http_client=http_client,
            )
        else:
            transport_context = streamable_http_client(url=url)
        read_stream, write_stream, _ = await stack.enter_async_context(
            transport_context
        )
        session = await stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                message_handler=message_handler,
            )
        )
        init_result = await session.initialize()
        return Result(
            value=_ClientHandle(
                session=session,
                stack=stack,
                instructions=init_result.instructions,
            )
        )
    except Exception as exc:
        await stack.aclose()
        detail = _redact_header_values(str(exc), server_config.headers)
        return Result(
            error=(
                f"{DOWNSTREAM_CONNECT_FAILED}: "
                f"server '{server_name}' streamable http connect failed: {detail}"
            )
        )


async def _open_client_for_server(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> Result[_ClientHandle, str]:
    """Open a connected client handle from a server config transport.

    Transport dispatch:
    - ``command`` set → stdio
    - ``url`` set (default) → Streamable HTTP
    - ``url`` set + ``transport == "sse"`` → SSE (legacy)
    """

    validation = _validate_transport_mode(server_name, server_config)
    if validation.is_err:
        return Result(error=validation.error)

    if server_config.command is not None:
        return await _open_stdio_client(
            server_name,
            server_config,
            message_handler=message_handler,
        )
    if server_config.transport == "sse":
        return await _open_sse_client(
            server_name,
            server_config,
            message_handler=message_handler,
        )
    return await _open_streamable_http_client(
        server_name,
        server_config,
        message_handler=message_handler,
    )


async def _enumerate_tools(
    session: ClientSession,
) -> Result[list[dict[str, Any]], str]:
    """Enumerate all tools from a downstream session via MCP ``tools/list``."""

    try:
        list_result = await session.list_tools()
        tools = list(list_result.tools)
        cursor = list_result.nextCursor

        while cursor is not None:
            list_result = await session.list_tools(cursor=cursor)
            tools.extend(list_result.tools)
            cursor = list_result.nextCursor
    except Exception as exc:
        return Result(error=f"DOWNSTREAM_ENUMERATE_FAILED: {exc}")

    return Result(
        value=[tool.model_dump(by_alias=True, exclude_none=True) for tool in tools]
    )
