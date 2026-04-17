# @invar:allow file_size: bridge lifecycle module coordinates framing, forwarding,
# recovery, readiness, and teardown — further splitting would break lifecycle cohesion.
"""Bridge runtime for ``tela connect``: stdio framing, MCP forwarding, recovery.

This module owns the bridge lifecycle after endpoint/token resolution:

- **Framed message I/O**: reading/writing MCP JSON-RPC frames from stdio
- **Forwarding loop**: the ``_forward_stdio_http`` request/response bridge
- **Bridge lifecycle**: ``run_bridge`` coordinates connect/register/forward/disconnect
- **Readiness polling**: ``_wait_for_gateway_readiness`` via ``GET /status``
- **Recovery**: ``_recover_gateway`` for bounded transient-error recovery
- **HTTP helpers**: ``_post_json``, ``_post_json_once``, ``_post_mcp_message``,
  ``_get_gateway_status``

Endpoint resolution, token precedence, autostart, and lockfile discovery remain
in ``connect_cmd`` — the bridge module receives resolved parameters and owns
the runtime thereafter.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from dataclasses import dataclass
from threading import Event
from types import FrameType
from typing import BinaryIO, Callable, Literal
from urllib import error as urllib_error
import uuid

from tela.core.models import LockfileData, StatusResponse
from tela.commands.connect_transport import (
    extract_response_messages,
    inject_bridge_connection_id,
)
from tela.commands.http_client import retry_http_request
from tela.shell.result import Result


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HTTP_TIMEOUT_SECONDS = 10.0
HTTP_TRANSIENT_RETRIES = 3
HTTP_TRANSIENT_BACKOFF_SECONDS = 0.5
BRIDGE_READINESS_MAX_POLLS = HTTP_TRANSIENT_RETRIES + 1
TEARDOWN_RESUME_TIMEOUT_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Diagnostic helper
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Framed message I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeMessage:
    """One stdio request payload and transport framing metadata."""

    payload: bytes
    is_content_length_framed: bool


@dataclass(frozen=True)
class ForwardedBridgeResponse:
    """One recovered forwarding outcome for a single MCP request."""

    mcp_url: str
    bearer_token: str
    session_id: str | None
    response_messages: list[bytes]


@dataclass
class BridgeRuntimeState:
    """Mutable bridge runtime state across recovery cycles."""

    base_url: str
    host: str
    port: int
    bearer_token: str
    recovery_attempts: int = 0


# @shell_complexity: dual-framing detection requires header parsing branches.
def read_framed_message(stream: BinaryIO) -> Result[BridgeMessage | None, str]:
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
                value=BridgeMessage(payload=payload, is_content_length_framed=True)
            )

        return Result(
            value=BridgeMessage(payload=stripped, is_content_length_framed=False)
        )


def write_framed_message(
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


# @invar:allow shell_result: pure parsing helper returning Optional[str], no I/O.
# @shell_orchestration: JSON-RPC method extraction is transport-bound bridge glue used only to manage initialize/session replay semantics at the stdio/HTTP boundary.
def extract_jsonrpc_method(payload: bytes) -> str | None:
    """Return JSON-RPC method name from payload when present."""

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(decoded, dict):
        return None
    method = decoded.get("method")
    return method if isinstance(method, str) else None


# ---------------------------------------------------------------------------
# Error classification and recovery
# ---------------------------------------------------------------------------


def is_recoverable_error(error: str) -> Result[bool, str]:
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


# @invar:allow shell_result: pure parsing helper returning list[str], no I/O.
# @shell_orchestration: bridge error extraction stays in Shell because it classifies protocol-bound transport responses for replay/recovery decisions.
# @shell_complexity: parser branches across JSON-RPC error envelopes and CallToolResult isError payload content.
def _extract_bridge_error_messages(payload: bytes) -> list[str]:
    """Return recoverable error text candidates from a bridge response payload."""

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    if not isinstance(decoded, dict):
        return []

    messages: list[str] = []

    error = decoded.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            messages.append(message)

    result = decoded.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    messages.append(text)

    return messages


# @invar:allow shell_result: pure response-classification helper returning bool, no I/O.
# @shell_orchestration: recovery marker classification is specific to bridge-session repair policy and therefore remains co-located with the Shell transport loop.
def _response_requires_bridge_recovery(response_messages: list[bytes]) -> bool:
    """Return True when a JSON-RPC error proves the bridge session is stale.

    Source:
    - User bug report: Tela MCP provider becomes unavailable after long idle.
    - ``tela.shell.gateway._ensure_connection`` fails closed with
      ``RECONNECT_REQUIRED`` when the bridge session/connection mapping is gone.
    - ``tela.shell.upstream.handle_initialize`` returns
      ``CONNECTION_NOT_FOUND: bridge initialize requires pre-registered
      connection`` when the bridge registration was reaped.

    These responses indicate pre-dispatch session loss, so replaying initialize
    and then retrying the original request is safe.
    """

    recovery_markers = (
        "reconnect_required:",
        "bridge initialize requires pre-registered connection",
    )
    for payload in response_messages:
        for message in _extract_bridge_error_messages(payload):
            normalized_message = message.lower()
            if any(marker in normalized_message for marker in recovery_markers):
                return True
    return False


DiscoverOrAutostartFn = Callable[..., Result[LockfileData, str]]


def recover_gateway(
    *,
    host: str,
    port: int,
    bearer_token: str,
    config_path: str | None,
    default_profile: str | None,
    discover_or_autostart: DiscoverOrAutostartFn | None = None,
) -> Result[tuple[str, int, str], str]:
    """Recover gateway endpoint via lockfile discovery or readiness polling.

    Args:
        host: Current gateway host.
        port: Current gateway port.
        bearer_token: Current bearer token.
        config_path: Config path for discovery recovery (None in explicit-server mode).
        default_profile: Default profile for discovery recovery.
        discover_or_autostart: Callable for lockfile discovery/autostart. Required
            when ``config_path`` is not None.

    Returns:
        Result with ``(host, port, token)`` tuple on success.
    """

    discovery_error = (
        "DISCOVERY_DISABLED: explicit server mode disables autostart recovery"
    )

    if config_path is not None and discover_or_autostart is not None:
        discovery_result = discover_or_autostart(
            config_path=config_path,
            default_profile=default_profile,
        )
        if discovery_result.is_ok:
            assert discovery_result.value is not None
            discovered = discovery_result.value
            return Result(value=(discovered.host, discovered.port, discovered.token))
        discovery_error = discovery_result.error or "DISCOVERY_FAILED"

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
            f"discovery={discovery_error}; readiness={readiness_result.error}"
        )
    )


def _recover_bridge_transport_state(
    *,
    recover_transport: Callable[[], Result[tuple[str, str], str]],
    message_method: str | None,
    initialize_payload: bytes | None,
    max_recovery_attempts: int,
) -> Result[tuple[str, str, str | None], str]:
    """Recover transport and re-bootstrap the MCP session when needed."""

    recovery_result = recover_transport()
    if recovery_result.is_err:
        return Result(error=recovery_result.error)
    assert recovery_result.value is not None
    recovered_mcp_url, recovered_bearer_token = recovery_result.value

    recovered_session_id: str | None = None
    if message_method != "initialize" and initialize_payload is not None:
        bootstrap_result = post_mcp_message(
            mcp_url=recovered_mcp_url,
            bearer_token=recovered_bearer_token,
            payload=initialize_payload,
            session_id=None,
            max_recovery_attempts=max_recovery_attempts,
        )
        if bootstrap_result.is_err:
            return Result(error=bootstrap_result.error)
        assert bootstrap_result.value is not None
        _content_type, _response_body, recovered_session_id = bootstrap_result.value

    return Result(
        value=(recovered_mcp_url, recovered_bearer_token, recovered_session_id)
    )


# ---------------------------------------------------------------------------
# HTTP bridge helpers
# ---------------------------------------------------------------------------


# @shell_complexity: HTTP POST with MCP-specific 503 contract retry and SSE/JSON
# content-type dispatch. Delegates retry/backoff to shared helper; caller
# retains response interpretation, session management, and MCP error semantics.
def post_mcp_message(
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

    503 responses are retried only when the response body matches the MCP
    transient warming contract (``_is_mcp_transient_warming_error``); other
    503 responses fail immediately. This preserves the caller-owned contract
    interpretation while delegating request/retry/backoff to the shared
    ``retry_http_request`` helper.

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

    def _mcp_error_from(helper_error: str) -> str:
        """Transform ``retry_http_request`` error format to MCP-specific format.

        ``retry_http_request`` returns ``HTTP_{code}: {url}`` for HTTP errors
        and ``HTTP_CONNECT_ERROR: {reason}`` for connection errors. MCP
        forwarding uses ``MCP_FORWARD_FAILED: http {code}`` and
        ``MCP_FORWARD_FAILED: {reason}`` respectively — these are caller-owned
        error semantics that must not leak into the shared helper.

        The ``HTTP_CONNECT_ERROR`` check MUST precede the generic ``HTTP_``
        check because ``HTTP_CONNECT_ERROR`` also starts with ``HTTP_``.
        """
        if helper_error.startswith("HTTP_CONNECT_ERROR: "):
            # HTTP_CONNECT_ERROR: {reason} → MCP_FORWARD_FAILED: {reason}
            reason = helper_error[len("HTTP_CONNECT_ERROR: ") :]
            return f"MCP_FORWARD_FAILED: {reason}"
        if helper_error.startswith("HTTP_"):
            # HTTP_{code}: {url} → MCP_FORWARD_FAILED: http {code}
            # Extract HTTP code between "HTTP_" and ":"
            code_end = helper_error.index(":")
            code = helper_error[len("HTTP_") : code_end]
            return f"MCP_FORWARD_FAILED: http {code}"
        return f"MCP_FORWARD_FAILED: {helper_error}"

    result = retry_http_request(
        url=mcp_url,
        method="POST",
        headers=headers,
        data=payload,
        max_retries=max_recovery_attempts,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
        backoff_seconds=HTTP_TRANSIENT_BACKOFF_SECONDS,
        retry_on_503=True,
        retry_on_transient=True,
        is_503_retryable=lambda exc: is_mcp_transient_warming_error(exc).value is True,
    )
    if result.is_err:
        return Result(error=_mcp_error_from(result.error or ""))
    assert result.value is not None

    try:
        content_type = result.value.headers.get("Content-Type", "")
        resp_session_id = result.value.headers.get("mcp-session-id")
        response_body = result.value.read()
    except Exception as exc:
        return Result(error=f"MCP_FORWARD_FAILED: {exc}")
    finally:
        try:
            result.value.close()
        except OSError:
            pass

    return Result(value=(content_type, response_body, resp_session_id))


# @shell_complexity: HTTP POST with transient retry — delegates retry/backoff to
# shared helper; caller retains lifecycle semantics (no body read on success).
def post_json(
    *, url: str, bearer_token: str, payload: dict[str, str]
) -> Result[None, str]:
    """POST JSON to a lifecycle endpoint with bearer auth and transient retry.

    Retries transient connection errors (connection refused, reset, broken pipe)
    that can occur when the gateway process is still binding or shutting down.
    Delegates request construction and retry/backoff to ``retry_http_request``.
    Response body is not read — the caller only needs confirmation of success.
    """

    body = json.dumps(payload).encode("utf-8")
    result = retry_http_request(
        url=url,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=body,
        max_retries=HTTP_TRANSIENT_RETRIES,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
        backoff_seconds=HTTP_TRANSIENT_BACKOFF_SECONDS,
        retry_on_503=True,
        retry_on_transient=True,
    )
    if result.is_err:
        return Result(error=result.error)
    assert result.value is not None
    # post_json does not read the response body; close the response.
    try:
        result.value.close()
    except OSError:
        pass
    return Result(value=None)


def post_json_once(
    *,
    url: str,
    bearer_token: str,
    payload: dict[str, str],
    timeout_seconds: float,
) -> Result[None, str]:
    """POST JSON once with a caller-supplied timeout and no retry.

    Used for bounded teardown-critical-section resume attempts.
    Delegates to ``retry_http_request`` with ``max_retries=0`` (single
    attempt, no retry/backoff). Response body is not read — this
    function only needs to confirm the request succeeded.
    """

    body = json.dumps(payload).encode("utf-8")
    result = retry_http_request(
        url=url,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=body,
        max_retries=0,
        timeout_seconds=timeout_seconds,
        retry_on_503=False,
        retry_on_transient=False,
    )
    if result.is_err:
        return Result(error=result.error)
    assert result.value is not None
    # post_json_once does not read the response body; close the response.
    try:
        result.value.close()
    except OSError:
        pass
    return Result(value=None)


# @shell_complexity: readiness polling branches on HTTP statuses, auth, and transient-warming detection
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


# @shell_complexity: gateway status fetch delegates retry/backoff to shared helper;
# caller retains response parsing and StatusResponse validation.
def _get_gateway_status(
    *, status_url: str, bearer_token: str
) -> Result[StatusResponse, str]:
    """Fetch and validate ``GET /status`` gateway readiness payload.

    Delegates request construction and retry/backoff to
    ``retry_http_request``. Response parsing and StatusResponse validation
    remain caller-owned.
    """

    result = retry_http_request(
        url=status_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        },
        max_retries=HTTP_TRANSIENT_RETRIES,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
        backoff_seconds=HTTP_TRANSIENT_BACKOFF_SECONDS,
        retry_on_503=True,
        retry_on_transient=True,
    )
    if result.is_err:
        return Result(error=result.error)
    assert result.value is not None

    try:
        decoded = result.value.read().decode("utf-8")
    except Exception as exc:
        return Result(error=f"INVALID_STATUS_PAYLOAD: {exc}")
    finally:
        try:
            result.value.close()
        except OSError:
            pass

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


# @shell_complexity: transient-warming error classification branches on HTTP code and body content
def is_mcp_transient_warming_error(exc: urllib_error.HTTPError) -> Result[bool, str]:
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


# @shell_complexity: request forwarding helper owns transport retry, response parsing, and protocol-level stale-session recovery in one bounded bridge step.
def _forward_request_with_recovery(
    *,
    mcp_url: str,
    bearer_token: str,
    message: bytes,
    session_id: str | None,
    message_method: str | None,
    initialize_payload: bytes | None,
    max_recovery_attempts: int,
    recover_transport: Callable[[], Result[tuple[str, str], str]] | None,
) -> Result[ForwardedBridgeResponse, str]:
    """Forward one MCP request, recovering transport when bridge state is stale."""

    current_mcp_url = mcp_url
    current_bearer_token = bearer_token
    current_session_id = session_id

    while True:
        http_result = post_mcp_message(
            mcp_url=current_mcp_url,
            bearer_token=current_bearer_token,
            payload=message,
            session_id=current_session_id,
            max_recovery_attempts=max_recovery_attempts,
        )
        if http_result.is_err:
            error_text = http_result.error or "MCP_FORWARD_FAILED: unknown error"
            recoverable_result = is_recoverable_error(error_text)
            if (
                recover_transport is None
                or recoverable_result.is_err
                or not recoverable_result.value
            ):
                return Result(error=error_text)

            recovery_result = _recover_bridge_transport_state(
                recover_transport=recover_transport,
                message_method=message_method,
                initialize_payload=initialize_payload,
                max_recovery_attempts=max_recovery_attempts,
            )
            if recovery_result.is_err:
                return Result(error=recovery_result.error)
            assert recovery_result.value is not None
            current_mcp_url, current_bearer_token, current_session_id = (
                recovery_result.value
            )
            continue

        assert http_result.value is not None
        content_type, response_body, response_session_id = http_result.value
        response_messages_result = extract_response_messages(
            content_type=content_type,
            response_body=response_body,
        )
        if response_messages_result.is_err:
            return Result(error=response_messages_result.error)
        assert response_messages_result.value is not None
        response_messages = response_messages_result.value

        if recover_transport is not None and _response_requires_bridge_recovery(
            response_messages
        ):
            recovery_result = _recover_bridge_transport_state(
                recover_transport=recover_transport,
                message_method=message_method,
                initialize_payload=initialize_payload,
                max_recovery_attempts=max_recovery_attempts,
            )
            if recovery_result.is_err:
                return Result(error=recovery_result.error)
            assert recovery_result.value is not None
            current_mcp_url, current_bearer_token, current_session_id = (
                recovery_result.value
            )
            continue

        next_session_id = (
            response_session_id
            if response_session_id is not None
            else current_session_id
        )
        return Result(
            value=ForwardedBridgeResponse(
                mcp_url=current_mcp_url,
                bearer_token=current_bearer_token,
                session_id=next_session_id,
                response_messages=response_messages,
            )
        )


def _write_bridge_response_messages(
    *, stdout_buffer: BinaryIO, response_messages: list[bytes], framed: bool
) -> Result[None, str]:
    """Write one or more MCP response payloads back to the upstream client."""

    for response_message in response_messages:
        write_result = write_framed_message(
            stdout_buffer,
            response_message,
            framed=framed,
        )
        if write_result.is_err:
            return Result(error=write_result.error)
    return Result(value=None)


# ---------------------------------------------------------------------------
# Forwarding loop
# ---------------------------------------------------------------------------


# @shell_complexity: forwarding loop bridges stdio framing and HTTP response variants.
def forward_stdio_http(
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
        message_result = read_framed_message(stdin_buffer)
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
        message_method = extract_jsonrpc_method(message)
        if message_method == "initialize":
            initialize_payload = message

        forward_result = _forward_request_with_recovery(
            mcp_url=mcp_url,
            bearer_token=bearer_token,
            message=message,
            session_id=session_id,
            message_method=message_method,
            initialize_payload=initialize_payload,
            max_recovery_attempts=max_recovery_attempts,
            recover_transport=recover_transport,
        )
        if forward_result.is_err:
            return Result(error=forward_result.error)
        assert forward_result.value is not None
        mcp_url = forward_result.value.mcp_url
        bearer_token = forward_result.value.bearer_token
        session_id = forward_result.value.session_id

        write_result = _write_bridge_response_messages(
            stdout_buffer=stdout_buffer,
            response_messages=forward_result.value.response_messages,
            framed=framed_message.is_content_length_framed,
        )
        if write_result.is_err:
            return Result(error=write_result.error)

    return Result(value=None)


# ---------------------------------------------------------------------------
# Bridge lifecycle
# ---------------------------------------------------------------------------


BridgeLoopAction = Literal["continue", "done"]


def _register_bridge_connection(
    *, base_url: str, bearer_token: str, connection_id: str
) -> Result[None, str]:
    """Register the bridge connection with the gateway lifecycle endpoint."""

    return post_json(
        url=f"{base_url}/connect",
        bearer_token=bearer_token,
        payload={"connection_id": connection_id},
    )


def _recover_inflight_transport(
    *,
    state: BridgeRuntimeState,
    connection_id: str,
    max_recovery_attempts: int,
    recovery_config_path: str | None,
    recovery_default_profile: str | None,
    discover_or_autostart: DiscoverOrAutostartFn | None,
) -> Result[tuple[str, str], str]:
    """Recover bridge transport state for an in-flight MCP request."""

    if state.recovery_attempts >= max_recovery_attempts:
        return Result(
            error="BRIDGE_RECOVERY_EXHAUSTED: in-flight MCP request could not be replayed"
        )

    state.recovery_attempts += 1
    gateway_recovery_result = recover_gateway(
        host=state.host,
        port=state.port,
        bearer_token=state.bearer_token,
        config_path=recovery_config_path,
        default_profile=recovery_default_profile,
        discover_or_autostart=discover_or_autostart,
    )
    if gateway_recovery_result.is_err:
        return Result(error=f"BRIDGE_RECOVERY_FAILED: {gateway_recovery_result.error}")
    assert gateway_recovery_result.value is not None
    state.host, state.port, state.bearer_token = gateway_recovery_result.value
    state.base_url = f"http://{state.host}:{state.port}"

    reconnect_result = _register_bridge_connection(
        base_url=state.base_url,
        bearer_token=state.bearer_token,
        connection_id=connection_id,
    )
    if reconnect_result.is_err:
        return Result(
            error=f"BRIDGE_RECOVERY_REGISTER_FAILED: {reconnect_result.error}"
        )

    return Result(value=(f"{state.base_url}/mcp", state.bearer_token))


# @shell_complexity: cycle helper coordinates readiness polling, forwarding, recoverability classification, and bridge-level retry budget.
def _run_bridge_cycle(
    *,
    state: BridgeRuntimeState,
    connection_id: str,
    stop_requested: Event,
    max_recovery_attempts: int,
    recovery_config_path: str | None,
    recovery_default_profile: str | None,
    discover_or_autostart: DiscoverOrAutostartFn | None,
) -> Result[BridgeLoopAction, str]:
    """Run one readiness + forwarding cycle for the active bridge."""

    readiness_result = _wait_for_gateway_readiness(
        status_url=f"{state.base_url}/status",
        bearer_token=state.bearer_token,
        max_polls=BRIDGE_READINESS_MAX_POLLS,
    )
    if readiness_result.is_err:
        cycle_error = readiness_result.error or "BRIDGE_READINESS_QUERY_FAILED"
        _emit_bridge_diagnostic(f"readiness wait stopped: {cycle_error}", connection_id)
    else:
        forward_result = forward_stdio_http(
            mcp_url=f"{state.base_url}/mcp",
            bearer_token=state.bearer_token,
            bridge_connection_id=connection_id,
            should_stop=stop_requested.is_set,
            stdin_buffer=sys.stdin.buffer,
            stdout_buffer=sys.stdout.buffer,
            max_recovery_attempts=max_recovery_attempts,
            recover_transport=lambda: _recover_inflight_transport(
                state=state,
                connection_id=connection_id,
                max_recovery_attempts=max_recovery_attempts,
                recovery_config_path=recovery_config_path,
                recovery_default_profile=recovery_default_profile,
                discover_or_autostart=discover_or_autostart,
            ),
        )
        if forward_result.is_ok:
            return Result(value="done")
        cycle_error = (
            forward_result.error or "BRIDGE_RUNTIME_ERROR: unknown bridge failure"
        )
        _emit_bridge_diagnostic(f"forwarding stopped: {cycle_error}", connection_id)

    recoverable_result = is_recoverable_error(cycle_error)
    if recoverable_result.is_err or not recoverable_result.value:
        return Result(error=cycle_error)

    if state.recovery_attempts >= max_recovery_attempts:
        return Result(error=f"BRIDGE_RECOVERY_EXHAUSTED: {cycle_error}")

    recovery_result = _recover_inflight_transport(
        state=state,
        connection_id=connection_id,
        max_recovery_attempts=max_recovery_attempts,
        recovery_config_path=recovery_config_path,
        recovery_default_profile=recovery_default_profile,
        discover_or_autostart=discover_or_autostart,
    )
    if recovery_result.is_err:
        return Result(error=recovery_result.error)
    return Result(value="continue")


# @shell_complexity: attach-loop helper repeatedly executes bridge cycles until completion, stop request, or interrupt.
def _run_bridge_attach_loop(
    *,
    state: BridgeRuntimeState,
    connection_id: str,
    stop_requested: Event,
    max_recovery_attempts: int,
    recovery_config_path: str | None,
    recovery_default_profile: str | None,
    discover_or_autostart: DiscoverOrAutostartFn | None,
) -> Result[None, str]:
    """Drive repeated readiness/forwarding cycles until completion or failure."""

    try:
        while not stop_requested.is_set():
            cycle_result = _run_bridge_cycle(
                state=state,
                connection_id=connection_id,
                stop_requested=stop_requested,
                max_recovery_attempts=max_recovery_attempts,
                recovery_config_path=recovery_config_path,
                recovery_default_profile=recovery_default_profile,
                discover_or_autostart=discover_or_autostart,
            )
            if cycle_result.is_err:
                return Result(error=cycle_result.error)
            assert cycle_result.value is not None
            if cycle_result.value == "done":
                return Result(value=None)
    except KeyboardInterrupt:
        _emit_bridge_diagnostic("attach loop interrupted", connection_id)
        return Result(error="INTERRUPT: bridge attach loop interrupted")

    return Result(value=None)


def _teardown_bridge_connection(
    *, base_url: str, bearer_token: str, connection_id: str
) -> Result[bool, str]:
    """Best-effort bridge disconnect with bounded interrupt-safe resume."""

    teardown_interrupted = False
    disconnect_url = f"{base_url}/disconnect"
    disconnect_payload = {"connection_id": connection_id}
    try:
        disconnect_result = post_json(
            url=disconnect_url,
            bearer_token=bearer_token,
            payload=disconnect_payload,
        )
        if disconnect_result.is_err:
            _emit_bridge_diagnostic(
                f"disconnect failed: {disconnect_result.error}", connection_id
            )
    except KeyboardInterrupt:
        teardown_interrupted = True
        _emit_bridge_diagnostic("disconnect interrupted", connection_id)
        current_sigint = signal.getsignal(signal.SIGINT)
        current_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            resumed_disconnect_result = post_json_once(
                url=disconnect_url,
                bearer_token=bearer_token,
                payload=disconnect_payload,
                timeout_seconds=TEARDOWN_RESUME_TIMEOUT_SECONDS,
            )
            if resumed_disconnect_result.is_err:
                _emit_bridge_diagnostic(
                    (f"disconnect resume failed: {resumed_disconnect_result.error}"),
                    connection_id,
                )
        finally:
            signal.signal(signal.SIGINT, current_sigint)
            signal.signal(signal.SIGTERM, current_sigterm)

    return Result(value=teardown_interrupted)


# @shell_complexity: bridge lifecycle coordinates signal handling, connect, forward, and disconnect.
def run_bridge(
    *,
    host: str,
    port: int,
    bearer_token: str,
    max_recovery_attempts: int = 3,
    recovery_config_path: str | None = None,
    recovery_default_profile: str | None = None,
    discover_or_autostart: DiscoverOrAutostartFn | None = None,
) -> Result[None, str]:
    """Run connect/register/forward/disconnect lifecycle.

    Args:
        host: Gateway host address.
        port: Gateway port number.
        bearer_token: Bearer token for authentication.
        max_recovery_attempts: Maximum transient error recovery retries.
        recovery_config_path: Config path for autostart recovery (None in explicit-server mode).
        recovery_default_profile: Default profile for autostart recovery.
        discover_or_autostart: Callable for gateway discovery during recovery.

    Returns:
        Result with None on success or error string on failure.
    """

    state = BridgeRuntimeState(
        base_url=f"http://{host}:{port}",
        host=host,
        port=port,
        bearer_token=bearer_token,
    )
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
        connect_result = _register_bridge_connection(
            base_url=state.base_url,
            bearer_token=state.bearer_token,
            connection_id=connection_id,
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

    bridge_result: Result[None, str] = Result(value=None)
    teardown_error: str | None = None

    try:
        bridge_result = _run_bridge_attach_loop(
            state=state,
            connection_id=connection_id,
            stop_requested=stop_requested,
            max_recovery_attempts=max_recovery_attempts,
            recovery_config_path=recovery_config_path,
            recovery_default_profile=recovery_default_profile,
            discover_or_autostart=discover_or_autostart,
        )
    finally:
        teardown_result = _teardown_bridge_connection(
            base_url=state.base_url,
            bearer_token=state.bearer_token,
            connection_id=connection_id,
        )
        if teardown_result.is_err:
            teardown_error = teardown_result.error or "BRIDGE_TEARDOWN_FAILED"
            teardown_interrupted = False
        else:
            assert teardown_result.value is not None
            teardown_interrupted = teardown_result.value
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)

        if teardown_interrupted and bridge_result.is_ok:
            pass

    if bridge_result.is_err:
        if teardown_error is not None:
            return Result(error=f"{bridge_result.error}; {teardown_error}")
        return Result(error=bridge_result.error)
    if teardown_error is not None:
        return Result(error=teardown_error)
    return Result(value=None)
