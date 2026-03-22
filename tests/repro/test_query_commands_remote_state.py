"""Repro guard for query-command remote runtime wiring."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator

import pytest

from tela.cli import main


@contextmanager
def _status_server(payload: dict[str, object], token: str) -> Iterator[tuple[str, int]]:
    """Serve status payload over a live local HTTP endpoint."""

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

            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        assert isinstance(host, str)
        assert isinstance(port, int)
        yield host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _write_lockfile(path: Path, *, host: str, port: int, token: str) -> None:
    """Write lockfile content compatible with lockfile parser."""

    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                "token": token,
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/tmp/tela.yaml",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )


def test_cli_query_commands_read_remote_state_from_lockfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`tela status/connections/audit` should query live remote state via lockfile."""

    payload = {
        "uptime_seconds": 2.0,
        "server_count": 0,
        "connected_servers": [],
        "active_connections": 0,
        "profile_count": 1,
        "total_tool_calls": 0,
        "connections": [],
        "audit_entries": [],
    }
    token = "repro-token"
    lockfile_path = tmp_path / "gateway.lock"

    with _status_server(payload, token) as (host, port):
        _write_lockfile(lockfile_path, host=host, port=port, token=token)
        monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

        assert main(["status"]) == 0
        assert main(["connections"]) == 0
        assert main(["audit"]) == 0

    captured = capsys.readouterr().out
    assert "uptime: 2.0s" in captured
    assert "No active connections." in captured
