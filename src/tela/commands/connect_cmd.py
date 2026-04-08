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

# @invar:allow file_size: Connect lifecycle owns transport discovery/autostart,
# bridge framing, interrupt-safe teardown, and HTTP bridge orchestration in one
# CLI boundary module to preserve lifecycle sequencing and signal semantics.

from __future__ import annotations

import json
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

from tela.core.models import LockfileData, StatusResponse
from tela.commands.connect_transport import (
    extract_response_messages,
    inject_bridge_connection_id,
)
from tela.commands.serve_cmd import _resolve_bearer_token_cli_or_env
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
TEARDOWN_RESUME_TIMEOUT_SECONDS = 1.0
BRIDGE_READINESS_MAX_POLLS = HTTP_TRANSIENT_RETRIES + 1


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
    max_recovery_attempts: int = 3,
) -> Result[int, str]:
    """Run ``tela connect`` stdio bridge.

    Args:
        config_path: Config file path used when auto-starting ``tela serve``.
        default_profile: Optional open-mode default profile for auto-started server.
        server: Optional explicit ``host:port`` endpoint.
        token: Optional bearer token override.
        max_recovery_attempts: Maximum transient error recovery retries.

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
        max_recovery_attempts=max_recovery_attempts,
        recovery_config_path=config_path if server is None else None,
        recovery_default_profile=default_profile if server is None else None,
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

    cli_env_result = _resolve_bearer_token_cli_or_env(cli_token)
    if cli_env_result.is_ok:
        return cli_env_result

    # Command-specific fallback: try lockfile token
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

    first_result = _coordinator_discover_or_autostart(
        config_path=config_path,
        default_profile=default_profile,
        read_lockfile=read_lockfile,
        wait_for_live_lockfile=_wait_for_live_lockfile,
        autostart_serve=_autostart_serve_adapter,
        lockfile_wait_timeout_seconds=LOCKFILE_WAIT_TIMEOUT_SECONDS,
    )
    if first_result.is_ok:
        return first_result

    # Bounded second-chance discovery for startup races where the first
    # coordinator run fails while the gateway is still converging.
    retryable_error = first_result.error or ""
    if not retryable_error.startswith("DISCOVERY_FAILED"):
        return first_result

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
def _run_bridge(
    *,
    host: str,
    port: int,
    bearer_token: str,
    max_recovery_attempts: int = 3,
    recovery_config_path: str | None = None,
    recovery_default_profile: str | None = None,
) -> Result[None, str]:
    """Run connect/register/forward/disconnect lifecycle.

    Args:
        host: Gateway host address.
        port: Gateway port number.
        bearer_token: Bearer token for authentication.
        max_recovery_attempts: Maximum transient error recovery retries.

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
    recovery_attempts = 0
    current_host = host
    current_port = port
    current_bearer_token = bearer_token

    def _recover_transport_for_inflight_message() -> Result[tuple[str, str], str]:
        nonlocal \
            recovery_attempts, \
            current_host, \
            current_port, \
            current_bearer_token, \
            base_url

        if recovery_attempts >= max_recovery_attempts:
            return Result(
                error="BRIDGE_RECOVERY_EXHAUSTED: in-flight MCP request could not be replayed"
            )

        recovery_attempts += 1
        gateway_recovery_result = _recover_gateway(
            host=current_host,
            port=current_port,
            bearer_token=current_bearer_token,
            config_path=recovery_config_path,
            default_profile=recovery_default_profile,
        )
        if gateway_recovery_result.is_err:
            return Result(
                error=f"BRIDGE_RECOVERY_FAILED: {gateway_recovery_result.error}"
            )
        assert gateway_recovery_result.value is not None
        current_host, current_port, current_bearer_token = gateway_recovery_result.value
        base_url = f"http://{current_host}:{current_port}"

        reconnect_result = _post_json(
            url=f"{base_url}/connect",
            bearer_token=current_bearer_token,
            payload={"connection_id": connection_id},
        )
        if reconnect_result.is_err:
            return Result(
                error=(f"BRIDGE_RECOVERY_REGISTER_FAILED: {reconnect_result.error}")
            )

        return Result(value=(f"{base_url}/mcp", current_bearer_token))

    try:
        try:
            while not stop_requested.is_set():
                readiness_result = _wait_for_gateway_readiness(
                    status_url=f"{base_url}/status",
                    bearer_token=current_bearer_token,
                    max_polls=BRIDGE_READINESS_MAX_POLLS,
                )
                if readiness_result.is_err:
                    cycle_error = readiness_result.error
                    _emit_bridge_diagnostic(
                        f"readiness wait stopped: {cycle_error}", connection_id
                    )
                else:
                    forward_result = _forward_stdio_http(
                        mcp_url=f"{base_url}/mcp",
                        bearer_token=current_bearer_token,
                        bridge_connection_id=connection_id,
                        should_stop=stop_requested.is_set,
                        stdin_buffer=sys.stdin.buffer,
                        stdout_buffer=sys.stdout.buffer,
                        max_recovery_attempts=max_recovery_attempts,
                        recover_transport=_recover_transport_for_inflight_message,
                    )
                    if forward_result.is_ok:
                        break
                    cycle_error = forward_result.error
                    _emit_bridge_diagnostic(
                        f"forwarding stopped: {cycle_error}", connection_id
                    )

                if cycle_error is None:
                    bridge_error = "BRIDGE_RUNTIME_ERROR: unknown bridge failure"
                    break

                recoverable_result = _is_recoverable_error(cycle_error)
                if recoverable_result.is_err or not recoverable_result.value:
                    bridge_error = cycle_error
                    break

                if recovery_attempts >= max_recovery_attempts:
                    bridge_error = f"BRIDGE_RECOVERY_EXHAUSTED: {cycle_error}"
                    break

                recovery_result = _recover_transport_for_inflight_message()
                if recovery_result.is_err:
                    bridge_error = recovery_result.error
                    break
        except KeyboardInterrupt:
            bridge_error = "INTERRUPT: bridge attach loop interrupted"
            _emit_bridge_diagnostic("attach loop interrupted", connection_id)
    finally:
        teardown_interrupted = False
        disconnect_url = f"{base_url}/disconnect"
        disconnect_payload = {"connection_id": connection_id}
        try:
            disconnect_result = _post_json(
                url=disconnect_url,
                bearer_token=current_bearer_token,
                payload=disconnect_payload,
            )
            if disconnect_result.is_err:
                _emit_bridge_diagnostic(
                    f"disconnect failed: {disconnect_result.error}", connection_id
                )
        except KeyboardInterrupt:
            teardown_interrupted = True
            _emit_bridge_diagnostic("disconnect interrupted", connection_id)
            # Bounded teardown critical section: ignore hard interrupts just long
            # enough to issue one final disconnect attempt so connection-scoped
            # cleanup is not orphaned by mid-teardown signals.
            current_sigint = signal.getsignal(signal.SIGINT)
            current_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                resumed_disconnect_result = _post_json_once(
                    url=disconnect_url,
                    bearer_token=current_bearer_token,
                    payload=disconnect_payload,
                    timeout_seconds=TEARDOWN_RESUME_TIMEOUT_SECONDS,
                )
                if resumed_disconnect_result.is_err:
                    _emit_bridge_diagnostic(
                        (
                            "disconnect resume failed: "
                            f"{resumed_disconnect_result.error}"
                        ),
                        connection_id,
                    )
            finally:
                signal.signal(signal.SIGINT, current_sigint)
                signal.signal(signal.SIGTERM, current_sigterm)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        # Only set interrupt error if the main bridge loop itself was interrupted,
        # not merely the teardown. Teardown interruption is logged but doesn't
        # fail the bridge if the main work succeeded.
        if teardown_interrupted and bridge_error is None:
            # Teardown was interrupted but main bridge succeeded - this is OK
            # The bridge lifecycle completed; teardown cleanup was attempted
            pass

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
    max_recovery_attempts: int = 3,
    recover_transport: Callable[[], Result[tuple[str, str], str]] | None = None,
) -> Result[None, str]:
    """Forward MCP stdio frames to HTTP and stream responses back.

    Maintains the Streamable HTTP ``mcp-session-id`` across requests so that
    all messages after ``initialize`` are routed to the same server session.
    """

    session_id: str | None = None
    initialize_payload: bytes | None = None

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
        message_method = _extract_jsonrpc_method(message)
        if message_method == "initialize":
            initialize_payload = message

        while True:
            http_result = _post_mcp_message(
                mcp_url=mcp_url,
                bearer_token=bearer_token,
                payload=message,
                session_id=session_id,
                max_recovery_attempts=max_recovery_attempts,
            )
            if http_result.is_ok:
                break

            error_text = http_result.error or "MCP_FORWARD_FAILED: unknown error"
            recoverable_result = _is_recoverable_error(error_text)
            if (
                recover_transport is None
                or recoverable_result.is_err
                or not recoverable_result.value
            ):
                return Result(error=error_text)

            recovery_result = recover_transport()
            if recovery_result.is_err:
                return Result(error=recovery_result.error)
            assert recovery_result.value is not None
            mcp_url, bearer_token = recovery_result.value
            session_id = None

            if message_method != "initialize" and initialize_payload is not None:
                bootstrap_result = _post_mcp_message(
                    mcp_url=mcp_url,
                    bearer_token=bearer_token,
                    payload=initialize_payload,
                    session_id=None,
                    max_recovery_attempts=max_recovery_attempts,
                )
                if bootstrap_result.is_err:
                    return Result(error=bootstrap_result.error)
                assert bootstrap_result.value is not None
                _content_type, _response_body, bootstrap_session_id = (
                    bootstrap_result.value
                )
                session_id = bootstrap_session_id

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


# @invar:allow shell_result: pure parsing helper returning Optional[str], no I/O.
def _extract_jsonrpc_method(payload: bytes) -> str | None:
    """Return JSON-RPC method name from payload when present."""

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(decoded, dict):
        return None
    method = decoded.get("method")
    return method if isinstance(method, str) else None


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
    if isinstance(reason, str):
        normalized_reason = reason.lower()
        transient_reason_markers = (
            "connection refused",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "timed out",
            "temporarily unavailable",
        )
        return Result(
            value=any(
                marker in normalized_reason for marker in transient_reason_markers
            )
        )
    return Result(value=False)


def _is_recoverable_error(error: str) -> Result[bool, str]:
    """Classify bridge/runtime errors eligible for bounded recovery."""

    normalized_error = error.lower()
    recoverable_markers = (
        "http_connect_error",
        "connection refused",
        "connection reset",
        "connection aborted",
        "broken pipe",
        "timed out",
        "http_503",
        "http 503",
        "bridge_readiness_query_failed",
        "bridge_not_ready: bounded readiness wait exhausted",
    )
    return Result(
        value=any(marker in normalized_error for marker in recoverable_markers)
    )


def _recover_gateway(
    *,
    host: str,
    port: int,
    bearer_token: str,
    config_path: str | None,
    default_profile: str | None,
) -> Result[tuple[str, int, str], str]:
    """Recover gateway endpoint via lockfile discovery or readiness polling."""

    if config_path is not None:
        discovery_result = _discover_or_autostart(
            config_path=config_path,
            default_profile=default_profile,
        )
        if discovery_result.is_ok:
            assert discovery_result.value is not None
            discovered = discovery_result.value
            return Result(value=(discovered.host, discovered.port, discovered.token))
    else:
        discovery_result = Result[LockfileData, str](
            error="DISCOVERY_DISABLED: explicit server mode disables autostart recovery"
        )

    readiness_result = _wait_for_gateway_readiness(
        status_url=f"http://{host}:{port}/status",
        bearer_token=bearer_token,
        max_polls=BRIDGE_READINESS_MAX_POLLS,
    )
    if readiness_result.is_ok:
        return Result(value=(host, port, bearer_token))

    return Result(
        error=(
            "GATEWAY_RECOVERY_FAILED: "
            f"discovery={discovery_result.error}; readiness={readiness_result.error}"
        )
    )


# @shell_complexity: HTTP POST with transient retry and SSE/JSON content-type dispatch.
def _post_mcp_message(
    *,
    mcp_url: str,
    bearer_token: str,
    payload: bytes,
    session_id: str | None = None,
    max_recovery_attempts: int = 3,
) -> Result[tuple[str, bytes, str | None], str]:
    """POST payload to MCP Streamable HTTP endpoint with transient retry.

    Retries up to ``max_recovery_attempts`` times on transient connection
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
    for attempt in range(max_recovery_attempts + 1):
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
            if (
                exc.code == 503
                and attempt < max_recovery_attempts
                and _is_mcp_transient_warming_error(exc).value
            ):
                time.sleep(HTTP_TRANSIENT_BACKOFF_SECONDS * (attempt + 1))
                continue
            return Result(error=f"MCP_FORWARD_FAILED: http {exc.code}")
        except urllib_error.URLError as exc:
            last_error = f"MCP_FORWARD_FAILED: {exc.reason}"
            if (
                not _is_transient_url_error(exc).value
                or attempt == max_recovery_attempts
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
            if exc.code == 503 and attempt < HTTP_TRANSIENT_RETRIES:
                time.sleep(HTTP_TRANSIENT_BACKOFF_SECONDS * (attempt + 1))
                continue
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


def _post_json_once(
    *,
    url: str,
    bearer_token: str,
    payload: dict[str, str],
    timeout_seconds: float,
) -> Result[None, str]:
    """POST JSON once with a caller-supplied timeout and no retry.

    Used for bounded teardown-critical-section resume attempts.
    """

    body = json.dumps(payload).encode("utf-8")
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
        with urllib_request.urlopen(request, timeout=timeout_seconds):
            return Result(value=None)
    except urllib_error.HTTPError as exc:
        return Result(error=f"HTTP_{exc.code}: {url}")
    except urllib_error.URLError as exc:
        return Result(error=f"HTTP_CONNECT_ERROR: {exc.reason}")


def _wait_for_gateway_readiness(
    *, status_url: str, bearer_token: str, max_polls: int
) -> Result[None, str]:
    """Poll ``GET /status`` until ready or bounded non-ready exit.

    ``tela connect`` consumes gateway-owned readiness truth from ``GET /status``
    and must not infer readiness from local lifecycle labels or fixed-delay-only
    assumptions.
    """

    for poll_index in range(max_polls):
        status_result = _get_gateway_status(
            status_url=status_url,
            bearer_token=bearer_token,
        )
        if status_result.is_err:
            return Result(error=f"BRIDGE_READINESS_QUERY_FAILED: {status_result.error}")
        assert status_result.value is not None
        status = status_result.value

        if status.state == "ready":
            return Result(value=None)

        if status.state == "degraded":
            degraded_reason = status.degraded_reason or "unknown"
            return Result(
                error=(
                    "BRIDGE_NOT_READY: state=degraded "
                    f"degraded_reason={degraded_reason}"
                )
            )

        if poll_index == max_polls - 1:
            state = status.state or "unknown"
            return Result(
                error=(
                    "BRIDGE_NOT_READY: bounded readiness wait exhausted "
                    f"state={state} polls={max_polls}"
                )
            )

        time.sleep(HTTP_TRANSIENT_BACKOFF_SECONDS * (poll_index + 1))

    return Result(error="BRIDGE_NOT_READY: bounded readiness wait exhausted")


def _get_gateway_status(
    *, status_url: str, bearer_token: str
) -> Result[StatusResponse, str]:
    """Fetch and validate ``GET /status`` gateway readiness payload."""

    decoded = ""
    last_connect_error = ""
    for attempt in range(HTTP_TRANSIENT_RETRIES + 1):
        request = urllib_request.Request(
            status_url,
            method="GET",
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib_request.urlopen(
                request, timeout=HTTP_TIMEOUT_SECONDS
            ) as response:
                decoded = response.read().decode("utf-8")
            break
        except urllib_error.HTTPError as exc:
            if exc.code == 503 and attempt < HTTP_TRANSIENT_RETRIES:
                time.sleep(HTTP_TRANSIENT_BACKOFF_SECONDS * (attempt + 1))
                continue
            return Result(error=f"HTTP_{exc.code}: {status_url}")
        except urllib_error.URLError as exc:
            last_connect_error = f"HTTP_CONNECT_ERROR: {exc.reason}"
            if (
                not _is_transient_url_error(exc).value
                or attempt == HTTP_TRANSIENT_RETRIES
            ):
                return Result(error=last_connect_error)
            time.sleep(HTTP_TRANSIENT_BACKOFF_SECONDS * (attempt + 1))
    else:
        if last_connect_error:
            return Result(error=last_connect_error)
        return Result(error=f"HTTP_CONNECT_ERROR: {status_url}")

    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        return Result(error=f"INVALID_STATUS_PAYLOAD: {exc}")

    if not isinstance(parsed, dict):
        return Result(error="INVALID_STATUS_PAYLOAD: expected object")

    try:
        return Result(value=StatusResponse.model_validate(parsed))
    except Exception as exc:
        return Result(error=f"INVALID_STATUS_PAYLOAD: {exc}")


def _is_mcp_transient_warming_error(exc: urllib_error.HTTPError) -> Result[bool, str]:
    """Return True when a 503 matches the transient MCP warming contract."""

    if exc.code != 503:
        return Result(value=False)

    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Result(value=False)

    if not isinstance(payload, dict):
        return Result(value=False)

    retry = payload.get("retry")
    if not isinstance(retry, dict):
        return Result(value=False)

    is_contract_match = (
        payload.get("code") == "ADMISSION_REJECTED_WARMING"
        and payload.get("transient") is True
        and retry.get("authorized") is True
        and retry.get("basis") == "gateway_signal"
        and retry.get("expectation") == "bounded"
        and payload.get("gateway_state") == "warming"
    )
    return Result(value=is_contract_match)
