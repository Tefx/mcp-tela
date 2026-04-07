# Runtime Uncertainty Register

### Input Artifact Reviewed
- behavioral proof register from `reclose.verify.refresh_registers`: `evidence/behavioral_proof_register.md`
- fresh runtime execution in this worktree: `uv run pytest tests/repro/test_adr006_runtime_hardening_probes.py -v --tb=short`
- fresh test suite result: `14 passed, 1 skipped`

| item_ref | provenance | disposition | evidence_sufficient | why | owner_step | closure_path | review_miss_cause | overturned_decision | re_close_proof_required |
|---|---|---|---|---|---|---|---|---|---|
| R13 | refreshed-from-actual-reclose-evidence | **PROVEN-2026-04-07** | yes | Fresh reclose probe run shows the dedicated runtime witness now passes: `test_r13_runtime_lock_state_during_network_await` PASS, with supporting PASS results for `test_r13_lock_released_before_lock_acquire_await` and `test_r13_lock_hold_scope_structure_proof`. This replaces the stale structural-only wording. | N/A — closed by fresh runtime witness already executed | **PROVEN**: executable runtime witness now exists and passed in this worktree; blocker no longer depends on a skipped probe or prose-only rationale. | Prior refresh artifacts froze R13 at structural-only closure after the witness had already been restored elsewhere. | `R13 treated as closed from stale refresh state rather than from the current executable witness result` | None; satisfied by the fresh passing runtime witness in this worktree. |
| R42-CONFIG-REMOVE-INFLIGHT | refreshed-from-actual-reclose-evidence | **PROVEN-2026-04-07** | yes | Fresh probe run passes `test_r42_config_remove_during_inflight_recovery`, `test_r42_config_missing_error_envelope_has_required_fields`, and the helper prune probe. The row is grounded in current execution, not carried-forward assumption. | N/A — closed by fresh runtime probe execution | **PROVEN**: config-remove during in-flight recovery is evidenced by passing runtime probes in this worktree. | Earlier artifacts were vulnerable to stale XFAIL-era wording. | `partial stale wording risk after earlier false-close cycle` | None; satisfied by current PASS evidence. |
| R42-DISCONNECT-UNDER-RECOVERY | refreshed-from-actual-reclose-evidence | **PROVEN-2026-04-07** | yes | Fresh probe run passes all three disconnect-under-recovery tests: `disconnect_all_clears_recovery_locks`, `lock_cleanup_with_held_lock`, and `prune_lock_after_client_removal`. | N/A — closed by fresh runtime probe execution | **PROVEN**: disconnect cleanup obligation is evidenced by current PASS results. | Earlier summaries risked collapsing the two R42 paths together. | `R42 disconnect path could be hidden by aggregate wording unless refreshed row-by-row` | None; satisfied by current PASS evidence. |
| UNC-LIVENESS-HEALTHY-NEIGHBOR | refreshed-from-actual-reclose-evidence | non-blocking | yes | `test_healthy_neighbor_uses_different_recovery_lock` PASS; `test_healthy_neighbor_concurrent_calls_during_peer_recovery` remains SKIPPED in the same fresh run. This remains monitor-only and is not a blocker row. | N/A — no blocker carry required for current gate shape | Monitor only; reopen only if later review requires the skipped two-server witness. | None material. |  | Confidence-improver only; not required for current gate opening. |
| UNC-CONFIG-MISSING-FAIL-CLOSED | refreshed-from-actual-reclose-evidence | non-blocking | yes | `test_get_runtime_server_config_returns_config_missing_true` PASS in the fresh reclose run. | N/A — already sufficient for current gate shape | No blocker remains here. | None material. |  | Reopen only if future changes contradict helper fail-closed behavior. |
| SURFACE-REENUMERATE | refreshed-from-actual-reclose-evidence | **CLOSED-2026-04-07** | yes | Fresh probe run passes `test_re_enumerate_is_importable` and `test_re_enumerate_surface_classification_audit`; documentation still classifies `re_enumerate()` as supported public surface. | N/A — closed by current audit chain | **CLOSED**: current passing audit evidence supports `RESOLVED_EXTERNAL_CONTRACT`; no stale false-close wording retained. | Earlier surface review omitted explicit downstream public-surface classification. | `surface taxonomy PASS had been over-read as full closure without row refresh` | None; satisfied by current PASS audit evidence. |
| AUTH-MCP-FASTMCP | refreshed-from-actual-reclose-evidence | **CLOSED-2026-04-07** | yes | Fresh probe run passes `test_fastmcp_authority_tuple_audit`; documentation still records the reconciled FastMCP authority tuple. | N/A — closed by current audit chain | **CLOSED**: current passing audit evidence supports the authoritative tuple; no stale false-close wording retained. | Earlier reviews let packaging/import/manifest evidence drift without one refreshed row. | `authority record looked closed before the refreshed audit chain was restated` | None; satisfied by current PASS audit evidence. |

## Provenance vs Disposition Check
- blocker rows now reflect actual reclose evidence, not prior assumptions: `R13`, `R42-CONFIG-REMOVE-INFLIGHT`, `R42-DISCONNECT-UNDER-RECOVERY`, `SURFACE-REENUMERATE`, and `AUTH-MCP-FASTMCP` are all closed from fresh PASS evidence.
- fresh execution context: `.vectl/worktrees/reclose.verify.refresh_registers`
- fresh execution result: `14 passed, 1 skipped`

## Normalized Blocker Basis Rule
- blocker-family decision basis is the refreshed register state above.
- `gate_open_allowed=true` because no blocker-family row remains unresolved.
- unresolved blocker rows: none.

## Carry-Forward Blockers
- None.

## Remaining Non-Blocking Gaps
- `UNC-LIVENESS-HEALTHY-NEIGHBOR`: one integration confidence-improver remains skipped, but it is not a blocker-family row and does not keep the gate closed.

## Final Disposition Summary
- `R13`: **PROVEN** — executable runtime witness now PASSING in current reclose worktree.
- `R42-CONFIG-REMOVE-INFLIGHT`: **PROVEN** — current PASS evidence.
- `R42-DISCONNECT-UNDER-RECOVERY`: **PROVEN** — current PASS evidence.
- `SURFACE-REENUMERATE`: **CLOSED** — current PASS audit evidence.
- `AUTH-MCP-FASTMCP`: **CLOSED** — current PASS audit evidence.
- `gate_open_allowed`: `true`

## Synchronization Basis
- `normalized_blocker_basis.md` now records the three blocker-family rows `R13`, `R42-CONFIG-REMOVE-INFLIGHT`, and `R42-DISCONNECT-UNDER-RECOVERY` as **PROVEN-2026-04-07** instead of carry-forward blocker rows.
- `behavioral_proof_register.md` remains the runtime-proof source for the exact PASS witness text cited by those normalized blocker rows.
- `gate_open_allowed=true` is consistent across all three authoritative blocker/gate artifacts because no blocker-family row remains unresolved.
