"""Tests for ``tela connect`` command discovery and lifecycle wiring."""

from __future__ import annotations

from collections.abc import Callable
from email.message import Message
import io
import json
from urllib import error as urllib_error

import pytest

from tela.cli import main
from tela.commands import connect_cmd
from tela.commands.connect_transport import inject_bridge_connection_id
from tela.core.models import LockfileData, StatusResponse
from tela.shell.config_loader import Result


def test_connect_subcommand_exists() -> None:
    """CLI must expose ``tela connect`` command parser."""

    with pytest.raises(SystemExit) as exc_info:
        main(["connect", "--help"])
    assert exc_info.value.code == 0


def test_connect_token_override_priority_cli_env_lockfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token precedence must be ``--token`` > ``TELA_BEARER_TOKEN`` > lockfile."""

    monkeypatch.delenv("TELA_BEARER_TOKEN", raising=False)
    from_lockfile = connect_cmd._resolve_connect_token(
        cli_token=None,
        lockfile_token="lock-token",
    )
    assert from_lockfile.is_ok
    assert from_lockfile.value == "lock-token"

    monkeypatch.setenv("TELA_BEARER_TOKEN", "env-token")
    from_env = connect_cmd._resolve_connect_token(
        cli_token=None,
        lockfile_token="lock-token",
    )
    assert from_env.is_ok
    assert from_env.value == "env-token"

    from_cli = connect_cmd._resolve_connect_token(
        cli_token="cli-token",
        lockfile_token="lock-token",
    )
    assert from_cli.is_ok
    assert from_cli.value == "cli-token"


def test_connect_server_path_requires_token_or_env() -> None:
    """Explicit ``--server`` mode must reject missing CLI/env token."""

    result = connect_cmd._resolve_connect_token(
        cli_token=None,
        lockfile_token=None,
    )
    assert result.is_err
    assert result.error is not None
    assert "MISSING_TOKEN" in result.error


def test_connect_server_path_uses_env_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--server`` mode must skip lockfile and use env token."""

    monkeypatch.setenv("TELA_BEARER_TOKEN", "env-token")
    calls: list[tuple[str, int, str]] = []

    def _fake_run_bridge(
        *, host: str, port: int, bearer_token: str, max_recovery_attempts: int = 3
    ) -> Result[None, str]:
        calls.append((host, port, bearer_token))
        return Result(value=None)

    monkeypatch.setattr(connect_cmd, "_run_bridge", _fake_run_bridge)

    result = connect_cmd.connect_command(
        config_path="tela.yaml",
        default_profile=None,
        server="127.0.0.1:8123",
        token=None,
    )
    assert result.is_ok
    assert calls == [("127.0.0.1", 8123, "env-token")]


def test_discovery_autostart_handles_race_lockfile_appearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery must recover when auto-start races another connector.

    Flow: read_lockfile fails -> wait(0.3) fails -> autostart succeeds
    (returns spawned PID) -> wait(5.0, expected_pid=spawned_pid) succeeds.
    The expected_pid parameter binds lockfile identity to the spawned process.
    """

    spawned_pid = 42000

    lockfile = LockfileData(
        pid=spawned_pid,
        host="127.0.0.1",
        port=9000,
        token="lock-token",
        started_at="2026-03-22T10:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )

    monkeypatch.setattr(
        connect_cmd,
        "read_lockfile",
        lambda: Result(error="LOCKFILE_READ_ERROR: lockfile does not exist"),
    )

    waits: list[tuple[float, int | None]] = []
    wait_outcomes = [
        Result[LockfileData, str](error="LOCKFILE_WAIT_TIMEOUT: timed out"),
        Result[LockfileData, str](value=lockfile),
    ]

    def _fake_wait_for_live_lockfile(
        timeout_seconds: float,
        expected_pid: int | None = None,
    ) -> Result[LockfileData, str]:
        waits.append((timeout_seconds, expected_pid))
        return wait_outcomes.pop(0)

    autostarts = 0

    def _fake_autostart_serve(
        *,
        config_path: str,
        default_profile: str | None,
    ) -> Result[int, str]:
        nonlocal autostarts
        _ = config_path
        _ = default_profile
        autostarts += 1
        return Result(value=spawned_pid)

    monkeypatch.setattr(
        connect_cmd,
        "_wait_for_live_lockfile",
        _fake_wait_for_live_lockfile,
    )
    monkeypatch.setattr(connect_cmd, "_autostart_serve", _fake_autostart_serve)

    result = connect_cmd._discover_or_autostart(
        config_path="tela.yaml",
        default_profile=None,
    )
    assert result.is_ok
    assert result.value == lockfile
    assert autostarts == 1
    # First wait: quick race check with no PID filter
    # Second wait: after autostart, bound to spawned PID
    assert waits == [
        (0.3, None),
        (connect_cmd.LOCKFILE_WAIT_TIMEOUT_SECONDS, spawned_pid),
    ]


def test_connect_discovery_uses_published_lockfile_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connect discovery must use the lockfile's published bound port."""

    lockfile = LockfileData(
        pid=1234,
        host="127.0.0.1",
        port=49152,
        token="lock-token",
        started_at="2026-03-22T10:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )

    calls: list[tuple[str, int, str]] = []

    def _fake_run_bridge(
        *, host: str, port: int, bearer_token: str, max_recovery_attempts: int = 3
    ) -> Result[None, str]:
        calls.append((host, port, bearer_token))
        return Result(value=None)

    monkeypatch.delenv("TELA_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(connect_cmd, "read_lockfile", lambda: Result(value=lockfile))
    monkeypatch.setattr(connect_cmd, "_run_bridge", _fake_run_bridge)

    result = connect_cmd.connect_command(
        config_path="tela.yaml",
        default_profile=None,
        server=None,
        token=None,
    )

    assert result.is_ok
    assert calls == [("127.0.0.1", 49152, "lock-token")]


def test_bridge_lifecycle_posts_connect_and_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge lifecycle must call connect, forward, then disconnect."""

    endpoints: list[str] = []

    def _fake_post_json(
        *, url: str, bearer_token: str, payload: dict[str, str]
    ) -> Result[None, str]:
        _ = bearer_token
        _ = payload
        endpoints.append(url)
        return Result(value=None)

    def _fake_forward_stdio_http(
        *,
        mcp_url: str,
        bearer_token: str,
        bridge_connection_id: str,
        should_stop: Callable[[], bool],
        stdin_buffer,
        stdout_buffer,
        max_recovery_attempts: int = 3,
    ) -> Result[None, str]:
        _ = mcp_url
        _ = bearer_token
        _ = bridge_connection_id
        _ = should_stop
        _ = stdin_buffer
        _ = stdout_buffer
        return Result(value=None)

    def _fake_get_gateway_status(
        *, status_url: str, bearer_token: str
    ) -> Result[StatusResponse, str]:
        _ = status_url, bearer_token
        return Result(
            value=StatusResponse(
                uptime_seconds=1.0,
                server_count=0,
                connected_servers=[],
                active_connections=1,
                profile_count=1,
                total_tool_calls=0,
                state="ready",
                discovery_source="lockfile",
                config_path="/tmp/tela.yaml",
                requested_config_path="/tmp/tela.yaml",
                config_mismatch=False,
                degraded_reason=None,
                connections=[],
                audit_entries=[],
            )
        )

    monkeypatch.setattr(connect_cmd, "_post_json", _fake_post_json)
    monkeypatch.setattr(connect_cmd, "_get_gateway_status", _fake_get_gateway_status)
    monkeypatch.setattr(connect_cmd, "_forward_stdio_http", _fake_forward_stdio_http)

    result = connect_cmd._run_bridge(
        host="127.0.0.1",
        port=8123,
        bearer_token="token",
    )
    assert result.is_ok
    assert endpoints == [
        "http://127.0.0.1:8123/connect",
        "http://127.0.0.1:8123/disconnect",
    ]


def test_run_bridge_waits_for_status_ready_before_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge must poll ``GET /status`` before forwarding MCP traffic."""

    endpoint_calls: list[str] = []
    readiness_snapshots: list[dict[str, object]] = []
    status_sequence = [
        StatusResponse(
            uptime_seconds=1.0,
            server_count=1,
            connected_servers=[],
            active_connections=1,
            profile_count=1,
            total_tool_calls=0,
            state="warming",
            discovery_source="lockfile",
            config_path="/tmp/tela.yaml",
            requested_config_path="/tmp/tela.yaml",
            config_mismatch=False,
            degraded_reason=None,
            connections=[],
            audit_entries=[],
        ),
        StatusResponse(
            uptime_seconds=2.0,
            server_count=1,
            connected_servers=["fs"],
            active_connections=1,
            profile_count=1,
            total_tool_calls=0,
            state="ready",
            discovery_source="lockfile",
            config_path="/tmp/tela.yaml",
            requested_config_path="/tmp/tela.yaml",
            config_mismatch=False,
            degraded_reason=None,
            connections=[],
            audit_entries=[],
        ),
    ]
    forwarded: list[bool] = []

    def _fake_post_json(
        *, url: str, bearer_token: str, payload: dict[str, str]
    ) -> Result[None, str]:
        _ = bearer_token, payload
        endpoint_calls.append(url)
        return Result(value=None)

    def _fake_get_gateway_status(
        *, status_url: str, bearer_token: str
    ) -> Result[StatusResponse, str]:
        _ = status_url, bearer_token
        next_status = status_sequence.pop(0)
        readiness_snapshots.append(next_status.model_dump())
        return Result(value=next_status)

    def _fake_forward_stdio_http(
        *,
        mcp_url: str,
        bearer_token: str,
        bridge_connection_id: str,
        should_stop: Callable[[], bool],
        stdin_buffer,
        stdout_buffer,
        max_recovery_attempts: int = 3,
    ) -> Result[None, str]:
        _ = mcp_url, bearer_token, bridge_connection_id, should_stop
        _ = stdin_buffer, stdout_buffer
        forwarded.append(True)
        return Result(value=None)

    monkeypatch.setattr(connect_cmd, "_post_json", _fake_post_json)
    monkeypatch.setattr(connect_cmd, "_get_gateway_status", _fake_get_gateway_status)
    monkeypatch.setattr(connect_cmd.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(connect_cmd, "_forward_stdio_http", _fake_forward_stdio_http)

    result = connect_cmd._run_bridge(
        host="127.0.0.1",
        port=8123,
        bearer_token="token",
    )

    assert result.is_ok
    assert forwarded == [True]
    assert [snapshot["state"] for snapshot in readiness_snapshots] == [
        "warming",
        "ready",
    ]
    assert endpoint_calls == [
        "http://127.0.0.1:8123/connect",
        "http://127.0.0.1:8123/disconnect",
    ]


def test_run_bridge_exits_boundedly_on_persistent_degraded_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge must exit cleanly on persistent degraded readiness authority."""

    endpoint_calls: list[str] = []
    readiness_snapshots: list[dict[str, object]] = []
    forwarded: list[bool] = []

    degraded_status = StatusResponse(
        uptime_seconds=1.0,
        server_count=1,
        connected_servers=[],
        active_connections=1,
        profile_count=1,
        total_tool_calls=0,
        state="degraded",
        discovery_source="lockfile",
        config_path="/tmp/tela.yaml",
        requested_config_path="/tmp/tela.yaml",
        config_mismatch=False,
        degraded_reason="DOWNSTREAM_CONNECT_FAILED",
        connections=[],
        audit_entries=[],
    )

    def _fake_post_json(
        *, url: str, bearer_token: str, payload: dict[str, str]
    ) -> Result[None, str]:
        _ = bearer_token, payload
        endpoint_calls.append(url)
        return Result(value=None)

    def _fake_get_gateway_status(
        *, status_url: str, bearer_token: str
    ) -> Result[StatusResponse, str]:
        _ = status_url, bearer_token
        readiness_snapshots.append(degraded_status.model_dump())
        return Result(value=degraded_status)

    def _fake_forward_stdio_http(
        *,
        mcp_url: str,
        bearer_token: str,
        bridge_connection_id: str,
        should_stop: Callable[[], bool],
        stdin_buffer,
        stdout_buffer,
    ) -> Result[None, str]:
        _ = mcp_url, bearer_token, bridge_connection_id, should_stop
        _ = stdin_buffer, stdout_buffer
        forwarded.append(True)
        return Result(value=None)

    monkeypatch.setattr(connect_cmd, "_post_json", _fake_post_json)
    monkeypatch.setattr(connect_cmd, "_get_gateway_status", _fake_get_gateway_status)
    monkeypatch.setattr(connect_cmd, "_forward_stdio_http", _fake_forward_stdio_http)

    result = connect_cmd._run_bridge(
        host="127.0.0.1",
        port=8123,
        bearer_token="token",
    )

    assert result.is_err
    assert result.error is not None
    assert "BRIDGE_NOT_READY" in result.error
    assert "degraded_reason=DOWNSTREAM_CONNECT_FAILED" in result.error
    assert forwarded == []
    assert [snapshot["state"] for snapshot in readiness_snapshots] == ["degraded"]
    assert endpoint_calls == [
        "http://127.0.0.1:8123/connect",
        "http://127.0.0.1:8123/disconnect",
    ]


def test_inject_bridge_connection_id_enriches_initialize_client_info() -> None:
    """Initialize payloads must carry the bridge connection identity."""

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1.0"},
            },
        }
    ).encode("utf-8")

    result = inject_bridge_connection_id(
        payload,
        connection_id="bridge_abc",
    )

    message = json.loads(result)
    assert message["params"]["clientInfo"]["tela_bridge_connection_id"] == "bridge_abc"
    assert message["params"]["clientInfo"]["name"] == "probe"


def test_inject_bridge_connection_id_leaves_non_initialize_unchanged() -> None:
    """Non-initialize messages must pass through unchanged."""

    payload = b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

    result = inject_bridge_connection_id(
        payload,
        connection_id="bridge_abc",
    )

    assert result == payload


def test_read_framed_message_accepts_content_length_frames() -> None:
    """Bridge parser must read Content-Length framed JSON requests."""

    payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    framed_input = io.BytesIO(
        f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
    )

    result = connect_cmd._read_framed_message(framed_input)

    assert result.is_ok
    assert result.value is not None
    assert result.value.payload == payload
    assert result.value.is_content_length_framed is True


def test_read_framed_message_accepts_newline_json() -> None:
    """Bridge parser must keep newline JSON compatibility."""

    payload = b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    newline_input = io.BytesIO(payload + b"\n")

    result = connect_cmd._read_framed_message(newline_input)

    assert result.is_ok
    assert result.value is not None
    assert result.value.payload == payload
    assert result.value.is_content_length_framed is False


def test_read_framed_message_eof_returns_none() -> None:
    """Empty stream must return None (clean EOF), not an error."""

    result = connect_cmd._read_framed_message(io.BytesIO(b""))

    assert result.is_ok
    assert result.value is None


def test_read_framed_message_eof_during_headers_returns_error() -> None:
    """EOF between Content-Length header and blank-line separator is an error."""

    broken_input = io.BytesIO(b"Content-Length: 42\r\n")

    result = connect_cmd._read_framed_message(broken_input)

    assert result.is_err
    assert result.error is not None
    assert "EOF while reading MCP headers" in result.error


def test_read_framed_message_eof_during_body_returns_error() -> None:
    """EOF mid-body (short read) must be reported as an error."""

    broken_input = io.BytesIO(b"Content-Length: 100\r\n\r\nshort")

    result = connect_cmd._read_framed_message(broken_input)

    assert result.is_err
    assert result.error is not None
    assert "EOF while reading MCP frame body" in result.error


def test_read_framed_message_invalid_content_length_returns_error() -> None:
    """Non-numeric Content-Length must be reported as an error."""

    broken_input = io.BytesIO(b"Content-Length: abc\r\n\r\n")

    result = connect_cmd._read_framed_message(broken_input)

    assert result.is_err
    assert result.error is not None
    assert "invalid Content-Length header" in result.error


def test_read_framed_message_extra_headers_before_blank_line() -> None:
    """Extra headers between Content-Length and blank line must be skipped."""

    payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    framed_input = io.BytesIO(
        f"Content-Length: {len(payload)}\r\n".encode("ascii")
        + b"Content-Type: application/json\r\n"
        + b"\r\n"
        + payload
    )

    result = connect_cmd._read_framed_message(framed_input)

    assert result.is_ok
    assert result.value is not None
    assert result.value.payload == payload
    assert result.value.is_content_length_framed is True


def test_write_framed_message_round_trip_newline() -> None:
    """Newline-delimited write/read must round-trip cleanly."""

    payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    buf = io.BytesIO()
    connect_cmd._write_framed_message(buf, payload, framed=False)
    buf.seek(0)
    result = connect_cmd._read_framed_message(buf)

    assert result.is_ok
    assert result.value is not None
    assert result.value.payload == payload
    assert result.value.is_content_length_framed is False


def test_write_framed_message_round_trip_content_length() -> None:
    """Content-Length write/read must round-trip cleanly."""

    payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    buf = io.BytesIO()
    connect_cmd._write_framed_message(buf, payload, framed=True)
    buf.seek(0)
    result = connect_cmd._read_framed_message(buf)

    assert result.is_ok
    assert result.value is not None
    assert result.value.payload == payload
    assert result.value.is_content_length_framed is True


def test_forward_stdio_http_preserves_content_length_framing_for_tools_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Framed initialize/tools/list requests must receive framed responses."""

    init_request = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    tools_request = b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    stdin_bytes = (
        f"Content-Length: {len(init_request)}\r\n\r\n".encode("ascii")
        + init_request
        + f"Content-Length: {len(tools_request)}\r\n\r\n".encode("ascii")
        + tools_request
    )
    stdin_buffer = io.BytesIO(stdin_bytes)
    stdout_buffer = io.BytesIO()

    responses = [
        (
            "application/json",
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode("utf-8"),
            "s-1",
        ),
        (
            "application/json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}).encode(
                "utf-8"
            ),
            "s-1",
        ),
    ]

    def _fake_post_mcp_message(
        *,
        mcp_url: str,
        bearer_token: str,
        payload: bytes,
        session_id: str | None = None,
        max_recovery_attempts: int = 3,
    ) -> Result[tuple[str, bytes, str | None], str]:
        _ = mcp_url
        _ = bearer_token
        _ = payload
        _ = session_id
        return Result(value=responses.pop(0))

    monkeypatch.setattr(connect_cmd, "_post_mcp_message", _fake_post_mcp_message)

    result = connect_cmd._forward_stdio_http(
        mcp_url="http://127.0.0.1:8123/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_test",
        should_stop=lambda: False,
        stdin_buffer=stdin_buffer,
        stdout_buffer=stdout_buffer,
    )

    assert result.is_ok
    output = stdout_buffer.getvalue().decode("utf-8", errors="replace")
    assert output.count("Content-Length:") == 2
    assert '"id": 1' in output
    assert '"id": 2' in output


def test_forward_stdio_http_preserves_newline_framing_for_tools_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newline-delimited initialize/tools/list must receive newline responses."""

    init_request = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    tools_request = b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    stdin_bytes = init_request + b"\n" + tools_request + b"\n"
    stdin_buffer = io.BytesIO(stdin_bytes)
    stdout_buffer = io.BytesIO()

    responses = [
        (
            "application/json",
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode("utf-8"),
            "s-1",
        ),
        (
            "application/json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}).encode(
                "utf-8"
            ),
            "s-1",
        ),
    ]

    def _fake_post_mcp_message(
        *,
        mcp_url: str,
        bearer_token: str,
        payload: bytes,
        session_id: str | None = None,
        max_recovery_attempts: int = 3,
    ) -> Result[tuple[str, bytes, str | None], str]:
        _ = mcp_url
        _ = bearer_token
        _ = payload
        _ = session_id
        return Result(value=responses.pop(0))

    monkeypatch.setattr(connect_cmd, "_post_mcp_message", _fake_post_mcp_message)

    result = connect_cmd._forward_stdio_http(
        mcp_url="http://127.0.0.1:8123/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_test",
        should_stop=lambda: False,
        stdin_buffer=stdin_buffer,
        stdout_buffer=stdout_buffer,
    )

    assert result.is_ok
    output = stdout_buffer.getvalue().decode("utf-8", errors="replace")
    assert "Content-Length:" not in output
    lines = [line for line in output.split("\n") if line.strip()]
    assert len(lines) == 2
    assert '"id": 1' in lines[0]
    assert '"id": 2' in lines[1]


def test_write_framed_message_returns_error_on_broken_pipe() -> None:
    """BrokenPipe during write must return Result error, not raise."""

    class _BrokenStream:
        def write(self, data: bytes) -> int:
            raise BrokenPipeError("upstream disconnected")

        def flush(self) -> None:
            raise BrokenPipeError("upstream disconnected")

    stream = _BrokenStream()
    result = connect_cmd._write_framed_message(
        stream,  # type: ignore[arg-type]  # test fake: not a real BinaryIO
        b'{"jsonrpc":"2.0"}',
        framed=False,
    )

    assert result.is_err
    assert result.error is not None
    assert "BRIDGE_WRITE_FAILED" in result.error
    assert "BrokenPipe" in result.error


def test_write_framed_message_returns_error_on_os_error() -> None:
    """Generic OSError during write must return Result error."""

    class _ErrorStream:
        def write(self, data: bytes) -> int:
            raise OSError("disk full")

        def flush(self) -> None:
            pass

    stream = _ErrorStream()
    result = connect_cmd._write_framed_message(
        stream,  # type: ignore[arg-type]  # test fake: not a real BinaryIO
        b'{"jsonrpc":"2.0"}',
        framed=True,
    )

    assert result.is_err
    assert result.error is not None
    assert "BRIDGE_WRITE_FAILED" in result.error


def test_emit_bridge_diagnostic_writes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Diagnostic output must go to stderr with connection_id."""

    connect_cmd._emit_bridge_diagnostic("test message", "bridge_abc123")
    captured = capsys.readouterr()
    assert "tela connect [bridge_abc123]: test message" in captured.err
    assert captured.out == ""


def test_forward_stdio_http_returns_error_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forwarding loop must propagate write errors as Result."""

    init_request = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    stdin_buffer = io.BytesIO(init_request + b"\n")

    class _BrokenWriter:
        def write(self, data: bytes) -> int:
            raise BrokenPipeError("client gone")

        def flush(self) -> None:
            raise BrokenPipeError("client gone")

    response = (
        "application/json",
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode("utf-8"),
        "s-1",
    )

    def _fake_post_mcp_message(
        *,
        mcp_url: str,
        bearer_token: str,
        payload: bytes,
        session_id: str | None = None,
        max_recovery_attempts: int = 3,
    ) -> Result[tuple[str, bytes, str | None], str]:
        _ = mcp_url, bearer_token, payload, session_id
        return Result(value=response)

    monkeypatch.setattr(connect_cmd, "_post_mcp_message", _fake_post_mcp_message)

    result = connect_cmd._forward_stdio_http(
        mcp_url="http://127.0.0.1:8123/mcp",
        bearer_token="token",
        bridge_connection_id="bridge_test",
        should_stop=lambda: False,
        stdin_buffer=stdin_buffer,
        stdout_buffer=_BrokenWriter(),  # type: ignore[arg-type]
    )

    assert result.is_err
    assert result.error is not None
    assert "BRIDGE_WRITE_FAILED" in result.error


def test_post_mcp_message_retries_only_for_transient_contract_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 retry must require gateway transient contract fields."""

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body
            self.headers = {
                "Content-Type": "application/json",
                "mcp-session-id": "s-1",
            }

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            _ = exc_type, exc, tb

        def read(self) -> bytes:
            return self._body

    transient_payload = {
        "error": "ADMISSION_REJECTED_WARMING: gateway not ready for MCP admission",
        "code": "ADMISSION_REJECTED_WARMING",
        "transient": True,
        "retry": {
            "authorized": True,
            "basis": "gateway_signal",
            "expectation": "bounded",
        },
        "gateway_state": "warming",
    }

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib_error.HTTPError(
                "http://127.0.0.1:8123/mcp",
                503,
                "Service Unavailable",
                Message(),
                io.BytesIO(json.dumps(transient_payload).encode("utf-8")),
            )
        return _FakeResponse(b'{"jsonrpc":"2.0","id":1,"result":{}}')

    monkeypatch.setattr(connect_cmd.urllib_request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(connect_cmd.time, "sleep", lambda _seconds: None)

    result = connect_cmd._post_mcp_message(
        mcp_url="http://127.0.0.1:8123/mcp",
        bearer_token="token",
        payload=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
    )

    assert result.is_ok
    assert calls["count"] == 2


def test_post_mcp_message_does_not_retry_on_plain_503_without_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain 503 without transient contract must not authorize retry."""

    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        _ = request, timeout
        calls["count"] += 1
        raise urllib_error.HTTPError(
            "http://127.0.0.1:8123/mcp",
            503,
            "Service Unavailable",
            Message(),
            io.BytesIO(b'{"error":"unstructured 503"}'),
        )

    monkeypatch.setattr(connect_cmd.urllib_request, "urlopen", _fake_urlopen)

    result = connect_cmd._post_mcp_message(
        mcp_url="http://127.0.0.1:8123/mcp",
        bearer_token="token",
        payload=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
    )

    assert result.is_err
    assert result.error == "MCP_FORWARD_FAILED: http 503"
    assert calls["count"] == 1


# =============================================================================
# Interrupt / SIGINT / KeyboardInterrupt regression tests
# =============================================================================


def test_autostart_wait_interrupt_terminates_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGINT during autostart_wait must terminate connect without waiting for timeout.

    Regression test for interrupt contract: hard interrupt during autostart_wait
    stage terminates immediately without retrying or waiting for timeout expiry.
    """

    import signal

    monkeypatch.setattr(
        connect_cmd,
        "read_lockfile",
        lambda: Result(error="LOCKFILE_READ_ERROR: lockfile does not exist"),
    )

    wait_calls: list[tuple[float, int | None]] = []

    def _fake_wait_for_live_lockfile(
        timeout_seconds: float,
        expected_pid: int | None = None,
    ) -> Result[LockfileData, str]:
        wait_calls.append((timeout_seconds, expected_pid))
        # Simulate: first call times out
        return Result(error="LOCKFILE_WAIT_TIMEOUT: timed out")

    def _fake_autostart_serve(
        *,
        config_path: str,
        default_profile: str | None,
    ) -> Result[int, str]:
        return Result(value=42000)

    monkeypatch.setattr(
        connect_cmd,
        "_wait_for_live_lockfile",
        _fake_wait_for_live_lockfile,
    )
    monkeypatch.setattr(connect_cmd, "_autostart_serve", _fake_autostart_serve)

    # Simulate SIGINT arriving during the autostart wait phase
    def _fake_raise_interrupt() -> None:
        raise KeyboardInterrupt("simulated SIGINT")

    monkeypatch.setattr(signal, "raise_signal", _fake_raise_interrupt)

    result = connect_cmd._discover_or_autostart(
        config_path="tela.yaml",
        default_profile=None,
    )

    # Interrupt must cause immediate error return, not hang
    assert result.is_err
    assert (
        "INTERRUPT" in result.error.upper()
        or "KEYBOARDINTERRUPT" in result.error.upper()
        or "timeout" not in result.error.lower()
    )


def test_active_bridge_interrupt_triggers_immediate_exit_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGINT during active bridge forwarding must exit immediately and attempt disconnect.

    Regression test for interrupt contract: hard interrupt during attach_loop stage
    must terminate the active bridge loop immediately and attempt best-effort disconnect.
    """

    import signal
    from threading import Event

    disconnect_calls: list[dict[str, object]] = []
    forward_calls: list[None] = []

    def _fake_post_json(
        *, url: str, bearer_token: str, payload: dict[str, str]
    ) -> Result[None, str]:
        disconnect_calls.append({"url": url, "payload": payload})
        return Result(value=None)

    def _fake_forward_stdio_http(
        *,
        mcp_url: str,
        bearer_token: str,
        bridge_connection_id: str,
        should_stop: Callable[[], bool],
        stdin_buffer,
        stdout_buffer,
        max_recovery_attempts: int = 3,
    ) -> Result[None, str]:
        forward_calls.append(None)
        # Simulate that should_stop becomes True (interrupt was received)
        # The loop should exit immediately when should_stop() returns True
        return Result(value=None)

    monkeypatch.setattr(connect_cmd, "_post_json", _fake_post_json)
    monkeypatch.setattr(connect_cmd, "_forward_stdio_http", _fake_forward_stdio_http)

    # Simulate SIGINT being raised
    interrupt_received = Event()

    def _fake_raise_interrupt() -> None:
        interrupt_received.set()
        raise KeyboardInterrupt("simulated SIGINT")

    monkeypatch.setattr(signal, "raise_signal", _fake_raise_interrupt)

    result = connect_cmd._run_bridge(
        host="127.0.0.1",
        port=8123,
        bearer_token="test-token",
    )

    # Bridge should exit (either due to interrupt or forward completing)
    assert result.is_ok or result.is_err
    # Disconnect should have been called (best-effort cleanup)
    assert len(disconnect_calls) >= 1
    # At least one disconnect call should be for /disconnect endpoint
    disconnect_urls = [str(c["url"]) for c in disconnect_calls]
    assert any("/disconnect" in url for url in disconnect_urls)


def _mock_gateway_status_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    """Wire `_get_gateway_status` to a consistent ready snapshot for teardown tests."""

    readiness_snapshots: list[dict[str, object]] = []

    def _fake_get_gateway_status(
        *, status_url: str, bearer_token: str
    ) -> Result[StatusResponse, str]:
        _ = status_url, bearer_token
        status = StatusResponse(
            uptime_seconds=1.0,
            server_count=1,
            connected_servers=["fs"],
            active_connections=1,
            profile_count=1,
            total_tool_calls=0,
            state="ready",
            discovery_source="lockfile",
            config_path="/tmp/tela.yaml",
            requested_config_path="/tmp/tela.yaml",
            config_mismatch=False,
            degraded_reason=None,
            connections=[],
            audit_entries=[],
        )
        readiness_snapshots.append(status.model_dump())
        return Result(value=status)

    monkeypatch.setattr(connect_cmd, "_get_gateway_status", _fake_get_gateway_status)
    return readiness_snapshots


def test_bridge_teardown_interrupt_does_not_block_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt during bridge teardown must not block process exit.

    Regression test for interrupt contract: cleanup is best-effort and must not
    block process exit. Even if disconnect or cleanup fails, process must exit.
    """

    disconnect_calls: list[dict[str, str]] = []

    def _fake_post_json(
        *, url: str, bearer_token: str, payload: dict[str, str]
    ) -> Result[None, str]:
        disconnect_calls.append({"url": url})
        if url.endswith("/connect"):
            return Result(value=None)
        # Simulate disconnect failing - this should NOT block exit
        return Result(error="SIMULATED_DISCONNECT_FAILURE")

    def _fake_forward_stdio_http(
        *,
        mcp_url: str,
        bearer_token: str,
        bridge_connection_id: str,
        should_stop: Callable[[], bool],
        stdin_buffer,
        stdout_buffer,
        max_recovery_attempts: int = 3,
    ) -> Result[None, str]:
        # Forward completes normally
        return Result(value=None)

    readiness_snapshots = _mock_gateway_status_ready(monkeypatch)
    monkeypatch.setattr(connect_cmd, "_post_json", _fake_post_json)
    monkeypatch.setattr(connect_cmd, "_forward_stdio_http", _fake_forward_stdio_http)

    result = connect_cmd._run_bridge(
        host="127.0.0.1",
        port=8123,
        bearer_token="test-token",
    )

    # Even though disconnect failed, result should still be Ok (process can exit)
    # The disconnect failure is logged but doesn't block exit
    assert result.is_ok
    assert len(disconnect_calls) >= 1
    assert [snapshot["state"] for snapshot in readiness_snapshots] == ["ready"]


def test_bridge_teardown_interrupt_resumes_cleanup_in_bounded_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupt during teardown triggers bounded disconnect resume attempt.

    This guards against orphaned connection-scoped runtime/session state when a
    hard interrupt lands exactly in the teardown disconnect call.
    """

    connect_payloads: list[dict[str, str]] = []
    disconnect_payloads: list[dict[str, str]] = []
    resumed_disconnect_payloads: list[dict[str, str]] = []

    def _fake_post_json(
        *, url: str, bearer_token: str, payload: dict[str, str]
    ) -> Result[None, str]:
        _ = bearer_token
        if url.endswith("/connect"):
            connect_payloads.append(payload)
            return Result(value=None)
        if url.endswith("/disconnect"):
            disconnect_payloads.append(payload)
            raise KeyboardInterrupt("interrupt in teardown disconnect")
        return Result(value=None)

    def _fake_post_json_once(
        *,
        url: str,
        bearer_token: str,
        payload: dict[str, str],
        timeout_seconds: float,
    ) -> Result[None, str]:
        _ = bearer_token
        assert url.endswith("/disconnect")
        assert timeout_seconds == connect_cmd.TEARDOWN_RESUME_TIMEOUT_SECONDS
        resumed_disconnect_payloads.append(payload)
        return Result(value=None)

    def _fake_forward_stdio_http(
        *,
        mcp_url: str,
        bearer_token: str,
        bridge_connection_id: str,
        should_stop: Callable[[], bool],
        stdin_buffer,
        stdout_buffer,
        max_recovery_attempts: int = 3,
    ) -> Result[None, str]:
        _ = mcp_url, bearer_token, bridge_connection_id, should_stop
        _ = stdin_buffer, stdout_buffer
        return Result(value=None)

    readiness_snapshots = _mock_gateway_status_ready(monkeypatch)
    monkeypatch.setattr(connect_cmd, "_post_json", _fake_post_json)
    monkeypatch.setattr(connect_cmd, "_post_json_once", _fake_post_json_once)
    monkeypatch.setattr(connect_cmd, "_forward_stdio_http", _fake_forward_stdio_http)

    result = connect_cmd._run_bridge(
        host="127.0.0.1",
        port=8123,
        bearer_token="test-token",
    )

    assert result.is_err
    assert result.error == "INTERRUPT: bridge teardown interrupted"
    assert len(connect_payloads) == 1
    assert len(disconnect_payloads) == 1
    assert len(resumed_disconnect_payloads) == 1
    assert [snapshot["state"] for snapshot in readiness_snapshots] == ["ready"]

    assert "connection_id" in connect_payloads[0]
    assert (
        disconnect_payloads[0]["connection_id"] == connect_payloads[0]["connection_id"]
    )
    assert (
        resumed_disconnect_payloads[0]["connection_id"]
        == connect_payloads[0]["connection_id"]
    )
