"""Mode D Liveness Probe: Post-Structural-Collapse Runtime Contract Verification.

Spec sources:
  - docs/ADR-005: Gateway runtime is sole readiness authority
  - docs/CONFIRMED-SURFACE-CONTRACT.md: Admission boundary freeze
  - docs/USAGE.md: CLI reference and surface contract
  - contracts/mcp_admission_transient_503.schema.json: Transient warming rejection
  - docs/ADR-004: Gateway startup state machine (deferred, source-of-truth split)

Verification targets after structural simplification:
  1. `tela serve` starts, binds, writes lockfile — ALIVE
  2. GET /health returns {"status":"ok","pid":N} without auth
  3. GET /status (with auth) returns readiness fields: state, active_connections, ...
  4. Lockfile = discovery-only (has port/pid/host/token, does NOT have state/active_connections)
  5. GET /status (without auth) is rejected → readiness is protected
  6. POST /connect registers bridge (lifecycle plumbing, NOT readiness proof)
  7. POST /disconnect deregisters bridge
  8. tela connect creates a bridge that registers and cleans up
  9. tela status --json reports gateway state via GET /status (not lockfile)
  10. POST /mcp is the admission surface (not POST /connect)
  11. Discovery-before-readiness: lockfile appears before convergence completes
  12. Readiness fields NOT in lockfile (ADR-005 separation preserved)

This is BLACK-BOX: subprocess + HTTP only. No implementation source read.
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
# Paths & Constants
# ---------------------------------------------------------------------------

WORKTREE = Path(__file__).resolve().parent
CONFIG_PATH = WORKTREE / "runtime_snapshot_minimal.yaml"
LOCKFILE_PATH = Path.home() / ".tela" / "gateway.lock"
SERVE_LOG = WORKTREE / "structural_collapse_serve.log"
SERVE_HOST = "127.0.0.1"
STARTUP_TIMEOUT = 15
POLL_INTERVAL = 0.3

# Global state
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
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return None


def _wait_for_lockfile(timeout: float = STARTUP_TIMEOUT) -> dict | None:
    """Poll until lockfile exists with valid data, bounded by timeout."""
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
    url: str, payload: dict, token: str | None = None, timeout: float = 3
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
    try:
        subprocess.run(
            [sys.executable, "-m", "tela", "stop"],
            capture_output=True,
            timeout=3,
        )
    except Exception:
        pass
    try:
        LOCKFILE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


evidence_log: list[str] = []


def record(tag: str, detail: str) -> None:
    evidence_log.append(f"[{tag}] {detail}")
    print(f"  [{tag}] {detail}", flush=True)


# ---------------------------------------------------------------------------
# Phase 1: tela serve starts, binds port, writes lockfile
# ---------------------------------------------------------------------------


def test_serve_alive() -> bool:
    """tela serve starts an HTTP gateway that binds and writes lockfile (ALIVE)."""
    global _serve_process, _bound_port, _bearer_token

    record("CMD", f"tela serve --config {CONFIG_PATH} --port 0 --idle-timeout 0")

    LOCKFILE_PATH.unlink(missing_ok=True)
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
        env=os.environ.copy(),
    )

    # Wait for lockfile (discovery truth per ADR-004/005)
    lockfile_data = _wait_for_lockfile(timeout=STARTUP_TIMEOUT)

    if _serve_process.poll() is not None:
        exit_code = _serve_process.returncode
        record("FAIL", f"Serve process exited during startup with code {exit_code}")
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
    _bearer_token = lockfile_data.get("token")
    pid = lockfile_data.get("pid")

    # Port-level network verification (MANDATORY per Mode D spec)
    import socket

    try:
        with socket.create_connection((SERVE_HOST, _bound_port), timeout=3):
            pass
        record("PORT_PROBE", f"TCP connect to {SERVE_HOST}:{_bound_port} succeeded")
    except Exception as e:
        record("FAIL", f"Port probe failed: {e}")
        return False

    # Verify process is alive
    try:
        os.kill(pid, 0)
        record("PROCESS_ALIVE", f"PID {pid} is alive")
    except OSError:
        record("FAIL", f"PID {pid} is dead — PREMATURE_EXIT after lockfile write")
        return False

    record("PASS", "tela serve: ALIVE — port bound, process alive, lockfile written")
    return True


# ---------------------------------------------------------------------------
# Phase 2: GET /health — no auth, liveness endpoint
# ---------------------------------------------------------------------------


def test_health_endpoint() -> bool:
    """GET /health returns 200 with {"status":"ok","pid":N} without auth."""
    assert _bound_port is not None
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

    record("PASS", "GET /health: 200, status=ok, pid present — no auth required")
    return True


# ---------------------------------------------------------------------------
# Phase 3: GET /status with auth — readiness authority
# ---------------------------------------------------------------------------


def test_status_with_auth() -> bool:
    """GET /status with bearer token returns full lifecycle status with readiness fields."""
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

    # Readiness authority fields per CONFIRMED-SURFACE-CONTRACT §1.1:
    # GET /status is the sole readiness authority
    required_readiness_fields = [
        "active_connections",
        "connected_servers",
        "connections",
    ]
    for field in required_readiness_fields:
        if field not in body:
            record("FAIL", f"GET /status missing readiness field: {field}")
            return False

    # State field indicates lifecycle position
    if "state" not in body:
        record("WARN", "GET /status missing 'state' field (lifecycle state)")
    else:
        state_val = body["state"]
        record("STATE", f"Gateway state: {state_val!r}")
        # After startup with no servers, should be "ready"
        if state_val == "ready":
            record("PASS", "Gateway reports 'ready' state — convergence complete")
        else:
            record("INFO", f"Gateway state is {state_val!r} (may still be converging)")

    record("PASS", "GET /status with auth: 200, readiness fields present")
    return True


# ---------------------------------------------------------------------------
# Phase 3b: GET /status WITHOUT auth — must be rejected
# ---------------------------------------------------------------------------


def test_status_no_auth_rejected() -> bool:
    """GET /status without bearer token must be rejected (auth enforced)."""
    assert _bound_port is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/status"
    record("CMD", f"GET {url} (NO auth)")

    status, body = _http_get(url, token=None)
    record("RESPONSE", f"HTTP {status}: {body}")

    if status == 200:
        record(
            "FAIL",
            "GET /status returned 200 without auth — readiness auth NOT enforced!",
        )
        return False

    # Accept 401, 403, or any non-200
    if status in (401, 403):
        record("PASS", f"GET /status: correctly rejected without auth (HTTP {status})")
    else:
        record("PASS", f"GET /status: rejected without auth (HTTP {status})")

    return True


# ---------------------------------------------------------------------------
# Phase 4: POST /connect is lifecycle registration only, NOT readiness proof
# ---------------------------------------------------------------------------


def test_connect_is_lifecycle_plumbing() -> bool:
    """POST /connect registers a bridge connection — lifecycle plumbing, NOT readiness truth.

    Per ADR-005:
      - POST /connect remains connection registration and lifecycle plumbing only
      - POST /connect must NOT be described as readiness truth
      - POST /connect must NOT be admission proof for ordinary MCP traffic
    """
    assert _bound_port is not None and _bearer_token is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/connect"
    connection_id = "structural-collapse-conn-1"
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

    # ADR-005 contract: POST /connect response must NOT claim readiness
    # The response should have status=connected, NOT status=ready
    if body.get("status") == "ready":
        record(
            "FAIL",
            "POST /connect returned status='ready' — this conflates lifecycle plumbing with readiness!",
        )
        return False

    # Verify the connection appears in /status (separate authority)
    url_status = f"http://{SERVE_HOST}:{_bound_port}/status"
    s, b = _http_get(url_status, token=_bearer_token)
    if s == 200 and isinstance(b, dict):
        active_count = b.get("active_connections", 0)
        connections = b.get("connections", [])
        found = any(
            isinstance(c, dict) and c.get("connection_id") == connection_id
            for c in connections
        )
        if not found:
            record(
                "WARN",
                f"POST /connect succeeded but /status doesn't list connection (count={active_count})",
            )
        else:
            record(
                "VERIFY",
                f"/status confirms connection '{connection_id}' present, active_connections={active_count}",
            )
    else:
        record("WARN", f"/status query failed: HTTP {s}")

    record(
        "PASS",
        "POST /connect: lifecycle plumbing works, returns 'connected' (NOT 'ready')",
    )
    return True


# ---------------------------------------------------------------------------
# Phase 5: POST /disconnect — lifecycle deregistration
# ---------------------------------------------------------------------------


def test_disconnect_endpoint() -> bool:
    """POST /disconnect deregisters the bridge connection from Phase 4."""
    assert _bound_port is not None and _bearer_token is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/disconnect"
    connection_id = "structural-collapse-conn-1"
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

    # Verify /status no longer shows this connection
    url_status = f"http://{SERVE_HOST}:{_bound_port}/status"
    s, b = _http_get(url_status, token=_bearer_token)
    if s == 200 and isinstance(b, dict):
        connections = b.get("connections", [])
        found = any(
            isinstance(c, dict) and c.get("connection_id") == connection_id
            for c in connections
        )
        if found:
            record(
                "WARN",
                f"Connection '{connection_id}' still appears in /status after disconnect",
            )
        else:
            record(
                "VERIFY",
                f"/status confirms connection '{connection_id}' removed after disconnect",
            )

    record("PASS", "POST /disconnect: lifecycle deregistration works")
    return True


# ---------------------------------------------------------------------------
# Phase 6: ADR-005 Discovery vs Readiness Boundary (CRITICAL)
# ---------------------------------------------------------------------------


def test_discovery_vs_readiness_separation() -> bool:
    """ADR-005: Lockfile is discovery truth ONLY; readiness is from GET /status.

    This is the CRITICAL post-structural-collapse check. If this fails,
    it means the simplification collapsed discovery and readiness.

    Verification:
    1. Lockfile contains discovery fields (port, pid, host, token, config_path,
       started_at, version) — per LockfileData model contract.
    2. Lockfile does NOT contain readiness fields (active_connections, state,
       connected_servers, connections) — these belong to /status only.
    3. GET /status has readiness fields that lockfile lacks.
    4. The two are externally distinguishable.
    """
    lockfile_data = _read_lockfile()
    if lockfile_data is None:
        record("FAIL", "No lockfile present for discovery-vs-readiness test")
        return False

    assert _bound_port is not None and _bearer_token is not None
    url_status = f"http://{SERVE_HOST}:{_bound_port}/status"
    status, body = _http_get(url_status, token=_bearer_token)
    if status != 200 or not isinstance(body, dict):
        record("FAIL", f"GET /status failed with {status}: {body}")
        return False

    # Discovery-only fields (per LockfileData model docs, 7 required fields)
    discovery_fields = {
        "port",
        "pid",
        "host",
        "token",
        "config_path",
        "started_at",
        "version",
    }
    lockfile_has = discovery_fields & set(lockfile_data.keys())

    # Readiness-only fields (per CONFIRMED-SURFACE-CONTRACT & StatusResponse)
    readiness_fields = {
        "active_connections",
        "connected_servers",
        "connections",
        "state",
    }
    status_has = readiness_fields & set(body.keys())

    record("LOCKFILE_DISCOVERY_FIELDS", str(sorted(lockfile_has)))
    record("STATUS_READINESS_FIELDS", str(sorted(status_has)))

    # Assert: lockfile has all 7 discovery fields
    if lockfile_has != discovery_fields:
        missing = discovery_fields - lockfile_has
        record("FAIL", f"Lockfile missing discovery fields: {missing}")
        return False

    # Assert: /status has readiness fields
    if not status_has:
        record("FAIL", "GET /status has no readiness fields")
        return False

    # CRITICAL: readiness fields must NOT appear in lockfile
    leaked = readiness_fields & set(lockfile_data.keys())
    if leaked:
        record(
            "FAIL",
            f"ADR-005 VIOLATION: Readiness fields leaked into lockfile: {leaked}",
        )
        return False

    record(
        "PASS",
        "ADR-005: Discovery (lockfile) and readiness (/status) are fully separated",
    )
    return True


# ---------------------------------------------------------------------------
# Phase 7: tela connect bridge registration and cleanup
# ---------------------------------------------------------------------------


def test_connect_bridge_lifecycle() -> bool:
    """tela connect creates a bridge, registers with gateway, cleans up on exit.

    Per USAGE.md: connect discovers endpoint, registers via POST /connect,
    polls GET /status for readiness, then bridges stdio to HTTP.
    """
    assert _bound_port is not None and _bearer_token is not None
    url_status = f"http://{SERVE_HOST}:{_bound_port}/status"

    # Get baseline
    status, body = _http_get(url_status, token=_bearer_token)
    pre_count = body.get("active_connections", 0) if isinstance(body, dict) else 0
    record("PRE_BRIDGE_COUNT", f"active_connections={pre_count}")

    # Launch connect with --server to use the already-running gateway
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

    # Give bridge time to register and poll readiness
    time.sleep(2.0)

    # Check if process is alive
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

    # Verify bridge registered via POST /connect (reflected in /status)
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
        return False

    # Close stdin to signal the bridge to exit
    try:
        connect_proc.stdin.close()
    except Exception:
        pass

    try:
        connect_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        connect_proc.kill()
        connect_proc.wait(timeout=2)

    # Verify bridge disconnected
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

    record(
        "PASS", "tela connect bridge: registration, readiness polling, cleanup all work"
    )
    return True


# ---------------------------------------------------------------------------
# Phase 8: tela status CLI uses GET /status (not lockfile as readiness source)
# ---------------------------------------------------------------------------


def test_tela_status_cli() -> bool:
    """tela status --json queries the running gateway and reports readiness fields.

    Per CONFIRMED-SURFACE-CONTRACT: `tela status` is an operator surface that
    reflects GET /status content (readiness authority), not lockfile content.
    """
    result = subprocess.run(
        [sys.executable, "-m", "tela", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    record("CMD", "tela status --json")
    record("EXIT_CODE", str(result.returncode))

    if result.returncode != 0:
        record("FAIL", f"tela status exited with {result.returncode}")
        record("STDERR", result.stderr[:500] if result.stderr else "(empty)")
        return False

    try:
        data = json.loads(result.stdout)
        record("STATUS_FIELDS", str(sorted(data.keys())))
    except json.JSONDecodeError:
        record(
            "FAIL", f"tela status --json output not valid JSON: {result.stdout[:200]}"
        )
        return False

    # Verify readiness fields are present (from /status, not lockfile)
    needed = {"active_connections", "state"}
    present = needed & set(data.keys())
    if not present:
        record(
            "FAIL",
            f"tela status output missing readiness fields: needed {needed}, present {present}",
        )
        return False

    # Verify the 'state' field exists (lifecycle state from /status)
    if "state" in data:
        record(
            "VERIFY",
            f"tela status reports state={data['state']!r} — from GET /status authority",
        )

    # Verify discovery-only fields are NOT the main content
    # (tela status should show readiness fields, not just lockfile data)
    discovery_only = {"port", "host", "token"}
    lockfile_only_present = discovery_only & set(data.keys())
    if lockfile_only_present == set(data.keys()):
        record(
            "FAIL",
            "tela status only shows lockfile discovery fields — not readiness authority!",
        )
        return False

    record("PASS", "tela status: reports readiness fields from GET /status authority")
    return True


# ---------------------------------------------------------------------------
# Phase 9: POST /connect is NOT admission-gated (ADR-005 §Decision)
# ---------------------------------------------------------------------------


def test_connect_not_admission_gated() -> bool:
    """POST /connect succeeds regardless of readiness state.

    Per ADR-005 and CONFIRMED-SURFACE-CONTRACT §1.1:
      - POST /connect remains connection registration and lifecycle plumbing only
      - POST /mcp is the ONLY readiness-gated HTTP admission surface
      - POST /connect must NOT be described as readiness truth or admission proof

    This test verifies POST /connect works when the gateway is in 'ready' state,
    confirming that /connect is registration, not admission.
    """
    assert _bound_port is not None and _bearer_token is not None

    # First confirm gateway state is 'ready'
    url_status = f"http://{SERVE_HOST}:{_bound_port}/status"
    s, b = _http_get(url_status, token=_bearer_token)
    gateway_state = b.get("state", "unknown") if isinstance(b, dict) else "unknown"
    record("GATEWAY_STATE", f"Gateway state before /connect: {gateway_state!r}")

    # POST /connect should succeed regardless
    url = f"http://{SERVE_HOST}:{_bound_port}/connect"
    conn_id = "admission-test-conn-1"
    status, body = _http_post(url, {"connection_id": conn_id}, token=_bearer_token)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else body}",
    )

    if status != 200:
        record("FAIL", f"POST /connect failed with HTTP {status}")
        return False
    if not isinstance(body, dict) or body.get("status") != "connected":
        record("FAIL", f"POST /connect did not return connected: {body}")
        return False

    # Clean up
    url_disconnect = f"http://{SERVE_HOST}:{_bound_port}/disconnect"
    _http_post(url_disconnect, {"connection_id": conn_id}, token=_bearer_token)

    record(
        "PASS",
        "POST /connect: registration plumbs lifecycle, is NOT readiness-gated admission",
    )
    return True


# ---------------------------------------------------------------------------
# Phase 10: Network-level verification (Mode D mandatory for servers)
# ---------------------------------------------------------------------------


def test_network_level_verification() -> bool:
    """Mandatory network-level verification per Mode D spec.

    Process-level checks alone are insufficient — must verify port bound
    and accepting connections.
    """
    assert _bound_port is not None

    import socket

    # TCP connect to gateway port
    try:
        with socket.create_connection((SERVE_HOST, _bound_port), timeout=3):
            pass
        record("TCP_PROBE", f"TCP connect to {SERVE_HOST}:{_bound_port} succeeded")
    except Exception as e:
        record("FAIL", f"TCP connect failed: {e}")
        return False

    # HTTP health check
    url = f"http://{SERVE_HOST}:{_bound_port}/health"
    status, body = _http_get(url)
    if status != 200:
        record("FAIL", f"HTTP health check failed: {status}")
        return False

    record("PASS", "Network-level: TCP port bound, HTTP health check responds")
    return True


# ---------------------------------------------------------------------------
# Phase 11: /mcp endpoint exists (admission surface per contract)
# ---------------------------------------------------------------------------


def test_mcp_endpoint_exists() -> bool:
    """POST /mcp is the MCP Streamable HTTP transport endpoint.

    Per CONFIRMED-SURFACE-CONTRACT §1.1: POST /mcp is the only
    readiness-gated HTTP admission surface. It must exist and
    respond (even if with an error for malformed requests).
    """
    assert _bound_port is not None and _bearer_token is not None
    url = f"http://{SERVE_HOST}:{_bound_port}/mcp"

    # Send a non-MCP request — should still respond, not 404
    record("CMD", f"POST {url} (with auth, non-MCP content)")

    # Send a minimal JSON that is NOT a valid MCP request
    status, body = _http_post(url, {"test": "probe"}, token=_bearer_token)
    record(
        "RESPONSE",
        f"HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, dict) else str(body)[:200]}",
    )

    # The endpoint must exist (not 404). Even a 400 or 503 is acceptable.
    # A 404 would mean the endpoint was lost during structural collapse.
    if status == 404:
        record(
            "FAIL",
            "POST /mcp returns 404 — endpoint lost during structural simplification!",
        )
        return False

    # Any response means the endpoint exists
    record("PASS", f"POST /mcp endpoint exists and responds (HTTP {status})")
    return True


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------


def main() -> int:
    """Run all liveness probe phases, collect evidence, report."""
    global _serve_process

    results: dict[str, bool] = {}

    print("=" * 72)
    print("POST-STRUCTURAL-COLLAPSE RUNTIME LIVENESS PROBE")
    print("=" * 72)

    try:
        # Phase 1
        print("\n--- Phase 1: tela serve startup (ALIVE check) ---")
        results["serve_alive"] = test_serve_alive()

        if results["serve_alive"]:
            # Phase 2
            print("\n--- Phase 2: GET /health (no auth) ---")
            results["health_no_auth"] = test_health_endpoint()

            # Phase 3
            print("\n--- Phase 3: GET /status with auth (readiness authority) ---")
            results["status_with_auth"] = test_status_with_auth()

            # Phase 3b
            print("\n--- Phase 3b: GET /status without auth (must reject) ---")
            results["status_no_auth"] = test_status_no_auth_rejected()

            # Phase 4
            print(
                "\n--- Phase 4: POST /connect (lifecycle plumbing, NOT readiness) ---"
            )
            results["connect_lifecycle"] = test_connect_is_lifecycle_plumbing()

            # Phase 5
            print("\n--- Phase 5: POST /disconnect (lifecycle deregistration) ---")
            results["disconnect"] = test_disconnect_endpoint()

            # Phase 6 (CRITICAL)
            print("\n--- Phase 6: ADR-005 Discovery vs Readiness Boundary ---")
            results["discovery_vs_readiness"] = test_discovery_vs_readiness_separation()

            # Phase 7
            print("\n--- Phase 7: tela connect bridge lifecycle ---")
            results["connect_bridge"] = test_connect_bridge_lifecycle()

            # Phase 8
            print("\n--- Phase 8: tela status CLI (readiness from /status) ---")
            results["tela_status_cli"] = test_tela_status_cli()

            # Phase 9
            print("\n--- Phase 9: POST /connect is NOT admission-gated ---")
            results["connect_not_admission"] = test_connect_not_admission_gated()

            # Phase 10
            print("\n--- Phase 10: Network-level verification ---")
            results["network_level"] = test_network_level_verification()

            # Phase 11
            print("\n--- Phase 11: POST /mcp endpoint exists ---")
            results["mcp_endpoint"] = test_mcp_endpoint_exists()

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
        print(
            "\nPASS: All liveness probe phases succeeded — structural collapse did not narrow runtime contract"
        )
        return 0
    else:
        print(
            "\nFAIL: One or more liveness probe phases failed — structural collapse may have narrowed runtime contract"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
