"""Runtime downstream connection tests for real and mocked MCP sessions."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from mcp import types as mcp_types
from mcp.types import ListToolsResult, Tool

from tela.core.models import ServerConfig, TelaConfig
from tela.shell import downstream
from tela.shell.config_loader import Result
from tela.shell.gateway import get_runtime_config, set_runtime_config


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
    assert tools.is_ok
    assert tools.value is not None
    assert "local_stdio" in tools.value
    assert len(tools.value["local_stdio"]) >= 1
    assert downstream.get_tool_server("ping").value == "local_stdio"

    first_disconnect = asyncio.run(downstream.disconnect_all())
    second_disconnect = asyncio.run(downstream.disconnect_all())
    assert first_disconnect.is_ok
    assert second_disconnect.is_ok
    assert downstream._clients == {}
    assert downstream.get_all_tools().value == {}


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
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        del server_name
        del server_config
        del message_handler
        return Result(value=downstream._ClientHandle(session=FakeSession(), stack=fake_stack))  # type: ignore[arg-type]  # test fakes

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
    assert downstream.get_tool_server("mock_tool").value == "mocked"

    cleanup = asyncio.run(downstream.disconnect_all())
    assert cleanup.is_ok
    assert fake_stack.closed is True
    assert downstream._clients == {}
    assert downstream.get_all_tools().value == {}


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

    async def _fake_open_client_for_sse(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        # Verify SSE transport path selected by URL
        assert server_name == "remote"
        assert server_config.url == "http://localhost:8765/sse"
        del message_handler
        return Result(value=downstream._ClientHandle(session=FakeSession(), stack=FakeStack()))  # type: ignore[arg-type]  # test fakes

    monkeypatch.setattr(downstream, "_open_client_for_server", _fake_open_client_for_sse)

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
    assert downstream.get_tool_server("sse_tool").value == "remote"

    cleanup = asyncio.run(downstream.disconnect_all())
    assert cleanup.is_ok


def test_call_tool_returns_real_downstream_result() -> None:
    """call_tool forwards to connected downstream session and returns payload."""

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

    async def _run() -> None:
        connect_result = await downstream.connect_all(servers)
        assert connect_result.is_ok

        call_result = await downstream.call_tool(
            "local_stdio",
            "echo",
            {"value": "hello"},
        )
        assert call_result.is_ok
        assert call_result.value is not None
        content = call_result.value.get("content")
        assert isinstance(content, list)
        assert len(content) > 0
        first_content = content[0]
        assert isinstance(first_content, dict)
        assert first_content.get("type") == "text"
        assert first_content.get("text") == "hello"

        cleanup = await downstream.disconnect_all()
        assert cleanup.is_ok

    asyncio.run(_run())


def test_re_enumerate_updates_tool_list_from_session(monkeypatch: Any) -> None:
    """re_enumerate re-lists tools from connected downstream session."""

    class FakeSession:
        def __init__(self) -> None:
            self.enumeration = 0

        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            del cursor
            del params
            if self.enumeration == 0:
                tools = [Tool(name="initial_tool", inputSchema={"type": "object"})]
            else:
                tools = [
                    Tool(name="initial_tool", inputSchema={"type": "object"}),
                    Tool(name="new_tool", inputSchema={"type": "object"}),
                ]
            self.enumeration += 1
            return ListToolsResult(tools=tools, nextCursor=None)

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            read_timeout_seconds: Any = None,
            progress_callback: Any = None,
            *,
            meta: dict[str, Any] | None = None,
        ) -> Any:
            del name
            del arguments
            del read_timeout_seconds
            del progress_callback
            del meta
            raise AssertionError("call_tool not expected in re_enumerate test")

    class FakeStack:
        async def aclose(self) -> None:
            return None

    fake_session = FakeSession()

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        del server_name
        del server_config
        del message_handler
        return Result(value=downstream._ClientHandle(session=fake_session, stack=FakeStack()))  # type: ignore[arg-type]  # test fakes

    monkeypatch.setattr(
        downstream,
        "_open_client_for_server",
        _fake_open_client_for_server,
    )

    servers = {"mocked": ServerConfig(name="mocked", command="unused-command")}
    connect_result = asyncio.run(downstream.connect_all(servers))
    assert connect_result.is_ok
    assert downstream.get_tool_server("initial_tool").value == "mocked"
    assert downstream.get_tool_server("new_tool").value is None

    previous_config = get_runtime_config()
    set_runtime_config(TelaConfig(servers=servers))
    try:
        re_enum_result = asyncio.run(downstream.re_enumerate("mocked"))
        assert re_enum_result.is_ok
        assert re_enum_result.value is not None
        tool_names = sorted(tool.name for tool in re_enum_result.value)
        assert tool_names == ["initial_tool", "new_tool"]

        assert downstream.get_tool_server("initial_tool").value == "mocked"
        assert downstream.get_tool_server("new_tool").value == "mocked"
    finally:
        set_runtime_config(previous_config)

    cleanup = asyncio.run(downstream.disconnect_all())
    assert cleanup.is_ok


def test_message_handler_routes_tools_changed_notification(monkeypatch: Any) -> None:
    """Downstream notifications/tools/list_changed triggers reload on_tools_changed."""
    from tela.shell.config_loader import Result

    observed: dict[str, Any] = {}

    class FakeSession:
        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            del cursor
            del params
            return ListToolsResult(
                tools=[
                    Tool(name="t1", inputSchema={"type": "object"}),
                    Tool(name="t2", inputSchema={"type": "object"}),
                ],
                nextCursor=None,
            )

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def _fake_on_tools_changed(
        server_name: str,
        server_config: ServerConfig,
        new_tool_list: list[dict],
    ) -> Result[None, str]:
        observed["server_name"] = server_name
        observed["server_config"] = server_config
        observed["new_tool_list"] = new_tool_list
        return Result(value=None)

    monkeypatch.setattr("tela.shell.reload.on_tools_changed", _fake_on_tools_changed)

    server_config = ServerConfig(name="mocked", command="unused")
    downstream._clients["mocked"] = downstream._ClientHandle(
        session=FakeSession(),  # type: ignore[arg-type]  # test fakes
        stack=FakeStack(),  # type: ignore[arg-type]  # test fakes
    )
    handler = downstream._build_downstream_message_handler("mocked", server_config)

    try:
        asyncio.run(
            handler(
                mcp_types.ServerNotification(
                    root=mcp_types.ToolListChangedNotification(
                        method="notifications/tools/list_changed"
                    )
                )
            )
        )
    finally:
        downstream._clients.clear()

    assert observed["server_name"] == "mocked"
    assert observed["server_config"] == server_config
    tool_names = sorted(tool["name"] for tool in observed["new_tool_list"])
    assert tool_names == ["t1", "t2"]


def test_message_handler_routes_reconnect_exception(monkeypatch: Any) -> None:
    """Downstream exception path attempts reconnect and triggers reload reconnect."""
    from tela.shell.config_loader import Result

    observed: dict[str, Any] = {}

    class FakeSession:
        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            del cursor
            del params
            return ListToolsResult(
                tools=[Tool(name="after_reconnect", inputSchema={})],
                nextCursor=None,
            )

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        del server_name
        del server_config
        del message_handler
        return Result(value=downstream._ClientHandle(session=FakeSession(), stack=FakeStack()))  # type: ignore[arg-type]  # test fakes

    async def _fake_on_server_reconnect(
        server_name: str,
        server_config: ServerConfig,
        tool_list: list[dict],
    ) -> Result[None, str]:
        observed["server_name"] = server_name
        observed["server_config"] = server_config
        observed["tool_list"] = tool_list
        return Result(value=None)

    monkeypatch.setattr(
        downstream,
        "_open_client_for_server",
        _fake_open_client_for_server,
    )
    monkeypatch.setattr(
        "tela.shell.reload.on_server_reconnect",
        _fake_on_server_reconnect,
    )

    server_config = ServerConfig(name="mocked", command="unused")
    handler = downstream._build_downstream_message_handler("mocked", server_config)

    try:
        asyncio.run(handler(RuntimeError("downstream receive loop dropped")))
    finally:
        downstream._clients.clear()

    assert observed["server_name"] == "mocked"
    assert observed["server_config"] == server_config
    assert observed["tool_list"][0]["name"] == "after_reconnect"
