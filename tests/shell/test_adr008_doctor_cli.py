"""ADR-008 doctor CLI recovery tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from tela.cli import main as cli_main
from tela.commands.doctor_cmd import doctor_command
from tela.core.classification import (
    AttachmentDisplayState,
    ClientAttachment,
    Recoverability,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeState,
)
from tela.core.models import LockfileData
from tela.shell import lockfile as lockfile_module
from tela.shell.adr008_registry_events import (
    append_runtime_event,
    runtime_events_path,
    upsert_client_attachment,
)
from tela.shell.result import Result


def _write_lockfile(path: Path, *, host: str, port: int, pid: int | None = None) -> LockfileData:
    data = LockfileData(
        pid=os.getpid() if pid is None else pid,
        host=host,
        port=port,
        token="doctor-token",
        started_at="2026-04-25T12:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data.model_dump_json(), encoding="utf-8")
    return data


class _DoctorStatusHandler(BaseHTTPRequestHandler):
    payload: dict[str, object] = {"state": "ready"}

    def do_GET(self) -> None:
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
        return


def _start_status_server(payload: dict[str, object]) -> HTTPServer:
    _DoctorStatusHandler.payload = payload
    server = HTTPServer(("127.0.0.1", 0), _DoctorStatusHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_doctor_probe_timeout_requires_recover() -> None:
    exit_code = cli_main(["doctor", "--probe-timeout", "1"])

    assert exit_code == 1


def test_doctor_passive_reads_cached_state_without_events(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, host="127.0.0.1", port=59999)

    result = doctor_command(json_output=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["probe_performed"] is False
    assert parsed["recover_performed"] is False
    assert parsed["recovery"]["events_appended"] == []


def test_doctor_recover_already_ready_appends_probe_only(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    server = _start_status_server({"state": "ready"})
    _write_lockfile(lockfile_path, host=server.server_address[0], port=server.server_address[1])

    try:
        result = doctor_command(json_output=True, recover=True)
    finally:
        server.shutdown()
        server.server_close()

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["recovery"]["already_ready"] is True
    assert parsed["recovery"]["recovery_succeeded"] is False
    assert parsed["recovery"]["events_appended"] == ["recovery_probe"]


@pytest.mark.parametrize("state", ["starting", "degraded", "unknown"])
def test_doctor_recover_records_non_ready_probe_states(
    state: str, monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr(
        "tela.commands.doctor_cmd._autostart_serve",
        lambda *, config_path, default_profile: Result(error="AUTOSTART_FAILED: blocked"),
    )
    server = _start_status_server({"state": state})
    _write_lockfile(lockfile_path, host=server.server_address[0], port=server.server_address[1])

    try:
        result = doctor_command(json_output=True, recover=True)
    finally:
        server.shutdown()
        server.server_close()

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["recovery"]["events_appended"] == ["recovery_probe", "recovery_failed"]
    assert parsed["recovery"]["cold_start_attempted"] is True


def test_doctor_recover_absent_cold_start_failure(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", tmp_path / "gateway.lock")
    monkeypatch.setattr(
        "tela.commands.doctor_cmd._autostart_serve",
        lambda *, config_path, default_profile: Result(error="AUTOSTART_FAILED: boom"),
    )

    result = doctor_command(json_output=True, recover=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["shared_runtime"]["state"] == "absent"
    assert parsed["recovery"]["cold_start_attempted"] is True
    assert parsed["recovery"]["events_appended"] == ["recovery_probe", "recovery_failed"]


def test_doctor_recover_stale_cleanup_true_then_success(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", tmp_path / "gateway.lock")
    server = _start_status_server({"state": "ready"})
    live = LockfileData(
        pid=321,
        host=server.server_address[0],
        port=server.server_address[1],
        token="doctor-token",
        started_at="2026-04-25T12:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )
    monkeypatch.setattr(lockfile_module, "read_lockfile", lambda: Result(error="LOCKFILE_STALE: old"))
    monkeypatch.setattr(lockfile_module, "delete_lockfile_if_stale", lambda: Result(value=True))
    monkeypatch.setattr("tela.commands.doctor_cmd._autostart_serve", lambda *, config_path, default_profile: Result(value=321))
    monkeypatch.setattr("tela.commands.doctor_cmd._wait_for_live_lockfile", lambda timeout_seconds, expected_pid=None: Result(value=live))

    try:
        result = doctor_command(json_output=True, recover=True)
    finally:
        server.shutdown()
        server.server_close()

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["recovery"]["stale_cleanup"] is True
    assert parsed["recovery"]["recovery_succeeded"] is True
    assert parsed["recovery"]["events_appended"] == [
        "recovery_probe",
        "recovery_probe",
        "recovery_succeeded",
    ]


def test_doctor_recover_stale_cleanup_false_reprobes_ready(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", tmp_path / "gateway.lock")
    server = _start_status_server({"state": "ready"})
    live = LockfileData(
        pid=os.getpid(),
        host=server.server_address[0],
        port=server.server_address[1],
        token="doctor-token",
        started_at="2026-04-25T12:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )
    reads = iter([Result(error="LOCKFILE_STALE: old"), Result(value=live), Result(value=live)])
    monkeypatch.setattr(lockfile_module, "read_lockfile", lambda: next(reads))
    monkeypatch.setattr(lockfile_module, "delete_lockfile_if_stale", lambda: Result(value=False))

    try:
        result = doctor_command(json_output=True, recover=True)
    finally:
        server.shutdown()
        server.server_close()

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["recovery"]["stale_cleanup"] is False
    assert parsed["recovery"]["already_ready"] is True
    assert parsed["recovery"]["cold_start_attempted"] is False


def test_doctor_recover_stale_cleanup_error(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", tmp_path / "gateway.lock")
    monkeypatch.setattr(lockfile_module, "read_lockfile", lambda: Result(error="LOCKFILE_STALE: old"))
    monkeypatch.setattr(lockfile_module, "delete_lockfile_if_stale", lambda: Result(error="LOCKFILE_DELETE_ERROR: denied"))

    result = doctor_command(json_output=True, recover=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["recovery"]["error"] == "LOCKFILE_DELETE_ERROR: denied"
    assert parsed["recovery"]["events_appended"] == ["recovery_probe", "recovery_failed"]


def test_doctor_json_reports_malformed_registry_and_last_events(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, host="127.0.0.1", port=59999)
    tela_dir = tmp_path / ".tela"
    tela_dir.mkdir(mode=0o700, exist_ok=True)
    (tela_dir / "client-attachments.json").write_text("{ broken", encoding="utf-8")
    append_runtime_event(
        RuntimeEvent(
            kind=RuntimeEventKind.CLIENT_PROVIDER_EXIT,
            client_id="client-1",
            client_kind="test",
            timestamp="2026-04-25T12:00:00Z",
            details={"exit_code": 1},
        )
    )
    append_runtime_event(
        RuntimeEvent(
            kind=RuntimeEventKind.PROVIDER_TIMEOUT,
            client_id="provider:slow",
            client_kind="downstream_provider",
            timestamp="2026-04-25T12:01:00Z",
            details={"provider_name": "slow", "phase": "tools_list"},
        )
    )
    events_file = runtime_events_path().value
    assert events_file is not None
    events_file.write_text(events_file.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8")

    result = doctor_command(json_output=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert "ATTACHMENT_REGISTRY_PARSE_ERROR" in parsed["client_attachments"]["registry_parse_error"]
    assert parsed["runtime_events"]["last_provider_exit"]["kind"] == "client_provider_exit"
    assert parsed["runtime_events"]["last_provider_startup_event"]["kind"] == "provider_timeout"
    assert parsed["runtime_events"]["last_provider_startup_event"]["details"]["provider_name"] == "slow"
    assert parsed["runtime_events"]["malformed_line_count"] == 1


def test_doctor_client_attachments_alive_liveness_priority(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, host="127.0.0.1", port=59999)
    upsert_client_attachment(
        ClientAttachment(
            client_id="client-live",
            client_kind="test",
            display_state=AttachmentDisplayState.HEALTHY,
            runtime_state=RuntimeState.ACTIVE,
            recoverability=Recoverability.RECOVERABLE,
            connected_at=datetime.now(UTC).isoformat(),
            last_heartbeat=datetime.now(UTC).isoformat(),
        )
    )
    append_runtime_event(
        RuntimeEvent(
            kind=RuntimeEventKind.CLIENT_PROVIDER_EXIT,
            client_id="client-old",
            client_kind="test",
            timestamp="2026-04-25T11:00:00Z",
        )
    )

    result = doctor_command(json_output=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["client_attachments"]["client_attachments_alive"] is True
    assert parsed["client_attachments"]["liveness_reason"] == "client_attachments_alive"


def test_doctor_ignores_stale_active_attachment_heartbeat(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, host="127.0.0.1", port=59999)
    upsert_client_attachment(
        ClientAttachment(
            client_id="client-stale",
            client_kind="test",
            display_state=AttachmentDisplayState.HEALTHY,
            runtime_state=RuntimeState.ACTIVE,
            recoverability=Recoverability.RECOVERABLE,
            connected_at="2020-01-01T00:00:00Z",
            last_heartbeat="2020-01-01T00:00:00Z",
        )
    )

    result = doctor_command(json_output=True)

    assert result.is_ok
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["client_attachments"]["client_attachments_alive"] is False
    assert parsed["client_attachments"]["liveness_reason"] == "no_live_client_attachments"
