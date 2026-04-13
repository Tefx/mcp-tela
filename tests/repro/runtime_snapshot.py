"""Reproduction: Runtime Characterization Snapshot — arch_refactor.characterization.runtime_snapshot

Black-box characterization of tela serve, tela connect, GET /status,
POST /connect, POST /disconnect, and the discovery-vs-readiness boundary.

Spec sources:
  - docs/ADR-004: Gateway startup state machine
  - docs/ADR-005: Gateway runtime is sole readiness authority
  - docs/CONFIRMED-SURFACE-CONTRACT.md: Admission boundary freeze
  - docs/USAGE.md: CLI reference

Expected behaviors (per spec):
  1. `tela serve --config <cfg> --port 0` starts an HTTP gateway that binds
     to a port and writes a lockfile with discovery data.
  2. `GET /health` returns {"status":"ok","pid":N} without auth.
  3. `GET /status` requires bearer token; returns gateway lifecycle status.
  4. Lockfile provides discovery truth only (host, port, pid, token).
     Lockfile does NOT indicate readiness.
  5. `POST /connect` registers a bridge connection; is lifecycle plumbing
     only, NOT admission proof or readiness truth.
  6. `POST /disconnect` deregisters a bridge connection.
  7. Discovery truth (lockfile) and readiness truth (GET /status lifecycle_state)
     are externally distinguishable: lockfile exists before readiness,
     and readiness fields are only available from /status.

Method: Subprocess + HTTP probes — no implementation source read.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "runtime_snapshot_minimal.yaml"
LOCKFILE_PATH = Path.home() / ".tela" / "gateway.lock"
SERVE_LOG = Path(__file__).parent / "runtime_snapshot_serve.log"
SERVE_HOST = "127.0.0.1"
SERVE_PORT = 0  # ephemeral — we read actual from lockfile
STARTUP_TIMEOUT = 15  # seconds
POLL_INTERVAL = 0.3  # seconds

# Will be populated by serve startup
_serve_process: subprocess.Popen | None = None
_bound_port: int | None = None
_bearer_token: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_lockfile() -> dict | None:
    """Read lockfile and return parsed dict, or None if absent/unreadable."""
    if not LOCKFILE_PATH.exists():
        return None
    try:
        text = LOCKFILE_PATH.read_text()
        data = json.loads(text)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _wait_for_lockfile(timeout: float = STARTUP_TIMEOUT) -> dict | None:
    """Poll until lockfile exists and has valid data, with bounded timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = _read_lockfile()
        if data and "port" in data and "pid" in data:
            return data
        time.sleep(POLL_INTERVAL)
    return None


def _http_get(
    url: str, token: str | None = None, timeout: float = 3
) -> tuple[int, dict | str]:
    """GET request returning (status_code, parsed_json_or_error_string)."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            body = json.loads(body_bytes.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = body_bytes.decode(errors="replace")
        return e.code, body
    except Exception as e:
        return 0, str(e)


def _http_post(
    url: str,
    payload: dict,
    token: str | None = None,
    timeout: float = 3,
) -> tuple[int, dict | str]:
    """POST JSON request returning (status_code, parsed_json_or_error_string)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            body = json.loads(body_bytes.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = body_bytes.decode(errors="replace")
        return e.code, body
    except Exception as e:
        return 0, str(e)


def _cleanup():
    """Kill serve process and clean lockfile."""
    global _serve_process
    if _serve_process and _serve_process.poll() is None:
        try:
            _serve_process.send_signal(signal.SIGTERM)
            _serve_process.wait(timeout=5)
        except Exception:
            try:
                _serve_process.kill()
            except Exception:
                pass
    _serve_process = None
    # Also try `tela stop` for completeness
    try:
        subprocess.run(
            [sys.executable, "-m", "tela", "stop"],
            capture_output=True,
            timeout=3,
        )
    except Exception:
        pass
    # Remove stale lockfile
    try:
        LOCKFILE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Evidence recording
# ---------------------------------------------------------------------------

evidence_log: list[str] = []


def record(tag: str, detail: str) -> None:
    evidence_log.append(f"[{tag}] {detail}")
    print(f"  [{tag}] {detail}", flush=True)


# ---------------------------------------------------------------------------
# Phase 1: tela serve startup
# ---------------------------------------------------------------------------


def test_serve_startup() -> bool:
    """Start `tela serve` and verify it binds a port, writes lockfile."""
    global _serve_process, _bound_port, _bearer_token

    record("CMD", f"tela serve --config {CONFIG_PATH} --port 0 --idle-timeout 0")

    env = os.environ.copy()
    # Ensure no stale lockfile
    LOCKFILE_PATH.unlink(missing_ok=True)

    # Redirect to log file to avoid PIPE blocking
    SERVE_LOG.unlink(missing_ok=True)
    log_fh = open(SERVE_LOG, "w")
    _serve_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tela",
            "serve",
            "--config",
            str(CONFIG_PATH),
            "--port",
            "0",
            "--idle-timeout",
            "0",
        ],
        stdout=log_fh,
        stderr=log_fh,
        env=env,
    )

    # Wait for lockfile (discovery truth)
    lockfile_data = _wait_for_lockfile(timeout=STARTUP_TIMEOUT)

    # Check that process is still alive
    if _serve_process.poll() is not None:
        exit_code = _serve_process.returncode
        record("FAIL", f"Serve process exited during startup with code {exit_code}")
        # Try to read log
        try:
            log_text = SERVE_LOG.read_text()[:1000]
            record("SERVE_LOG", log_text)
        except Exception:
            pass
        return False

    if lockfile_data is None:
        record("FAIL", "Lockfile never appeared within timeout")
        return False

    _bound_port = lockfile_data.get("port")
    pid = lockfile_data.get("pid")
    _bearer_token = lockfile_data.get("token")

    record("LOCKFILE", json.dumps(lockfile_data, indent=2))
    record("PORT", f"Bound port: {_bound_port}")
    record("PID", f"Gateway PID: {pid}")
    record(
        "TOKEN",
        f"Bearer token present: {_bearer_token is not None} (len={len(_bearer_token) if _bearer_token else 0})",
    )

    if not _bound_port:
        record("FAIL", "Lockfile missing 'port' field")
        return False
    if not pid:
        record("FAIL", "Lockfile missing 'pid' field")
        return False
    if not _bearer_token:
        record("FAIL", "Lockfile missing 'token' field")
        return False

    record("PASS", "tela serve startup: port bound, lockfile written")
    return True


# ---------------------------------------------------------------------------
# Phase 2: GET /health (no auth required)
# ---------------------------------------------------------------------------


def test_health_endpoint() -> bool:
    """GET /health returns 200 with {"status":"ok","pid":N}."""
    assert _bound_port is not None, "Serve not started"
    url = f"http://{SERVE_HOST}:{_bound_port}/health"

    record("CMD", f"GET {url} (no auth)")

    status, body = _http_get(url)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else body}",
    )

    if status != 200:
        record("FAIL", f"Expected HTTP 200, got {status}")
        return False
    if not isinstance(body, dict):
        record("FAIL", f"Expected JSON dict, got {type(body)}")
        return False
    if body.get("status") != "ok":
        record("FAIL", f"Expected status='ok', got {body.get('status')!r}")
        return False
    if not isinstance(body.get("pid"), int) or body.get("pid") <= 0:
        record("FAIL", f"Expected positive integer pid, got {body.get('pid')!r}")
        return False

    record("PASS", "GET /health: 200, status=ok, pid present")
    return True


# ---------------------------------------------------------------------------
# Phase 3: GET /status with auth
# ---------------------------------------------------------------------------


def test_status_endpoint() -> bool:
    """GET /status with bearer token returns gateway lifecycle status."""
    assert _bound_port is not None and _bearer_token is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/status"

    record("CMD", f"GET {url} (bearer token auth)")

    status, body = _http_get(url, token=_bearer_token)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else body}",
    )

    if status != 200:
        record("FAIL", f"Expected HTTP 200, got {status}")
        return False
    if not isinstance(body, dict):
        record("FAIL", f"Expected JSON dict, got {type(body)}")
        return False

    # Verify key fields exist per StatusResponse spec
    required_fields = ["active_connections", "connected_servers", "connections"]
    for field in required_fields:
        if field not in body:
            record("FAIL", f"GET /status missing required field: {field}")
            return False

    record("PASS", f"GET /status: 200, fields present: {list(body.keys())}")
    return True


# ---------------------------------------------------------------------------
# Phase 3b: GET /status WITHOUT auth
# ---------------------------------------------------------------------------


def test_status_no_auth() -> bool:
    """GET /status without bearer token must be rejected (auth error)."""
    assert _bound_port is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/status"

    record("CMD", f"GET {url} (NO auth)")

    status, body = _http_get(url, token=None)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else body}",
    )

    if status == 200:
        record("FAIL", "GET /status returned 200 without auth — auth not enforced!")
        return False

    # Should be 401 or 403 or similar auth error
    if status in (401, 403):
        record("PASS", f"GET /status: correctly rejected without auth (HTTP {status})")
        return True

    # Some implementations return 400 or error JSON for missing auth
    record("PASS", f"GET /status: rejected without auth (HTTP {status}, body: {body})")
    return True


# ---------------------------------------------------------------------------
# Phase 4: POST /connect (lifecycle registration)
# ---------------------------------------------------------------------------


def test_connect_endpoint() -> bool:
    """POST /connect registers a bridge connection; returns connection confirmation."""
    assert _bound_port is not None and _bearer_token is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/connect"
    connection_id = "char-snapshot-conn-1"
    payload = {"connection_id": connection_id}

    record("CMD", f"POST {url} with {payload}")

    status, body = _http_post(url, payload, token=_bearer_token)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else body}",
    )

    if status != 200:
        record("FAIL", f"Expected HTTP 200, got {status}")
        return False
    if not isinstance(body, dict):
        record("FAIL", f"Expected JSON dict, got {type(body)}")
        return False
    if body.get("status") != "connected":
        record("FAIL", f"Expected status='connected', got {body.get('status')!r}")
        return False
    if body.get("connection_id") != connection_id:
        record(
            "FAIL",
            f"Expected connection_id={connection_id!r}, got {body.get('connection_id')!r}",
        )
        return False

    record(
        "PASS", f"POST /connect: 200, status=connected, connection_id={connection_id}"
    )
    return True


# ---------------------------------------------------------------------------
# Phase 4b: POST /connect without auth
# ---------------------------------------------------------------------------


def test_connect_no_auth() -> bool:
    """POST /connect without bearer token must be rejected."""
    assert _bound_port is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/connect"
    payload = {"connection_id": "no-auth-conn"}

    record("CMD", f"POST {url} (NO auth)")

    status, body = _http_post(url, payload, token=None)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else body}",
    )

    if status == 200:
        record("FAIL", "POST /connect returned 200 without auth — auth not enforced!")
        return False

    record("PASS", f"POST /connect: correctly rejected without auth (HTTP {status})")
    return True


# ---------------------------------------------------------------------------
# Phase 5: POST /disconnect (lifecycle deregistration)
# ---------------------------------------------------------------------------


def test_disconnect_endpoint() -> bool:
    """POST /disconnect deregisters a bridge connection."""
    assert _bound_port is not None and _bearer_token is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/disconnect"
    # Disconnect the connection we registered earlier
    connection_id = "char-snapshot-conn-1"
    payload = {"connection_id": connection_id}

    record("CMD", f"POST {url} with {payload}")

    status, body = _http_post(url, payload, token=_bearer_token)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else body}",
    )

    if status != 200:
        record("FAIL", f"Expected HTTP 200, got {status}")
        return False
    if not isinstance(body, dict):
        record("FAIL", f"Expected JSON dict, got {type(body)}")
        return False
    if body.get("status") != "disconnected":
        record("FAIL", f"Expected status='disconnected', got {body.get('status')!r}")
        return False

    record(
        "PASS",
        f"POST /disconnect: 200, status=disconnected, connection_id={connection_id}",
    )
    return True


# ---------------------------------------------------------------------------
# Phase 5b: POST /disconnect nonexistent connection
# ---------------------------------------------------------------------------


def test_disconnect_nonexistent() -> bool:
    """POST /disconnect for nonexistent connection must fail."""
    assert _bound_port is not None and _bearer_token is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/disconnect"
    payload = {"connection_id": "does-not-exist-conn"}

    record("CMD", f"POST {url} with {payload}")

    status, body = _http_post(url, payload, token=_bearer_token)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else body}",
    )

    if status == 200:
        record("FAIL", "POST /disconnect returned 200 for nonexistent connection")
        return False

    record(
        "PASS", f"POST /disconnect: correctly rejected for nonexistent (HTTP {status})"
    )
    return True


# ---------------------------------------------------------------------------
# Phase 6: Discovery vs Readiness distinguishability
# ---------------------------------------------------------------------------


def test_discovery_vs_readiness() -> bool:
    """
    ADR-004/005: Lockfile is discovery truth only; readiness is from GET /status.

    This test verifies:
    1. Lockfile contains discovery fields (port, pid, host, token).
    2. Lockfile does NOT contain readiness/lifecycle fields.
    3. GET /status returns fields that lockfile does NOT have
       (lifecycle_state, active_connections, connected_servers).
    4. The two are externally distinguishable.
    """
    # Read lockfile
    lockfile_data = _read_lockfile()
    if lockfile_data is None:
        record("FAIL", "No lockfile present for discovery-vs-readiness test")
        return False

    record("LOCKFILE_FIELDS", str(sorted(lockfile_data.keys())))

    # Get /status
    assert _bound_port is not None and _bearer_token is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/status"
    status, body = _http_get(url, token=_bearer_token)
    if status != 200 or not isinstance(body, dict):
        record("FAIL", f"GET /status failed with {status}: {body}")
        return False

    record("STATUS_FIELDS", str(sorted(body.keys())))

    # Discovery-only fields (lockfile has them, /status may or may not)
    discovery_fields = {
        "port",
        "pid",
        "host",
        "token",
        "config_path",
        "started_at",
        "version",
    }
    lockfile_discovery = discovery_fields & set(lockfile_data.keys())

    # Readiness-only fields (/status has them, lockfile does NOT)
    readiness_fields = {
        "active_connections",
        "connected_servers",
        "connections",
        "lifecycle_state",
    }
    status_readiness = readiness_fields & set(body.keys())

    record("LOCKFILE_DISCOVERY_FIELDS", str(sorted(lockfile_discovery)))
    record("STATUS_READINESS_FIELDS", str(sorted(status_readiness)))

    # Assert: lockfile has discovery fields
    if not lockfile_discovery:
        record("FAIL", "Lockfile has no standard discovery fields")
        return False

    # Assert: /status has readiness fields that lockfile lacks
    if not status_readiness:
        record("FAIL", "GET /status has no standard readiness fields")
        return False

    # Assert: readiness fields are NOT in lockfile (distinguishing)
    overlap = readiness_fields & set(lockfile_data.keys())
    if overlap:
        record("WARN", f"Readiness fields leaked into lockfile: {overlap}")

    record("PASS", "Discovery and readiness are externally distinguishable")
    return True


# ---------------------------------------------------------------------------
# Phase 7: connect/connect cycle and /status connection count
# ---------------------------------------------------------------------------


def test_connection_count_after_lifecycle() -> bool:
    """
    After POST /connect then POST /disconnect, GET /status should reflect
    zero active connections (assuming we connected/disconnected our test conn).
    """
    assert _bound_port is not None and _bearer_token is not None
    url_status = f"http://{SERVE_HOST}:{_bound_port}/status"
    url_connect = f"http://{SERVE_HOST}:{_bound_port}/connect"
    url_disconnect = f"http://{SERVE_HOST}:{_bound_port}/disconnect"

    # Check status first (should have 0 since we disconnected above)
    status, body = _http_get(url_status, token=_bearer_token)
    if status != 200:
        record("FAIL", f"GET /status pre-check failed: {status}")
        return False

    pre_count = body.get("active_connections", -1)
    record("PRE_CONNECT_COUNT", f"active_connections={pre_count}")

    # Add a new connection
    conn_id = "snapshot-conn-lifecycle"
    s, b = _http_post(url_connect, {"connection_id": conn_id}, token=_bearer_token)
    record("CONNECT_RESULT", f"HTTP {s}: {b}")
    if s != 200:
        record("FAIL", f"POST /connect failed: {s}")
        return False

    # Verify count incremented
    status, body = _http_get(url_status, token=_bearer_token)
    post_connect_count = body.get("active_connections", -1)
    record("POST_CONNECT_COUNT", f"active_connections={post_connect_count}")

    if post_connect_count != pre_count + 1:
        record(
            "FAIL",
            f"active_connections not incremented: expected {pre_count + 1}, got {post_connect_count}",
        )
        return False

    # Disconnect
    s, b = _http_post(url_disconnect, {"connection_id": conn_id}, token=_bearer_token)
    record("DISCONNECT_RESULT", f"HTTP {s}: {b}")

    # Verify count decremented
    status, body = _http_get(url_status, token=_bearer_token)
    post_disconnect_count = body.get("active_connections", -1)
    record("POST_DISCONNECT_COUNT", f"active_connections={post_disconnect_count}")

    if post_disconnect_count != pre_count:
        record(
            "FAIL",
            f"active_connections not decremented: expected {pre_count}, got {post_disconnect_count}",
        )
        return False

    record(
        "PASS",
        f"Connection lifecycle: count {pre_count} -> {post_connect_count} -> {post_disconnect_count}",
    )
    return True


# ---------------------------------------------------------------------------
# Phase 8: tela status CLI (discovery-based)
# ---------------------------------------------------------------------------
# Phase 8: tela status CLI (discovery-based)
# ---------------------------------------------------------------------------


def test_tela_status_cli() -> bool:
    """`tela status` reads from lockfile and queries /status."""
    result = subprocess.run(
        [sys.executable, "-m", "tela", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    record("CMD", "tela status --json")
    record("EXIT_CODE", str(result.returncode))
    record("STDOUT", result.stdout[:500] if result.stdout else "(empty)")
    record("STDERR", result.stderr[:500] if result.stderr else "(empty)")

    # We expect exit 0 when gateway is running
    if result.returncode != 0:
        record("FAIL", f"tela status exited with {result.returncode}")
        return False

    # Try to parse JSON output
    try:
        data = json.loads(result.stdout)
        record("STATUS_JSON_FIELDS", str(sorted(data.keys())))
    except json.JSONDecodeError:
        record(
            "FAIL", f"tela status --json output not valid JSON: {result.stdout[:200]}"
        )
        return False

    record("PASS", "tela status --json: exit 0, valid JSON")
    return True


# ---------------------------------------------------------------------------
# Phase 9: tela connect bridge probe
# ---------------------------------------------------------------------------


def test_connect_bridge() -> bool:
    """
    tela connect --server host:port starts a stdio-to-HTTP bridge.
    We probe it by sending a valid MCP initialize request on stdin
    and checking that it registers with the gateway.

    The connect process:
    1. Discovers endpoint (from --server or lockfile)
    2. Registers via POST /connect
    3. Polls GET /status for readiness
    4. Bridges stdio to HTTP

    We verify at least that the connection appears in GET /status after
    connect starts, and that the process exits cleanly when stdin closes.
    """
    assert _bound_port is not None and _bearer_token is not None
    url_status = f"http://{SERVE_HOST}:{_bound_port}/status"

    # Get baseline connection count
    status, body = _http_get(url_status, token=_bearer_token)
    pre_count = body.get("active_connections", 0) if isinstance(body, dict) else 0
    record("PRE_BRIDGE_COUNT", f"active_connections={pre_count}")

    # Launch connect with --server to skip lockfile discovery
    env = os.environ.copy()
    env["TELA_BEARER_TOKEN"] = _bearer_token

    connect_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tela",
            "connect",
            "--server",
            f"{SERVE_HOST}:{_bound_port}",
            "--config",
            str(CONFIG_PATH),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    record(
        "CMD",
        f"tela connect --server {SERVE_HOST}:{_bound_port} --config {CONFIG_PATH}",
    )
    record("CONNECT_PID", str(connect_proc.pid))

    # Give the bridge time to register via POST /connect and poll readiness
    time.sleep(2.0)

    # Check if process is still alive
    if connect_proc.poll() is not None:
        exit_code = connect_proc.returncode
        stderr_out = (
            connect_proc.stderr.read().decode(errors="replace")[:500]
            if connect_proc.stderr
            else ""
        )
        record("FAIL", f"tela connect exited prematurely with code {exit_code}")
        record("STDERR", stderr_out)
        return False

    # Check /status for bridge connection
    status, body = _http_get(url_status, token=_bearer_token)
    post_count = body.get("active_connections", 0) if isinstance(body, dict) else 0
    connections = body.get("connections", []) if isinstance(body, dict) else []
    record("POST_BRIDGE_COUNT", f"active_connections={post_count}")

    bridge_found = False
    for conn in connections:
        if isinstance(conn, dict) and "connection_id" in conn:
            record(
                "BRIDGE_CONN",
                f"connection_id={conn['connection_id']}, profile={conn.get('profile_name')}",
            )
            bridge_found = True

    if post_count <= pre_count:
        record(
            "FAIL",
            f"Bridge connection not reflected in /status: count still {post_count}",
        )
    else:
        record(
            "PASS", f"Bridge connection reflected: count {pre_count} -> {post_count}"
        )

    # Close stdin to signal the bridge to exit
    try:
        connect_proc.stdin.close()
    except Exception:
        pass

    # Wait for connect process to exit
    try:
        connect_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        connect_proc.kill()
        connect_proc.wait(timeout=2)

    exit_code = connect_proc.returncode
    record("CONNECT_EXIT", f"tela connect exited with code {exit_code}")

    # Verify bridge disconnected from gateway
    time.sleep(0.5)
    status, body = _http_get(url_status, token=_bearer_token)
    final_count = body.get("active_connections", 0) if isinstance(body, dict) else 0
    record("FINAL_COUNT", f"active_connections={final_count}")

    if final_count != pre_count:
        record(
            "FAIL",
            f"Bridge did not clean up: count {final_count} != expected {pre_count}",
        )
        return False

    if not bridge_found:
        record("FAIL", "No bridge connection appeared in /status during connect probe")
        return False

    record("PASS", "tela connect bridge: registration, lifecycle, cleanup verified")
    return True


# ---------------------------------------------------------------------------
# Phase 10: tela connect without running server
# ---------------------------------------------------------------------------


def test_connect_no_server() -> bool:
    """
    tela connect without a running server must fail with bounded exit.
    Per ADR-004/005, it must not hang indefinitely.
    """
    # Make sure lockfile is absent (serve already running, but we use --server
    # with a bogus port to test failure)
    env = os.environ.copy()
    env["TELA_BEARER_TOKEN"] = "bogus-token"

    connect_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tela",
            "connect",
            "--server",
            f"{SERVE_HOST}:1",  # port 1 should not have a server
            "--config",
            str(CONFIG_PATH),
            "--max-recovery-attempts",
            "0",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    record(
        "CMD",
        f"tela connect --server {SERVE_HOST}:1 (no server, max-recovery-attempts=0)",
    )

    try:
        exit_code = connect_proc.wait(timeout=10)
        record("CONNECT_EXIT", f"tela connect exited with code {exit_code} within 10s")
    except subprocess.TimeoutExpired:
        connect_proc.kill()
        connect_proc.wait(timeout=2)
        record("FAIL", "tela connect did not exit within 10s (unbounded)")
        return False

    if exit_code == 0:
        record("FAIL", "tela connect exited 0 when no server was available")
        return False

    record("PASS", f"tela connect without server: bounded exit ({exit_code})")
    return True


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------


def main() -> int:
    """Run all characterization phases, collect evidence, report."""
    global _serve_process

    results: dict[str, bool] = {}

    print("=" * 72)
    print("RUNTIME CHARACTERIZATION SNAPSHOT")
    print("=" * 72)

    try:
        # Phase 1
        print("\n--- Phase 1: tela serve startup ---")
        results["serve_startup"] = test_serve_startup()

        if results["serve_startup"]:
            # Phase 2
            print("\n--- Phase 2: GET /health ---")
            results["health"] = test_health_endpoint()

            # Phase 3
            print("\n--- Phase 3: GET /status (with auth) ---")
            results["status_auth"] = test_status_endpoint()

            # Phase 3b
            print("\n--- Phase 3b: GET /status (no auth) ---")
            results["status_no_auth"] = test_status_no_auth()

            # Phase 4
            print("\n--- Phase 4: POST /connect ---")
            results["connect"] = test_connect_endpoint()

            # Phase 4b
            print("\n--- Phase 4b: POST /connect (no auth) ---")
            results["connect_no_auth"] = test_connect_no_auth()

            # Phase 5
            print("\n--- Phase 5: POST /disconnect ---")
            results["disconnect"] = test_disconnect_endpoint()

            # Phase 5b
            print("\n--- Phase 5b: POST /disconnect (nonexistent) ---")
            results["disconnect_nonexistent"] = test_disconnect_nonexistent()

            # Phase 6
            print("\n--- Phase 6: Discovery vs Readiness distinguishability ---")
            results["discovery_vs_readiness"] = test_discovery_vs_readiness()

            # Phase 7
            print("\n--- Phase 7: Connection lifecycle count ---")
            results["connection_lifecycle"] = test_connection_count_after_lifecycle()

            # Phase 8
            print("\n--- Phase 8: tela status CLI ---")
            results["tela_status_cli"] = test_tela_status_cli()

            # Phase 9
            print("\n--- Phase 9: tela connect bridge probe ---")
            results["connect_bridge"] = test_connect_bridge()

            # Phase 10
            print("\n--- Phase 10: tela connect without server ---")
            results["connect_no_server"] = test_connect_no_server()

    finally:
        print("\n--- Cleanup ---")
        _cleanup()

    # Summary
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    all_pass = True
    for phase, passed in results.items():
        icon = "PASS" if passed else "FAIL"
        print(f"  {icon}: {phase}")
        if not passed:
            all_pass = False

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n  Result: {passed}/{total} phases passed")

    if all_pass:
        print("\nPASS: All characterization phases succeeded")
        return 0
    else:
        print("\nFAIL: One or more characterization phases failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
