## Independent Retest Report

**Tester**: integration-verifier
**Independence Level**: L2
**Date**: 2026-03-26
**Worktree**: `.vectl/worktrees/rem.final_verify.retest-b1-after-batched-fix`

---

### Commands Executed

All commands executed in fresh/cold environment (first `uv run` created new `.venv`).

#### 1. B1 Blocker Suite

```bash
uv run pytest tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry -q
```

```output
........                                                                 [100%]
8 passed in 0.65s
```

#### 2. Liveness Suite

```bash
uv run pytest tests/repro/test_liveness.py -q
```

```output
....                                                                     [100%]
4 passed in 10.49s
```

#### 3. Full Blocker Regression Suite (B1 + B2 + B3)

```bash
uv run pytest tests/repro/test_gate_blocker_regressions.py -v --tb=short
```

```output
tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry::test_is_transient_url_error_connection_refused PASSED [  6%]
tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry::test_is_transient_url_error_connection_reset PASSED [ 13%]
tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry::test_is_transient_url_error_broken_pipe PASSED [ 20%]
tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry::test_is_transient_url_error_non_transient PASSED [ 26%]
tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry::test_post_mcp_message_retries_on_transient_error PASSED [ 33%]
tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry::test_post_mcp_message_fails_after_max_retries PASSED [ 40%]
tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry::test_post_mcp_message_no_retry_on_http_error PASSED [ 46%]
tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry::test_post_json_retries_on_transient_error PASSED [ 53%]
tests/repro/test_gate_blocker_regressions.py::TestB2LockfilePidBinding::test_autostart_serve_returns_spawned_pid PASSED [ 60%]
tests/repro/test_gate_blocker_regressions.py::TestB2LockfilePidBinding::test_wait_for_live_lockfile_rejects_mismatched_pid PASSED [ 66%]
tests/repro/test_gate_blocker_regressions.py::TestB2LockfilePidBinding::test_wait_for_live_lockfile_accepts_matching_pid PASSED [ 73%]
tests/repro/test_gate_blocker_regressions.py::TestB2LockfilePidBinding::test_wait_for_live_lockfile_no_pid_filter_accepts_any PASSED [ 80%]
tests/repro/test_gate_blocker_regressions.py::TestB3SoakPipefailPropagation::test_soak_script_has_pipefail PASSED [ 86%]
tests/repro/test_gate_blocker_regressions.py::TestB3SoakPipefailPropagation::test_pipefail_propagates_inner_failure PASSED [ 93%]
tests/repro/test_gate_blocker_regressions.py::TestB3SoakPipefailPropagation::test_without_pipefail_tee_masks_failure PASSED [100%]

15 passed in 0.57s
```

---

### Test Integrity Analysis (P2: Fake Seam)

Tests examined for mock contamination at integration boundaries:

**TestB1TransientRetry**:
- Uses `unittest.mock.patch` on `urllib_request.urlopen`
- ✓ Justified: Tests are verifying retry logic for transient network errors which cannot be reliably reproduced in CI
- Tests mock the network layer, not the application's retry logic itself
- The classifier `_is_transient_url_error` is tested WITHOUT mocks (tests 1-4)
- The retry behavior tests (5-8) mock at the HTTP transport layer, which is appropriate for testing transient failure handling

**test_liveness.py**:
- Uses NO mocks - spawns real subprocess processes (`python -m tela serve/connect`)
- ✓ Integration tests are real, test actual HTTP gateway and stdio mode behavior
- Tests verify:
  - Ready banner does NOT corrupt stdout (MCP transport channel)
  - MCP initialize protocol response
  - HTTP mode stays alive for 3+ seconds
  - `tela status --json` returns meaningful state

**Conclusion**: No fake seams at integration boundaries. Mocks used appropriately for network layer simulation in transient retry tests.

---

### Artifact Consistency Check

**Regenerated artifact reviewed**: `artifacts/rem_bridge_flake/loop2_retest/retest_evidence_report.md`

**Fresh outputs vs artifact claims**:

| Claim | Artifact | Fresh Output | Status |
|-------|----------|--------------|--------|
| B1 test count | 8 passed | 8 passed | ✓ MATCH |
| B1 test status | 8/8 pass | 8/8 pass | ✓ MATCH |
| Liveness test count | 4 passed | 4 passed | ✓ MATCH |
| Liveness test status | 4/4 pass | 4/4 pass | ✓ MATCH |
| Full suite count | 15 passed | 15 passed | ✓ MATCH |
| B2 test count | 4 passed | 4 passed | ✓ MATCH |
| B3 test count | 3 passed | 3 passed | ✓ MATCH |
| Blocker intersection | CLOSED | No issues found | ✓ MATCH |

**Contradictions noted**: None. Artifact claims match fresh execution output.

---

### Source Code Verification

Verified the fix is in place at `src/tela/commands/connect_cmd.py:521-563`:

```python
def _is_transient_url_error(exc: urllib_error.URLError) -> Result[bool, str]:
    """..."""
    reason = exc.reason
    if isinstance(reason, OSError):
        # Type-based classification (PRIMARY PATH)
        transient_types = (
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            TimeoutError,
        )
        if isinstance(reason, transient_types):
            return Result(value=True)

        # Errno-based fallback (for generic OSError with numeric errno)
        import errno
        transient_errnos = {
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.EPIPE,
            errno.ETIMEDOUT,
        }
        return Result(value=reason.errno in transient_errnos)
    return Result(value=False)
```

The fix correctly:
1. Uses type-based classification as primary path (handles exception without errno)
2. Falls back to errno check for generic OSError instances
3. Covers all required transient error types (B1 fix verified)

---

### Disposition

| Blocker | Status | Evidence |
|---------|--------|----------|
| B1: transient retry classifier | **CLOSED** | Type-based classification fix; 8/8 tests pass fresh |
| B2: lockfile PID identity | **PASSING** | 4/4 tests pass (unchanged) |
| B3: soak pipefail | **PASSING** | 3/3 tests pass (unchanged) |

**Blocker intersection disposition**: `blocking-now closed`

All gate-blocking regression tests pass. No residual issues remain in this blocker family.

---

### Independence Certification

- ✓ Fresh environment (new `.venv` created by first `uv run`)
- ✓ No stale evidence reuse
- ✓ All commands executed verbatim from worktree
- ✓ Test source code examined for fake seams
- ✓ Artifact claims compared against fresh output

**Result**: SUCCESS - No issues found. Gate can proceed.