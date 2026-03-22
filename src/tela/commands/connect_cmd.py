"""Connect command entrypoint for stdio-to-HTTP bridge lifecycle.

Implements ``tela connect`` service discovery, optional auto-start of
``tela serve``, and bridge lifecycle management:

1. Discover endpoint from lockfile (unless ``--server`` is given)
2. Resolve bearer token precedence
3. Register connection via ``POST /connect``
4. Forward stdio MCP frames to ``POST /mcp``
5. Deregister connection via ``POST /disconnect`` on exit/signals
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
from tela.shell.config_loader import Result
from tela.shell.lockfile import delete_lockfile, read_lockfile


LOCKFILE_WAIT_TIMEOUT_SECONDS = 5.0
LOCKFILE_WAIT_POLL_SECONDS = 0.1
LOCKFILE_START_RACE_RETRIES = 3
HTTP_TIMEOUT_SECONDS = 10.0


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


# @shell_complexity: discovery flow includes stale handling, autostart, and race retries.
def _discover_or_autostart(
    *,
    config_path: str,
    default_profile: str | None,
) -> Result[LockfileData, str]:
    """Discover running server via lockfile, auto-starting with race retry."""

    lockfile_result = read_lockfile()
    if lockfile_result.is_ok:
        assert lockfile_result.value is not None
        return Result(value=lockfile_result.value)

    for _attempt in range(LOCKFILE_START_RACE_RETRIES):
        lockfile_result = _wait_for_live_lockfile(timeout_seconds=0.3)
        if lockfile_result.is_ok:
            assert lockfile_result.value is not None
            return Result(value=lockfile_result.value)

        start_result = _autostart_serve(
            config_path=config_path,
            default_profile=default_profile,
        )
        wait_result = _wait_for_live_lockfile(
            timeout_seconds=LOCKFILE_WAIT_TIMEOUT_SECONDS
        )
        if wait_result.is_ok:
            assert wait_result.value is not None
            return Result(value=wait_result.value)

        if start_result.is_err:
            continue

    return Result(
        error=(
            "DISCOVERY_FAILED: could not discover or auto-start tela serve via lockfile"
        )
    )


def _wait_for_live_lockfile(timeout_seconds: float) -> Result[LockfileData, str]:
    """Wait for a non-stale lockfile to become available."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        lockfile_result = read_lockfile()
        if lockfile_result.is_ok:
            assert lockfile_result.value is not None
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
) -> Result[None, str]:
    """Auto-start ``tela serve`` as detached subprocess."""

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
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return Result(error=f"AUTOSTART_FAILED: {exc}")

    return Result(value=None)


def _run_bridge(*, host: str, port: int, bearer_token: str) -> Result[None, str]:
    """Run connect/register/forward/disconnect lifecycle."""

    base_url = f"http://{host}:{port}"
    connection_id = f"bridge_{uuid.uuid4().hex}"
    stop_requested = Event()

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)

    def _handle_stop(_signum: int, _frame: FrameType | None) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    connect_result = _post_json(
        url=f"{base_url}/connect",
        bearer_token=bearer_token,
        payload={"connection_id": connection_id},
    )
    if connect_result.is_err:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        return Result(error=connect_result.error)

    bridge_error: str | None = None
    try:
        forward_result = _forward_stdio_http(
            mcp_url=f"{base_url}/mcp",
            bearer_token=bearer_token,
            should_stop=stop_requested.is_set,
            stdin_buffer=sys.stdin.buffer,
            stdout_buffer=sys.stdout.buffer,
        )
        if forward_result.is_err:
            bridge_error = forward_result.error
    finally:
        _ = _post_json(
            url=f"{base_url}/disconnect",
            bearer_token=bearer_token,
            payload={"connection_id": connection_id},
        )
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)

    if bridge_error is not None:
        return Result(error=bridge_error)
    return Result(value=None)


# @shell_complexity: forwarding loop bridges stdio framing and HTTP response variants.
def _forward_stdio_http(
    *,
    mcp_url: str,
    bearer_token: str,
    should_stop: Callable[[], bool],
    stdin_buffer: BinaryIO,
    stdout_buffer: BinaryIO,
) -> Result[None, str]:
    """Forward MCP stdio frames to HTTP and stream responses back."""

    while not should_stop():
        message_result = _read_framed_message(stdin_buffer)
        if message_result.is_err:
            return Result(error=message_result.error)
        assert message_result.value is not None or message_result.error is None
        message = message_result.value
        if message is None:
            return Result(value=None)

        http_result = _post_mcp_message(
            mcp_url=mcp_url,
            bearer_token=bearer_token,
            payload=message,
        )
        if http_result.is_err:
            return Result(error=http_result.error)
        assert http_result.value is not None
        content_type, response_body = http_result.value

        response_messages_result = _extract_response_messages(
            content_type=content_type,
            response_body=response_body,
        )
        if response_messages_result.is_err:
            return Result(error=response_messages_result.error)
        assert response_messages_result.value is not None
        for response_message in response_messages_result.value:
            _write_framed_message(stdout_buffer, response_message)

    return Result(value=None)


# @shell_complexity: parser handles header scanning and content-length extraction.
def _read_framed_message(stream: BinaryIO) -> Result[bytes | None, str]:
    """Read one stdio-framed MCP message payload from stream."""

    content_length: int | None = None
    while True:
        line = stream.readline()
        if line == b"":
            return Result(value=None)
        stripped = line.strip()
        if stripped == b"":
            break
        key, sep, value = stripped.partition(b":")
        if sep == b"" or key.lower() != b"content-length":
            continue
        try:
            content_length = int(value.strip())
        except ValueError:
            return Result(error="INVALID_FRAME: non-integer Content-Length")

    if content_length is None or content_length < 0:
        return Result(error="INVALID_FRAME: missing Content-Length")

    payload = stream.read(content_length)
    if len(payload) != content_length:
        return Result(error="INVALID_FRAME: truncated payload")
    return Result(value=payload)


def _write_framed_message(stream: BinaryIO, payload: bytes) -> None:
    """Write one stdio-framed MCP message payload."""

    header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(payload)
    stream.flush()


def _post_mcp_message(
    *,
    mcp_url: str,
    bearer_token: str,
    payload: bytes,
) -> Result[tuple[str, bytes], str]:
    """POST payload to MCP streamable HTTP endpoint."""

    request = urllib_request.Request(
        mcp_url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            return Result(value=(content_type, response.read()))
    except urllib_error.HTTPError as exc:
        return Result(error=f"MCP_FORWARD_FAILED: http {exc.code}")
    except urllib_error.URLError as exc:
        return Result(error=f"MCP_FORWARD_FAILED: {exc.reason}")


# @shell_orchestration: response mapping stays in shell because it is transport framing glue.
def _extract_response_messages(
    *, content_type: str, response_body: bytes
) -> Result[list[bytes], str]:
    """Convert HTTP response body into one-or-more MCP stdio payloads."""

    if response_body == b"":
        return Result(value=[])

    if "text/event-stream" in content_type.lower():
        return _parse_sse_payloads(response_body)
    return Result(value=[response_body])


# @shell_complexity: parser handles event boundaries and data line accumulation.
# @shell_orchestration: SSE parsing is coupled to HTTP transport framing behavior.
def _parse_sse_payloads(raw_body: bytes) -> Result[list[bytes], str]:
    """Parse SSE body into MCP JSON payload bytes."""

    text = raw_body.decode("utf-8", errors="replace")
    payloads: list[bytes] = []
    current_data: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "":
            if current_data:
                payload = "\n".join(current_data)
                if payload and payload != "[DONE]":
                    payloads.append(payload.encode("utf-8"))
                current_data.clear()
            continue

        if stripped.startswith("data:"):
            current_data.append(stripped[5:].lstrip())

    if current_data:
        payload = "\n".join(current_data)
        if payload and payload != "[DONE]":
            payloads.append(payload.encode("utf-8"))

    return Result(value=payloads)


def _post_json(
    *, url: str, bearer_token: str, payload: dict[str, str]
) -> Result[None, str]:
    """POST JSON to a lifecycle endpoint with bearer auth."""

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
        with urllib_request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS):
            return Result(value=None)
    except urllib_error.HTTPError as exc:
        return Result(error=f"HTTP_{exc.code}: {url}")
    except urllib_error.URLError as exc:
        return Result(error=f"HTTP_CONNECT_ERROR: {exc.reason}")
