"""Exercise SDK session expiry and bridge reinitialization over real HTTP."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import Callable, Iterator
from threading import Thread
from urllib.request import Request, urlopen

import pytest
import uvicorn

from tela.commands import connect_bridge
from tela.core.models import AuthConfig, AuthMode, GatewayTransport, ProfileConfig, TelaConfig
from tela.shell.audit import clear_audit_entries, get_audit_entries
from tela.shell.gateway import GatewayStartupConfig, gateway_shutdown, gateway_start
from tela.shell.gateway_runtime import get_upstream_http_app, with_upstream_server
from tela.shell.result import Result


TOKEN = "session-recovery-test"
BRIDGE_ID = "bridge_session_recovery"
INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": "init",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "session-recovery-test",
                "version": "1",
                "tela_bridge_connection_id": BRIDGE_ID,
            },
        },
    }
).encode()
CALL = b'{"jsonrpc":"2.0","id":"call","method":"tools/call","params":{"name":"tela_list_profiles","arguments":{}}}'


@pytest.fixture
def gateway_url() -> Iterator[str]:
    """Serve the production MCP app without using the shared gateway/lockfile."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        started = asyncio.run(
            gateway_start(
                GatewayStartupConfig(
                    transport=GatewayTransport.HTTP,
                    port=port,
                    auth_mode=AuthMode.OPEN,
                    default_profile="dev",
                ),
                tela_config=TelaConfig(
                    auth=AuthConfig(mode=AuthMode.OPEN),
                    profiles={"dev": ProfileConfig(name="dev", default=True)},
                    resolved_default_profile="dev",
                ),
                expected_bearer_token=TOKEN,
            )
        )
        assert started.is_ok, started.error
        try:
            app = get_upstream_http_app()
            assert app.is_ok and app.value is not None
            manager = with_upstream_server(lambda server: server.session_manager).value
            assert manager is not None
            # Compress an enabled SDK timeout; preserve the disabled policy. The
            # SDK's actual idle timer and HTTP session lookup still execute.
            if manager.session_idle_timeout is not None:
                manager.session_idle_timeout = 0.05
            if manager.max_sessions is not None:
                manager.max_sessions = 1
            server = uvicorn.Server(
                uvicorn.Config(app.value, log_level="critical", ws="none")
            )
            thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{port}"
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                assert not thread.is_alive(), "test gateway failed to stop"
        finally:
            asyncio.run(gateway_shutdown())


def _register_and_initialize(base_url: str) -> str:
    registered = connect_bridge.post_json(
        url=f"{base_url}/connect", bearer_token=TOKEN, payload={"server_name": BRIDGE_ID}
    )
    assert registered.is_ok, registered.error
    initialized = connect_bridge.post_mcp_message(
        mcp_url=f"{base_url}/mcp", bearer_token=TOKEN, payload=INITIALIZE
    )
    assert initialized.is_ok, initialized.error
    assert initialized.value is not None
    session_id = initialized.value[2]
    assert session_id
    return session_id


def _call(
    base_url: str,
    session_id: str,
    recover_transport: Callable[[], Result[tuple[str, str], str]] | None = None,
) -> Result[connect_bridge.ForwardedBridgeResponse, str]:
    return connect_bridge._forward_request_with_recovery(
        mcp_url=f"{base_url}/mcp",
        bearer_token=TOKEN,
        message=CALL,
        session_id=session_id,
        message_method="tools/call",
        initialize_payload=INITIALIZE,
        max_recovery_attempts=3,
        recover_transport=recover_transport,
        bridge_connection_id=BRIDGE_ID,
    )


def _assert_profile_result(response: connect_bridge.ForwardedBridgeResponse) -> None:
    assert len(response.response_messages) == 1
    payload = json.loads(response.response_messages[0])
    assert payload["id"] == "call"
    assert "error" not in payload
    assert payload["result"].get("isError", False) is False
    content = payload["result"]["content"][0]
    assert content["type"] == "resource"
    assert content["resource"]["mimeType"] == "application/json"
    profiles = json.loads(content["resource"]["text"])
    assert profiles == [{"profile_id": "dev", "capabilities": {}, "default": True}]


def test_gateway_session_survives_sdk_idle_deadline(gateway_url: str) -> None:
    session_id = _register_and_initialize(gateway_url)
    time.sleep(0.15)

    result = _call(gateway_url, session_id)

    assert result.is_ok, result.error
    assert result.value is not None
    assert result.value.session_id == session_id
    _assert_profile_result(result.value)


def test_gateway_does_not_inherit_sdk_cumulative_session_cap(gateway_url: str) -> None:
    first_session = _register_and_initialize(gateway_url)
    disconnected = connect_bridge.post_json(
        url=f"{gateway_url}/disconnect",
        bearer_token=TOKEN,
        payload={"connection_id": BRIDGE_ID},
    )
    assert disconnected.is_ok, disconnected.error

    second_session = _register_and_initialize(gateway_url)

    assert second_session != first_session
    result = _call(gateway_url, second_session)
    assert result.is_ok, result.error
    assert result.value is not None
    _assert_profile_result(result.value)


def test_bridge_reinitializes_missing_session_and_executes_call_once(gateway_url: str) -> None:
    session_id = _register_and_initialize(gateway_url)
    # DELETE discards the SDK session, so the next POST encounters the real
    # pre-handler 404 path also used for expiry and gateway restarts.
    request = Request(
        f"{gateway_url}/mcp",
        method="DELETE",
        headers={"Authorization": f"Bearer {TOKEN}", "mcp-session-id": session_id},
    )
    with urlopen(request, timeout=3) as response:
        assert response.status == 200
    clear_audit_entries()
    recoveries = 0

    def recover_transport() -> Result[tuple[str, str], str]:
        nonlocal recoveries
        recoveries += 1
        if recoveries > 3:
            return Result(error="BRIDGE_RECOVERY_EXHAUSTED: test recovery limit")
        registered = connect_bridge.post_json(
            url=f"{gateway_url}/connect", bearer_token=TOKEN, payload={"server_name": BRIDGE_ID}
        )
        assert registered.is_ok, registered.error
        return Result(value=(f"{gateway_url}/mcp", TOKEN))

    recovered = _call(gateway_url, session_id, recover_transport)

    assert recovered.is_ok, recovered.error
    assert recovered.value is not None
    _assert_profile_result(recovered.value)
    new_session_id = recovered.value.session_id
    assert new_session_id and new_session_id != session_id
    assert recoveries == 1
    entries = get_audit_entries()
    assert entries.is_ok and entries.value is not None
    calls = [entry for entry in entries.value if entry.tool_name == "tela_list_profiles"]
    assert len(calls) == 1
    assert calls[0].connection_id == BRIDGE_ID
    assert calls[0].profile_id == "dev"

    subsequent = _call(gateway_url, new_session_id, recover_transport)
    assert subsequent.is_ok, subsequent.error
    assert subsequent.value is not None
    _assert_profile_result(subsequent.value)
    assert subsequent.value.session_id == new_session_id
    assert recoveries == 1
    entries = get_audit_entries()
    assert entries.is_ok and entries.value is not None
    assert len([entry for entry in entries.value if entry.tool_name == "tela_list_profiles"]) == 2
