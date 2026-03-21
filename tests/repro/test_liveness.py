"""Liveness probe: verify tela gateway actually starts and serves MCP traffic.

Expected (per README.md, docs/INTERFACES.md, docs/USAGE.md):
  - stdio mode: process stays alive while stdin is open, responds to MCP initialize
  - SSE mode: process binds to --port and stays alive, accepts HTTP connections
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
            [sys.executable, "-m", "tela", "start", "--config", config_path,
             "--default-profile", "dev"],
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

        proc = subprocess.Popen(
            [sys.executable, "-m", "tela", "start", "--config", config_path,
             "--default-profile", "dev"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Send MCP initialize request (newline-delimited JSON-RPC)
        init_request = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0.1"},
            },
        })

        try:
            proc.stdin.write((init_request + "\n").encode())
            proc.stdin.flush()

            # Wait for response with timeout
            import select

            ready, _, _ = select.select([proc.stdout], [], [], 5.0)
            assert ready, (
                "Liveness: stdio mode did not respond to MCP initialize "
                "within 5 seconds. The gateway accepts connections but does "
                "not speak the MCP protocol."
            )

            response_line = proc.stdout.readline().decode("utf-8", errors="replace")
            # Filter out non-JSON lines (like the ready banner)
            lines = response_line.strip().split("\n")
            json_responses = []
            for line in lines:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        json_responses.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

            assert json_responses, (
                f"Liveness: No valid JSON-RPC response to initialize. "
                f"Raw stdout: {response_line!r}"
            )

            resp = json_responses[0]
            assert "result" in resp or "error" in resp, (
                f"Liveness: Response is not a valid JSON-RPC response: {resp!r}"
            )

        finally:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)


def test_sse_mode_stays_alive():
    """SSE mode must bind to the given port and stay alive.

    Per README.md: 'tela start --config tela.yaml --port 8080' starts a
    long-lived gateway process and exposes it over SSE.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_minimal_config(tmp_dir)

        proc = subprocess.Popen(
            [sys.executable, "-m", "tela", "start", "--config", config_path,
             "--port", "18932", "--transport", "sse", "--default-profile", "dev"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # SSE should stay alive for at least 3 seconds
        time.sleep(3)

        poll_result = proc.poll()

        if poll_result is not None:
            stdout_data = proc.stdout.read().decode("utf-8", errors="replace")
            stderr_data = proc.stderr.read().decode("utf-8", errors="replace")

            assert False, (
                f"Liveness: SSE mode exited immediately with code {poll_result}. "
                f"Expected a long-lived process bound to port 18932. "
                f"stdout={stdout_data!r}, stderr={stderr_data!r}"
            )
        else:
            proc.terminate()
            proc.wait(timeout=5)


def test_status_reports_meaningful_state():
    """tela status --json should report meaningful state when gateway is running.

    Per docs/INTERFACES.md: 'tela status' prints uptime, connected downstream
    servers, active connections, profile count.

    When called standalone (no running gateway), it is acceptable to show zeros,
    but profile_count should reflect loaded config if available.
    """
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


if __name__ == "__main__":
    import traceback

    tests = [
        test_stdio_ready_banner_not_on_stdout,
        test_stdio_responds_to_mcp_initialize,
        test_sse_mode_stays_alive,
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
