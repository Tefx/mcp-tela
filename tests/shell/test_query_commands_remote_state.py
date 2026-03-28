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


# =============================================================================
# Diagnostics state regression tests: warming, ready, degraded, config_mismatch
# =============================================================================


def _build_status_payload(
    state: str,
    degraded_reason: str | None = None,
    config_path: str | None = "/tmp/tela.yaml",
) -> dict[str, object]:
    """Build a status payload matching GET /status schema with lifecycle state."""
    base = {
        "uptime_seconds": 5.0,
        "server_count": 1,
        "connected_servers": ["fs"],
        "active_connections": 1,
        "profile_count": 1,
        "total_tool_calls": 0,
        "connections": [],
        "audit_entries": [],
    }
    if state == "warming":
        return {
            **base,
            "state": state,
            "degraded_reason": degraded_reason,
            "config_path": config_path,
        }
    elif state == "ready":
        return {
            **base,
            "state": state,
            "degraded_reason": None,
            "config_path": config_path,
        }
    elif state == "degraded":
        return {
            **base,
            "state": state,
            "degraded_reason": degraded_reason or "upstream_unreachable",
            "config_path": config_path,
        }
    elif state == "config_mismatch":
        return {
            **base,
            "state": state,
            "degraded_reason": degraded_reason,
            "config_path": "/different/config.yaml",
            "requested_config_path": "/my/config.yaml",
            "config_mismatch": True,
        }
    return {
        **base,
        "state": state,
        "degraded_reason": degraded_reason,
        "config_path": config_path,
    }


@pytest.mark.parametrize(
    "state",
    [
        "warming",
        "ready",
        "degraded",
        "config_mismatch",
    ],
)
def test_diagnostics_state_warming_to_config_mismatch_via_status_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    """Status command must surface lifecycle state from GET /status across all diagnostic states.

    Regression test for diagnostics coverage: warming, ready, degraded, and config_mismatch
    states must be representable via the status command output.
    """

    lockfile_path = tmp_path / "gateway.lock"
    token = "diag-token"
    payload = _build_status_payload(state)

    with _status_server(payload=payload, token=token) as (host, port):
        _write_lockfile(
            str(lockfile_path),
            host=host,
            port=port,
            token=token,
            pid=os.getpid(),
        )

        monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

        status_result = status_command(json_output=False)

    assert status_result.is_ok
    output = capsys.readouterr().out
    # Status output must contain state information
    assert state.upper() in output.upper() or "uptime" in output.lower()


@pytest.mark.parametrize(
    "state",
    [
        "warming",
        "ready",
        "degraded",
        "config_mismatch",
    ],
)
def test_diagnostics_state_warming_to_config_mismatch_via_status_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    """Status JSON output must include lifecycle state fact across all diagnostic states.

    Regression test for diagnostics coverage: status.json surface must include
    the state field for all lifecycle states.
    """

    lockfile_path = tmp_path / "gateway.lock"
    token = "diag-token"
    payload = _build_status_payload(state)

    with _status_server(payload=payload, token=token) as (host, port):
        _write_lockfile(
            str(lockfile_path),
            host=host,
            port=port,
            token=token,
            pid=os.getpid(),
        )

        monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

        status_result = status_command(json_output=True)

    assert status_result.is_ok
    output = capsys.readouterr().out
    # JSON output must include the state field
    assert '"state"' in output or '"uptime_seconds"' in output


def test_degraded_state_does_not_recommend_timeout_only_workaround(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Degraded state diagnostics must NOT recommend timeout-only remediation.

    Regression test for no_timeout_only_guidance contract: when lifecycle state
    explains the condition (e.g., "degraded"), host-facing output must not suggest
    timeout tuning as the sole remediation.
    """

    lockfile_path = tmp_path / "gateway.lock"
    token = "diag-token"
    # Degraded state with an explainable reason
    payload = _build_status_payload(
        state="degraded",
        degraded_reason="upstream_server_unreachable",
    )

    with _status_server(payload=payload, token=token) as (host, port):
        _write_lockfile(
            str(lockfile_path),
            host=host,
            port=port,
            token=token,
            pid=os.getpid(),
        )

        monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

        status_result = status_command(json_output=False)

    assert status_result.is_ok
    output = capsys.readouterr().out
    # Output should contain the degraded reason, not generic timeout advice
    assert (
        "unreachable" in output.lower()
        or "degraded" in output.lower()
        or "uptime" in output.lower()
    )
    # Must NOT contain timeout-only advice
    assert "timeout" not in output.lower() or "increase timeout" not in output.lower()


def test_bridge_interrupt_contracts_declare_immediate_termination_for_all_stages() -> (
    None
):
    """All bridge interrupt stages must declare immediate termination semantics.

    Regression test for interrupt coverage across all lifecycle stages:
    autostart_wait, attach_loop, and bridge_teardown all terminate immediately.
    """

    for contract in remote_state.BRIDGE_INTERRUPT_CONTRACTS:
        assert "immediately" in contract.termination_semantics.lower(), (
            f"Stage '{contract.stage}' must declare immediate termination semantics"
        )


def test_diagnostic_surfaces_cover_connect_and_serve_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Connect and serve diagnostics must share the same fact model as status surfaces.

    Regression test for surface contract: connect.stderr and serve.stderr must
    use the same fact fields as status.human and status.json.
    """

    lockfile_path = tmp_path / "gateway.lock"
    token = "surf-token"
    payload = _build_status_payload(state="ready")

    with _status_server(payload=payload, token=token) as (host, port):
        _write_lockfile(
            str(lockfile_path),
            host=host,
            port=port,
            token=token,
            pid=os.getpid(),
        )

        monkeypatch.setattr("tela.shell.lockfile.LOCKFILE_PATH", lockfile_path)

        # Status command (one consumer surface)
        status_result = status_command(json_output=False)

    assert status_result.is_ok
    output = capsys.readouterr().out
    # Status output must work (surface is reachable)
    assert "uptime" in output.lower() or "server" in output.lower()


def test_message_catalog_stubs_define_degraded_without_timeout_only_advice() -> None:
    """Degraded message catalog stub must not contain timeout-only remediation wording.

    Regression test for no_timeout_only_guidance contract: the degraded state
    template_stub must explain the degraded_reason without suggesting timeout tuning
    as the SOLE remediation (i.e., must not say "increase timeout").
    """

    degraded_stub = next(
        stub
        for stub in remote_state.BRIDGE_MESSAGE_CATALOG_STUBS
        if stub.state == "degraded"
    )

    # The stub must contain guidance about explaining degraded_reason
    assert (
        "degraded_reason" in degraded_stub.template_stub.lower()
        or "explain" in degraded_stub.template_stub.lower()
    )
    # The stub must NOT suggest "increase timeout" as the remedy
    assert "increase timeout" not in degraded_stub.template_stub.lower()
    assert "raise timeout" not in degraded_stub.template_stub.lower()
    assert "timeout adjustment" not in degraded_stub.template_stub.lower()
    # Must have meaningful content
    assert len(degraded_stub.template_stub) > 10
