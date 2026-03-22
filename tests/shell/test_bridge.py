"""Tests for stdio <-> HTTP bridge adapter."""

from __future__ import annotations

import io
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tela.shell.bridge import BridgeConfig, StdioHttpBridge


@dataclass
class _RecordedRequest:
    path: str
    authorization: str
    body: dict[str, object]


def _start_mcp_server() -> tuple[ThreadingHTTPServer, list[_RecordedRequest], str]:
    requests: list[_RecordedRequest] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body_bytes = self.rfile.read(length)
            payload = json.loads(body_bytes.decode("utf-8"))
            assert isinstance(payload, dict)

            requests.append(
                _RecordedRequest(
                    path=self.path,
                    authorization=self.headers.get("Authorization", ""),
                    body=payload,
                )
            )

            response = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"echo_method": payload.get("method")},
            }
            encoded = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            _ = format
            _ = args

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    address = server.server_address
    host = str(address[0])
    port = int(address[1])
    endpoint = f"http://{host}:{port}/mcp"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, requests, endpoint


def test_bridge_forwards_stdio_to_http_and_writes_stdout() -> None:
    """Bridge forwards initialize/tool/shutdown and mirrors responses to stdout."""

    server, requests, endpoint = _start_mcp_server()
    try:
        stdin = io.StringIO(
            "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05"},
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "demo", "arguments": {}},
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "shutdown",
                            "params": {},
                        }
                    ),
                ]
            )
            + "\n"
        )
        stdout = io.StringIO()

        bridge = StdioHttpBridge(
            BridgeConfig(endpoint=endpoint, bearer_token="bridge-token"),
            stdin=stdin,
            stdout=stdout,
        )
        result = bridge.run()

        assert result.is_ok
        assert result.value == 0

        assert len(requests) == 3
        assert all(entry.path == "/mcp" for entry in requests)
        assert all(entry.authorization == "Bearer bridge-token" for entry in requests)
        assert [entry.body.get("method") for entry in requests] == [
            "initialize",
            "tools/call",
            "shutdown",
        ]

        responses = [
            json.loads(line)
            for line in stdout.getvalue().splitlines()
            if line.strip() != ""
        ]
        assert [resp["id"] for resp in responses] == [1, 2, 3]
        assert [resp["result"]["echo_method"] for resp in responses] == [
            "initialize",
            "tools/call",
            "shutdown",
        ]
    finally:
        server.shutdown()
        server.server_close()


def test_bridge_graceful_shutdown_on_eof() -> None:
    """Bridge exits cleanly when stdin reaches EOF."""

    server, _, endpoint = _start_mcp_server()
    try:
        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                }
            )
            + "\n"
        )
        stdout = io.StringIO()

        bridge = StdioHttpBridge(
            BridgeConfig(endpoint=endpoint, bearer_token="bridge-token"),
            stdin=stdin,
            stdout=stdout,
        )
        result = bridge.run()

        assert result.is_ok
        assert result.value == 0
    finally:
        server.shutdown()
        server.server_close()


def test_bridge_sigterm_handler_sets_stop_event() -> None:
    """SIGTERM handler flips shutdown event for graceful termination."""

    stop_event = threading.Event()
    bridge = StdioHttpBridge(
        BridgeConfig(endpoint="http://127.0.0.1:1/mcp", bearer_token="token"),
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stop_event=stop_event,
    )

    assert not stop_event.is_set()
    bridge._handle_sigterm(15, None)
    assert stop_event.is_set()
