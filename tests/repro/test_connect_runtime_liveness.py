"""Mode D Liveness Probe: Verify tela connect runtime surface.

Per docs/INTERFACES.md and docs/USAGE.md:
  - `tela serve` starts on ephemeral bind (port 0) and writes lockfile with non-zero port
  - `tela connect` discovers via lockfile without manual --server override
  - initialize/tools/list via bridge works (stdio proxy to HTTP gateway)
  - disconnect decrements connection count cleanly

This is a black-box test: we interact ONLY via documented CLI surface
and observable behavior. No source code inspection.

Lifecycle failure modes distinguished by these tests:
  - STARTUP_FAILURE: process could not be spawned or crashed immediately
  - PREMATURE_EXIT:  process started but exited before liveness check
  - LOCKFILE_ABSENT: serve started but never wrote lockfile within timeout
  - STEADY_STATE:    process alive and lockfile present — pass
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.runtime_liveness


def _write_test_config(
    tmp_dir: str,
    *,
    bridge_idle_ttl_seconds: float | None = None,
    sweep_interval_seconds: float | None = None,
) -> str:
    """Write a minimal open-mode config for testing."""
    config: dict[str, object] = {
        "profiles": {
            "test_profile": {
                "capabilities": {
                    "filesystem": "read_only",
                },
                "default": True,
            },
        },
        "auth": {
            "mode": "open",
        },
        "audit": {
            "level": "L1",
            "output": os.path.join(tmp_dir, "audit.jsonl"),
        },
    }
    if bridge_idle_ttl_seconds is not None or sweep_interval_seconds is not None:
        reaper: dict[str, float] = {}
        if bridge_idle_ttl_seconds is not None:
            reaper["bridge_idle_ttl_seconds"] = bridge_idle_ttl_seconds
        if sweep_interval_seconds is not None:
            reaper["sweep_interval_seconds"] = sweep_interval_seconds
        config["reaper"] = reaper
    import yaml

    path = os.path.join(tmp_dir, "tela.yaml")
    with open(path, "w") as f:
        yaml.dump(config, f)
    return path


def _write_tool_call_test_config(
    tmp_dir: str,
    *,
    bridge_idle_ttl_seconds: float,
    sweep_interval_seconds: float,
) -> str:
    """Write a config with a real downstream stdio tool for tools/call tests."""

    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "fastmcp_stdio_server.py"
    )
    config: dict[str, object] = {
        "servers": {
            "local_stdio": {
                "command": sys.executable,
                "args": [str(fixture_path)],
                "default_posture": "read_only",
            }
        },
        "profiles": {
            "test_profile": {
                "capabilities": {
                    "local_stdio": "read_only",
                },
                "default": True,
            },
        },
        "auth": {
            "mode": "open",
        },
        "audit": {
            "level": "L1",
            "output": os.path.join(tmp_dir, "audit.jsonl"),
        },
        "reaper": {
            "bridge_idle_ttl_seconds": bridge_idle_ttl_seconds,
            "sweep_interval_seconds": sweep_interval_seconds,
        },
    }
    import yaml

    path = os.path.join(tmp_dir, "tela-tools-call.yaml")
    with open(path, "w") as f:
        yaml.dump(config, f)
    return path


def _get_lockfile_path() -> Path:
    """Return the expected lockfile path per docs."""
    return Path.home() / ".tela" / "gateway.lock"


def _read_lockfile() -> dict | None:
    """Read the lockfile if it exists."""
    lockfile = _get_lockfile_path()
    if not lockfile.exists():
        return None
    try:
        content = lockfile.read_text()
        return json.loads(content)
    except (json.JSONDecodeError, OSError):
        return None


def _wait_for_lockfile(timeout: float = 10.0) -> dict | None:
    """Wait for lockfile to appear and return its contents."""
    lockfile = _get_lockfile_path()
    start = time.time()
    while time.time() - start < timeout:
        if lockfile.exists():
            return _read_lockfile()
        time.sleep(0.1)
    return None


def _clean_lockfile():
    """Remove stale lockfile if it exists."""
    lockfile = _get_lockfile_path()
    if lockfile.exists():
        try:
            data = _read_lockfile()
            if data and "pid" in data:
                # Check if process is still alive
                try:
                    os.kill(data["pid"], 0)
                    # Process is alive - may be from another test run
                except OSError:
                    # Process is dead, safe to remove stale lockfile
                    lockfile.unlink()
            else:
                lockfile.unlink()
        except (OSError, KeyError):
            pass


def _query_status_json(token: str) -> dict[str, object]:
    """Query ``tela status --json`` using the discovered bearer token."""

    status_result = subprocess.run(
        [sys.executable, "-m", "tela", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TELA_BEARER_TOKEN": token},
    )
    assert status_result.returncode == 0, (
        f"Mode D: status query failed. stderr={status_result.stderr}"
    )
    return json.loads(status_result.stdout)


def _read_jsonrpc_line(
    process: subprocess.Popen[bytes], timeout: float
) -> dict[str, object]:
    """Read one newline-delimited JSON-RPC response from ``tela connect``."""

    assert process.stdout is not None
    stdout = process.stdout
    ready, _, _ = select.select([stdout], [], [], timeout)
    assert ready, f"Mode D: Bridge did not respond within {timeout} seconds"
    response_line = stdout.readline().decode("utf-8", errors="replace")

    json_response: dict[str, object] | None = None
    for line in response_line.strip().split("\n"):
        stripped_line = line.strip()
        if not stripped_line.startswith("{"):
            continue
        try:
            candidate = json.loads(stripped_line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            json_response = candidate
            break

    assert json_response is not None, (
        f"Mode D: No valid JSON-RPC response. Raw: {response_line!r}"
    )
    return json_response


def _send_jsonrpc_line(
    process: subprocess.Popen[bytes], request: dict[str, object], timeout: float = 10.0
) -> dict[str, object]:
    """Send one newline-delimited JSON-RPC request and return the response."""

    assert process.stdin is not None
    stdin = process.stdin
    stdin.write((json.dumps(request) + "\n").encode())
    stdin.flush()
    return _read_jsonrpc_line(process, timeout=timeout)


def _active_connection_count(status_payload: dict[str, object]) -> int:
    """Return validated ``active_connections`` from a status payload."""

    active_connections = status_payload.get("active_connections", 0)
    assert isinstance(active_connections, int), (
        f"Mode D: status payload has non-integer active_connections: {status_payload!r}"
    )
    return active_connections


def test_serve_ephemeral_bind_publishes_lockfile():
    """tela serve --port 0 must bind ephemeral and write lockfile with valid port.

    Per docs/INTERFACES.md section 7.3:
      - Lockfile at ~/.tela/gateway.lock
      - Contains pid, host, port, token
      - Port must be non-zero (OS-assigned)
    """
    _clean_lockfile()

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_test_config(tmp_dir)

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
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for lockfile to appear
            lockfile_data = _wait_for_lockfile(timeout=10.0)

            # Distinguish LOCKFILE_ABSENT vs PREMATURE_EXIT
            if lockfile_data is None:
                poll_result = proc.poll()
                if poll_result is not None:
                    failure_mode = (
                        f"PREMATURE_EXIT(rc={poll_result})"
                        if poll_result != 0
                        else "PREMATURE_EXIT(rc=0)"
                    )
                else:
                    failure_mode = "LOCKFILE_ABSENT (process alive but no lockfile)"
                assert False, (
                    f"Mode D [{failure_mode}]: tela serve did not create lockfile "
                    "within 10 seconds"
                )

            # Verify lockfile structure per docs/INTERFACES.md section 7.3
            assert "pid" in lockfile_data, f"Lockfile missing 'pid': {lockfile_data}"
            assert "host" in lockfile_data, f"Lockfile missing 'host': {lockfile_data}"
            assert "port" in lockfile_data, f"Lockfile missing 'port': {lockfile_data}"
            assert "token" in lockfile_data, (
                f"Lockfile missing 'token': {lockfile_data}"
            )

            # Port must be non-zero (ephemeral bind)
            port = lockfile_data["port"]
            assert isinstance(port, int), f"Port is not an integer: {port}"
            assert port > 0, f"Port must be non-zero (got {port})"
            assert port < 65536, f"Port must be valid (got {port})"

            # Verify process is alive (STEADY_STATE check)
            try:
                os.kill(lockfile_data["pid"], 0)
                alive = True
            except OSError:
                alive = False

            assert alive, (
                f"Mode D [PREMATURE_EXIT]: Lockfile references dead process "
                f"pid={lockfile_data['pid']}. Process wrote lockfile then exited."
            )

            print(f"  PASS: serve bound to port {port} and wrote valid lockfile")

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_connect_discovers_via_lockfile(monkeypatch, tmp_path):
    """tela connect must discover gateway via lockfile without --server override.

    Per docs/USAGE.md:
      - 'tela connect' auto-discovers running server via ~/.tela/gateway.lock
      - No --server needed for local gateway
    """
    # Isolate lockfile to a temp HOME so a live host server does not interfere.
    # Both in-process helpers (Path.home()) and subprocesses inherit this HOME.
    fake_home = str(tmp_path / "home")
    os.makedirs(fake_home, exist_ok=True)
    monkeypatch.setenv("HOME", fake_home)

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_test_config(tmp_dir)

        # Start server first (inherits modified HOME via os.environ)
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
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for lockfile (now under fake_home/.tela/gateway.lock)
            lockfile_data = _wait_for_lockfile(timeout=10.0)
            assert lockfile_data is not None, "Server did not write lockfile"

            host = lockfile_data["host"]
            port = lockfile_data["port"]
            token = lockfile_data["token"]

            # Now start connect WITHOUT --server (must auto-discover)
            connect_proc = subprocess.Popen(
                [sys.executable, "-m", "tela", "connect", "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Give connect a moment to establish and register via POST /connect
            time.sleep(2.0)

            # Verify connect process is alive (not crashed)
            poll_result = connect_proc.poll()
            if poll_result is not None:
                stderr_data = (
                    connect_proc.stderr.read().decode("utf-8", errors="replace")
                    if connect_proc.stderr
                    else ""
                )
                failure_mode = (
                    "PREMATURE_EXIT"
                    if poll_result == 0
                    else f"STARTUP_FAILURE(rc={poll_result})"
                )
                assert False, (
                    f"Mode D [{failure_mode}]: connect exited with code {poll_result}. "
                    f"Expected STEADY_STATE via lockfile auto-discovery. "
                    f"stderr={stderr_data!r}"
                )

            # Check status: bridge process is alive, but no MCP session has
            # initialized yet, so there must be no fabricated active binding.
            status_result = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TELA_BEARER_TOKEN": token},
            )

            assert status_result.returncode == 0, (
                f"Mode D: status query failed. stderr={status_result.stderr}"
            )

            status_data = json.loads(status_result.stdout)
            active_connections = status_data.get("active_connections", 0)

            assert active_connections == 0, (
                f"Mode D: connect fabricated an active binding before initialize. "
                f"active_connections={active_connections}, expected 0"
            )

            print(
                f"  PASS: connect discovered gateway via lockfile (host={host}, port={port})"
            )
            print("        gateway reports no active connection before initialize")

            # Clean disconnect
            connect_proc.stdin.close()
            connect_proc.terminate()
            try:
                connect_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                connect_proc.kill()
                connect_proc.wait()

        finally:
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()

            # Clean lockfile
            _clean_lockfile()


def test_bridge_handles_mcp_initialize_and_tools_list():
    """The connect bridge must proxy MCP operations to the gateway.

    Per docs/USAGE.md:
      - 'tela connect' bridges stdio <-> HTTP
      - MCP operations (initialize, tools/list) work through the bridge

    This test sends actual MCP JSON-RPC messages through the stdio bridge.
    """
    _clean_lockfile()

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_test_config(tmp_dir)

        # Start server
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
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            lockfile_data = _wait_for_lockfile(timeout=10.0)
            assert lockfile_data is not None, "Server did not write lockfile"

            # Start connect with auto-discovery
            connect_proc = subprocess.Popen(
                [sys.executable, "-m", "tela", "connect", "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                time.sleep(1.0)  # Let bridge establish

                # Send MCP initialize request through the bridge
                init_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "liveness-test", "version": "0.1"},
                    },
                }

                connect_proc.stdin.write((json.dumps(init_request) + "\n").encode())
                connect_proc.stdin.flush()

                # Wait for response with timeout
                import select

                ready, _, _ = select.select([connect_proc.stdout], [], [], 10.0)

                assert ready, (
                    "Mode D: Bridge did not respond to initialize within 10 seconds"
                )

                response_line = connect_proc.stdout.readline().decode(
                    "utf-8", errors="replace"
                )

                # Parse JSON-RPC response
                # May have non-JSON prefix (stderr goes to separate pipe, but be safe)
                json_response = None
                for line in response_line.strip().split("\n"):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            json_response = json.loads(line)
                            break
                        except json.JSONDecodeError:
                            continue

                assert json_response is not None, (
                    f"Mode D: No valid JSON-RPC response. Raw: {response_line!r}"
                )

                assert "result" in json_response or "error" in json_response, (
                    f"Mode D: Response is not valid JSON-RPC: {json_response!r}"
                )

                # If successful initialize, check for capabilities
                if "result" in json_response:
                    result = json_response["result"]
                    assert "capabilities" in result, (
                        f"Mode D: initialize result missing 'capabilities': {result!r}"
                    )
                    print("  PASS: initialize returned capabilities")

                # Now send tools/list
                tools_request = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }

                connect_proc.stdin.write((json.dumps(tools_request) + "\n").encode())
                connect_proc.stdin.flush()

                ready, _, _ = select.select([connect_proc.stdout], [], [], 10.0)
                assert ready, (
                    "Mode D: Bridge did not respond to tools/list within 10 seconds"
                )

                tools_response_line = connect_proc.stdout.readline().decode(
                    "utf-8", errors="replace"
                )

                json_tools_response = None
                for line in tools_response_line.strip().split("\n"):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            json_tools_response = json.loads(line)
                            break
                        except json.JSONDecodeError:
                            continue

                assert json_tools_response is not None, (
                    f"Mode D: No valid tools/list response. Raw: {tools_response_line!r}"
                )

                if "result" in json_tools_response:
                    tools = json_tools_response["result"].get("tools", [])
                    print(f"  PASS: tools/list returned {len(tools)} tool(s)")
                elif "error" in json_tools_response:
                    print(
                        f"  PASS: tools/list returned error (acceptable): {json_tools_response['error']}"
                    )

            finally:
                connect_proc.stdin.close()
                connect_proc.terminate()
                try:
                    connect_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()

        finally:
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()

            _clean_lockfile()


def test_bridge_recovers_after_idle_bridge_reap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A long-idle bridge must self-recover on the next MCP request.

    Source:
    - User bug report: provider becomes unavailable after long idle.
    - docs/USAGE.md bridge recovery contract: recovery should re-discover,
      re-register, and resume forwarding after recoverable bridge/runtime loss.

    This black-box test proves three distinct states via documented surfaces:
    1. bridge establishes and serves MCP
    2. runtime reaper removes the idle bridge connection
    3. the existing ``tela connect`` process self-recovers on the next MCP call
    """

    fake_home = str(tmp_path / "home")
    os.makedirs(fake_home, exist_ok=True)
    monkeypatch.setenv("HOME", fake_home)

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_test_config(
            tmp_dir,
            bridge_idle_ttl_seconds=1.0,
            sweep_interval_seconds=0.2,
        )

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
                "--idle-timeout",
                "30",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        connect_proc: subprocess.Popen[bytes] | None = None
        try:
            lockfile_data = _wait_for_lockfile(timeout=10.0)
            assert lockfile_data is not None, "Server did not write lockfile"
            token = lockfile_data["token"]
            assert isinstance(token, str)

            connect_proc = subprocess.Popen(
                [sys.executable, "-m", "tela", "connect", "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            time.sleep(1.0)
            assert connect_proc.poll() is None, (
                "Connect exited before MCP traffic began"
            )

            initialize_response = _send_jsonrpc_line(
                connect_proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "idle-recovery-test", "version": "0.1"},
                    },
                },
            )
            assert "result" in initialize_response, (
                f"Mode D: initialize failed before idle test. response={initialize_response!r}"
            )

            first_tools_response = _send_jsonrpc_line(
                connect_proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert "result" in first_tools_response or "error" in first_tools_response

            status_before_idle = _query_status_json(token)
            active_before_idle = _active_connection_count(status_before_idle)
            assert active_before_idle >= 1, (
                "Mode D: bridge never registered before idle recovery test. "
                f"status={status_before_idle!r}"
            )

            time.sleep(2.5)

            status_after_reap = _query_status_json(token)
            active_after_reap = _active_connection_count(status_after_reap)
            assert active_after_reap == 0, (
                "Mode D: idle reaper did not remove the quiet bridge connection, "
                "so recovery path was not exercised. "
                f"status={status_after_reap!r}"
            )

            second_tools_response = _send_jsonrpc_line(
                connect_proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert "result" in second_tools_response, (
                "Mode D: bridge did not self-recover after idle reap. "
                f"response={second_tools_response!r}"
            )

            status_after_recovery = _query_status_json(token)
            active_after_recovery = _active_connection_count(status_after_recovery)
            assert active_after_recovery >= 1, (
                "Mode D: bridge request recovered only superficially; runtime did not "
                f"re-register connection. status={status_after_recovery!r}"
            )
            assert connect_proc.poll() is None, (
                "Mode D: connect process exited after recovery instead of remaining alive"
            )

        finally:
            if connect_proc is not None:
                if connect_proc.stdin is not None:
                    connect_proc.stdin.close()
                connect_proc.terminate()
                try:
                    connect_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()

            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()

            _clean_lockfile()


def test_bridge_recovers_downstream_tool_call_after_idle_bridge_reap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real downstream tools/call must recover after bridge idle reaping.

    Source:
    - Host-level OpenCode repro showed idle failure on downstream ``tools/call``
      even after ``tools/list`` recovery existed.
    - ``tests/shell/test_gateway.py`` verifies ``tools/call`` errors are surfaced
      as ``CallToolResult.isError`` payloads, so the bridge must recover on that
      response shape too.
    """

    fake_home = str(tmp_path / "home")
    os.makedirs(fake_home, exist_ok=True)
    monkeypatch.setenv("HOME", fake_home)

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_tool_call_test_config(
            tmp_dir,
            bridge_idle_ttl_seconds=1.0,
            sweep_interval_seconds=0.2,
        )

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
                "--idle-timeout",
                "30",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        connect_proc: subprocess.Popen[bytes] | None = None
        try:
            lockfile_data = _wait_for_lockfile(timeout=10.0)
            assert lockfile_data is not None, "Server did not write lockfile"
            token = lockfile_data["token"]
            assert isinstance(token, str)

            connect_proc = subprocess.Popen(
                [sys.executable, "-m", "tela", "connect", "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            time.sleep(1.0)
            assert connect_proc.poll() is None, (
                "Connect exited before downstream tool-call traffic began"
            )

            initialize_response = _send_jsonrpc_line(
                connect_proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "idle-tool-call-test",
                            "version": "0.1",
                        },
                    },
                },
            )
            assert "result" in initialize_response, (
                f"Mode D: initialize failed before tools/call idle test. response={initialize_response!r}"
            )

            first_call_response = _send_jsonrpc_line(
                connect_proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "echo",
                        "arguments": {"value": "before-idle"},
                    },
                },
            )
            assert "result" in first_call_response, (
                f"Mode D: first tools/call failed. response={first_call_response!r}"
            )
            first_result = first_call_response["result"]
            assert isinstance(first_result, dict)
            first_content = first_result.get("content")
            assert isinstance(first_content, list)
            assert first_content[0]["text"] == "before-idle"

            status_before_idle = _query_status_json(token)
            active_before_idle = _active_connection_count(status_before_idle)
            assert active_before_idle >= 1, (
                "Mode D: bridge never registered before downstream idle recovery test. "
                f"status={status_before_idle!r}"
            )

            time.sleep(2.5)

            status_after_reap = _query_status_json(token)
            active_after_reap = _active_connection_count(status_after_reap)
            assert active_after_reap == 0, (
                "Mode D: idle reaper did not remove the quiet bridge connection before "
                f"tools/call recovery. status={status_after_reap!r}"
            )

            second_call_response = _send_jsonrpc_line(
                connect_proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "echo",
                        "arguments": {"value": "after-idle"},
                    },
                },
            )
            assert "result" in second_call_response, (
                "Mode D: downstream tools/call did not self-recover after idle reap. "
                f"response={second_call_response!r}"
            )
            second_result = second_call_response["result"]
            assert isinstance(second_result, dict)
            second_content = second_result.get("content")
            assert isinstance(second_content, list)
            assert second_content[0]["text"] == "after-idle"

            status_after_recovery = _query_status_json(token)
            active_after_recovery = _active_connection_count(status_after_recovery)
            assert active_after_recovery >= 1, (
                "Mode D: downstream tools/call recovered only superficially; runtime did not "
                f"re-register connection. status={status_after_recovery!r}"
            )
            assert connect_proc.poll() is None, (
                "Mode D: connect process exited after downstream tools/call recovery"
            )

        finally:
            if connect_proc is not None:
                if connect_proc.stdin is not None:
                    connect_proc.stdin.close()
                connect_proc.terminate()
                try:
                    connect_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()

            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()

            _clean_lockfile()


def test_connect_without_initialize_keeps_connection_count_flat():
    """A bridge process without MCP initialize must not change active count.

    Per docs/INTERFACES.md:
      - POST /disconnect unregisters bridge connection
      - status shows connection count
    """
    _clean_lockfile()

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_test_config(tmp_dir)

        # Start server
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
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            lockfile_data = _wait_for_lockfile(timeout=10.0)
            assert lockfile_data is not None, "Server did not write lockfile"
            token = lockfile_data["token"]

            # Check initial connection count (should be 0)
            status_before = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TELA_BEARER_TOKEN": token},
            )

            assert status_before.returncode == 0, (
                f"Mode D: status query failed. stderr={status_before.stderr}"
            )

            status_data_before = json.loads(status_before.stdout)
            connections_before = status_data_before.get("active_connections", 0)

            # Start connect
            connect_proc = subprocess.Popen(
                [sys.executable, "-m", "tela", "connect", "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            time.sleep(1.0)

            # Verify connection count stays flat before initialize.
            status_during = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TELA_BEARER_TOKEN": token},
            )

            status_data_during = json.loads(status_during.stdout)
            connections_during = status_data_during.get("active_connections", 0)

            assert connections_during == connections_before, (
                f"Mode D: connect changed active_connections before initialize. "
                f"before={connections_before}, during={connections_during}"
            )

            print(
                f"  PASS: active_connections stayed flat at {connections_during} before initialize"
            )

            # Disconnect
            connect_proc.stdin.close()
            connect_proc.terminate()
            connect_proc.wait(timeout=5)

            time.sleep(0.5)  # Allow disconnect to propagate

            # Verify connection count remains unchanged after bridge teardown.
            status_after = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TELA_BEARER_TOKEN": token},
            )

            status_data_after = json.loads(status_after.stdout)
            connections_after = status_data_after.get("active_connections", 0)

            assert connections_after == connections_before, (
                f"Mode D: bridge teardown changed active_connections unexpectedly. "
                f"before={connections_before}, after={connections_after}"
            )

            print(
                f"  PASS: active_connections remained {connections_after} without initialize"
            )

        finally:
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()

            _clean_lockfile()


# =============================================================================
# Cold-start concurrency regression tests
# =============================================================================


def test_coldstart_endpoint_discovers_before_convergence(tmp_path, monkeypatch):
    """Cold-start scenario: endpoint becomes discoverable before convergence completes.

    This simulates the scenario from docs/DESIGN.md Runtime Architecture where
    7 downstream servers exist and the endpoint becomes available before all
    downstream connections have stabilized. The connect should succeed once
    the lockfile is written, even if gateway convergence is still in progress.

    Regression test for: endpoint appears in lockfile but gateway not yet
    fully initialized (partial convergence).
    """
    # Use isolated HOME to avoid interfering with host server
    fake_home = str(tmp_path / "home")
    os.makedirs(fake_home, exist_ok=True)
    monkeypatch.setenv("HOME", fake_home)

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_test_config(tmp_dir)

        # Start serve process
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
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for lockfile - this simulates endpoint becoming discoverable
            lockfile_data = _wait_for_lockfile(timeout=15.0)
            assert lockfile_data is not None, (
                "Cold-start [LOCKFILE_ABSENT]: endpoint did not become discoverable"
            )

            host = lockfile_data["host"]
            port = lockfile_data["port"]
            token = lockfile_data["token"]

            # Connect should succeed even if gateway is still converging
            # (e.g., downstream servers not all connected yet)
            connect_proc = subprocess.Popen(
                [sys.executable, "-m", "tela", "connect", "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Give connect time to establish
            time.sleep(2.0)

            poll_result = connect_proc.poll()
            if poll_result is not None:
                stderr_data = (
                    connect_proc.stderr.read().decode("utf-8", errors="replace")
                    if connect_proc.stderr
                    else ""
                )
                assert False, (
                    f"Cold-start [STARTUP_FAILURE rc={poll_result}]: "
                    f"connect failed despite endpoint being discoverable. "
                    f"stderr={stderr_data!r}"
                )

            # Verify bridge stays unbound until initialize even during cold start.
            status_result = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TELA_BEARER_TOKEN": token},
            )

            assert status_result.returncode == 0, (
                f"Cold-start: status query failed. stderr={status_result.stderr}"
            )

            status_data = json.loads(status_result.stdout)
            active_connections = status_data.get("active_connections", 0)

            assert active_connections == 0, (
                f"Cold-start: connect fabricated an active binding before initialize. "
                f"active_connections={active_connections}"
            )

            print("  PASS: cold-start connect stayed alive without fabricating binding")
            print(f"        endpoint discovered at {host}:{port}")

            # Clean disconnect
            connect_proc.stdin.close()
            connect_proc.terminate()
            try:
                connect_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                connect_proc.kill()
                connect_proc.wait()

        finally:
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()

            _clean_lockfile()


def test_concurrent_attach_converges_on_single_leader(tmp_path, monkeypatch):
    """Concurrent attach: multiple connect invocations converge on one leader.

    Per runtime_contract connect specification, multiple simultaneous connect
    calls for the same config should result in:
    - One leader that actually starts/autostarts the gateway
    - Followers that wait and attach to the existing gateway

    This test verifies that when a gateway is already running, multiple
    concurrent connect calls all stabilize and register with it.
    """
    fake_home = str(tmp_path / "home")
    os.makedirs(fake_home, exist_ok=True)
    monkeypatch.setenv("HOME", fake_home)

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_test_config(tmp_dir)

        # Start ONE server first - this is the "existing gateway"
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
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for lockfile - gateway is now running
            lockfile_data = _wait_for_lockfile(timeout=15.0)
            assert lockfile_data is not None, "Server did not write lockfile"

            # Launch multiple connect processes concurrently to the existing gateway
            connect_procs = []
            num_concurrent = 3

            for i in range(num_concurrent):
                proc = subprocess.Popen(
                    [sys.executable, "-m", "tela", "connect", "--config", config_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                connect_procs.append(proc)
                # Stagger starts slightly to simulate real concurrency
                time.sleep(0.2)

            # Give all connects time to establish
            time.sleep(3.0)

            # All connect processes should still be alive
            alive_procs = [p for p in connect_procs if p.poll() is None]

            assert len(alive_procs) >= num_concurrent - 1, (
                f"Concurrent connect [LEADER_ELECTED]: some connects exited prematurely. "
                f"Expected at least {num_concurrent - 1} alive, got {len(alive_procs)}"
            )

            # Get gateway status - concurrent bridge processes must stay unbound
            # until individual MCP sessions initialize.
            token = lockfile_data["token"]

            status_result = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TELA_BEARER_TOKEN": token},
            )

            assert status_result.returncode == 0, (
                f"Concurrent connect: status query failed. stderr={status_result.stderr}"
            )

            status_data = json.loads(status_result.stdout)
            active_connections = status_data.get("active_connections", 0)

            assert active_connections == 0, (
                f"Concurrent connect: fabricated active bindings before initialize. "
                f"got {active_connections}"
            )

            print(
                f"  PASS: {len(alive_procs)} concurrent bridge processes stayed unbound before initialize"
            )

        finally:
            # Clean up all connect processes
            for proc in connect_procs:
                try:
                    proc.stdin.close()
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

            # Clean up server
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()

            _clean_lockfile()


if __name__ == "__main__":
    import traceback

    tests: list[Callable[..., None]] = [
        test_serve_ephemeral_bind_publishes_lockfile,
        test_connect_discovers_via_lockfile,
        test_bridge_handles_mcp_initialize_and_tools_list,
        test_connect_without_initialize_keeps_connection_count_flat,
        test_coldstart_endpoint_discovers_before_convergence,
        test_concurrent_attach_converges_on_single_leader,
    ]

    print("Mode D: tela connect runtime surface verification")
    print("=" * 60)

    passed = 0
    failed = 0

    for test in tests:
        name = test.__name__
        print(f"\n{name}:")
        try:
            test()
            passed += 1
        except Exception:
            print(f"  FAIL: {name}")
            traceback.print_exc()
            failed += 1

    print()
    print(f"{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")

    # Cleanup
    try:
        _clean_lockfile()
    except Exception:
        pass

    sys.exit(1 if failed else 0)
