"""Liveness probe: verify tela gateway actually starts and serves MCP traffic.

Expected (per README.md, docs/INTERFACES.md, docs/USAGE.md):
  - stdio mode: `tela connect` stays alive while stdin is open, responds to MCP initialize
  - HTTP gateway mode: `tela serve --port` stays alive and accepts HTTP connections
  - "tela: ready" banner goes to stderr (not stdout), since stdout IS the MCP transport
  - CLI subcommands (status, profiles, connections, audit) produce meaningful output

Actual (observed via black-box probing):
  - stdio mode: process stays alive with open stdin but does NOT respond to MCP messages
  - SSE mode: process prints "ready" and exits immediately (exit 0)
  - "tela: ready" banner is written to stdout, corrupting the MCP stdio transport
  - CLI subcommands work but report zero state (status shows 0 servers, 0 profiles)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time


def _write_minimal_config(tmp_dir: str) -> str:
    """Write a minimal open-mode config for testing."""
    config = {
        "profiles": {
            "dev": {
                "default": True,
            },
        },
        "auth": {
            "mode": "open",
        },
    }
    path = os.path.join(tmp_dir, "tela.yaml")
    import yaml  # noqa: F811

    with open(path, "w") as f:
        yaml.dump(config, f)
    return path


def test_stdio_ready_banner_not_on_stdout():
    """The 'tela: ready' banner must NOT go to stdout (MCP transport channel).

    Per MCP spec, stdout is the JSON-RPC transport. Any non-JSON-RPC data
    on stdout corrupts the transport. The ready banner should go to stderr.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_minimal_config(tmp_dir)

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tela",
                "connect",
                "--config",
                config_path,
                "--default-profile",
                "dev",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Give process time to emit its banner
        time.sleep(2)

        # Close stdin to let process exit
        proc.stdin.close()
        proc.wait(timeout=5)

        stdout_data = proc.stdout.read().decode("utf-8", errors="replace")
        stderr_data = proc.stderr.read().decode("utf-8", errors="replace")

        # The ready banner should NOT appear on stdout
        assert "tela: ready" not in stdout_data, (
            f"Liveness: 'tela: ready' banner is on stdout, which corrupts "
            f"the MCP stdio transport. stdout={stdout_data!r}, stderr={stderr_data!r}"
        )


def test_stdio_responds_to_mcp_initialize():
    """stdio mode must respond to a JSON-RPC initialize request.

    Per MCP protocol and docs/INTERFACES.md section F, tela exposes a standard
    MCP server. It must respond to initialize with capabilities and tools/list.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_minimal_config(tmp_dir)

        serve_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tela",
                "serve",
                "--config",
                config_path,
                "--port",
                "0",
                "--default-profile",
                "dev",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(2)

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tela",
                "connect",
                "--config",
                config_path,
                "--default-profile",
                "dev",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        init_request_bytes = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0.1"},
                },
            }
        ).encode("utf-8")
        framed_request = (
            f"Content-Length: {len(init_request_bytes)}\r\n\r\n".encode("ascii")
            + init_request_bytes
        )

        try:
            proc.stdin.write(framed_request)
            proc.stdin.flush()

            import select

            ready, _, _ = select.select([proc.stdout], [], [], 10.0)
            assert ready, (
                "Liveness: stdio mode did not respond to MCP initialize "
                "within 10 seconds. The gateway accepts connections but does "
                "not speak the MCP protocol."
            )

            header_bytes = b""
            while b"\r\n\r\n" not in header_bytes:
                chunk = proc.stdout.read(1)
                assert chunk, "Liveness: EOF while reading MCP response headers"
                header_bytes += chunk

            header_text = header_bytes.decode("ascii", errors="replace")
            content_length = None
            for line in header_text.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
                    break

            assert content_length is not None, (
                f"Liveness: Missing Content-Length header in response: {header_text!r}"
            )

            body = proc.stdout.read(content_length)
            resp = json.loads(body.decode("utf-8", errors="replace"))
            assert "result" in resp or "error" in resp, (
                f"Liveness: Response is not a valid JSON-RPC response: {resp!r}"
            )

        finally:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
            serve_proc.terminate()
            serve_proc.wait(timeout=5)


def test_http_mode_stays_alive():
    """HTTP gateway mode must bind to the given port and stay alive.

    Per README.md: 'tela serve --config tela.yaml --port 8080' starts a
    long-lived gateway process and exposes it over HTTP.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_minimal_config(tmp_dir)

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tela",
                "serve",
                "--config",
                config_path,
                "--port",
                "0",
                "--default-profile",
                "dev",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # HTTP gateway should stay alive for at least 3 seconds
        time.sleep(3)

        poll_result = proc.poll()

        if poll_result is not None:
            stdout_data = proc.stdout.read().decode("utf-8", errors="replace")
            stderr_data = proc.stderr.read().decode("utf-8", errors="replace")

            assert False, (
                f"Liveness: HTTP gateway exited immediately with code {poll_result}. "
                "Expected a long-lived process on an ephemeral port. "
                f"stdout={stdout_data!r}, stderr={stderr_data!r}"
            )
        else:
            proc.terminate()
            proc.wait(timeout=5)


def test_status_reports_meaningful_state():
    """tela status --json should report meaningful state when gateway is running.

    Per docs/INTERFACES.md: 'tela status' prints uptime, connected downstream
    servers, active connections, profile count.

    This query requires a running gateway discoverable via lockfile.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_minimal_config(tmp_dir)

        serve_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tela",
                "serve",
                "--config",
                config_path,
                "--port",
                "0",
                "--default-profile",
                "dev",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            time.sleep(2)
            result = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, (
                f"Liveness: 'tela status --json' failed with code {result.returncode}. "
                f"stderr={result.stderr!r}"
            )

            data = json.loads(result.stdout)
            assert "uptime_seconds" in data, (
                f"Liveness: status output missing 'uptime_seconds': {data!r}"
            )
        finally:
            serve_proc.terminate()
            serve_proc.wait(timeout=5)


if __name__ == "__main__":
    import traceback

    tests = [
        test_stdio_ready_banner_not_on_stdout,
        test_stdio_responds_to_mcp_initialize,
        test_http_mode_stays_alive,
        test_status_reports_meaningful_state,
    ]

    passed = 0
    failed = 0
    for test in tests:
        name = test.__name__
        try:
            test()
            print(f"  PASS: {name}")
            passed += 1
        except Exception:
            print(f"  FAIL: {name}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
