"""ADR-008 client attachment workflow proofs.

These tests pin the end-to-end attachment semantics requested by ADR-008:
client-neutral attachments share the gateway runtime, lockfiles are discovery
only, recovery failures are request-scoped, and operator recovery is explicit.
"""

from __future__ import annotations

import io
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from tela.commands import connect_bridge
from tela.commands.doctor_cmd import doctor_command
from tela.commands.status_cmd import status_command
from tela.core.classification import (
    AttachmentDisplayState,
    ClientAttachment,
    Recoverability,
    RuntimeEventKind,
    RuntimeState,
)
from tela.core.models import LockfileData
from tela.shell import lockfile as lockfile_module
from tela.shell.adr008_registry_events import (
    read_attachment_registry,
    read_runtime_events,
    runtime_events_path,
    upsert_client_attachment,
)
from tela.shell.result import Result


def _json_lines(output: bytes) -> list[dict[str, object]]:
    """Decode newline-delimited JSON output from the bridge.

    Args:
        output: Raw bytes written by ``forward_stdio_http``.

    Returns:
        Decoded JSON objects in write order.
    """

    return [json.loads(line) for line in output.decode("utf-8").splitlines()]


def _write_lockfile(path: Path, *, host: str, port: int, pid: int | None = None) -> None:
    """Write a lockfile fixture for operator command discovery.

    Args:
        path: Lockfile path patched into ``tela.shell.lockfile``.
        host: Runtime host value.
        port: Runtime port value.
        pid: Runtime PID; defaults to the current process.
    """

    data = LockfileData(
        pid=os.getpid() if pid is None else pid,
        host=host,
        port=port,
        token="adr008-token",
        started_at="2026-04-25T12:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data.model_dump_json(), encoding="utf-8")


class _StatusHandler(BaseHTTPRequestHandler):
    """Small real HTTP status endpoint used by workflow tests."""

    payload: dict[str, object] = {"state": "ready"}
    calls: int = 0

    def do_GET(self) -> None:
        """Serve ``GET /status`` with the configured JSON payload."""

        _StatusHandler.calls += 1
        if self.path != "/status":
            self.send_response(404)
            self.end_headers()
            return
        encoded = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        """Suppress fixture HTTP logging during tests."""

        return


def _start_status_server(payload: dict[str, object]) -> HTTPServer:
    """Start a real local status server.

    Args:
        payload: JSON payload returned from ``GET /status``.

    Returns:
        Started HTTP server; caller owns shutdown.
    """

    _StatusHandler.payload = payload
    _StatusHandler.calls = 0
    server = HTTPServer(("127.0.0.1", 0), _StatusHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _attachment(client_id: str, *, kind: str = "cli") -> ClientAttachment:
    """Build one active client attachment fixture.

    Args:
        client_id: Stable attachment identifier.
        kind: Client kind label to persist.

    Returns:
        Active recoverable attachment record.
    """

    return ClientAttachment(
        client_id=client_id,
        client_kind=kind,
        display_state=AttachmentDisplayState.HEALTHY,
        runtime_state=RuntimeState.ACTIVE,
        recoverability=Recoverability.RECOVERABLE,
        connected_at="2026-04-25T12:00:00Z",
        last_heartbeat="2026-04-25T12:01:00Z",
    )


def test_serve_idle_shutdown_does_not_terminate_connect_provider_loop(monkeypatch) -> None:
    """A request-scoped recovery failure must not terminate the connect loop."""

    requests = (
        b'{"jsonrpc":"2.0","id":"idle","method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":"after-idle","method":"tools/list"}\n'
    )
    writes = io.BytesIO()
    post_calls = {"count": 0}

    def _post_mcp_message(**_kwargs: object) -> Result[tuple[str, bytes, str | None], str]:
        post_calls["count"] += 1
        if post_calls["count"] == 1:
            return Result(error="MCP_FORWARD_FAILED: Connection refused")
        return Result(
            value=(
                "application/json",
                b'{"jsonrpc":"2.0","id":"after-idle","result":{"provider_loop":"alive"}}',
                None,
            )
        )

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_idle",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=writes,
        max_recovery_attempts=1,
        recover_transport=lambda: Result(error="GATEWAY_RECOVERY_FAILED: idle reaped runtime"),
    )

    assert result.is_ok
    messages = _json_lines(writes.getvalue())
    assert messages[0]["id"] == "idle"
    assert messages[0]["error"]["data"] == {"code": "RECOVERY_FAILED_FOR_REQUEST"}
    assert messages[1] == {
        "jsonrpc": "2.0",
        "id": "after-idle",
        "result": {"provider_loop": "alive"},
    }


def test_next_request_after_idle_failure_remains_request_scoped(monkeypatch) -> None:
    """Next request after idle either recovers or fails alone while loop continues."""

    requests = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}\n'
    )
    writes = io.BytesIO()
    post_calls = {"count": 0}

    def _post_mcp_message(**_kwargs: object) -> Result[tuple[str, bytes, str | None], str]:
        post_calls["count"] += 1
        if post_calls["count"] in {1, 2}:
            return Result(error="MCP_FORWARD_FAILED: Connection reset by peer")
        return Result(
            value=(
                "application/json",
                b'{"jsonrpc":"2.0","id":3,"result":{"recovered":true}}',
                None,
            )
        )

    recover_calls = {"count": 0}

    def _recover_transport() -> Result[tuple[str, str], str]:
        recover_calls["count"] += 1
        if recover_calls["count"] == 1:
            return Result(error="GATEWAY_RECOVERY_FAILED: runtime absent")
        return Result(value=("http://127.0.0.1:2/mcp", "new-token"))

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_next",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=writes,
        max_recovery_attempts=1,
        recover_transport=_recover_transport,
    )

    assert result.is_ok
    messages = _json_lines(writes.getvalue())
    assert messages[0]["error"]["data"] == {"code": "RECOVERY_FAILED_FOR_REQUEST"}
    assert messages[1] == {"jsonrpc": "2.0", "id": 3, "result": {"recovered": True}}


def test_lockfile_before_readiness_does_not_count_as_ready(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Lockfile discovery alone must not report a ready runtime."""

    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, host="127.0.0.1", port=59999)

    result = status_command(json_output=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["probe_performed"] is False
    assert parsed["shared_runtime"]["lockfile"] == "present"
    assert parsed["shared_runtime"]["state"] == "unknown"


def test_recovery_budgets_do_not_accumulate_across_unrelated_events(monkeypatch) -> None:
    """Request-level failures reset the per-request recovery budget."""

    requests = (
        b'{"jsonrpc":"2.0","id":"first","method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":"second","method":"tools/list"}\n'
    )
    writes = io.BytesIO()
    post_calls = {"count": 0}
    outstanding_attempts = {"count": 0}

    def _post_mcp_message(**_kwargs: object) -> Result[tuple[str, bytes, str | None], str]:
        post_calls["count"] += 1
        if post_calls["count"] in {1, 2}:
            return Result(error="MCP_FORWARD_FAILED: Connection refused")
        return Result(
            value=(
                "application/json",
                b'{"jsonrpc":"2.0","id":"second","result":{"budget":"reset"}}',
                None,
            )
        )

    def _recover_transport() -> Result[tuple[str, str], str]:
        if outstanding_attempts["count"] >= 1:
            return Result(error="BRIDGE_RECOVERY_EXHAUSTED: budget leaked")
        outstanding_attempts["count"] += 1
        if post_calls["count"] == 1:
            return Result(error="GATEWAY_RECOVERY_FAILED: first event failed")
        return Result(value=("http://127.0.0.1:2/mcp", "new-token"))

    monkeypatch.setattr(connect_bridge, "post_mcp_message", _post_mcp_message)

    result = connect_bridge.forward_stdio_http(
        mcp_url="http://127.0.0.1:1/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_budget",
        should_stop=lambda: False,
        stdin_buffer=io.BytesIO(requests),
        stdout_buffer=writes,
        max_recovery_attempts=1,
        recover_transport=_recover_transport,
        reset_recovery_attempts=lambda: outstanding_attempts.update(count=0),
    )

    assert result.is_ok
    messages = _json_lines(writes.getvalue())
    assert messages[0]["error"]["data"] == {"code": "RECOVERY_FAILED_FOR_REQUEST"}
    assert messages[1] == {
        "jsonrpc": "2.0",
        "id": "second",
        "result": {"budget": "reset"},
    }


def test_multiple_client_attachments_share_one_runtime_and_status_clients(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Two client attachments share one runtime endpoint in ``status --clients``."""

    monkeypatch.setenv("HOME", str(tmp_path))
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, host="127.0.0.1", port=49152)
    assert upsert_client_attachment(_attachment("client-a", kind="cli")).is_ok
    assert upsert_client_attachment(_attachment("client-b", kind="editor")).is_ok

    result = status_command(json_output=True, clients=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["shared_runtime"]["endpoint"] == "http://127.0.0.1:49152"
    assert {item["client_id"] for item in parsed["client_attachments"]} == {
        "client-a",
        "client-b",
    }


def test_transport_eof_exits_connect_and_records_host_transport_closed(
    monkeypatch, tmp_path: Path
) -> None:
    """EOF on the host transport exits connect and records HOST_TRANSPORT_CLOSED."""

    monkeypatch.setenv("HOME", str(tmp_path))
    state = connect_bridge.BridgeRuntimeState(
        base_url="http://127.0.0.1:1",
        host="127.0.0.1",
        port=1,
        bearer_token="token",
    )
    monkeypatch.setattr(
        connect_bridge,
        "_wait_for_gateway_readiness",
        lambda **_kwargs: Result(value=None),
    )
    monkeypatch.setattr(
        connect_bridge,
        "forward_stdio_http",
        lambda **_kwargs: Result(value=None),
    )

    result = connect_bridge._run_bridge_attach_loop(
        state=state,
        connection_id="bridge_eof",
        stop_requested=connect_bridge.Event(),
        max_recovery_attempts=1,
        recovery_config_path=None,
        recovery_default_profile=None,
        discover_or_autostart=None,
        client_id="client-eof",
        client_kind="cli",
        connected_at="2026-04-25T12:00:00Z",
    )

    assert result.is_ok
    events = read_runtime_events()
    assert events.is_ok and events.value is not None
    assert RuntimeEventKind.HOST_TRANSPORT_CLOSED in [event.kind for event in events.value.events]


def test_status_probe_does_not_cold_start_absent_runtime(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """``status --probe`` reports absent and does not invoke recovery/autostart."""

    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", tmp_path / "gateway.lock")

    result = status_command(json_output=True, probe=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["probe_performed"] is True
    assert parsed["shared_runtime"]["state"] == "absent"


def test_doctor_passive_does_not_mutate_without_recover(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Passive doctor reads diagnostics without creating recovery artifacts."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", tmp_path / "gateway.lock")

    result = doctor_command(json_output=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["recover_performed"] is False
    assert parsed["recovery"]["events_appended"] == []
    events_path = runtime_events_path().value
    assert events_path is not None
    assert not events_path.exists()


def test_doctor_recover_may_cold_start_and_records_recovery_events(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """``doctor --recover`` may cold-start and records probe/success events."""

    monkeypatch.setenv("HOME", str(tmp_path))
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    server = _start_status_server({"state": "ready"})
    live = LockfileData(
        pid=4321,
        host=server.server_address[0],
        port=server.server_address[1],
        token="adr008-token",
        started_at="2026-04-25T12:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )
    monkeypatch.setattr(
        "tela.commands.doctor_cmd._autostart_serve",
        lambda *, config_path, default_profile: Result(value=4321),
    )
    monkeypatch.setattr(
        "tela.commands.doctor_cmd._wait_for_live_lockfile",
        lambda timeout_seconds, expected_pid=None: Result(value=live),
    )

    try:
        result = doctor_command(json_output=True, recover=True)
    finally:
        server.shutdown()
        server.server_close()

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["recovery"]["cold_start_attempted"] is True
    assert parsed["recovery"]["recovery_succeeded"] is True
    assert parsed["recovery"]["events_appended"] == [
        "recovery_probe",
        "recovery_probe",
        "recovery_succeeded",
    ]
    events = read_runtime_events()
    assert events.is_ok and events.value is not None
    assert [event.kind.value for event in events.value.events] == parsed["recovery"]["events_appended"]
    assert read_attachment_registry().is_ok
