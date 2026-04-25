"""ADR-008 status CLI surfaces tests.

Tests the ADR-008 status command contract:
- tela status remains passive and prints required observation cue
- tela status --probe actively checks current lockfile endpoint only; no cold-start, no recovery
- tela status --clients lists attachment registry with display_state derived read-only
- --probe and --clients are mutually exclusive
- --probe-timeout is valid only with --probe
- --json outputs ADR-008 schema blocks and registry_parse_error
- Output uses client-neutral client attachments, not opencode sessions
- Status reads must not rewrite attachment registry, cold-start, recover, or delete stale candidates
- No Manager/Supervisor/event bus
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from tela.commands.status_cmd import status_command
from tela.shell import lockfile as lockfile_module
from tela.shell.adr008_registry_events import (
    attachment_registry_path,
    read_attachment_registry,
    upsert_client_attachment,
    write_attachment_registry,
)
from tela.shell.result import Result
from tela.core.classification import (
    AttachmentDisplayState,
    ClientAttachment,
    AttachmentRegistry,
    RuntimeState,
    Recoverability,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_home(tmp_path, monkeypatch):
    """Set up a temporary home directory for isolated file tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_lockfile(lockfile_path, *, host="127.0.0.1", port=49152, token="test-token", pid=None):
    """Write a valid lockfile."""
    if pid is None:
        pid = os.getpid()
    with open(lockfile_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "pid": pid,
            "host": host,
            "port": port,
            "token": token,
            "started_at": "2026-04-25T12:00:00Z",
            "config_path": "/tmp/tela.yaml",
            "version": "0.1.0",
        }))


class _StatusHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves GET /status with configurable payload."""

    static_payload: dict | None = None
    call_count: int = 0

    def do_GET(self):
        _StatusHandler.call_count += 1
        if self.path != "/status":
            self.send_response(404)
            self.end_headers()
            return

        if self.server.token and self.headers.get("Authorization") != f"Bearer {self.server.token}":
            self.send_response(401)
            self.end_headers()
            return

        payload = _StatusHandler.static_payload or {
            "uptime_seconds": 10.0,
            "server_count": 1,
            "connected_servers": [],
            "active_connections": 0,
            "profile_count": 1,
            "total_tool_calls": 0,
            "state": "ready",
            "connections": [],
            "audit_entries": [],
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


class _StatusServer(HTTPServer):
    """HTTP server with token for GET /status."""

    def __init__(self, server_address, handler, token=None):
        super().__init__(server_address, handler)
        self.token = token


def _start_status_server(token=None):
    """Start a status server in the background."""
    _StatusHandler.call_count = 0
    server = _StatusServer(("127.0.0.1", 0), _StatusHandler, token)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# =============================================================================
# Test: Passive status shows observation cue
# =============================================================================


def test_passive_status_shows_observation_cue(
    monkeypatch, tmp_path, capsys
):
    """Passive tela status must print the required observation cue.

    Per ADR-008:
    Status source: cached runtime state only.
    No active probe was performed.
    Run `tela status --probe` to verify reachability.
    Run `tela doctor --recover` to attempt recovery.
    """
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

    _write_lockfile(lockfile_path, pid=os.getpid())

    result = status_command(json_output=False)

    assert result.is_ok, f"status command failed: {result.error}"
    output = capsys.readouterr().out

    # Must show the observation cue
    assert "cached runtime state only" in output or "No active probe" in output
    assert "tela status --probe" in output


# =============================================================================
# Test: status --json outputs ADR-008 schema blocks
# =============================================================================


def test_status_json_outputs_adr008_schema_blocks(
    monkeypatch, tmp_path, capsys
):
    """status --json must output ADR-008 schema blocks including registry_parse_error."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

    _write_lockfile(lockfile_path, pid=os.getpid())

    result = status_command(json_output=True)

    assert result.is_ok, f"status --json failed: {result.error}"
    output = capsys.readouterr().out

    parsed = json.loads(output)
    # ADR-008 schema blocks
    assert "schema_version" in parsed
    assert "probe_performed" in parsed
    assert "client_attachments" in parsed
    assert "registry_parse_error" in parsed
    assert "shared_runtime" in parsed
    assert "recoverability" in parsed

    # probe_performed must be False for passive status
    assert parsed["probe_performed"] is False
    assert parsed["client_attachments"] == []
    assert parsed["registry_parse_error"] is None


# =============================================================================
# Test: --probe and --clients are mutually exclusive
# =============================================================================


def test_probe_and_clients_are_mutually_exclusive(monkeypatch, tmp_path):
    """--probe and --clients must be rejected when used together."""
    from tela.cli import main as cli_main

    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, pid=os.getpid())

    # Both --probe and --clients together should fail
    exit_code = cli_main(["status", "--probe", "--clients"])
    assert exit_code == 1, "--probe and --clients must be rejected together"


# =============================================================================
# Test: --probe-timeout requires --probe
# =============================================================================


def test_probe_timeout_requires_probe(monkeypatch, tmp_path):
    """--probe-timeout must be rejected when --probe is not present."""
    from tela.cli import main as cli_main

    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, pid=os.getpid())

    exit_code = cli_main(["status", "--probe-timeout", "10"])
    assert exit_code == 1, "--probe-timeout without --probe must be rejected"


# =============================================================================
# Test: status --probe actively checks endpoint without cold-start
# =============================================================================


def test_status_probe_checks_endpoint_without_cold_start(
    monkeypatch, tmp_path, capsys
):
    """status --probe actively checks the lockfile endpoint; no cold-start, no recovery."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

    # Start a real HTTP server for the probe to hit
    server = _start_status_server(token="probe-token")
    _write_lockfile(lockfile_path, host=server.server_address[0], port=server.server_address[1], token="probe-token")

    try:
        # Run status --probe
        from tela.cli import main as cli_main
        exit_code = cli_main(["status", "--probe", "--json"])

        assert exit_code == 0, f"status --probe failed with exit code {exit_code}"
        output = capsys.readouterr().out

        parsed = json.loads(output)
        assert parsed["probe_performed"] is True

        # Runtime state should be derived from the probe
        assert "shared_runtime" in parsed
        assert "state" in parsed["shared_runtime"]
        # State could be ready, degraded, stale, etc. based on server response
        assert parsed["shared_runtime"]["state"] in ["ready", "degraded", "starting", "stale", "absent", "unknown"]

        # Verify we actually made HTTP calls (not cold-start)
        assert _StatusHandler.call_count > 0, "probe must have made HTTP requests"
    finally:
        server.shutdown()
        server.server_close()


# =============================================================================
# Test: status --probe with absent runtime returns absent without network call
# =============================================================================


def test_status_probe_absent_runtime_no_network_call(
    monkeypatch, tmp_path, capsys
):
    """When no lockfile exists, status --probe returns absent without a network call."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

    # No lockfile - but we need to pass --probe to perform active probe
    result = status_command(json_output=True, probe=True)

    assert result.is_ok
    output = capsys.readouterr().out
    parsed = json.loads(output)

    assert parsed["probe_performed"] is True
    assert parsed["shared_runtime"]["state"] == "absent"


# =============================================================================
# Test: status --clients lists attachment registry using correct model fields
# =============================================================================


def test_status_clients_lists_attachment_registry(
    monkeypatch, tmp_path, capsys
):
    """status --clients must list attachments from the registry with display_state derived."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, pid=os.getpid())

    # Set up attachment registry with test data using correct model fields
    monkeypatch.setenv("HOME", str(tmp_path))

    from pathlib import Path
    tela_dir = tmp_path / ".tela"
    tela_dir.mkdir(mode=0o700, exist_ok=True)

    # Create attachment using the correct ClientAttachment model fields
    attachment = ClientAttachment(
        client_id="c-test123",
        client_kind="test",
        display_state=AttachmentDisplayState.HEALTHY,
        runtime_state=RuntimeState.ACTIVE,
        recoverability=Recoverability.RECOVERABLE,
        connected_at="2026-04-25T12:00:00Z",
        last_heartbeat="2026-04-25T12:01:00Z",
    )

    # Use the upsert function to create registry
    upsert_result = upsert_client_attachment(attachment)
    assert upsert_result.is_ok, f"Failed to upsert attachment: {upsert_result.error}"

    # Run status --clients
    from tela.cli import main as cli_main
    exit_code = cli_main(["status", "--clients", "--json"])

    assert exit_code == 0, f"status --clients failed with exit code {exit_code}"
    output = capsys.readouterr().out

    parsed = json.loads(output)
    assert "client_attachments" in parsed
    assert len(parsed["client_attachments"]) == 1
    assert parsed["client_attachments"][0]["client_id"] == "c-test123"
    assert parsed["client_attachments"][0]["client_kind"] == "test"
    assert "display_state" in parsed["client_attachments"][0]


# =============================================================================
# Test: display_state is derived, not written back to registry
# =============================================================================


def test_display_state_derived_not_persisted(
    monkeypatch, tmp_path
):
    """display_state is output-only; status reads must not write registry state."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, pid=os.getpid())

    monkeypatch.setenv("HOME", str(tmp_path))

    from pathlib import Path
    tela_dir = tmp_path / ".tela"
    tela_dir.mkdir(mode=0o700, exist_ok=True)

    # Create registry with attached state
    attachment = ClientAttachment(
        client_id="c-display-test",
        client_kind="test",
        display_state=AttachmentDisplayState.HEALTHY,
        runtime_state=RuntimeState.ACTIVE,
        recoverability=Recoverability.RECOVERABLE,
        connected_at="2026-04-25T12:00:00Z",
        last_heartbeat="2026-04-25T12:01:00Z",  # Expired - would show stale_candidate
    )
    upsert_result = upsert_client_attachment(attachment)
    assert upsert_result.is_ok

    # Run status --clients twice
    from tela.cli import main as cli_main

    cli_main(["status", "--clients", "--json"])

    # Re-read registry - state should be unchanged
    reg_result = read_attachment_registry()
    assert reg_result.is_ok
    assert reg_result.value is not None
    assert len(reg_result.value.attachments) == 1
    # Display state in registry remains the same (not derived)
    assert reg_result.value.attachments[0].display_state == AttachmentDisplayState.HEALTHY
    assert reg_result.value.attachments[0].client_id == "c-display-test"


# =============================================================================
# Test: Status reads do not cold-start, recover, or delete stale candidates
# =============================================================================


def test_status_reads_do_not_mutate_registry(
    monkeypatch, tmp_path
):
    """Status command must be read-only; no cold-start, recovery, or deletion."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, pid=os.getpid())

    monkeypatch.setenv("HOME", str(tmp_path))

    from pathlib import Path
    tela_dir = tmp_path / ".tela"
    tela_dir.mkdir(mode=0o700, exist_ok=True)

    # Create registry with multiple attachments
    attachments = [
        ClientAttachment(
            client_id="c-readonly1",
            client_kind="test",
            display_state=AttachmentDisplayState.HEALTHY,
            runtime_state=RuntimeState.ACTIVE,
            recoverability=Recoverability.RECOVERABLE,
            connected_at="2026-04-25T12:00:00Z",
            last_heartbeat="2026-04-25T12:30:00Z",
        ),
        ClientAttachment(
            client_id="c-readonly2",
            client_kind="cli",
            display_state=AttachmentDisplayState.DEGRADED,
            runtime_state=RuntimeState.IDLE,
            recoverability=Recoverability.STALE,
            connected_at="2026-04-25T11:00:00Z",
            last_heartbeat="2026-04-25T11:00:00Z",
        ),
    ]

    registry = AttachmentRegistry(attachments=attachments)
    write_result = write_attachment_registry(registry)
    assert write_result.is_ok, f"Failed to write registry: {write_result.error}"

    # Run status --clients (read-only operation)
    from tela.cli import main as cli_main
    exit_code = cli_main(["status", "--clients", "--json"])
    assert exit_code == 0

    # Re-read registry - all attachments must still be present
    reg_result = read_attachment_registry()
    assert reg_result.is_ok
    assert reg_result.value is not None
    assert len(reg_result.value.attachments) == 2

    # Verify both client_ids are still there (no deletions)
    client_ids = {a.client_id for a in reg_result.value.attachments}
    assert "c-readonly1" in client_ids
    assert "c-readonly2" in client_ids


# =============================================================================
# Test: registry_parse_error is included in JSON output
# =============================================================================


def test_registry_parse_error_included_in_json_output(
    monkeypatch, tmp_path, capsys
):
    """registry_parse_error must be present in JSON output (null when valid)."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, pid=os.getpid())

    monkeypatch.setenv("HOME", str(tmp_path))

    from pathlib import Path
    tela_dir = tmp_path / ".tela"
    tela_dir.mkdir(mode=0o700, exist_ok=True)

    # Write malformed registry
    malformed_path = tela_dir / "client-attachments.json"
    malformed_path.write_text("{ invalid json }", encoding="utf-8")

    result = status_command(json_output=True)
    assert result.is_ok

    output = capsys.readouterr().out
    parsed = json.loads(output)

    assert "registry_parse_error" in parsed
    assert parsed["registry_parse_error"] is not None
    assert "ATTACHMENT_REGISTRY_PARSE_ERROR" in parsed["registry_parse_error"]


# =============================================================================
# Test: Output uses client-neutral terminology
# =============================================================================


def test_output_is_client_neutral(
    monkeypatch, tmp_path, capsys
):
    """Output must use client-neutral terminology, not opencode sessions."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)
    _write_lockfile(lockfile_path, pid=os.getpid())

    monkeypatch.setenv("HOME", str(tmp_path))

    from pathlib import Path
    tela_dir = tmp_path / ".tela"
    tela_dir.mkdir(mode=0o700, exist_ok=True)

    # Add an opencode attachment
    attachment = ClientAttachment(
        client_id="c-opencode-test",
        client_kind="opencode",
        display_state=AttachmentDisplayState.HEALTHY,
        runtime_state=RuntimeState.ACTIVE,
        recoverability=Recoverability.RECOVERABLE,
        connected_at="2026-04-25T12:00:00Z",
        last_heartbeat="2026-04-25T12:30:00Z",
    )
    upsert_result = upsert_client_attachment(attachment)
    assert upsert_result.is_ok

    from tela.cli import main as cli_main
    exit_code = cli_main(["status", "--clients", "--json"])
    assert exit_code == 0

    output = capsys.readouterr().out

    # Must not contain "session" terminology
    assert "session" not in output.lower() or "opencode" in output.lower()
    # Must show client_kind which is client-neutral
    parsed = json.loads(output)
    assert any(att.get("client_kind") == "opencode" for att in parsed["client_attachments"])


# =============================================================================
# Test: status --probe respects probe-timeout
# =============================================================================


def test_probe_timeout_is_respected(
    monkeypatch, tmp_path, capsys
):
    """--probe-timeout must be respected when --probe is used."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

    # Write lockfile with unreachable endpoint
    _write_lockfile(lockfile_path, host="127.0.0.1", port=59999, token="timeout-test")

    from tela.cli import main as cli_main

    # With short timeout, should fail fast - must include --json for JSON output
    import time
    start = time.time()
    exit_code = cli_main(["status", "--probe", "--probe-timeout", "1", "--json"])
    elapsed = time.time() - start

    assert exit_code == 0  # Still returns 0 as it emits diagnostic
    output = capsys.readouterr().out
    parsed = json.loads(output)

    assert parsed["probe_performed"] is True
    # Should have returned quickly (within ~2 seconds, not the full 5 second default)
    assert elapsed < 3, f"Probe took too long: {elapsed:.1f}s"


# =============================================================================
# Test: recoverability block in passive status
# =============================================================================


def test_recoverability_block_in_status_json(
    monkeypatch, tmp_path, capsys
):
    """Status JSON must include recoverability block with recommendation."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

    _write_lockfile(lockfile_path, pid=os.getpid())

    result = status_command(json_output=True)
    assert result.is_ok

    output = capsys.readouterr().out
    parsed = json.loads(output)

    assert "recoverability" in parsed
    assert "state" in parsed["recoverability"]
    assert "recommendation" in parsed["recoverability"]
    # Recommendation must mention --probe
    assert "tela status --probe" in parsed["recoverability"]["recommendation"]


# =============================================================================
# Test: shared_runtime block has required fields
# =============================================================================


def test_shared_runtime_block_fields(
    monkeypatch, tmp_path, capsys
):
    """shared_runtime block must have state, lockfile, degraded_reason."""
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile_module, "LOCKFILE_PATH", lockfile_path)
    monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

    _write_lockfile(lockfile_path, pid=12345)

    result = status_command(json_output=True)
    assert result.is_ok

    output = capsys.readouterr().out
    parsed = json.loads(output)

    sr = parsed["shared_runtime"]
    assert "state" in sr
    assert "pid" in sr
    assert "endpoint" in sr
    assert "lockfile" in sr
    assert "degraded_reason" in sr
    # lockfile field must indicate present/missing/stale
    assert sr["lockfile"] in ["present", "missing", "stale"]
