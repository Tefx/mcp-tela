# Baseline Guard Characterization — arch_refactor.characterization.guard_baseline

**Date**: 2026-04-14
**Verifier**: integration-verifier-tacit
**Step**: arch_refactor.characterization.guard_baseline

---

## refs Read Confirmation (MANDATORY)

- `docs/ARCHITECTURE-REFACTOR-ASSESSMENT.md#verified-findings` — READ. Key passages: 5 verified findings covering oversized shell files, weak abstractions, fragmented runtime state ownership, import/compat residue, and strong test safety net. Target files: downstream.py (1228L), connect_cmd.py (1108L), gateway.py (973L), serve_cmd.py (658L), upstream.py (724L), gateway_runtime.py (574L).
- `docs/ARCHITECTURE-REFACTOR-ASSESSMENT.md#suggested-exit-criteria` — READ. Key passages: exit criteria include no shell file exceeding guard limit, unused protocols/adapters deleted, single-implementation indirections collapsed, Result import paths direct, backward-compat exports reduced, full guard and adversary tests pass.
- `docs/DESIGN.md#module-boundaries` — READ. Key passages: core/ is pure logic, shell/ handles I/O and process edges, commands/ are CLI entrypoints only. Result re-export via config_loader is documented.
- `docs/DESIGN.md#shell-module-responsibilities` — READ. Full module-level contract for all shell modules including ownership, dependencies, concurrency model. Confirms _runtime ownership in gateway_runtime.py, _session_registry in upstream.py, _clients/_registry/_server_instructions/_recovery_locks in downstream.py, _startup_manifest/_reaper/_converge_event in gateway.py.

---

## Baseline Commands

```
uvx invar-tools guard --all
.venv/bin/python -m pytest tests/shell/test_gateway.py -v --tb=short
.venv/bin/python -m pytest tests/shell/test_downstream.py -v --tb=short
.venv/bin/python -m pytest tests/shell/test_connect_cmd.py -v --tb=short
.venv/bin/python -m pytest tests/shell/test_reload.py -v --tb=short
.venv/bin/python -m pytest tests/integration/test_end_to_end.py -v --tb=short
.venv/bin/python -m pytest tests/repro/test_runtime_boundary_immutability.py -v --tb=short
.venv/bin/python -m pytest tests/repro/test_startup_coord_liveness.py -v --tb=short
.venv/bin/python -m pytest tests/repro/test_connect_runtime_liveness.py -v --tb=short
.venv/bin/python -m pytest tests/shell/ tests/core/ tests/integration/ tests/repro/ tests/black_box/ --tb=short -q
```

---

## Baseline Findings

### Guard Errors (9)

| # | File | Rule | Message |
|---|------|------|---------|
| 1 | src/tela/core/contracts.py:24 | missing_contract | `_meta_pre` has no @pre or @post contract |
| 2 | src/tela/core/contracts.py:44 | missing_contract | `_meta_post` has no @pre or @post contract |
| 3 | src/tela/core/contracts.py:68 | missing_contract | `pre` has no @pre or @post contract |
| 4 | src/tela/core/contracts.py:111 | missing_contract | `post` has no @pre or @post contract |
| 5 | src/tela/shell/downstream.py | file_size | 1229 lines (max: 700 for shell) |
| 6 | <project> | shell_complexity_debt | 13 unaddressed complexity warnings (limit: 5) |
| 7 | src/tela/shell/downstream.py | escape_hatch_file_limit | File rule budget shell_result: 4/2 |
| 8 | <project> | escape_hatch_project_limit | Project rule budget shell_result: 16/5 |
| 9 | <project> | escape_hatch_budget | Escape hatch weighted budget: 57/19 |

### Guard Warnings (37)

Breakdown:
- shell_pure_logic: 20 warnings
- function_size: 7 warnings
- redundant_type_contract (info): 9 items
- shell_too_complex (info): 13 items
- file_size_warning: 3 warnings
- dead_assign: 2 warnings
- dead_param: 1 warning
- dead_export: 1 warning
- review_suggested: 2 warnings
- contract_quality_ratio: 1 warning

### Guard Summary

- Files checked: 54
- Guard passed: FALSE
- Doctest passed: TRUE
- Crosshair: SKIPPED (prior failures)
- Property tests: SKIPPED (prior failures)
- Escape hatches: 19
- Escape hatch budget: 57/19 (EXCEEDED by 3x)
- Gating status: exceeded

### Oversized Files Observed

| File | Lines | Shell Limit | Status |
|------|-------|-------------|--------|
| src/tela/shell/downstream.py | 1228 | 700 | EXCEEDED (error) |
| src/tela/commands/connect_cmd.py | 1108 | N/A (command) | file_size escape hatch |
| src/tela/shell/gateway.py | 973 | 700 | EXCEEDED (file_size escape hatch) |
| src/tela/shell/upstream.py | 724 | 700 | EXCEEDED (file_size escape hatch) |
| src/tela/commands/serve_cmd.py | 658 | N/A (command) | warning (file_size_warning) |
| src/tela/shell/gateway_runtime.py | 574 | 700 | warning (file_size_warning) |
| src/tela/shell/reload.py | 440 | 700 | within limits |
| src/tela/shell/config_loader.py | 93 | 700 | within limits |

### Shared-State Ownership Hotspots Observed

| Module | State | Type |
|--------|-------|------|
| gateway_runtime.py | `_runtime` | GatewayRuntime singleton (gateway_runtime.py:133) |
| upstream.py | `_session_registry` | dict[str, UpstreamSession] (upstream.py:73) |
| downstream.py | `_clients`, `_registry`, `_server_instructions`, `_recovery_locks` | Module-level dicts |
| gateway.py | `_startup_manifest`, `_reaper`, `_converge_event` | Module-level globals |

### Fake Abstraction Inventory

| Symbol | File | Status |
|--------|------|--------|
| `EventEntryAdapter` | src/tela/shell/downstream.py:107 | Protocol defined, NEVER used outside definition (orphan) |
| `ConvergencePolicyConsumer` | src/tela/shell/reload.py:89 | Protocol defined, NEVER used outside definition (orphan) |
| `SingleServerConvergenceKernel` | src/tela/shell/reload.py:76 | Protocol with exactly 1 concrete impl (`_RegistrySingleServerConvergenceKernel`), never tested directly |

### Import/Compat Residue Inventory

- **Result re-export via config_loader**: 25 source files import `Result` from `tela.shell.config_loader` instead of canonical `tela.shell.result`
- **gateway.py backward-compat re-export block**: gateway.py re-exports gateway_runtime symbols for compatibility
- **`tela.commands.start.py`**: deprecated but still retained

---

## Protected Suites

### Primary Adversary Suites (must stay green)

| Suite | Result | Count |
|-------|--------|-------|
| tests/shell/test_gateway.py | PASS | 58 passed |
| tests/shell/test_downstream.py | PASS | 63 passed |
| tests/shell/test_connect_cmd.py | PASS | 38 passed |
| tests/shell/test_reload.py | PASS | 27 passed |
| tests/integration/test_end_to_end.py | PASS | 14 passed |
| tests/repro/test_runtime_boundary_immutability.py | PASS | 19 passed |
| tests/repro/test_startup_coord_liveness.py | PASS | 5 passed (26.10s) |
| tests/repro/test_connect_runtime_liveness.py | PASS (flaky) | 5 passed, 1 intermittent FAIL |

### Full Suite Run

```
1043 tests collected
7 failed, 1015 passed, 1 skipped, 9 xfailed, 11 xpassed in 97.18s
```

### Pre-existing Failure Classification

| Failed Test | Root Cause | Blocking? |
|-------------|-----------|-----------|
| test_connect_bridge_readiness_contract_freeze (2) | Documentation surface contract wording drift | NO — doc-contract drift, not runtime regression |
| test_discover_or_autostart_re_autostarts_after_wait_timeout | Red-test: autostart retry count assertion mismatch | NO — known gap in startup coordinator retry logic |
| test_conn_v2_blackbox::test_status_schema_fields | Orphaned tela server process (LOCKFILE_READ_ERROR) | NO — test-env isolation issue, orphaned PID |
| test_conn_v2_blackbox::test_connections_schema_fields | Orphaned tela server process (LOCKFILE_READ_ERROR) | NO — test-env isolation issue, orphaned PID |
| test_tool_prefix_blackbox (2) | Reserved prefix enforcement not implemented | NO — documented as known implementation gap |

### Connect Runtime Liveness Flaky Test

`test_disconnect_decrements_connection_count` fails intermittently (JSON decode error on status subprocess stdout — race condition on server readiness). Passes on rerun (3.27s). This is a timing-sensitive subprocess test, not an architecture regression.

---

## Downstream Gates Protected

- arch_refactor.structural_collapse.gate
- arch_refactor.layer_dissolution.full_plan_check
- arch_refactor.finalization.gate

These gates must verify that:
1. No shell file exceeds its guard limit post-refactor (currently downstream.py is 75% over)
2. Escape hatch budget drops from 57/19 toward ≤ 19/19
3. Shell complexity debt drops from 13/5 toward ≤ 5/5
4. All primary adversary suites above remain PASS
5. No new guard errors introduced
6. Fake abstractions deleted (EventEntryAdapter, ConvergencePolicyConsumer, SingleServerConvergenceKernel unnecessary indirection)

---

## Step Semantic Reporting

- **step_intent**: Establish the pre-refactor baseline for architecture simplification by running guard and targeted adversary suites, recording file-size debt, shell-complexity debt, and runtime-critical suites
- **expected_result**: Guard reports known errors; adversary suites pass; baseline metrics captured for comparison
- **observed_result**: Guard: 9 errors, 37 warnings, budget 57/19 exceeded. Adversary suites: all 8 primary suites PASS (1 flaky). Full suite: 1015 passed, 7 failed (all pre-existing non-blocking gaps). Baseline metrics fully captured.
- **failure_alignment**: All 7 failures are pre-existing known gaps or test-env isolation issues. None represent regressions from the codebase's current state. The flaky connect liveness test passes on rerun.
- **product_implementation_files_modified**: NONE — this is a verification-only step. No src/ files were modified.

---

## Behavioral-Proof Reporting

- **behavioral_proof_register**: Guard baseline captured at commit. Adversary suite results captured. File-size, complexity, and ownership hotspot inventories recorded. Failure root causes classified.
- **gate_open_allowed**: YES — baseline is adequately captured. All primary adversary suites are green. Pre-existing failures are classified as non-blocking. The refactor phases can proceed with confidence that deviations from this baseline are detectable.
- **explicit_uncertainty_sources**: The flaky `test_disconnect_decrements_connection_count` may cause CI noise but is not an architecture regression. The conn_v2_blackbox failures appear to be test-env isolation (orphaned processes) rather than code defects.

---

## Exit Criteria Baseline Check

| Exit Criterion | Current Status | Target |
|----------------|---------------|--------|
| No shell file exceeds guard limit | FAIL (downstream.py 1228/700) | PASS |
| Unused protocols/adapters deleted | FAIL (3 orphan abstractions) | PASS |
| Single-implementation indirections collapsed | FAIL (SingleServerConvergenceKernel) | PASS |
| Result import paths direct and consistent | FAIL (25 files via config_loader) | PASS |
| Backward-compatibility exports reduced | FAIL (gateway re-exports) | PASS |
| Full guard and adversary tests pass | FAIL (guard: 9 errors) | PASS |