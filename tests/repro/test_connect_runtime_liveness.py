"""Mode D Liveness Probe: Verify tela connect runtime surface.

Per docs/INTERFACES.md and docs/USAGE.md:
  - `tela serve` starts on ephemeral bind (port 0) and writes lockfile with non-zero port
  - `tela connect` discovers via lockfile without manual --server override
  - initialize/tools/list via bridge works (stdio proxy to HTTP gateway)
  - disconnect decrements connection count cleanly

This is a black-box test: we interact ONLY via documented CLI surface
and observable behavior. No source code inspection.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _write_test_config(tmp_dir: str) -> str:
    """Write a minimal open-mode config for testing."""
    config = {
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
    import yaml

    path = os.path.join(tmp_dir, "tela.yaml")
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

            assert lockfile_data is not None, (
                "Mode D: tela serve did not create lockfile within 10 seconds"
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

            # Verify process is alive
            try:
                os.kill(lockfile_data["pid"], 0)
                alive = True
            except OSError:
                alive = False

            assert alive, (
                f"Mode D: Lockfile references dead process pid={lockfile_data['pid']}"
            )

            print(f"  PASS: serve bound to port {port} and wrote valid lockfile")

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_connect_discovers_via_lockfile():
    """tela connect must discover gateway via lockfile without --server override.

    Per docs/USAGE.md:
      - 'tela connect' auto-discovers running server via ~/.tela/gateway.lock
      - No --server needed for local gateway
    """
    _clean_lockfile()

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = _write_test_config(tmp_dir)

        # Start server first
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
            # Wait for lockfile
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

            # Give connect a moment to establish
            time.sleep(1.0)

            # Verify connect process is alive (not crashed)
            poll_result = connect_proc.poll()
            assert poll_result is None, (
                f"Mode D: connect exited immediately with code {poll_result}. "
                f"Expected auto-discovery via lockfile."
            )

            # Check status to verify connection registered
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

            assert active_connections >= 1, (
                f"Mode D: connect did not register with gateway. "
                f"active_connections={active_connections}, expected >= 1"
            )

            print(
                f"  PASS: connect discovered gateway via lockfile (host={host}, port={port})"
            )
            print(f"        gateway reports {active_connections} active connection(s)")

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


def test_disconnect_decrements_connection_count():
    """When connect disconnects, connection count must decrease.

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

            # Verify connection registered
            status_during = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TELA_BEARER_TOKEN": token},
            )

            status_data_during = json.loads(status_during.stdout)
            connections_during = status_data_during.get("active_connections", 0)

            assert connections_during > connections_before, (
                f"Mode D: connect did not increment active_connections. "
                f"before={connections_before}, during={connections_during}"
            )

            print(
                f"  PASS: active_connections incremented {connections_before} -> {connections_during}"
            )

            # Disconnect
            connect_proc.stdin.close()
            connect_proc.terminate()
            connect_proc.wait(timeout=5)

            time.sleep(0.5)  # Allow disconnect to propagate

            # Verify connection count decreased
            status_after = subprocess.run(
                [sys.executable, "-m", "tela", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TELA_BEARER_TOKEN": token},
            )

            status_data_after = json.loads(status_after.stdout)
            connections_after = status_data_after.get("active_connections", 0)

            assert connections_after < connections_during, (
                f"Mode D: disconnect did not decrement active_connections. "
                f"during={connections_during}, after={connections_after}"
            )

            print(
                f"  PASS: active_connections decremented {connections_during} -> {connections_after}"
            )

        finally:
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()

            _clean_lockfile()


if __name__ == "__main__":
    import traceback

    tests = [
        test_serve_ephemeral_bind_publishes_lockfile,
        test_connect_discovers_via_lockfile,
        test_bridge_handles_mcp_initialize_and_tools_list,
        test_disconnect_decrements_connection_count,
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
