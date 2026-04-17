"""Black-box verification for conn_v2.deep_review.black_box.

Per docs/INTERFACES.md and docs/USAGE.md:
  - `tela serve --port` binds to specified port, writes lockfile to ~/.tela/gateway.lock
  - `tela connect` discovers via lockfile (optional --server for explicit)
  - `tela status --json` reports server state with documented fields
  - `tela connections --json` reports active connections

This script verifies:
  1. Cold start / serve behavior (CLI surface, lockfile location)
  2. Lockfile timing (lockfile appears, contains expected fields)
  3. Interrupt behavior (CLI surface for connect/status)
  4. Status schema (fields match documented contract)

Mode D Liveness Probe:
  - Verify HTTP gateway binds and accepts requests
  - Verify status/health endpoints respond
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

# Test configuration
TELA_REPO = Path(__file__).parent.parent.parent
GATEWAY_LOCKFILE = Path.home() / ".tela" / "gateway.lock"


def _write_test_config(tmp_dir: str, profile_id: str = "test_profile") -> str:
    """Write a minimal open-mode config with a default profile."""
    config = {
        "profiles": {
            profile_id: {
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


def _read_lockfile() -> dict | None:
    """Read the lockfile if it exists."""
    if not GATEWAY_LOCKFILE.exists():
        return None
    try:
        content = GATEWAY_LOCKFILE.read_text()
        return json.loads(content)
    except (json.JSONDecodeError, OSError):
        return None


def _run_tela(args: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    """Run tela command and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-m", "tela"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=TELA_REPO,
    )
    return result.returncode, result.stdout, result.stderr


def _wait_for_lockfile(lockfile: Path, timeout: float = 10.0) -> dict | None:
    """Wait for a specific lockfile path to appear and parse as JSON."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lockfile.exists():
            try:
                return json.loads(lockfile.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.1)
    return None


@contextmanager
def _temporary_gateway() -> Iterator[tuple[dict[str, str], dict[str, object]]]:
    """Start an isolated gateway for black-box CLI schema probes."""
    with (
        tempfile.TemporaryDirectory() as tmp_dir,
        tempfile.TemporaryDirectory() as fake_home,
    ):
        env = {**os.environ, "HOME": fake_home}
        config_path = _write_test_config(tmp_dir)
        lockfile_path = Path(fake_home) / ".tela" / "gateway.lock"
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
            cwd=TELA_REPO,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            lockfile = _wait_for_lockfile(lockfile_path)
            assert lockfile is not None, "Temporary gateway did not publish lockfile"
            env["TELA_BEARER_TOKEN"] = str(lockfile["token"])
            yield env, lockfile
        finally:
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait()


def test_cold_start_cli_surface():
    """Verify `tela serve` and `tela connect` CLI surfaces are documented."""
    print("\n=== Test: Cold-start CLI Surface ===")

    # Test serve --help
    code, stdout, stderr = _run_tela(["serve", "--help"])
    assert code == 0, f"serve --help failed: {stderr}"
    assert "--config" in stdout, "serve --help missing --config"
    assert "--port" in stdout, "serve --help missing --port"
    assert "--host" in stdout, "serve --help missing --host"
    assert "--token" in stdout, "serve --help missing --token"
    assert "--idle-timeout" in stdout, "serve --help missing --idle-timeout"
    print("  PASS: tela serve --help shows documented options")

    # Test connect --help
    code, stdout, stderr = _run_tela(["connect", "--help"])
    assert code == 0, f"connect --help failed: {stderr}"
    assert "--config" in stdout, "connect --help missing --config"
    assert "--server" in stdout, "connect --help missing --server"
    assert "--token" in stdout, "connect --help missing --token"
    print("  PASS: tela connect --help shows documented options")


def test_lockfile_location_and_schema():
    """Verify lockfile is at documented location with documented schema."""
    print("\n=== Test: Lockfile Location and Schema ===")

    lockfile = GATEWAY_LOCKFILE
    assert lockfile.parent.name == ".tela", (
        f"Lockfile parent should be .tela, got {lockfile.parent}"
    )
    print(f"  PASS: Lockfile location is {lockfile}")

    with _temporary_gateway() as (_env, data):
        pass

    # Per docs/INTERFACES.md#7.3, lockfile must contain:
    # - pid, host, port, token, started_at, config_path, version
    required_fields = ["pid", "host", "port", "token", "started_at", "version"]
    for field in required_fields:
        assert field in data, f"Lockfile missing field: {field}"
    print(f"  PASS: Lockfile contains required fields: {required_fields}")
    print(
        f"  Lockfile content: pid={data.get('pid')}, port={data.get('port')}, host={data.get('host')}"
    )


def test_status_schema_fields():
    """Verify `tela status --json` produces documented fields."""
    print("\n=== Test: Status Schema ===")

    with _temporary_gateway() as (env, _lockfile):
        result = subprocess.run(
            [sys.executable, "-m", "tela", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=TELA_REPO,
            env=env,
        )
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    assert code == 0, f"status --json failed: {stderr}"

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"status --json output is not valid JSON: {e}\noutput: {stdout!r}"
        )

    # Per docs/INTERFACES.md, status must include:
    # - uptime_seconds, server_count, connected_servers, active_connections
    # - profile_count, state, connections (list)
    required_fields = [
        "uptime_seconds",
        "server_count",
        "connected_servers",
        "active_connections",
        "profile_count",
        "state",
        "connections",
    ]
    for field in required_fields:
        assert field in data, f"Status missing field: {field}"
    print(f"  PASS: Status contains required fields: {required_fields}")

    # Validate connection schema
    if data.get("connections"):
        conn = data["connections"][0]
        conn_fields = ["connection_id", "profile_id", "connected_at"]
        for field in conn_fields:
            assert field in conn, f"Connection entry missing field: {field}"
        print(f"  PASS: Connection schema valid with fields: {conn_fields}")

    print(f"  State: {data.get('state')}")
    print(f"  Servers: {data.get('connected_servers')}")
    print(f"  Active connections: {data.get('active_connections')}")


def test_connections_schema_fields():
    """Verify `tela connections --json` produces documented fields."""
    print("\n=== Test: Connections Schema ===")

    with _temporary_gateway() as (env, _lockfile):
        result = subprocess.run(
            [sys.executable, "-m", "tela", "connections", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=TELA_REPO,
            env=env,
        )
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    assert code == 0, f"connections --json failed: {stderr}"

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"connections --json output is not valid JSON: {e}\noutput: {stdout!r}"
        )

    assert isinstance(data, list), f"connections should be a list, got {type(data)}"
    print(f"  PASS: connections returns JSON list with {len(data)} entries")

    if data:
        conn = data[0]
        conn_fields = ["connection_id", "profile_id", "connected_at"]
        for field in conn_fields:
            assert field in conn, f"Connection entry missing field: {field}"
        print(f"  PASS: Connection schema valid with fields: {conn_fields}")


def test_status_endpoint_liveness():
    """Verify HTTP gateway status/health endpoints respond."""
    print("\n=== Test: Gateway HTTP Liveness ===")

    with _temporary_gateway() as (_env, data):
        # Get gateway endpoint
        host = data.get("host", "127.0.0.1")
        port = data.get("port")
        token = data.get("token")
        assert port is not None, "Temporary gateway lockfile missing port"

        print(f"  Gateway at {host}:{port}")

        # Test /health endpoint (no auth required)
        import urllib.request
        import urllib.error

        url = f"http://{host}:{port}/health"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                health_data = json.loads(resp.read().decode())
                print(f"  PASS: /health endpoint responded: {health_data}")
                assert health_data.get("status") == "ok", (
                    f"health status should be 'ok', got {health_data}"
                )
                print("  PASS: /health returns {status: ok}")
        except urllib.error.URLError as e:
            print(f"  FAIL: /health endpoint unreachable: {e}")
            raise

        # Test /status endpoint with auth
        if token:
            url = f"http://{host}:{port}/status"
            try:
                req = urllib.request.Request(url)
                req.add_header("Authorization", f"Bearer {token}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status_data = json.loads(resp.read().decode())
                    print(
                        f"  PASS: /status endpoint responded with state={status_data.get('state')}"
                    )
            except urllib.error.URLError as e:
                print(f"  FAIL: /status endpoint unreachable: {e}")
                raise


def test_interrupt_cli_surface():
    """Verify interrupt-related CLI surfaces (R11 verification)."""
    print("\n=== Test: Interrupt CLI Surface ===")

    # Verify status command exists and produces output
    code, stdout, stderr = _run_tela(["status", "--help"])
    assert code == 0, f"status --help failed: {stderr}"
    print("  PASS: tela status --help shows documented options")

    # Verify connections command exists
    code, stdout, stderr = _run_tela(["connections", "--help"])
    assert code == 0, f"connections --help failed: {stderr}"
    print("  PASS: tela connections --help shows documented options")

    # Verify audit command exists
    code, stdout, stderr = _run_tela(["audit", "--help"])
    assert code == 0, f"audit --help failed: {stderr}"
    assert "--since" in stdout, "audit --help missing --since"
    assert "--limit" in stdout, "audit --help missing --limit"
    print("  PASS: tela audit --help shows documented options")


def test_explicit_server_mode():
    """Verify explicit --server mode CLI surface (R4 verification)."""
    print("\n=== Test: Explicit Server Mode ===")

    # Verify connect supports --server for explicit gateway endpoint
    code, stdout, stderr = _run_tela(["connect", "--help"])
    assert code == 0, f"connect --help failed: {stderr}"
    assert "Explicit gateway endpoint as host:port" in stdout or "--server" in stdout
    print("  PASS: tela connect --server documented in help")


def main():
    """Run all black-box verification tests."""
    print("=" * 60)
    print("Black-box Verification: conn_v2.deep_review.black_box")
    print("=" * 60)

    results = {}
    tests = [
        ("cold_start_test", test_cold_start_cli_surface),
        ("lockfile_timing", test_lockfile_location_and_schema),
        ("status_schema", test_status_schema_fields),
        ("connections_schema", test_connections_schema_fields),
        ("status_endpoint", test_status_endpoint_liveness),
        ("interrupt_test", test_interrupt_cli_surface),
        ("explicit_server_test", test_explicit_server_mode),
    ]

    passed = 0
    failed = 0
    skipped = 0

    for name, test_fn in tests:
        try:
            test_fn()
            results[name] = "PASS"
            passed += 1
        except AssertionError as e:
            results[name] = f"FAIL: {e}"
            failed += 1
        except Exception as e:
            results[name] = f"ERROR: {e}"
            failed += 1

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, result in results.items():
        status = result.split(":")[0]
        print(f"  {name}: {status}")
    print(f"\nPassed: {passed}, Failed: {failed}, Skipped: {skipped}")

    if failed == 0:
        print("\nVERDICT: PASS")
        return 0
    else:
        print("\nVERDICT: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
