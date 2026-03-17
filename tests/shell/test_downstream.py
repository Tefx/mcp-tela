"""Contract tests for downstream server management stubs."""

from __future__ import annotations

import asyncio

import pytest

from tela.shell.downstream import (
    call_tool,
    connect_all,
    disconnect_all,
    get_all_tools,
    get_tool_server,
    re_enumerate,
)


def test_connect_all_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(connect_all({}))


def test_disconnect_all_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(disconnect_all())


def test_call_tool_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(call_tool("srv", "tool", {}))


def test_get_all_tools_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        get_all_tools()


def test_get_tool_server_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        get_tool_server("some_tool")


def test_re_enumerate_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(re_enumerate("srv"))
