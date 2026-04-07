# Normalized Blocker Basis

Execution timestamp: 2026-04-07
Step: reclose.basis.normalize_blockers

## Purpose

This is the authoritative blocker-class ledger for downstream reclose work.
It normalizes blocker naming, preserves provenance-vs-disposition distinctions,
and fixes the earlier invalid closure shape where open blocker rows were carried
inside a success path.

## Authoritative Blocker Families

| blocker_family | provenance | disposition | basis | downstream_reclose_owner | reclose_verification_path |
|---|---|---|---|---|---|
| R13 | pre-existing | **PROVEN-2026-04-07** | Fresh runtime witness now passes for `_registry_lock` release during awaited network I/O: `test_r13_lock_released_before_lock_acquire_await`, `test_r13_runtime_lock_state_during_network_await`, and `test_r13_lock_hold_scope_structure_proof` all PASS in `evidence/behavioral_proof_register.md`. | N/A — closed by refreshed runtime witness already recorded | **PROVEN**: `behavioral_proof_register.md` and `runtime_uncertainty_register.md` both record the fresh PASS-backed closure. |
| R42-CONFIG-REMOVE-INFLIGHT | pre-existing | **PROVEN-2026-04-07** | Fresh runtime witness now passes for config-reload-remove during in-flight recovery, including `config_missing=true` fail-closed signaling and stale-lock cleanup: `test_r42_config_remove_during_inflight_recovery`, `test_r42_config_missing_error_envelope_has_required_fields`, and `test_r42_prune_lock_after_config_remove` all PASS in `evidence/behavioral_proof_register.md`. | N/A — closed by refreshed runtime witness already recorded | **PROVEN**: row-level closure is now backed by the current PASS evidence in `behavioral_proof_register.md` and `runtime_uncertainty_register.md`. |
| R42-DISCONNECT-UNDER-RECOVERY | pre-existing | **PROVEN-2026-04-07** | Fresh runtime witness now passes for the separate disconnect-under-recovery cleanup path: `test_r42_disconnect_all_clears_recovery_locks`, `test_r42_lock_cleanup_with_held_lock`, and `test_r42_prune_lock_after_client_removal` all PASS in `evidence/behavioral_proof_register.md`. | N/A — closed by refreshed runtime witness already recorded | **PROVEN**: the disconnect-under-recovery path remains a distinct blocker-family row and is now closed by current PASS evidence. |
| SURFACE-REENUMERATE | out-of-step-origin | **CLOSED-2026-04-07** | `re_enumerate()` classified as `RESOLVED_EXTERNAL_CONTRACT` per `docs/DESIGN.md:620`. Surface classification explicitly documented; synchronized across runtime uncertainty register and behavioral proof register. | debt_closure.impl.decide_surface_and_manifest_authority | **CLOSED**: Classification verified in DESIGN.md public API documentation. |
| AUTH-MCP-FASTMCP | out-of-step-origin | **CLOSED-2026-04-07** | FastMCP authority tuple reconciled per `docs/DESIGN.md:561-578` "FastMCP Translation Boundary" section. Package=`fastmcp>=2.0.0`, runtime import=`from mcp.server.fastmcp import FastMCP`, manifest=implementation-agnostic. Translation boundary explicitly documented; synchronized across runtime uncertainty register and behavioral proof register. | debt_closure.impl.decide_surface_and_manifest_authority | **CLOSED**: Authority tuple verified in DESIGN.md translation boundary documentation. |

## Resolved Blocker Records (Post-Synchronization)

The following blocker families were resolved as part of `reclose.surface.sync_evidence_registers` step execution:

| resolved_family | resolution_date | resolution_basis | artifacts_updated |
|---|---|---|---|
| SURFACE-REENUMERATE | 2026-04-07 | `docs/DESIGN.md:620` explicit public surface classification | runtime_uncertainty_register.md, behavioral_proof_register.md, normalized_blocker_basis.md |
| AUTH-MCP-FASTMCP | 2026-04-07 | `docs/DESIGN.md:561-578` FastMCP Translation Boundary authority tuple | runtime_uncertainty_register.md, behavioral_proof_register.md, normalized_blocker_basis.md |

## Gate Rule

- `gate_open_allowed=true` because every ADR-006 behavioral blocker row (`R13`, `R42-CONFIG-REMOVE-INFLIGHT`, `R42-DISCONNECT-UNDER-RECOVERY`) is now **PROVEN-2026-04-07** in the synchronized structured evidence set.
- SURFACE-REENUMERATE and AUTH-MCP-FASTMCP resolved 2026-04-07 via documentation synchronization; these do not block gate-open.
- Supportive structure evidence may accompany a blocker row, but it does not change disposition from `blocking-now` by itself.
- No behavioral blocker family may be softened to non-blocking without explicit non-intersection evidence naming the remaining gates it does not intersect. This rule remains satisfied because the three ADR-006 behavioral blocker families are now closed from runtime PASS evidence rather than softened by prose.

## Register Alignment Notes

- `evidence/behavioral_proof_register.md` now treats `R13`, `R42-CONFIG-REMOVE-INFLIGHT`, and `R42-DISCONNECT-UNDER-RECOVERY` as fresh PASS-backed **PROVEN** rows.
- `evidence/runtime_uncertainty_register.md` now mirrors the same three rows as **PROVEN-2026-04-07** with no carry-forward blocker families remaining.
- **SURFACE-REENUMERATE and AUTH-MCP-FASTMCP closed 2026-04-07**: Both blocker families synchronized across all three registers with reference to `docs/DESIGN.md` authoritative documentation.
- This basis intentionally does not downgrade supporting non-blocker observations such as `UNC-LIVENESS-HEALTHY-NEIGHBOR` or `UNC-CONFIG-MISSING-FAIL-CLOSED`; they remain support evidence only and do not intersect blocker closure unless a future artifact proves otherwise.

## Prior Closure Invalidity

The earlier closure was invalid because it declared success while the blocker-class rows were still open, which violated the hardened rule that blocker rows must be closed rather than merely documented before gate-open.
