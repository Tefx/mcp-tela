"""stdio <-> HTTP MCP session bridge adapter.

Reads newline-delimited MCP JSON-RPC messages from stdin, forwards each message
to ``POST /mcp`` with Bearer auth, and writes the HTTP response JSON back to
stdout.
"""

from __future__ import annotations

import json
import select
import signal
import threading
from dataclasses import dataclass
from types import FrameType
from typing import IO, Callable, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from tela.shell.result import Result


JsonValue = object
JsonObject = dict[str, JsonValue]


class _HTTPResponse(Protocol):
    def read(self, amt: int = -1) -> bytes: ...

    def __enter__(self) -> "_HTTPResponse": ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None: ...


Opener = Callable[..., _HTTPResponse]


@dataclass(frozen=True)
class BridgeConfig:
    """Configuration for stdio <-> HTTP message forwarding.

    Args:
        endpoint: Full MCP endpoint URL (for example ``http://127.0.0.1:8080/mcp``).
        bearer_token: Bearer token used for Authorization header.
        timeout_seconds: HTTP request timeout in seconds.
    """

    endpoint: str
    bearer_token: str
    timeout_seconds: float = 30.0


class StdioHttpBridge:
    """Forward MCP JSON-RPC messages from stdin to HTTP and back to stdout."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        stdin: IO[str],
        stdout: IO[str],
        opener: Opener = urllib_request.urlopen,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Create bridge runtime.

        Args:
            config: Bridge network/auth configuration.
            stdin: Input stream with newline-delimited JSON-RPC requests.
            stdout: Output stream where JSON-RPC responses are written.
            opener: HTTP opener function (defaults to ``urllib.request.urlopen``).
            stop_event: Optional externally-controlled shutdown event.
        """

        self._config = config
        self._stdin = stdin
        self._stdout = stdout
        self._opener = opener
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._initialized = False

    def run(self) -> Result[int, str]:
        """Run the bridge loop until EOF, shutdown request, or SIGTERM.

        Returns:
            ``Result(value=0)`` on graceful shutdown; ``Result(error=...)`` on
            unrecoverable failures.
        """

        previous_sigterm = None
        can_install_signal = threading.current_thread() is threading.main_thread()
        if can_install_signal:
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._handle_sigterm)

        try:
            while not self._stop_event.is_set():
                line = self._read_next_line()
                if line is None:
                    break

                stripped = line.strip()
                if stripped == "":
                    continue

                request_obj = self._parse_request(stripped)
                if request_obj is None:
                    continue

                response_obj = self._forward_to_http(request_obj)
                self._write_response(response_obj)

                method = self._jsonrpc_method(request_obj)
                if method == "initialize" and "error" not in response_obj:
                    self._initialized = True

                if method in {"shutdown", "exit"}:
                    break

            return Result(value=0)
        finally:
            if can_install_signal and previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)

    def _handle_sigterm(self, _signum: int, _frame: FrameType | None) -> None:
        self._stop_event.set()

    def _read_next_line(self) -> str | None:
        """Read next stdin line, polling shutdown event when selectable."""

        if not self._supports_fileno(self._stdin):
            line = self._stdin.readline()
            if line == "":
                return None
            return line

        while not self._stop_event.is_set():
            readable, _, _ = select.select([self._stdin], [], [], 0.1)
            if not readable:
                continue
            line = self._stdin.readline()
            if line == "":
                return None
            return line

        return None

    def _parse_request(self, raw: str) -> JsonObject | None:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            self._write_response(
                self._jsonrpc_error(request_id=None, code=-32700, message="PARSE_ERROR")
            )
            return None

        if not isinstance(decoded, dict):
            self._write_response(
                self._jsonrpc_error(
                    request_id=None,
                    code=-32600,
                    message="INVALID_REQUEST",
                )
            )
            return None

        return decoded

    def _forward_to_http(self, request_obj: JsonObject) -> JsonObject:
        payload = json.dumps(request_obj, separators=(",", ":")).encode("utf-8")
        request = urllib_request.Request(
            self._config.endpoint,
            method="POST",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._config.bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with self._opener(
                request, timeout=self._config.timeout_seconds
            ) as response:
                response_payload = response.read()
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return self._error_from_http_failure(request_obj=request_obj, detail=body)
        except urllib_error.URLError as exc:
            return self._error_from_http_failure(
                request_obj=request_obj, detail=str(exc.reason)
            )

        try:
            decoded = json.loads(response_payload.decode("utf-8"))
        except json.JSONDecodeError:
            return self._error_from_http_failure(
                request_obj=request_obj,
                detail="NON_JSON_HTTP_RESPONSE",
            )

        if not isinstance(decoded, dict):
            return self._error_from_http_failure(
                request_obj=request_obj,
                detail="INVALID_JSONRPC_RESPONSE_SHAPE",
            )

        return decoded

    def _write_response(self, response_obj: JsonObject) -> None:
        self._stdout.write(json.dumps(response_obj, separators=(",", ":")) + "\n")
        self._stdout.flush()

    @staticmethod
    def _supports_fileno(stream: IO[str]) -> bool:
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            return False
        return True

    @staticmethod
    def _jsonrpc_method(request_obj: JsonObject) -> str | None:
        method = request_obj.get("method")
        if isinstance(method, str):
            return method
        return None

    @classmethod
    def _error_from_http_failure(
        cls, request_obj: JsonObject, detail: str
    ) -> JsonObject:
        return cls._jsonrpc_error(
            request_id=request_obj.get("id"),
            code=-32000,
            message=f"MCP_HTTP_FORWARD_FAILED: {detail}",
        )

    @staticmethod
    def _jsonrpc_error(request_id: JsonValue, code: int, message: str) -> JsonObject:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
