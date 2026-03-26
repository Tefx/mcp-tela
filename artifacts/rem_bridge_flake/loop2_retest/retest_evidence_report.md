# Retest Evidence: rem.final_verify.fix-b1-retry-classification-and-evidence

**Date**: 2026-03-26
**Verifier**: python-senior
**Scope**: Batched fix for B1 transient retry classifier mismatch + evidence regeneration

---

## Root Cause Analysis

**Root cause**: `_is_transient_url_error` in `src/tela/commands/connect_cmd.py` classified
transient errors solely by `reason.errno`, but `ConnectionRefusedError("msg")`,
`ConnectionResetError("msg")`, and `BrokenPipeError("msg")` constructed with only a
message string have `errno=None`. Both the test suite and real urllib raise these
exception types without a numeric errno.

**Symptom**: 6 of 8 `TestB1TransientRetry` tests failed because the classifier returned
`False` for genuinely transient exceptions.

**Prior artifact contradiction**: The previous `retest_evidence_report.md` claimed
"B1 RETIRED" and "8/0/0" without matching fresh execution output; the tests were
actually failing at the time of that report.

**Fix**: Added type-based classification (`isinstance` check against
`ConnectionRefusedError`, `ConnectionResetError`, `ConnectionAbortedError`,
`BrokenPipeError`, `TimeoutError`) as the primary path, with the errno-based
check retained as a fallback for generic `OSError` instances that carry a numeric
errno but no dedicated subclass.

---

## Fresh Command Outputs

### 1. Blocker Suite (B1)

```bash
uv run pytest tests/repro/test_gate_blocker_regressions.py::TestB1TransientRetry -q
```

```output
........                                                                 [100%]
8 passed in 0.47s
```

### 2. Full Blocker Regression Suite (B1 + B2 + B3)

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

15 passed in 0.54s
```

### 3. Liveness Suite

```bash
uv run pytest tests/repro/test_liveness.py -q
```

```output
....                                                                     [100%]
4 passed in 10.47s
```

---

## Blocker Disposition

| Blocker | Status | Evidence |
|---------|--------|----------|
| B1: transient retry classifier | CLOSED | Type-based classification fix; 8/8 tests pass fresh |
| B2: lockfile PID identity | PASSING | 4/4 tests pass (no change needed this step) |
| B3: soak pipefail | PASSING | 3/3 tests pass (no change needed this step) |

## Test Summary

| Suite | Passed | Failed | Skipped |
|-------|--------|--------|---------|
| TestB1TransientRetry | 8 | 0 | 0 |
| TestB2LockfilePidBinding | 4 | 0 | 0 |
| TestB3SoakPipefailPropagation | 3 | 0 | 0 |
| test_liveness.py | 4 | 0 | 0 |

**Total**: 19 passed, 0 failed, 0 skipped

## Blocker Intersection

**Status**: `blocking-now closed`

B1 transient retry classifier mismatch is fixed. All gate-blocking regression tests pass.
No residual issues remain in this blocker family.
