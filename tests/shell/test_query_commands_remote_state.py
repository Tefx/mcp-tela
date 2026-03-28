"""Tests for remote query-command state discovery via lockfile + HTTP."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Iterator

import pytest

from tela.commands.audit_cmd import audit_command
from tela.commands.connections_cmd import connections_command
from tela.commands import remote_state
from tela.commands.status_cmd import status_command


@contextmanager
def _status_server(payload: dict[str, object], token: str) -> Iterator[tuple[str, int]]:
    """Run a tiny live HTTP server that serves ``GET /status``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/status":
                self.send_response(404)
                self.end_headers()
                return

            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return

            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address  # type: ignore[misc]  # HTTPServer.server_address is (str, int)
        assert isinstance(host, str)
        assert isinstance(port, int)
        yield (host, port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _write_lockfile(
    lockfile_path: str, *, host: str, port: int, token: str, pid: int
) -> None:
    """Write lockfile payload that matches ``LockfileData`` contract."""

    with open(lockfile_path, "w", encoding="utf-8") as lockfile:
        lockfile.write(
            json.dumps(
                {
                    "pid": pid,
                    "host": host,
                    "port": port,
                    "token": token,
                    "started_at": "2026-03-22T10:00:00Z",
                    "config_path": "/tmp/tela.yaml",
                    "version": "0.1.0",
                }
            )
        )


def test_query_commands_use_remote_status_when_lockfile_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All query commands must read lockfile and query a live HTTP server."""

    lockfile_path = tmp_path / "gateway.lock"
    token = "query-token"
    payload = {
        "uptime_seconds": 12.5,
        "server_count": 1,
        "connected_servers": ["fs"],
        "active_connections": 1,
        "profile_count": 2,
        "total_tool_calls": 3,
        "connections": [
            {
                "connection_id": "bridge_123",
                "profile_name": "dev",
                "connected_at": "2026-03-22T10:00:05Z",
                "tool_call_count": 0,
            }
        ],
        "audit_entries": [
            {
                "timestamp": "2026-03-22T10:00:06Z",
                "level": "L1",
                "connection_id": "bridge_123",
                "profile_name": "dev",
                "tool_name": "read_file",
                "server_name": "fs",
                "verdict": "allow",
                "denied_by": None,
                "error_code": None,
                "latency_ms": 2.0,
                "param_hash": None,
                "request_content": None,
                "response_content": None,
                "meta": None,
            }
        ],
    }

    with _status_server(payload=payload, token=token) as (host, port):
        _write_lockfile(
            str(lockfile_path),
            host=host,
            port=port,
            token=token,
            pid=os.getpid(),
        )

        monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)
        monkeypatch.setattr(
            "tela.commands.status_cmd.gateway_status",
            lambda: (_ for _ in ()).throw(RuntimeError("in-process path used")),
            raising=False,
        )
        monkeypatch.setattr(
            "tela.commands.connections_cmd.gateway_connections",
            lambda: (_ for _ in ()).throw(RuntimeError("in-process path used")),
            raising=False,
        )
        monkeypatch.setattr(
            "tela.commands.audit_cmd.audit_query",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("in-process path used")
            ),
            raising=False,
        )

        status_result = status_command(json_output=False)
        connections_result = connections_command(json_output=True)
        audit_result = audit_command(json_output=False)

    output = capsys.readouterr().out
    assert status_result.is_ok
    assert connections_result.is_ok
    assert audit_result.is_ok
    assert "uptime: 12.5s" in output
    assert "bridge_123" in output
    assert "ALLOW read_file (fs) profile=dev" in output


def test_bridge_interrupt_contract_covers_hard_interrupt_stages() -> None:
    """Contract must declare immediate interrupt semantics for all bridge stages."""

    contracts = remote_state.BRIDGE_INTERRUPT_CONTRACTS
    assert [item.stage for item in contracts] == [
        "autostart_wait",
        "attach_loop",
        "bridge_teardown",
    ]
    for item in contracts:
        assert "immediately" in item.termination_semantics
        assert "stdout" in item.stdout_contract.lower()
        assert item.message_key.startswith("connect.interrupt.")


def test_bridge_diagnostic_surfaces_share_one_fact_model() -> None:
    """CLI and HTTP diagnostics must be pinned to one shared resolved fact set."""

    fact_names = {field.name for field in remote_state.BRIDGE_STATUS_FACT_FIELDS}
    status_json = next(
        surface
        for surface in remote_state.BRIDGE_DIAGNOSTIC_SURFACES
        if surface.surface == "status.json"
    )
    http_status = next(
        surface
        for surface in remote_state.BRIDGE_DIAGNOSTIC_SURFACES
        if surface.surface == "http.status"
    )

    assert set(status_json.fact_fields) == fact_names
    assert set(http_status.fact_fields) == fact_names
    assert status_json.fact_fields == http_status.fact_fields


def test_bridge_message_catalog_stubs_cover_required_host_states() -> None:
    """Contract must stub the required bridge startup and degradation states."""

    states = {item.state for item in remote_state.BRIDGE_MESSAGE_CATALOG_STUBS}
    assert states == {
        "discoverable",
        "warming",
        "ready",
        "degraded",
        "config_mismatch",
        "concurrent_startup_follower",
    }

    degraded = next(
        item
        for item in remote_state.BRIDGE_MESSAGE_CATALOG_STUBS
        if item.state == "degraded"
    )
    assert "timeout-only" in degraded.template_stub


@pytest.mark.parametrize(
    "command",
    [
        status_command,
        connections_command,
        audit_command,
    ],
)
def test_query_commands_report_missing_or_stale_lockfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    command,
) -> None:
    """Missing or stale lockfile must return a clear no-running-server error."""

    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

    missing_result = command()
    assert missing_result.is_err
    assert missing_result.error is not None
    assert "NO_RUNNING_SERVER" in missing_result.error

    _write_lockfile(
        str(lockfile_path),
        host="127.0.0.1",
        port=12345,
        token="token",
        pid=999999,
    )
    stale_result = command()
    assert stale_result.is_err
    assert stale_result.error is not None
    assert "NO_RUNNING_SERVER" in stale_result.error
