# Behavioral Proof Register (ADR-006 debt slice)

Execution timestamp: 2026-04-07
Agent: software-architect-tacit
Step: reclose.verify.refresh_registers

## Behavioral Proof Register

| requirement_ref | behavior_claim | runtime_proof_expected | evidence_ref | status | closure_path | gate_decision_basis |
|---|---|---|---|---|---|---|
| R13 | `_registry_lock` is not held across awaited network I/O in downstream recovery paths | Executable runtime witness proving: awaited network operation occurs, `_registry_lock` is unheld during that await window, and lock-state instrumentation catches violations | `uv run pytest tests/repro/test_adr006_runtime_hardening_probes.py -v --tb=short` → `TestR13RegistryLockNotHeldDuringAwait::{test_r13_lock_released_before_lock_acquire_await, test_r13_runtime_lock_state_during_network_await, test_r13_lock_hold_scope_structure_proof}` all PASS in `.vectl/worktrees/reclose.verify.refresh_registers` | **PROVEN-2026-04-07** | Executable runtime witness restored and passing; structural proof now supports, but does not replace, runtime closure | **PROVEN**: fresh reclose execution shows the dedicated runtime witness now PASSES, so this row is no longer a structural-only carry and no skip-based false-close wording remains. |
| R42-CONFIG-REMOVE-INFLIGHT | Per-server recovery lock is pruned when config reload removes a server during in-flight recovery, with fail-closed `config_missing=true` surfaced | Runtime witness for config-remove during in-flight recovery plus fail-closed error-envelope fields | `uv run pytest tests/repro/test_adr006_runtime_hardening_probes.py -v --tb=short` → `TestR42ConfigReloadRemovesLock::{test_r42_config_remove_during_inflight_recovery, test_r42_config_missing_error_envelope_has_required_fields}` PASS; helper prune probe `test_r42_prune_lock_after_config_remove` also PASS | **PROVEN-2026-04-07** | Runtime witness proves config-missing signaling and cleanup path | **PROVEN**: fresh reclose probe run shows the config-remove path is exercised successfully and no stale XFAIL wording remains. |
| R42-DISCONNECT-UNDER-RECOVERY | Per-server recovery lock is pruned after disconnect pressure while recovery is underway, leaving no stale lock state | Runtime witness covering disconnect-all cleanup, held-lock cleanup, and orphan prune behavior | `uv run pytest tests/repro/test_adr006_runtime_hardening_probes.py -v --tb=short` → `TestR42DisconnectUnderRecovery::{test_r42_disconnect_all_clears_recovery_locks, test_r42_lock_cleanup_with_held_lock, test_r42_prune_lock_after_client_removal}` PASS | **PROVEN-2026-04-07** | Runtime witness proves disconnect cleanup path | **PROVEN**: fresh reclose probe run confirms all disconnect-under-recovery cleanup probes pass. |
| UNC-LIVENESS-HEALTHY-NEIGHBOR | Healthy-neighbor liveness remains unaffected while failing server recovery is in progress | At least one passing partitioning probe; fuller two-server witness optional unless blocker reopens | `TestHealthyNeighborLiveness::test_healthy_neighbor_uses_different_recovery_lock` PASS; `test_healthy_neighbor_concurrent_calls_during_peer_recovery` SKIPPED in same fresh run | RESOLVED_NON_BLOCKING | Non-blocking adjacent edge retained as monitor-only | Non-blocking: one confidence-improver integration probe remains skipped, but this row is not a downstream gate blocker. |
| UNC-CONFIG-MISSING-FAIL-CLOSED | Missing-server path fails closed with `config_missing=true` where closure contract requires it | Passing probe for helper-level fail-closed semantics | `TestConfigMissingFailClosed::test_get_runtime_server_config_returns_config_missing_true` PASS in fresh reclose probe run | RESOLVED_NON_BLOCKING | Monitor-only unless later evidence contradicts helper fail-closed behavior | Non-blocking: fail-closed behavior remains evidenced and does not hold the gate. |
| SURFACE-REENUMERATE | `re_enumerate()` classification is explicitly one of: `RESOLVED_EXTERNAL_CONTRACT`, `RESOLVED_INTERNAL_ONLY`, or `RESOLVED_COMPATIBILITY_SHIM` | Passing audit probe plus matching documentation classification | `TestReEnumerateSurfaceClassification::{test_re_enumerate_is_importable, test_re_enumerate_surface_classification_audit}` PASS; `docs/DESIGN.md:620` documents `re_enumerate()` as supported public surface | **RESOLVED_EXTERNAL_CONTRACT** | Existing docs/test chain closes the surface classification row | **CLOSED**: refreshed register keeps this row closed from actual passing audit evidence, not prior assumption. |
| AUTH-MCP-FASTMCP | mcp/fastmcp authority reconciled to one authoritative tuple: package, import, manifest | Passing audit probe plus one reconciled documentation tuple | `TestFastMCPAuthorityTuple::test_fastmcp_authority_tuple_audit` PASS; `docs/DESIGN.md:561-578` records the authoritative FastMCP translation boundary tuple | **CLOSED-2026-04-07** | Existing docs/import/test chain closes the authority row | **CLOSED**: refreshed register keeps this row closed from actual passing audit evidence, not stale false-close wording. |

## Commands Executed

```
uv run pytest tests/repro/test_adr006_runtime_hardening_probes.py -v --tb=short
```

## Actual Output (Fresh Execution in `.vectl/worktrees/reclose.verify.refresh_registers`)

```
============================= test session starts ==============================
platform darwin -- Python 3.12.12, pytest-9.0.2, pluggy-1.6.0
collected 15 items

tests/repro/test_adr006_runtime_hardening_probes.py::TestR42ConfigReloadRemovesLock::test_r42_prune_lock_after_config_remove PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestR42ConfigReloadRemovesLock::test_r42_config_remove_during_inflight_recovery PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestR42ConfigReloadRemovesLock::test_r42_config_missing_error_envelope_has_required_fields PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestR42DisconnectUnderRecovery::test_r42_disconnect_all_clears_recovery_locks PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestR42DisconnectUnderRecovery::test_r42_lock_cleanup_with_held_lock PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestR42DisconnectUnderRecovery::test_r42_prune_lock_after_client_removal PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestR13RegistryLockNotHeldDuringAwait::test_r13_lock_released_before_lock_acquire_await PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestR13RegistryLockNotHeldDuringAwait::test_r13_runtime_lock_state_during_network_await PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestR13RegistryLockNotHeldDuringAwait::test_r13_lock_hold_scope_structure_proof PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestHealthyNeighborLiveness::test_healthy_neighbor_uses_different_recovery_lock PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestHealthyNeighborLiveness::test_healthy_neighbor_concurrent_calls_during_peer_recovery SKIPPED
tests/repro/test_adr006_runtime_hardening_probes.py::TestConfigMissingFailClosed::test_get_runtime_server_config_returns_config_missing_true PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestReEnumerateSurfaceClassification::test_re_enumerate_is_importable PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestReEnumerateSurfaceClassification::test_re_enumerate_surface_classification_audit PASSED
tests/repro/test_adr006_runtime_hardening_probes.py::TestFastMCPAuthorityTuple::test_fastmcp_authority_tuple_audit PASSED

======================== 14 passed, 1 skipped in 1.21s =========================
```

## Gate Decision

- `gate_open_allowed=true`
- blocker rows resolved from actual fresh reclose evidence: `R13`, `R42-CONFIG-REMOVE-INFLIGHT`, `R42-DISCONNECT-UNDER-RECOVERY`, `SURFACE-REENUMERATE`, `AUTH-MCP-FASTMCP`
- unresolved blocker rows: none
- remaining skipped probe: `UNC-LIVENESS-HEALTHY-NEIGHBOR` integration confidence-improver only; not a blocker row

## Synchronization Basis

- `normalized_blocker_basis.md` now mirrors `R13`, `R42-CONFIG-REMOVE-INFLIGHT`, and `R42-DISCONNECT-UNDER-RECOVERY` as **PROVEN-2026-04-07** instead of blocker-open carry rows.
- `runtime_uncertainty_register.md` carries the same three blocker-family rows as **PROVEN-2026-04-07**.
- `gate_open_allowed=true` is authoritative across the synchronized blocker-basis, behavioral-proof, and runtime-uncertainty artifacts because no blocker-family row remains unresolved.
