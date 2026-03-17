"""Contract tests for gateway lifecycle stubs."""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import AuthMode, GatewayTransport
from tela.shell.gateway import (
    GatewayStartupConfig,
    gateway_connections,
    gateway_shutdown,
    gateway_start,
    gateway_status,
)


def test_gateway_start_is_contract_stub() -> None:
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(gateway_start(config))


def test_gateway_shutdown_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(gateway_shutdown())


def test_gateway_status_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        gateway_status()


def test_gateway_connections_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        gateway_connections()
