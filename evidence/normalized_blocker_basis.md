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
| R13 | pre-existing | blocking-now | Behavioral proof still lacks runtime witness that `_registry_lock` is released during awaited network I/O. Static analysis is supportive but non-closing. | debt_closure.impl.close_runtime_gap_if_exposed | debt_closure.verify.runtime_evidence_r13_r42 -> debt_closure.final_review.behavior_rereview -> debt_closure.final_review.final_gate |
| R42-CONFIG-REMOVE-INFLIGHT | pre-existing | blocking-now | Current evidence shows helper-level pruning support plus an XFAIL for config-reload-remove during in-flight recovery; the required live `config_missing=true` + stale-lock cleanup witness is still missing. | debt_closure.impl.close_runtime_gap_if_exposed | debt_closure.verify.runtime_evidence_r13_r42 -> debt_closure.final_review.behavior_rereview -> debt_closure.final_review.final_gate |
| R42-DISCONNECT-UNDER-RECOVERY | pre-existing | blocking-now | The disconnect-under-recovery path never received its own proof row, so the R42 family cannot be treated as closed or partially retired. | debt_closure.impl.close_runtime_gap_if_exposed | debt_closure.verify.runtime_evidence_r13_r42 -> debt_closure.final_review.behavior_rereview -> debt_closure.final_review.final_gate |
| SURFACE-REENUMERATE | out-of-step-origin | blocking-now | `re_enumerate()` remains blocker-class until docs/tests/annotations converge on one explicit surface classification and the reclose packet proves that classification. | debt_closure.impl.decide_surface_and_manifest_authority | debt_closure.verify.wiring_and_manifest_audit -> debt_closure.final_review.wiring_rereview -> debt_closure.final_review.final_gate |
| AUTH-MCP-FASTMCP | out-of-step-origin | blocking-now | Package authority, runtime import authority, and manifest/header authority remain split; no explicit translation-boundary closure record exists yet. | debt_closure.impl.decide_surface_and_manifest_authority | debt_closure.verify.wiring_and_manifest_audit -> debt_closure.final_review.wiring_rereview -> debt_closure.final_review.final_gate |

## Gate Rule

- `gate_open_allowed=false` until downstream reclose verification closes every blocker row above in the structured evidence set.
- Supportive structure evidence may accompany a blocker row, but it does not change disposition from `blocking-now` by itself.
- No blocker family may be softened to non-blocking without explicit non-intersection evidence naming the remaining gates it does not intersect. No such evidence currently exists for any of the five blocker families.

## Register Alignment Notes

- `evidence/behavioral_proof_register.md` now treats `R13`, `R42-CONFIG-REMOVE-INFLIGHT`, and `R42-DISCONNECT-UNDER-RECOVERY` as open blocker rows rather than partially-proven success cases.
- `evidence/runtime_uncertainty_register.md` remains the authoritative provenance/disposition ledger for all five blocker families.
- This basis intentionally does not downgrade supporting non-blocker observations such as `UNC-LIVENESS-HEALTHY-NEIGHBOR` or `UNC-CONFIG-MISSING-FAIL-CLOSED`; they remain support evidence only and do not intersect blocker closure unless a future artifact proves otherwise.

## Prior Closure Invalidity

The earlier closure was invalid because it declared success while the blocker-class rows were still open, which violated the hardened rule that blocker rows must be closed rather than merely documented before gate-open.
