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
from tela.shell.result import Result
from tela.shell.gateway_runtime import get_runtime_config, set_runtime_config


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
        return Result(
            value=downstream._ClientHandle(session=FakeSession(), stack=fake_stack)
        )  # type: ignore[arg-type]  # test fakes

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


def test_connect_all_opens_downstreams_in_parallel(monkeypatch: Any) -> None:
    """connect_all should overlap downstream startup instead of serializing it."""

    class FakeSession:
        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            del cursor
            del params
            return ListToolsResult(
                tools=[Tool(name="shared_tool", inputSchema={})],
                nextCursor=None,
            )

    class FakeStack:
        async def aclose(self) -> None:
            return None

    inflight = 0
    max_inflight = 0
    lock = asyncio.Lock()

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        del server_config
        del message_handler
        nonlocal inflight, max_inflight

        class _ServerSession(FakeSession):
            async def list_tools(
                self, cursor: str | None = None, *, params: Any = None
            ) -> ListToolsResult:
                del cursor
                del params
                return ListToolsResult(
                    tools=[Tool(name=f"tool_{server_name}", inputSchema={})],
                    nextCursor=None,
                )

        async with lock:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.05)
        async with lock:
            inflight -= 1
        return Result(
            value=downstream._ClientHandle(session=_ServerSession(), stack=FakeStack())
        )  # type: ignore[arg-type]  # test fakes

    monkeypatch.setattr(
        downstream,
        "_open_client_for_server",
        _fake_open_client_for_server,
    )

    servers = {
        "a": ServerConfig(name="a", command="cmd-a"),
        "b": ServerConfig(name="b", command="cmd-b"),
        "c": ServerConfig(name="c", command="cmd-c"),
    }

    result = asyncio.run(downstream.connect_all(servers))
    assert result.is_ok
    assert max_inflight >= 2

    cleanup = asyncio.run(downstream.disconnect_all())
    assert cleanup.is_ok


def test_connect_all_closes_successful_handles_when_parallel_peer_fails(
    monkeypatch: Any,
) -> None:
    """Parallel startup failure must clean up handles opened by other servers."""

    class FakeSession:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            del cursor
            del params
            if self.fail:
                raise RuntimeError("boom")
            return ListToolsResult(
                tools=[Tool(name="ok_tool", inputSchema={})],
                nextCursor=None,
            )

    class FakeStack:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    stacks: dict[str, FakeStack] = {}

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        del server_config
        del message_handler
        stack = FakeStack()
        stacks[server_name] = stack
        return Result(
            value=downstream._ClientHandle(
                session=FakeSession(fail=(server_name == "bad")),
                stack=stack,
            )
        )  # type: ignore[arg-type]  # test fakes

    monkeypatch.setattr(
        downstream,
        "_open_client_for_server",
        _fake_open_client_for_server,
    )

    servers = {
        "good": ServerConfig(name="good", command="cmd-good"),
        "bad": ServerConfig(name="bad", command="cmd-bad"),
    }

    result = asyncio.run(downstream.connect_all(servers))
    assert result.is_err
    assert "DOWNSTREAM_CONNECT_FAILED" in (result.error or "")
    assert stacks["good"].closed is True
    assert stacks["bad"].closed is True
    assert downstream._clients == {}


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
        return Result(
            value=downstream._ClientHandle(session=FakeSession(), stack=FakeStack())
        )  # type: ignore[arg-type]  # test fakes

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_sse
    )

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


def test_open_streamable_http_client_propagates_headers(monkeypatch: Any) -> None:
    """RH-5: Streamable HTTP forwards configured headers via SDK client path."""
    passed_headers: dict[str, str] | None = None
    constructed_headers: dict[str, str] | None = None

    class FakeHttpClient:
        def __init__(self, *, headers: dict[str, str]) -> None:
            nonlocal constructed_headers
            constructed_headers = headers
            self.headers = headers

        async def __aenter__(self) -> "FakeHttpClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

    def fake_streamable_http_client(
        url: str,
        *,
        http_client: Any | None = None,
        terminate_on_close: bool = True,
    ) -> Any:
        nonlocal passed_headers
        del url
        del terminate_on_close
        if http_client is not None:
            passed_headers = http_client.headers
        import contextlib

        @contextlib.asynccontextmanager
        async def fake_cm():
            yield None, None, None

        return fake_cm()

    import tela.shell.downstream_clients as clients_module

    monkeypatch.setattr(clients_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(
        clients_module, "streamable_http_client", fake_streamable_http_client
    )

    server_config = ServerConfig(
        name="remote",
        url="http://x",
        headers={"Custom": "Test", "Authorization": "Bearer token"},
    )

    class FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args
            del kwargs

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def initialize(self) -> Any:
            import collections

            InitResult = collections.namedtuple("InitResult", ["instructions"])
            return InitResult(instructions=None)

    monkeypatch.setattr(clients_module, "ClientSession", FakeSession)

    result = asyncio.run(
        clients_module._open_streamable_http_client("remote", server_config)
    )
    assert result.is_ok
    assert constructed_headers == server_config.headers
    assert passed_headers == server_config.headers


def test_open_sse_client_propagates_headers(monkeypatch: Any) -> None:
    """RH-5: SSE forwards configured headers to the SDK transport."""
    passed_headers: dict[str, str] | None = None

    def fake_sse_client(url: str, headers: dict[str, str] | None = None) -> Any:
        nonlocal passed_headers
        del url
        passed_headers = headers
        import contextlib

        @contextlib.asynccontextmanager
        async def fake_cm():
            yield None, None

        return fake_cm()

    import tela.shell.downstream_clients as clients_module

    monkeypatch.setattr(clients_module, "sse_client", fake_sse_client)

    server_config = ServerConfig(
        name="remote",
        url="http://x",
        transport="sse",
        headers={"X-Test": "Sse"},
    )

    class FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args
            del kwargs

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def initialize(self) -> Any:
            import collections

            InitResult = collections.namedtuple("InitResult", ["instructions"])
            return InitResult(instructions=None)

    monkeypatch.setattr(clients_module, "ClientSession", FakeSession)

    result = asyncio.run(clients_module._open_sse_client("remote", server_config))
    assert result.is_ok
    assert passed_headers == server_config.headers


def test_open_streamable_http_client_omits_empty_headers(monkeypatch: Any) -> None:
    """RH-5: Empty Streamable HTTP headers preserve the existing SDK call shape."""
    passed_http_client: Any | None = "not-called"

    def fake_streamable_http_client(
        url: str,
        *,
        http_client: Any | None = None,
        terminate_on_close: bool = True,
    ) -> Any:
        nonlocal passed_http_client
        del url
        del terminate_on_close
        passed_http_client = http_client
        import contextlib

        @contextlib.asynccontextmanager
        async def fake_cm():
            yield None, None, None

        return fake_cm()

    import tela.shell.downstream_clients as clients_module

    monkeypatch.setattr(
        clients_module, "streamable_http_client", fake_streamable_http_client
    )

    class FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args
            del kwargs

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def initialize(self) -> Any:
            import collections

            InitResult = collections.namedtuple("InitResult", ["instructions"])
            return InitResult(instructions=None)

    monkeypatch.setattr(clients_module, "ClientSession", FakeSession)

    result = asyncio.run(
        clients_module._open_streamable_http_client(
            "remote", ServerConfig(name="remote", url="http://x")
        )
    )
    assert result.is_ok
    assert passed_http_client is None


def test_open_sse_client_omits_empty_headers(monkeypatch: Any) -> None:
    """RH-5: Empty SSE headers preserve the existing SDK call shape."""
    passed_headers: dict[str, str] | None | str = "not-called"

    def fake_sse_client(url: str, headers: dict[str, str] | None = None) -> Any:
        nonlocal passed_headers
        del url
        passed_headers = headers
        import contextlib

        @contextlib.asynccontextmanager
        async def fake_cm():
            yield None, None

        return fake_cm()

    import tela.shell.downstream_clients as clients_module

    monkeypatch.setattr(clients_module, "sse_client", fake_sse_client)

    class FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args
            del kwargs

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def initialize(self) -> Any:
            import collections

            InitResult = collections.namedtuple("InitResult", ["instructions"])
            return InitResult(instructions=None)

    monkeypatch.setattr(clients_module, "ClientSession", FakeSession)

    result = asyncio.run(
        clients_module._open_sse_client(
            "remote", ServerConfig(name="remote", url="http://x", transport="sse")
        )
    )
    assert result.is_ok
    assert passed_headers is None


def test_open_streamable_http_client_redacts_header_values_from_errors(
    monkeypatch: Any,
) -> None:
    """RH-5: Streamable HTTP errors must not serialize configured header values."""

    def fake_streamable_http_client(
        url: str,
        *,
        http_client: Any | None = None,
        terminate_on_close: bool = True,
    ) -> Any:
        del url
        del http_client
        del terminate_on_close
        import contextlib

        @contextlib.asynccontextmanager
        async def fake_cm():
            raise RuntimeError("connect failed with Bearer secret-token")
            yield None, None, None

        return fake_cm()

    import tela.shell.downstream_clients as clients_module

    monkeypatch.setattr(
        clients_module, "streamable_http_client", fake_streamable_http_client
    )

    result = asyncio.run(
        clients_module._open_streamable_http_client(
            "remote",
            ServerConfig(
                name="remote",
                url="http://x",
                headers={"Authorization": "Bearer secret-token"},
            ),
        )
    )
    assert result.is_err
    assert result.error is not None
    assert "Bearer secret-token" not in result.error
    assert "[redacted]" in result.error


def test_open_sse_client_redacts_header_values_from_errors(monkeypatch: Any) -> None:
    """RH-5: SSE errors must not serialize configured header values."""

    def fake_sse_client(url: str, headers: dict[str, str] | None = None) -> Any:
        del url
        del headers
        import contextlib

        @contextlib.asynccontextmanager
        async def fake_cm():
            raise RuntimeError("connect failed with sse-secret")
            yield None, None

        return fake_cm()

    import tela.shell.downstream_clients as clients_module

    monkeypatch.setattr(clients_module, "sse_client", fake_sse_client)

    result = asyncio.run(
        clients_module._open_sse_client(
            "remote",
            ServerConfig(
                name="remote",
                url="http://x",
                transport="sse",
                headers={"X-Secret": "sse-secret"},
            ),
        )
    )
    assert result.is_err
    assert result.error is not None
    assert "sse-secret" not in result.error
    assert "[redacted]" in result.error


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
        return Result(
            value=downstream._ClientHandle(session=fake_session, stack=FakeStack())
        )  # type: ignore[arg-type]  # test fakes

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

    previous_config = get_runtime_config().value
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
    from tela.shell.result import Result

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
    from tela.shell.result import Result

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
        return Result(
            value=downstream._ClientHandle(session=FakeSession(), stack=FakeStack())
        )  # type: ignore[arg-type]  # test fakes

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
    # Set up runtime config so reconnect handler can resolve server config
    old_runtime = get_runtime_config()
    set_runtime_config(TelaConfig(servers={"mocked": server_config}))
    handler = downstream._build_downstream_message_handler("mocked", server_config)

    try:
        asyncio.run(handler(RuntimeError("downstream receive loop dropped")))
    finally:
        set_runtime_config(old_runtime.value if old_runtime.is_ok else None)
        downstream._clients.clear()

    assert observed["server_name"] == "mocked"
    assert observed["server_config"] == server_config
    assert observed["tool_list"][0]["name"] == "after_reconnect"
