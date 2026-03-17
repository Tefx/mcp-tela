"""Contract tests for hot reload stubs."""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import TelaConfig
from tela.shell.reload import on_config_changed, on_server_reconnect, on_tools_changed


def test_on_tools_changed_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(on_tools_changed("fs"))


def test_on_server_reconnect_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(on_server_reconnect("fs"))


def test_on_config_changed_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(on_config_changed(TelaConfig()))
