"""ADR-008 connect survival tests.

These tests pin the step-level survival contract for ``tela connect``:
in-flight recovery failures are surfaced as request-level JSON-RPC errors,
the provider loop remains available for later requests, and ADR-008
attachment/runtime diagnostic state is recorded best-effort.
"""

from __future__ import annotations

import io
import json

from tela.commands import connect_bridge, connect_cmd
from tela.core.classification import RuntimeEventKind
from tela.shell.adr008_registry_events import (
    read_attachment_registry,
    read_runtime_events,
)
from tela.shell.result import Result


def _json_lines(output: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.decode("utf-8").splitlines()]


def test_recovery_exhaustion_is_request_level_and_provider_loop_survives(
    monkeypatch,
) -> None:
    """Exhausted in-flight recovery must not terminate the provider loop."""

    requests = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    stdin_buffer = io.BytesIO(requests)
    stdout_buffer = io.BytesIO()
    post_calls = {"count": 0}

    def _post_mcp_message(**_kwargs) -> Result[tuple[str, bytes, str | None], str]:
        post_calls["count"] += 1
        if post_calls["count"] == 1:
            return Result(error="MCP_FORWARD_FAILED: Connection refused")
        return Result(
            value=(
                "application/json",
                b'{"jsonrpc":"2.0","id":2,"result":{"alive":true}}',
                None,
            )
        )

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr008",
        should_stop=lambda: False,
        stdin_buffer=stdin_buffer,
        stdout_buffer=stdout_buffer,
        max_recovery_attempts=0,
        recover_transport=lambda: Result(
            error="BRIDGE_RECOVERY_EXHAUSTED: in-flight MCP request could not be replayed"
        ),
    )

    assert result.is_ok
    messages = _json_lines(stdout_buffer.getvalue())
    assert len(messages) == 2
    first_error = messages[0]["error"]
    assert isinstance(first_error, dict)
    assert first_error["data"] == {"code": "BRIDGE_RECOVERY_EXHAUSTED"}
    assert messages[0]["id"] == 1
    assert messages[1] == {"jsonrpc": "2.0", "id": 2, "result": {"alive": True}}


def test_runtime_recovery_failure_is_request_level_and_loop_continues(
    monkeypatch,
) -> None:
    """Missing/stale runtime recovery failure stays scoped to one request."""

    requests = (
        b'{"jsonrpc":"2.0","id":"stale","method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":"next","method":"tools/list"}\n'
    )
    stdin_buffer = io.BytesIO(requests)
    stdout_buffer = io.BytesIO()
    post_calls = {"count": 0}

    def _post_mcp_message(**_kwargs) -> Result[tuple[str, bytes, str | None], str]:
        post_calls["count"] += 1
        if post_calls["count"] == 1:
            return Result(error="MCP_FORWARD_FAILED: Connection refused")
        return Result(
            value=(
                "application/json",
                b'{"jsonrpc":"2.0","id":"next","result":{"loop":"alive"}}',
                None,
            )
        )

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_adr008",
        should_stop=lambda: False,
        stdin_buffer=stdin_buffer,
        stdout_buffer=stdout_buffer,
        max_recovery_attempts=1,
        recover_transport=lambda: Result(
            error="GATEWAY_RECOVERY_FAILED: stale runtime could not be probed"
        ),
    )

    assert result.is_ok
    messages = _json_lines(stdout_buffer.getvalue())
    first_error = messages[0]["error"]
    assert isinstance(first_error, dict)
    assert first_error["data"] == {"code": "RECOVERY_FAILED_FOR_REQUEST"}
    assert messages[1] == {
        "jsonrpc": "2.0",
        "id": "next",
        "result": {"loop": "alive"},
    }


def test_connect_records_attachment_lifecycle_and_recovery_events(
    monkeypatch,
    tmp_path,
) -> None:
    """Connect lifecycle and recovery probe/failure events are persisted."""

    monkeypatch.setenv("HOME", str(tmp_path))

    def _post_json(**_kwargs) -> Result[None, str]:
        return Result(value=None)

    def _wait_for_gateway_readiness(**_kwargs) -> Result[None, str]:
        return Result(value=None)

    def _forward_stdio_http(**_kwargs) -> Result[None, str]:
        return Result(value=None)

    def _recover_gateway(**_kwargs) -> Result[tuple[str, int, str], str]:
        return Result(error="GATEWAY_RECOVERY_FAILED: probe failed")

    monkeypatch.setattr(connect_bridge, "post_json", _post_json)
    monkeypatch.setattr(
        connect_bridge, "_wait_for_gateway_readiness", _wait_for_gateway_readiness
    )
    monkeypatch.setattr(connect_bridge, "forward_stdio_http", _forward_stdio_http)
    monkeypatch.setattr(connect_bridge, "recover_gateway", _recover_gateway)

    bridge_result = connect_bridge.run_bridge(
        host="127.0.0.1",
        port=1,
        bearer_token="token",
        client_id="client-adr008",
        client_kind="cli",
    )
    state = connect_bridge.BridgeRuntimeState(
        base_url="http://127.0.0.1:1",
        host="127.0.0.1",
        port=1,
        bearer_token="token",
    )
    recovery_result = connect_bridge._recover_inflight_transport(
        state=state,
        connection_id="bridge-adr008",
        max_recovery_attempts=1,
        recovery_config_path=None,
        recovery_default_profile=None,
        discover_or_autostart=None,
        client_id="client-adr008",
        client_kind="cli",
    )

    registry_result = read_attachment_registry()
    events_result = read_runtime_events()

    assert bridge_result.is_ok
    assert recovery_result.is_err
    assert registry_result.is_ok
    assert events_result.is_ok
    assert registry_result.value is not None
    assert registry_result.value.attachments[0].client_id == "client-adr008"
    assert registry_result.value.attachments[0].client_kind == "cli"
    assert events_result.value is not None
    kinds = [event.kind for event in events_result.value.events]
    assert RuntimeEventKind.CLIENT_ATTACHMENT_STARTED in kinds
    assert RuntimeEventKind.HEARTBEAT in kinds
    assert RuntimeEventKind.RECOVERY_PROBE in kinds
    assert RuntimeEventKind.RECOVERY_FAILED in kinds
    assert RuntimeEventKind.CLIENT_PROVIDER_EXIT in kinds


def test_client_kind_precedence_and_recovery_attempt_bound(monkeypatch) -> None:
    """Client kind uses CLI > env > unknown and recovery attempts are bounded."""

    monkeypatch.setenv("TELA_CLIENT_KIND", "env-kind")

    assert connect_cmd._resolve_client_kind(cli_client_kind="cli-kind") == "cli-kind"
    assert connect_cmd._resolve_client_kind(cli_client_kind=None) == "env-kind"
    monkeypatch.delenv("TELA_CLIENT_KIND")
    assert connect_cmd._resolve_client_kind(cli_client_kind=None) == "unknown"

    result = connect_cmd.connect_command(max_recovery_attempts=-1)

    assert result.is_err
    assert result.error == "INVALID_MAX_RECOVERY_ATTEMPTS: must be >= 0"
