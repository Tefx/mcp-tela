"""Connect command entrypoint for stdio-to-HTTP bridge lifecycle.

Implements ``tela connect`` service discovery, optional auto-start of
``tela serve``, and bridge lifecycle management:

1. Discover endpoint from lockfile (unless ``--server`` is given)
2. Resolve bearer token precedence
3. Register connection via ``POST /connect``
4. Forward stdio MCP frames to ``POST /mcp``
5. Deregister connection via ``POST /disconnect`` on exit/signals

Interrupt semantics and host-facing message-state contracts are declared in
``tela.commands.remote_state``. This module intentionally stays focused on
bridge wiring and does not finalize host rendering text.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
import signal
import subprocess
import sys
from threading import Event
import time
from types import FrameType
from typing import BinaryIO, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request
import uuid

from tela.core.models import LockfileData
from tela.commands.connect_transport import (
    extract_response_messages,
    inject_bridge_connection_id,
)
from tela.shell.config_loader import Result
from tela.shell.lockfile import delete_lockfile, read_lockfile
from tela.shell.startup_coordinator import (
    discover_or_autostart as _coordinator_discover_or_autostart,
)


LOCKFILE_WAIT_TIMEOUT_SECONDS = 5.0
LOCKFILE_WAIT_POLL_SECONDS = 0.1
LOCKFILE_START_RACE_RETRIES = 3
HTTP_TIMEOUT_SECONDS = 10.0
HTTP_TRANSIENT_RETRIES = 3
HTTP_TRANSIENT_BACKOFF_SECONDS = 0.5


def _emit_bridge_diagnostic(message: str, connection_id: str) -> None:
    """Write a diagnostic message to stderr for bridge troubleshooting.

    Diagnostic output goes to stderr because stdout is the MCP transport.
    Failures to write diagnostics are silently ignored — diagnostics must
    never interrupt the bridge lifecycle.

    Args:
        message: Human-readable diagnostic detail.
        connection_id: Bridge connection identifier for correlation.
    """
    try:
        sys.stderr.write(f"tela connect [{connection_id}]: {message}\n")
        sys.stderr.flush()
    except OSError:
        pass


@dataclass(frozen=True)
class ConnectEndpoint:
    """Resolved server endpoint for bridge transport."""

    host: str
    port: int
    lockfile_token: str | None


# @shell_complexity: command orchestrates lockfile discovery/autostart and bridge lifecycle.
def connect_command(
    config_path: str = "tela.yaml",
    default_profile: str | None = None,
    server: str | None = None,
    token: str | None = None,
) -> Result[int, str]:
    """Run ``tela connect`` stdio bridge.

    Args:
        config_path: Config file path used when auto-starting ``tela serve``.
        default_profile: Optional open-mode default profile for auto-started server.
        server: Optional explicit ``host:port`` endpoint.
        token: Optional bearer token override.

    Returns:
        Result with exit code ``0`` on success.
    """

    endpoint_result = _resolve_endpoint(
        config_path=config_path,
        default_profile=default_profile,
        server=server,
    )
    if endpoint_result.is_err:
        return Result(error=endpoint_result.error)
    assert endpoint_result.value is not None

    token_result = _resolve_connect_token(
        cli_token=token,
        lockfile_token=endpoint_result.value.lockfile_token,
    )
    if token_result.is_err:
        return Result(error=token_result.error)
    assert token_result.value is not None

    bridge_result = _run_bridge(
        host=endpoint_result.value.host,
        port=endpoint_result.value.port,
        bearer_token=token_result.value,
    )
    if bridge_result.is_err:
        return Result(error=bridge_result.error)
    return Result(value=0)


def _resolve_endpoint(
    *,
    config_path: str,
    default_profile: str | None,
    server: str | None,
) -> Result[ConnectEndpoint, str]:
    """Resolve endpoint either from ``--server`` or lockfile discovery."""

    if server is not None:
        server_result = _parse_server(server)
        if server_result.is_err:
            return Result(error=server_result.error)
        assert server_result.value is not None
        host, port = server_result.value
        return Result(value=ConnectEndpoint(host=host, port=port, lockfile_token=None))

    discovery_result = _discover_or_autostart(
        config_path=config_path,
        default_profile=default_profile,
    )
    if discovery_result.is_err:
        return Result(error=discovery_result.error)
    assert discovery_result.value is not None
    lockfile = discovery_result.value
    return Result(
        value=ConnectEndpoint(
            host=lockfile.host,
            port=lockfile.port,
            lockfile_token=lockfile.token,
        )
    )


def _parse_server(raw_server: str) -> Result[tuple[str, int], str]:
    """Parse explicit ``--server`` value as ``host:port``."""

    host, sep, port_text = raw_server.rpartition(":")
    if sep == "" or host == "" or port_text == "":
        return Result(error="INVALID_SERVER: expected --server host:port")
    try:
        port = int(port_text)
    except ValueError:
        return Result(error="INVALID_SERVER: expected --server host:port")

    if port < 1 or port > 65535:
        return Result(error="INVALID_SERVER: port must be in range 1..65535")
    return Result(value=(host, port))


def _resolve_connect_token(
    *,
    cli_token: str | None,
    lockfile_token: str | None,
) -> Result[str, str]:
    """Resolve bearer token precedence for ``tela connect``.

    Precedence order:
    1. ``--token``
    2. ``TELA_BEARER_TOKEN``
    3. lockfile ``token`` field
    """

    if cli_token is not None:
        return Result(value=cli_token)

    env_token = os.environ.get("TELA_BEARER_TOKEN")
    if env_token is not None:
        return Result(value=env_token)

    if lockfile_token is not None:
        return Result(value=lockfile_token)

    return Result(
        error=(
            "MISSING_TOKEN: --server requires --token or TELA_BEARER_TOKEN "
            "because lockfile discovery is disabled"
        )
    )


# @shell_complexity: discovery flow delegates to startup coordinator for leader/follower arbitration.
def _autostart_serve_adapter(
    config_path: str,
    default_profile: str | None,
) -> Result[int, str]:
    """Adapter for startup coordinator: wraps _autostart_serve with positional args."""

    return _autostart_serve(
        config_path=config_path,
        default_profile=default_profile,
    )


def _discover_or_autostart(
    *,
    config_path: str,
    default_profile: str | None,
) -> Result[LockfileData, str]:
    """Discover running server via lockfile or coordinate autostart leadership.

    Delegates to the startup coordinator for single-leader election and
    config-path ownership validation during concurrent connect invocations.
    """

    return _coordinator_discover_or_autostart(
        config_path=config_path,
        default_profile=default_profile,
        read_lockfile=read_lockfile,
        wait_for_live_lockfile=_wait_for_live_lockfile,
        autostart_serve=_autostart_serve_adapter,
        lockfile_wait_timeout_seconds=LOCKFILE_WAIT_TIMEOUT_SECONDS,
    )


# @shell_complexity: polling loop branches on deadline, stale state, and PID identity filter.
def _wait_for_live_lockfile(
    timeout_seconds: float,
    expected_pid: int | None = None,
) -> Result[LockfileData, str]:
    """Wait for a non-stale lockfile to become available.

    Args:
        timeout_seconds: Maximum time to wait for lockfile.
        expected_pid: If set, only accept a lockfile whose ``pid`` matches this
            value. This prevents cross-contamination from concurrent or stale
            serve processes by binding lockfile identity to the specific process
            that was spawned.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        lockfile_result = read_lockfile()
        if lockfile_result.is_ok:
            assert lockfile_result.value is not None
            if expected_pid is not None and lockfile_result.value.pid != expected_pid:
                time.sleep(LOCKFILE_WAIT_POLL_SECONDS)
                continue
            return Result(value=lockfile_result.value)

        if lockfile_result.error is not None and lockfile_result.error.startswith(
            "LOCKFILE_STALE"
        ):
            _ = delete_lockfile()

        time.sleep(LOCKFILE_WAIT_POLL_SECONDS)

    return Result(error="LOCKFILE_WAIT_TIMEOUT: timed out waiting for gateway.lock")


def _autostart_serve(
    *,
    config_path: str,
    default_profile: str | None,
) -> Result[int, str]:
    """Auto-start ``tela serve`` as detached subprocess.

    Returns:
        Result with the spawned process PID on success, enabling callers to
        validate that the lockfile belongs to the exact process that was started.
    """

    command: list[str] = [
        sys.executable,
        "-m",
        "tela",
        "serve",
        "--config",
        config_path,
        "--idle-timeout",
        "300",
    ]
    if default_profile is not None:
        command.extend(["--default-profile", default_profile])

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return Result(error=f"AUTOSTART_FAILED: {exc}")

    return Result(value=proc.pid)


# @shell_complexity: bridge lifecycle coordinates signal handling, connect, forward, and disconnect.
def _run_bridge(*, host: str, port: int, bearer_token: str) -> Result[None, str]:
    """Run connect/register/forward/disconnect lifecycle.

    Args:
        host: Gateway host address.
        port: Gateway port number.
        bearer_token: Bearer token for authentication.

    Returns:
        Result with None on success or error string on failure.
    """

    base_url = f"http://{host}:{port}"
    connection_id = f"bridge_{uuid.uuid4().hex}"
    stop_requested = Event()

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)

    def _handle_stop(_signum: int, _frame: FrameType | None) -> None:
        stop_requested.set()
        raise KeyboardInterrupt("bridge interrupted by signal")

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        connect_result = _post_json(
            url=f"{base_url}/connect",
            bearer_token=bearer_token,
            payload={"connection_id": connection_id},
        )
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        _emit_bridge_diagnostic("registration interrupted", connection_id)
        return Result(error="INTERRUPT: bridge registration interrupted")
    if connect_result.is_err:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        _emit_bridge_diagnostic(
            f"registration failed: {connect_result.error}", connection_id
        )
        return Result(error=connect_result.error)

    bridge_error: str | None = None
    try:
        try:
            forward_result = _forward_stdio_http(
                mcp_url=f"{base_url}/mcp",
                bearer_token=bearer_token,
                bridge_connection_id=connection_id,
                should_stop=stop_requested.is_set,
                stdin_buffer=sys.stdin.buffer,
                stdout_buffer=sys.stdout.buffer,
            )
            if forward_result.is_err:
                bridge_error = forward_result.error
                _emit_bridge_diagnostic(
                    f"forwarding stopped: {bridge_error}", connection_id
                )
        except KeyboardInterrupt:
            bridge_error = "INTERRUPT: bridge attach loop interrupted"
            _emit_bridge_diagnostic("attach loop interrupted", connection_id)
    finally:
        teardown_interrupted = False
        try:
            disconnect_result = _post_json(
                url=f"{base_url}/disconnect",
                bearer_token=bearer_token,
                payload={"connection_id": connection_id},
            )
            if disconnect_result.is_err:
                _emit_bridge_diagnostic(
                    f"disconnect failed: {disconnect_result.error}", connection_id
                )
        except KeyboardInterrupt:
            teardown_interrupted = True
            _emit_bridge_diagnostic("disconnect interrupted", connection_id)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        if teardown_interrupted and bridge_error is None:
            bridge_error = "INTERRUPT: bridge teardown interrupted"

    if bridge_error is not None:
        return Result(error=bridge_error)
    return Result(value=None)


# @shell_complexity: forwarding loop bridges stdio framing and HTTP response variants.
def _forward_stdio_http(
    *,
    mcp_url: str,
    bearer_token: str,
    bridge_connection_id: str,
    should_stop: Callable[[], bool],
    stdin_buffer: BinaryIO,
    stdout_buffer: BinaryIO,
) -> Result[None, str]:
    """Forward MCP stdio frames to HTTP and stream responses back.

    Maintains the Streamable HTTP ``mcp-session-id`` across requests so that
    all messages after ``initialize`` are routed to the same server session.
    """

    session_id: str | None = None

    while not should_stop():
        message_result = _read_framed_message(stdin_buffer)
        if message_result.is_err:
            return Result(error=message_result.error)
        assert message_result.value is not None or message_result.error is None
        framed_message = message_result.value
        if framed_message is None:
            return Result(value=None)
        message = inject_bridge_connection_id(
            framed_message.payload,
            connection_id=bridge_connection_id,
        )

        http_result = _post_mcp_message(
            mcp_url=mcp_url,
            bearer_token=bearer_token,
            payload=message,
            session_id=session_id,
        )
        if http_result.is_err:
            return Result(error=http_result.error)
        assert http_result.value is not None
        content_type, response_body, response_session_id = http_result.value

        if response_session_id is not None:
            session_id = response_session_id

        response_messages_result = extract_response_messages(
            content_type=content_type,
            response_body=response_body,
        )
        if response_messages_result.is_err:
            return Result(error=response_messages_result.error)
        assert response_messages_result.value is not None
        for response_message in response_messages_result.value:
            write_result = _write_framed_message(
                stdout_buffer,
                response_message,
                framed=framed_message.is_content_length_framed,
            )
            if write_result.is_err:
                return Result(error=write_result.error)

    return Result(value=None)


@dataclass(frozen=True)
class _BridgeMessage:
    """One stdio request payload and transport framing metadata."""

    payload: bytes
    is_content_length_framed: bool


# @shell_complexity: dual-framing detection requires header parsing branches.
def _read_framed_message(stream: BinaryIO) -> Result[_BridgeMessage | None, str]:
    """Read one MCP JSON-RPC message from stdio transport.

    Supports both Content-Length framed payloads and newline-delimited JSON.
    """

    while True:
        line = stream.readline()
        if line == b"":
            return Result(value=None)
        stripped = line.strip()
        if stripped == b"":
            continue
        if line.lower().startswith(b"content-length:"):
            length_token = line.split(b":", 1)[1].strip()
            try:
                content_length = int(length_token)
            except ValueError:
                return Result(error="MCP_FORWARD_FAILED: invalid Content-Length header")

            while True:
                header_line = stream.readline()
                if header_line == b"":
                    return Result(
                        error="MCP_FORWARD_FAILED: EOF while reading MCP headers"
                    )
                if header_line in {b"\r\n", b"\n"}:
                    break

            payload = stream.read(content_length)
            if len(payload) != content_length:
                return Result(
                    error="MCP_FORWARD_FAILED: EOF while reading MCP frame body"
                )
            return Result(
                value=_BridgeMessage(payload=payload, is_content_length_framed=True)
            )

        return Result(
            value=_BridgeMessage(payload=stripped, is_content_length_framed=False)
        )


def _write_framed_message(
    stream: BinaryIO, payload: bytes, *, framed: bool
) -> Result[None, str]:
    """Write one MCP JSON-RPC message to stdio transport.

    Args:
        stream: Output stream (typically stdout).
        payload: JSON-RPC message bytes.
        framed: If True, wrap with Content-Length header; else newline-delimited.

    Returns:
        Result with None on success; error string on BrokenPipe/write failure.
    """
    try:
        if framed:
            header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
            stream.write(header + payload)
        else:
            stream.write(payload.rstrip(b"\r\n") + b"\n")
        stream.flush()
    except BrokenPipeError:
        return Result(
            error="BRIDGE_WRITE_FAILED: upstream client disconnected (BrokenPipe)"
        )
    except OSError as exc:
        return Result(error=f"BRIDGE_WRITE_FAILED: {exc}")
    return Result(value=None)


def _is_transient_url_error(exc: urllib_error.URLError) -> Result[bool, str]:
    """Classify whether a URLError is a transient connection failure.

    Transient failures (connection refused, reset, broken pipe) can occur when
    the gateway HTTP server is still starting up or temporarily unreachable.
    Non-transient failures (DNS, SSL, etc.) should not be retried.

    Args:
        exc: The URLError to classify.

    Returns:
        Result with True if the underlying error is a transient connection
        failure, False otherwise.
    """
    reason = exc.reason
    if isinstance(reason, OSError):
        # Prefer type-based classification: Python's builtin subclasses
        # (ConnectionRefusedError, ConnectionResetError, etc.) may carry
        # errno=None when constructed with only a message string — which is
        # the common pattern in both production urllib and test fixtures.
        transient_types = (
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            TimeoutError,
        )
        if isinstance(reason, transient_types):
            return Result(value=True)

        # Fallback: errno check for generic OSError instances raised by the
        # OS with a numeric errno but no dedicated exception subclass.
        import errno

        transient_errnos = {
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.EPIPE,
            errno.ETIMEDOUT,
        }
        return Result(value=reason.errno in transient_errnos)
    return Result(value=False)


# @shell_complexity: HTTP POST with transient retry and SSE/JSON content-type dispatch.
def _post_mcp_message(
    *,
    mcp_url: str,
    bearer_token: str,
    payload: bytes,
    session_id: str | None = None,
) -> Result[tuple[str, bytes, str | None], str]:
    """POST payload to MCP Streamable HTTP endpoint with transient retry.

    Retries up to ``HTTP_TRANSIENT_RETRIES`` times on transient connection
    errors (connection refused, reset, broken pipe) that occur when the
    gateway is still starting up. Non-transient errors are returned immediately.

    Returns ``(content_type, body, session_id)`` where *session_id* is the
    ``mcp-session-id`` returned by the server (may be ``None``).
    """

    headers: dict[str, str] = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id

    last_error: str = ""
    for attempt in range(HTTP_TRANSIENT_RETRIES + 1):
        request = urllib_request.Request(
            mcp_url,
            data=payload,
            method="POST",
            headers=headers,
        )
        try:
            with urllib_request.urlopen(
                request, timeout=HTTP_TIMEOUT_SECONDS
            ) as response:
                content_type = response.headers.get("Content-Type", "")
                resp_session_id = response.headers.get("mcp-session-id")
                return Result(value=(content_type, response.read(), resp_session_id))
        except urllib_error.HTTPError as exc:
            return Result(error=f"MCP_FORWARD_FAILED: http {exc.code}")
        except urllib_error.URLError as exc:
            last_error = f"MCP_FORWARD_FAILED: {exc.reason}"
            if (
                not _is_transient_url_error(exc).value
                or attempt == HTTP_TRANSIENT_RETRIES
            ):
                return Result(error=last_error)
            time.sleep(HTTP_TRANSIENT_BACKOFF_SECONDS * (attempt + 1))

    return Result(error=last_error)


# @shell_complexity: HTTP POST with transient retry and backoff on connection errors.
def _post_json(
    *, url: str, bearer_token: str, payload: dict[str, str]
) -> Result[None, str]:
    """POST JSON to a lifecycle endpoint with bearer auth and transient retry.

    Retries transient connection errors (connection refused, reset, broken pipe)
    that can occur when the gateway process is still binding or shutting down.
    """

    body = json.dumps(payload).encode("utf-8")
    last_error: str = ""

    for attempt in range(HTTP_TRANSIENT_RETRIES + 1):
        request = urllib_request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib_request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS):
                return Result(value=None)
        except urllib_error.HTTPError as exc:
            return Result(error=f"HTTP_{exc.code}: {url}")
        except urllib_error.URLError as exc:
            last_error = f"HTTP_CONNECT_ERROR: {exc.reason}"
            if (
                not _is_transient_url_error(exc).value
                or attempt == HTTP_TRANSIENT_RETRIES
            ):
                return Result(error=last_error)
            time.sleep(HTTP_TRANSIENT_BACKOFF_SECONDS * (attempt + 1))

    return Result(error=last_error)
