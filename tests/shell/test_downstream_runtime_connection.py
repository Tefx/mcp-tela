"""Runtime downstream connection tests for real and mocked MCP sessions."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from mcp.types import ListToolsResult, Tool

from tela.core.models import ServerConfig
from tela.shell import downstream


def test_connect_all_enumerates_real_stdio_server() -> None:
    """connect_all uses MCP tools/list and populates registry for stdio server."""

    server_script = (
        Path(__file__).resolve().parents[1] / "fixtures" / "fastmcp_stdio_server.py"
    )
    servers = {
        "local_stdio": ServerConfig(
            name="local_stdio",
            command=sys.executable,
            args=[str(server_script)],
        )
    }

    result = asyncio.run(downstream.connect_all(servers))
    assert result.is_ok

    tools = downstream.get_all_tools()
    assert "local_stdio" in tools
    assert len(tools["local_stdio"]) >= 1
    assert downstream.get_tool_server("ping") == "local_stdio"

    first_disconnect = asyncio.run(downstream.disconnect_all())
    second_disconnect = asyncio.run(downstream.disconnect_all())
    assert first_disconnect.is_ok
    assert second_disconnect.is_ok
    assert downstream._clients == {}
    assert downstream.get_all_tools() == {}


def test_connect_all_enumerates_mocked_session(monkeypatch: Any) -> None:
    """connect_all can populate registry from a mocked MCP session path."""

    class FakeSession:
        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            del cursor
            del params
            return ListToolsResult(
                tools=[
                    Tool(name="mock_tool", inputSchema={"type": "object"}),
                ],
                nextCursor=None,
            )

        async def initialize(self) -> None:
            return None

    class FakeStack:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    fake_stack = FakeStack()

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
    ) -> downstream._ClientHandle:
        del server_name
        del server_config
        return downstream._ClientHandle(session=FakeSession(), stack=fake_stack)

    monkeypatch.setattr(
        downstream,
        "_open_client_for_server",
        _fake_open_client_for_server,
    )

    servers = {
        "mocked": ServerConfig(
            name="mocked",
            command="unused-command-for-mock",
        )
    }

    result = asyncio.run(downstream.connect_all(servers))
    assert result.is_ok
    assert downstream.get_tool_server("mock_tool") == "mocked"

    cleanup = asyncio.run(downstream.disconnect_all())
    assert cleanup.is_ok
    assert fake_stack.closed is True
    assert downstream._clients == {}
    assert downstream.get_all_tools() == {}


def test_connect_all_uses_sse_transport_when_url_set(monkeypatch: Any) -> None:
    """connect_all selects SSE transport for url-based server configs."""

    class FakeSession:
        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            del cursor
            del params
            return ListToolsResult(
                tools=[Tool(name="sse_tool", inputSchema={"type": "object"})],
                nextCursor=None,
            )

        async def initialize(self) -> None:
            return None

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def _fake_open_sse_client(
        server_name: str,
        server_config: ServerConfig,
    ) -> downstream._ClientHandle:
        assert server_name == "remote"
        assert server_config.url == "http://localhost:8765/sse"
        return downstream._ClientHandle(session=FakeSession(), stack=FakeStack())

    async def _fail_open_stdio_client(
        server_name: str,
        server_config: ServerConfig,
    ) -> downstream._ClientHandle:
        del server_name
        del server_config
        raise AssertionError("stdio path must not be used for SSE server")

    monkeypatch.setattr(downstream, "_open_sse_client", _fake_open_sse_client)
    monkeypatch.setattr(downstream, "_open_stdio_client", _fail_open_stdio_client)

    result = asyncio.run(
        downstream.connect_all(
            {
                "remote": ServerConfig(
                    name="remote",
                    url="http://localhost:8765/sse",
                )
            }
        )
    )
    assert result.is_ok
    assert downstream.get_tool_server("sse_tool") == "remote"

    cleanup = asyncio.run(downstream.disconnect_all())
    assert cleanup.is_ok
