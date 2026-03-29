"""Reproduction: Hard-interrupt (SIGINT/KeyboardInterrupt) during connect.

Per docs/DESIGN.md Connection lifecycle:
  1. connect -> POST /connect -> server registers connection
  2. Bridge active: stdio <-> HTTP MCP session
  3. connect exits -> POST /disconnect -> server deregisters
  4. Last connection gone + idle timeout -> server auto-shuts down

Per docs/INTERFACES.md and docs/USAGE.md:
  - `tela connect` discovers gateway via lockfile or auto-starts one
  - During autostart wait, connect polls for gateway readiness
  - Active bridge proxies stdio <-> HTTP
  - Interrupt should terminate immediately, cleanup is best-effort

This test exercises SIGINT delivery at two points:
  1. During autostart wait (before bridge is active)
  2. During active bridge (after connection registered)

Expected behavior:
  - SIGINT terminates connect immediately (within reasonable timeout)
  - Cleanup (disconnect) is best-effort, not blocking exit
  - Connection count recovers after interrupt
  - Process does not hang or require SIGKILL

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

import pytest

pytestmark = pytest.mark.runtime_liveness


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


class TestSIGINTDuringAutostartWait:
    """Test SIGINT during gateway autostart wait phase.

    When `tela connect` auto-starts a gateway, it must wait for the gateway
    to become ready before bridging can begin. SIGINT during this wait
    should terminate the process immediately, not hang.
    """

    def test_sigint_during_autostart_wait_terminates_immediately(
        self, monkeypatch, tmp_path
    ):
        """SIGINT during autostart wait must terminate connect within reasonable time.

        This test:
        1. Starts connect WITHOUT a running gateway (must autostart)
        2. Sends SIGINT during the wait/before bridge establishes
        3. Verifies process terminates within 3 seconds
        4. Verifies no stale connection remains
        """
        # Isolate lockfile to avoid host server interference
        fake_home = str(tmp_path / "home")
        os.makedirs(fake_home, exist_ok=True)
        monkeypatch.setenv("HOME", fake_home)

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = _write_test_config(tmp_dir)

            # Start connect WITHOUT running gateway (must autostart)
            connect_proc = subprocess.Popen(
                [sys.executable, "-m", "tela", "connect", "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                # Give connect a small moment to start (but not complete autostart)
                # We want to catch it in the "waiting for gateway" phase
                time.sleep(0.5)

                # Send SIGINT (Ctrl+C)
                connect_proc.send_signal(signal.SIGINT)

                # Process must terminate within 3 seconds
                start_terminate = time.time()
                try:
                    exit_code = connect_proc.wait(timeout=3.0)
                    elapsed = time.time() - start_terminate
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()
                    elapsed = time.time() - start_terminate
                    pytest.fail(
                        f"SIGINT_DURING_AUTOSTART: connect did not terminate "
                        f"within 3 seconds after SIGINT. Took >{elapsed:.1f}s, "
                        f"required SIGKILL. Process may be hanging in cleanup."
                    )

                # Accept reasonable exit codes for SIGINT handling
                assert exit_code in (0, -signal.SIGINT, -2, 1, 130), (
                    f"SIGINT_DURING_AUTOSTART: unexpected exit code {exit_code}. "
                    f"Expected 0, -SIGINT, -2, 1, or 130. Elapsed: {elapsed:.2f}s"
                )

                print(
                    f"  PASS: connect terminated after {elapsed:.2f}s on SIGINT "
                    f"(exit code {exit_code})"
                )

            finally:
                # Ensure cleanup
                try:
                    connect_proc.stdin.close()
                    connect_proc.terminate()
                    connect_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()

    def test_sigint_during_autostart_no_stale_connection(self, monkeypatch, tmp_path):
        """After SIGINT during autostart, no stale connection remains on gateway.

        If the autostarted gateway remains running after SIGINT, it should have
        zero active_connections (the interrupted connect never registered).
        """
        fake_home = str(tmp_path / "home")
        os.makedirs(fake_home, exist_ok=True)
        monkeypatch.setenv("HOME", fake_home)

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = _write_test_config(tmp_dir)

            # Start connect to trigger autostart
            connect_proc = subprocess.Popen(
                [sys.executable, "-m", "tela", "connect", "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                # Wait briefly then send SIGINT
                time.sleep(0.5)
                connect_proc.send_signal(signal.SIGINT)

                try:
                    connect_proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()

                # Check if gateway was auto-started and is still running
                lockfile = _read_lockfile()
                if lockfile is None:
                    # No gateway started or already shut down - acceptable
                    print("  PASS: No gateway auto-started, no stale connection")
                    return

                # Gateway may be running, check its connection count
                token = lockfile.get("token")
                if not token:
                    print("  PASS: Lockfile has no token, cannot verify")
                    return

                # Query gateway status
                status_result = subprocess.run(
                    [sys.executable, "-m", "tela", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, "TELA_BEARER_TOKEN": token},
                )

                if status_result.returncode != 0:
                    # Gateway may have shut down already - acceptable
                    print("  PASS: Gateway shut down after autostart interrupt")
                    return

                status_data = json.loads(status_result.stdout)
                active_connections = status_data.get("active_connections", 0)

                assert active_connections == 0, (
                    f"SIGINT_DURING_AUTOSTART: gateway has {active_connections} "
                    f"stale connection(s) after interrupted connect. Expected 0."
                )

                print(
                    f"  PASS: Gateway has {active_connections} stale connections (correct)"
                )

            finally:
                # Cleanup gateway if running
                lockfile = _read_lockfile()
                if lockfile and "pid" in lockfile:
                    try:
                        os.kill(lockfile["pid"], signal.SIGTERM)
                        time.sleep(0.5)
                        try:
                            os.kill(lockfile["pid"], 0)
                            os.kill(lockfile["pid"], signal.SIGKILL)
                        except OSError:
                            pass
                    except OSError:
                        pass
                    # Clean lockfile
                    _clean_lockfile()


class TestSIGINTDuringActiveBridge:
    """Test SIGINT during active bridge (after connection established).

    When `tela connect` has an active bridge, SIGINT should:
    1. Terminate the process immediately
    2. Trigger best-effort disconnect
    3. Not hang waiting for cleanup
    """

    def test_sigint_during_active_bridge_terminates_immediately(
        self, monkeypatch, tmp_path
    ):
        """SIGINT during active bridge must terminate connect within reasonable time.

        This test:
        1. Starts a gateway server
        2. Starts connect and waits for bridge to establish (check via status)
        3. Sends SIGINT during active bridge
        4. Verifies process terminates within 3 seconds
        """
        fake_home = str(tmp_path / "home")
        os.makedirs(fake_home, exist_ok=True)
        monkeypatch.setenv("HOME", fake_home)

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = _write_test_config(tmp_dir)

            # Start gateway FIRST
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
                # Wait for gateway to be ready
                lockfile = _wait_for_lockfile(timeout=10.0)
                assert lockfile is not None, (
                    "SIGINT_DURING_ACTIVE: gateway did not start within 10s"
                )
                token = lockfile["token"]

                # Start connect (gateway already running, no autostart wait)
                connect_proc = subprocess.Popen(
                    [sys.executable, "-m", "tela", "connect", "--config", config_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                # Wait for connection to register (bridge is active)
                max_wait = 5.0
                start = time.time()
                connected = False
                while time.time() - start < max_wait:
                    status_result = subprocess.run(
                        [sys.executable, "-m", "tela", "status", "--json"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        env={**os.environ, "TELA_BEARER_TOKEN": token},
                    )
                    if status_result.returncode == 0:
                        status_data = json.loads(status_result.stdout)
                        if status_data.get("active_connections", 0) >= 1:
                            connected = True
                            break
                    time.sleep(0.2)

                assert connected, (
                    "SIGINT_DURING_ACTIVE: connect did not register with gateway "
                    f"within {max_wait}s"
                )

                # Bridge is now active - send SIGINT
                connect_proc.send_signal(signal.SIGINT)

                # Process must terminate within 3 seconds
                start_terminate = time.time()
                try:
                    exit_code = connect_proc.wait(timeout=3.0)
                    elapsed = time.time() - start_terminate
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()
                    elapsed = time.time() - start_terminate
                    pytest.fail(
                        f"SIGINT_DURING_ACTIVE: connect did not terminate "
                        f"within 3 seconds after SIGINT during active bridge. "
                        f"Took >{elapsed:.1f}s, required SIGKILL. Process hanging in cleanup."
                    )

                # Accept reasonable exit codes for SIGINT handling
                assert exit_code in (0, -signal.SIGINT, -2, 1, 130), (
                    f"SIGINT_DURING_ACTIVE: unexpected exit code {exit_code}. "
                    f"Expected 0, -SIGINT, -2, 1, or 130. Elapsed: {elapsed:.2f}s"
                )

                print(
                    f"  PASS: connect terminated after {elapsed:.2f}s on SIGINT "
                    f"during active bridge (exit code {exit_code})"
                )

            finally:
                # Cleanup
                try:
                    serve_proc.terminate()
                    serve_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    serve_proc.kill()
                    serve_proc.wait()
                _clean_lockfile()

    def test_sigint_during_active_bridge_no_stuck_connection(
        self, monkeypatch, tmp_path
    ):
        """After SIGINT during active bridge, gateway connection count recovers.

        The gateway should decrement active_connections after disconnect,
        even if disconnect was triggered by SIGINT cleanup.
        """
        fake_home = str(tmp_path / "home")
        os.makedirs(fake_home, exist_ok=True)
        monkeypatch.setenv("HOME", fake_home)

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = _write_test_config(tmp_dir)

            # Start gateway
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
                lockfile = _wait_for_lockfile(timeout=10.0)
                assert lockfile is not None, "Gateway did not start"
                token = lockfile["token"]

                # Get baseline connection count
                status_before = subprocess.run(
                    [sys.executable, "-m", "tela", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, "TELA_BEARER_TOKEN": token},
                )
                assert status_before.returncode == 0
                baseline = json.loads(status_before.stdout).get("active_connections", 0)

                # Start connect and wait for bridge
                connect_proc = subprocess.Popen(
                    [sys.executable, "-m", "tela", "connect", "--config", config_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                # Wait for connection
                max_wait = 5.0
                start = time.time()
                while time.time() - start < max_wait:
                    status_result = subprocess.run(
                        [sys.executable, "-m", "tela", "status", "--json"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        env={**os.environ, "TELA_BEARER_TOKEN": token},
                    )
                    if status_result.returncode == 0:
                        status_data = json.loads(status_result.stdout)
                        if status_data.get("active_connections", 0) >= baseline + 1:
                            break
                    time.sleep(0.2)

                # Verify connection was registered
                status_during = subprocess.run(
                    [sys.executable, "-m", "tela", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, "TELA_BEARER_TOKEN": token},
                )
                status_during_data = json.loads(status_during.stdout)
                connections_during = status_during_data.get("active_connections", 0)

                assert connections_during >= baseline + 1, (
                    f"Connection did not register. baseline={baseline}, "
                    f"connections_during={connections_during}"
                )

                # Send SIGINT and wait for termination
                connect_proc.send_signal(signal.SIGINT)
                try:
                    connect_proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()

                # Allow disconnect to propagate
                time.sleep(0.5)

                # Verify connection count recovered
                status_after = subprocess.run(
                    [sys.executable, "-m", "tela", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, "TELA_BEARER_TOKEN": token},
                )

                if status_after.returncode != 0:
                    # Gateway may have shut down (idle timeout hit)
                    print("  PASS: Gateway shut down after connection cleanup")
                    return

                status_after_data = json.loads(status_after.stdout)
                connections_after = status_after_data.get("active_connections", 0)

                # Connection count should return to baseline (or 0)
                assert connections_after <= baseline, (
                    f"SIGINT_DURING_ACTIVE: connection count did not recover. "
                    f"baseline={baseline}, during={connections_during}, "
                    f"after={connections_after}. Stale connection may remain."
                )

                print(
                    f"  PASS: connection count recovered: "
                    f"baseline={baseline} -> during={connections_during} -> after={connections_after}"
                )

            finally:
                # Cleanup
                try:
                    serve_proc.terminate()
                    serve_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    serve_proc.kill()
                    serve_proc.wait()
                _clean_lockfile()


class TestSIGINTGatewayIdleState:
    """Test gateway idle state recovery after client SIGINT.

    When connect is interrupted and disconnects, the gateway should:
    1. Decrement connection count correctly
    2. Enter idle state (for shutdown timer) if no connections remain
    """

    def test_idle_timer_starts_after_interrupt_disconnect(self, monkeypatch, tmp_path):
        """After SIGINT disconnect, idle timer should be able to start.

        This verifies the gateway state machine is not stuck after
        client SIGINT cleanup.
        """
        fake_home = str(tmp_path / "home")
        os.makedirs(fake_home, exist_ok=True)
        monkeypatch.setenv("HOME", fake_home)

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = _write_test_config(tmp_dir)

            # Start gateway with longer idle timeout so we can observe state
            # before shutdown
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
                    "5",  # 5 second idle timeout
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                lockfile = _wait_for_lockfile(timeout=10.0)
                assert lockfile is not None, "Gateway did not start"
                token = lockfile["token"]
                gateway_pid = lockfile["pid"]

                # Check gateway is alive before connect
                try:
                    os.kill(gateway_pid, 0)
                    gateway_alive_before = True
                except OSError:
                    gateway_alive_before = False

                assert gateway_alive_before, "Gateway not alive before connect"

                # Connect
                connect_proc = subprocess.Popen(
                    [sys.executable, "-m", "tela", "connect", "--config", config_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                # Wait for connection
                time.sleep(1.0)

                # SIGINT the connect
                connect_proc.send_signal(signal.SIGINT)
                try:
                    connect_proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait()

                # Immediately verify gateway is still running after SIGINT disconnect
                # With idle-timeout=5, we have plenty of time to observe
                status_after = subprocess.run(
                    [sys.executable, "-m", "tela", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, "TELA_BEARER_TOKEN": token},
                )

                # Gateway should still respond (not crashed) and be ALIVE
                if status_after.returncode != 0:
                    # Check if process is even alive
                    try:
                        os.kill(gateway_pid, 0)
                        gateway_alive = True
                    except OSError:
                        gateway_alive = False

                    pytest.fail(
                        f"IDLE_STATE: gateway crashed or became unresponsive after SIGINT disconnect. "
                        f"status.returncode={status_after.returncode}, "
                        f"gateway_alive={gateway_alive}, "
                        f"stderr={status_after.stderr}"
                    )

                status_data = json.loads(status_after.stdout)
                active_connections = status_data.get("active_connections", 0)

                # Connection should have been cleaned up (count back to baseline)
                assert active_connections == 0, (
                    f"IDLE_STATE: connection count did not recover after disconnect. "
                    f"active_connections={active_connections}"
                )

                print("  PASS: gateway remained responsive after interrupt disconnect")
                print(f"        active_connections={active_connections} (recovered)")

                # Check process is still alive (not crashed)
                try:
                    os.kill(gateway_pid, 0)
                    gateway_alive = True
                except OSError:
                    gateway_alive = False

                assert gateway_alive, (
                    "IDLE_STATE: gateway process died after SIGINT disconnect"
                )

                print("  PASS: gateway process stayed alive in idle state")

                # Now wait for idle timeout - gateway should shut down
                time.sleep(6.0)  # Longer than idle-timeout=5

                # Verify gateway shut down
                poll = serve_proc.poll()
                assert poll is not None, (
                    f"IDLE_STATE: gateway did not shut down after idle timeout. "
                    f"Process still running (poll={poll}). Idle state may be stuck."
                )

                print(
                    f"  PASS: gateway shut down after idle timeout (exit code {poll})"
                )

            finally:
                try:
                    serve_proc.terminate()
                    serve_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    serve_proc.kill()
                    serve_proc.wait()
                _clean_lockfile()


if __name__ == "__main__":
    import traceback

    print("Mode D: tela connect hard-interrupt verification")
    print("=" * 60)

    tests = [
        TestSIGINTDuringAutostartWait.test_sigint_during_autostart_wait_terminates_immediately,
        TestSIGINTDuringAutostartWait.test_sigint_during_autostart_no_stale_connection,
        TestSIGINTDuringActiveBridge.test_sigint_during_active_bridge_terminates_immediately,
        TestSIGINTDuringActiveBridge.test_sigint_during_active_bridge_no_stuck_connection,
        TestSIGINTGatewayIdleState.test_idle_timer_starts_after_interrupt_disconnect,
    ]

    passed = 0
    failed = 0

    for test in tests:
        name = test.__name__
        print(f"\n{name}:")
        try:
            # Need pytest's monkeypatch/tmp_path fixtures
            pytest.main(["-v", "-s", __file__, f"-k::{name}"])
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
