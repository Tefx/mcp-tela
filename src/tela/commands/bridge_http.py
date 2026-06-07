"""Phase-aware MCP data-plane HTTP executor for the stdio bridge.

This module is intentionally scoped to ``POST /mcp`` data-plane forwarding.
Control-plane readiness/lifecycle retries remain in ``tela.commands.http_client``.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import socket
from typing import Callable, Literal
from urllib.parse import urlsplit

from tela.core.bridge_protocol import bridge_http_headers, bridge_http_timeouts_valid
from tela.shell.result import Result


INVALID_TIMEOUT_MESSAGE = (
    "INVALID_TIMEOUT: connect_timeout_seconds and write_timeout_seconds must be finite > 0; "
    "response_timeout_seconds must be None or finite > 0"
)


# @invar:allow shell_result: local type guard prevents Core precondition assertions
# before returning the public shell Result error shape.
# @shell_orchestration: validation straddles Shell bad-input handling and Core helper contracts.
def _timeout_args_have_valid_types(
    *,
    connect_timeout_seconds: object,
    write_timeout_seconds: object,
    response_timeout_seconds: object,
) -> bool:
    """Return True only when timeout values are safe for Core validation."""

    if not isinstance(connect_timeout_seconds, (int, float)) or isinstance(
        connect_timeout_seconds, bool
    ):
        return False
    if not isinstance(write_timeout_seconds, (int, float)) or isinstance(
        write_timeout_seconds, bool
    ):
        return False
    if response_timeout_seconds is None:
        return True
    return isinstance(response_timeout_seconds, (int, float)) and not isinstance(
        response_timeout_seconds, bool
    )


@dataclass(frozen=True)
class BridgeHttpError:
    """Phase-aware MCP HTTP failure details."""

    phase: Literal[
        "connect",
        "write",
        "response_headers",
        "response_body",
        "http_status",
    ]
    message: str
    request_sent: bool | None
    mcp_admitted: bool | None
    status_code: int | None = None
    retryable_warming: bool = False


@dataclass(frozen=True)
class BridgeHttpResponse:
    """Successful MCP HTTP response details."""

    content_type: str
    body: bytes
    session_id: str | None


# @shell_complexity: ADR-009 lifecycle mapping necessarily branches by HTTP phase.
def post_mcp_http(
    *,
    mcp_url: str,
    bearer_token: str,
    payload: bytes,
    session_id: str | None,
    connect_timeout_seconds: float,
    write_timeout_seconds: float,
    response_timeout_seconds: float | None,
    is_503_retryable: Callable[[bytes], bool],
) -> Result[BridgeHttpResponse, BridgeHttpError]:
    """POST an MCP JSON-RPC payload with ADR-009 lifecycle semantics.

    The executor distinguishes connect, write, response-header, response-body,
    and HTTP-status failures so bridge recovery can decide whether replay is
    safe without string-matching transport errors.
    """

    if not _timeout_args_have_valid_types(
        connect_timeout_seconds=connect_timeout_seconds,
        write_timeout_seconds=write_timeout_seconds,
        response_timeout_seconds=response_timeout_seconds,
    ) or not bridge_http_timeouts_valid(
        connect_timeout_seconds=connect_timeout_seconds,
        write_timeout_seconds=write_timeout_seconds,
        response_timeout_seconds=response_timeout_seconds,
    ):
        return Result(
            error=BridgeHttpError(
                phase="connect",
                message=INVALID_TIMEOUT_MESSAGE,
                request_sent=False,
                mcp_admitted=None,
            )
        )

    parsed = urlsplit(mcp_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return Result(
            error=BridgeHttpError(
                phase="connect",
                message=f"INVALID_MCP_URL: unsupported or missing host: {mcp_url}",
                request_sent=False,
                mcp_admitted=None,
            )
        )

    connection_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_cls(parsed.hostname, parsed.port, timeout=connect_timeout_seconds)

    try:
        try:
            connection.connect()
        except Exception as exc:  # noqa: BLE001 - shell boundary maps I/O failures.
            return Result(
                error=BridgeHttpError(
                    phase="connect",
                    message=str(exc),
                    request_sent=False,
                    mcp_admitted=None,
                )
            )

        if connection.sock is not None:
            connection.sock.settimeout(write_timeout_seconds)

        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        headers = bridge_http_headers(
            bearer_token=bearer_token,
            payload_length=len(payload),
            session_id=session_id,
        )
        try:
            connection.putrequest("POST", target)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
        except Exception as exc:  # noqa: BLE001 - no body bytes were sent by this code path.
            return Result(
                error=BridgeHttpError(
                    phase="write",
                    message=str(exc),
                    request_sent=False,
                    mcp_admitted=None,
                )
            )

        try:
            if payload:
                connection.send(payload)
            request_sent = True
        except Exception as exc:  # noqa: BLE001 - sendall partial state is unknowable.
            return Result(
                error=BridgeHttpError(
                    phase="write",
                    message=str(exc),
                    request_sent=None,
                    mcp_admitted=None,
                )
            )

        if connection.sock is not None:
            connection.sock.settimeout(response_timeout_seconds)

        try:
            response = connection.getresponse()
        except Exception as exc:  # noqa: BLE001 - response-header I/O boundary.
            message = str(exc)
            if response_timeout_seconds is not None and isinstance(
                exc, (TimeoutError, socket.timeout)
            ):
                message = "MCP_REQUEST_TIMEOUT: response deadline expired"
            return Result(
                error=BridgeHttpError(
                    phase="response_headers",
                    message=message,
                    request_sent=request_sent,
                    mcp_admitted=None,
                )
            )

        try:
            body = response.read()
        except Exception as exc:  # noqa: BLE001 - response-body I/O boundary.
            message = str(exc)
            if response_timeout_seconds is not None and isinstance(
                exc, (TimeoutError, socket.timeout)
            ):
                message = "MCP_REQUEST_TIMEOUT: response deadline expired"
            return Result(
                error=BridgeHttpError(
                    phase="response_body",
                    message=message,
                    request_sent=request_sent,
                    mcp_admitted=None,
                )
            )

        content_type = response.getheader("Content-Type", "") or ""
        response_session_id = response.getheader("mcp-session-id")
        if 200 <= response.status <= 299:
            return Result(
                value=BridgeHttpResponse(
                    content_type=content_type,
                    body=body,
                    session_id=response_session_id,
                )
            )

        retryable_warming = response.status == 503 and is_503_retryable(body)
        status_message = f"HTTP_{response.status}: gateway warming"
        if not retryable_warming:
            status_detail = f"http {response.status}"
            if response.reason:
                status_detail = f"{status_detail} {response.reason}"
            status_message = f"MCP_FORWARD_FAILED: {status_detail}"
        return Result(
            error=BridgeHttpError(
                phase="http_status",
                message=status_message,
                request_sent=request_sent,
                mcp_admitted=False if retryable_warming else None,
                status_code=response.status,
                retryable_warming=retryable_warming,
            )
        )
    finally:
        connection.close()
