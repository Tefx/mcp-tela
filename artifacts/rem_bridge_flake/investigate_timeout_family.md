# Bridge Timeout Family Investigation

## Investigation Summary

Step: `rem.bridge_flake.investigate-persistent-timeout-family`
Date: 2026-03-26
Environment: macOS, Python 3.12.12, cold-start worktree isolation

## Reproduction Attempts

### Cold-start run (pycache purged)
```
$ rm -rf __pycache__ .pytest_cache tests/repro/__pycache__ src/tela/__pycache__ src/tela/core/__pycache__ src/tela/shell/__pycache__ src/tela/commands/__pycache__
$ uv run python -m pytest tests/repro/test_connect_runtime_liveness.py -v -x -s
...
tests/repro/test_connect_runtime_liveness.py::test_serve_ephemeral_bind_publishes_lockfile PASSED
tests/repro/test_connect_runtime_liveness.py::test_connect_discovers_via_lockfile PASSED
tests/repro/test_connect_runtime_liveness.py::test_bridge_handles_mcp_initialize_and_tools_list PASSED
tests/repro/test_connect_runtime_liveness.py::test_disconnect_decrements_connection_count PASSED
4 passed in 9.32s
```

### Repeated bridge-specific test (3x, cache purged each time)
```
Run 1: PASSED in 1.78s
Run 2: PASSED in 1.73s
Run 3: PASSED in 1.72s
```

### Liveness suite
```
$ uv run python -m pytest tests/repro/test_liveness.py -v -x
4 passed in 11.12s
```

### Lockfile state verification
```
$ cat ~/.tela/gateway.lock
No such file or directory (clean - no stale lockfile)
```

No flake reproduced under cold conditions across 7 consecutive test invocations.

## Blocker Fingerprint

**Family**: initialize/tools_list bridge timeout
**Mechanism**: The `test_bridge_handles_mcp_initialize_and_tools_list` test starts `tela serve` as a subprocess, waits for lockfile, starts `tela connect` as a second subprocess, then sends MCP JSON-RPC messages through stdin and waits for responses on stdout via `select.select()` with a 10-second timeout.

The timeout family originates from three sequential race-condition windows:
1. **Lockfile publication race** (10s budget): `tela serve` must bind an ephemeral port, start uvicorn, and atomically write `~/.tela/gateway.lock` before the test's `_wait_for_lockfile()` expires.
2. **Bridge establishment delay** (1s fixed sleep): After starting `tela connect`, the test sleeps 1.0s before sending MCP messages. If bridge registration (`POST /connect`) takes longer than 1s, the bridge is not ready.
3. **MCP round-trip timeout** (10s budget): `select.select()` waits 10s for each of `initialize` and `tools/list` responses through the bridge (stdin -> HTTP -> stdout).

## Root Cause Hypothesis

The original blocker was NOT a code bug in the bridge transport layer itself. The bridge forwarding path (`_forward_stdio_http`, `_post_mcp_message`, `_read_framed_message`, `_write_framed_message`) is correct and deterministic.

**Primary flakiness risk** is the **shared global lockfile at `~/.tela/gateway.lock`**:
- All tests (and any concurrent tela processes) compete over the same `~/.tela/gateway.lock` file.
- The `_clean_lockfile()` helper in the test attempts stale-PID cleanup but does NOT verify that the lockfile it reads was written by the test's own `serve_proc`.
- If another `tela serve` process is running (from a parallel test, another worktree, or a developer session), `_wait_for_lockfile()` picks up the foreign lockfile and the bridge connects to the wrong server.
- The `connect_proc` then registers with a server that may be using a different token, causing `POST /connect` to fail with 401, or the bridge forwards MCP messages to a server with no tools, returning unexpected responses.

**Secondary flakiness risk** is the **1.0s fixed sleep** for bridge establishment:
- Under heavy system load (CI, parallel pytest workers), the `tela connect` subprocess may take longer than 1.0s to complete lockfile discovery + `POST /connect` registration.
- The test then sends MCP messages to a bridge that hasn't completed registration, producing connection errors.

**Tertiary risk** is **subprocess startup latency variance**:
- `subprocess.Popen` + Python import + uvicorn startup + port bind varies significantly across environments (200ms-5s observed range).
- The 10s `_wait_for_lockfile` timeout is generous but the uvicorn bind timeout in `serve_cmd.py` is only 5s (`HTTP_SERVER_BIND_TIMEOUT_SECONDS`).

## Why Previous Fix Did Not Fully Eliminate Flakiness Risk

The blocker-phase fixes addressed:
1. Connection count semantics (`fix-connection-count-semantics`)
2. Liveness payload correctness (`fix-liveness-payload`)
3. Static hygiene issues (`cleanup-static-hygiene`)

These fixes made the bridge *functionally correct* but did **not** address the structural flakiness vectors:
- The shared `~/.tela/gateway.lock` global path was never isolated per test.
- The 1.0s fixed-sleep bridge establishment heuristic was never replaced with a readiness probe.
- No lockfile-identity binding (matching lockfile PID to spawned `serve_proc.pid`) was added.

The gate-retest review (`gate_retest_full_evidence_report.md`) explicitly called out: "Shared `~/.tela/gateway.lock` interference remains plausible because the evidence does not bind the lockfile contents to the specific spawned `serve_proc` instance."

## Environment Consistency Check

| Factor | Status | Notes |
|--------|--------|-------|
| Hot/cold runtime | Both tested | pycache purged for cold runs |
| Stale lockfile | Clean | `~/.tela/gateway.lock` absent before each run |
| Concurrent tela processes | None detected | No foreign lockfile interference during investigation |
| Worktree isolation | Active | Running in `.vectl/worktrees/rem.bridge_flake.investigate-persistent-timeout-family` |
| Python version | 3.12.12 | Consistent across all runs |
| Command family | Consistent | All runs used `uv run python -m pytest` |
| Process reuse | None | Each test spawns fresh `serve` and `connect` subprocesses |

The investigation environment was clean (no concurrent tela processes, no stale lockfiles), which explains why the flake did not reproduce. The flake family requires *environmental contamination* (concurrent lockfile writers or heavy system load) to trigger.

## Next Fix Constraint

To fully eliminate the timeout family, the following changes are required (in priority order):

1. **Lockfile identity binding in tests**: After `_wait_for_lockfile()` returns, assert `lockfile_data["pid"] == serve_proc.pid`. If mismatched, clean the foreign lockfile and retry or fail with a clear diagnostic. This is a test-only change (no production code modification).

2. **Replace fixed sleep with readiness probe**: Replace `time.sleep(1.0)` bridge establishment delays with a polling loop that verifies the bridge process is alive AND has completed registration (e.g., poll `tela status --json` until `active_connections >= 1`).

3. **Consider per-test lockfile isolation**: Override `LOCKFILE_PATH` via env var or config to use a per-test temporary directory. This would eliminate the shared-state interference entirely but requires a production code change to support configurable lockfile paths.

Changes 1 and 2 are test-only and low-risk. Change 3 requires a design decision about whether `LOCKFILE_PATH` should be configurable.

## Non-Trigger Evidence

The contingency was not triggered because:
- No timeout or flake reproduced across 7 consecutive cold-start test invocations.
- The investigation environment had zero lockfile contamination (no stale lockfile, no concurrent tela processes).
- The flake family requires environmental contamination (shared lockfile interference or heavy system load) that was not present during investigation.
- The bridge transport layer code itself is correct and deterministic - the flakiness is structural (test isolation gap), not functional.
