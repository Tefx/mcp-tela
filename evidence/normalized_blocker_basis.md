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
| SURFACE-REENUMERATE | out-of-step-origin | **CLOSED-2026-04-07** | `re_enumerate()` classified as `RESOLVED_EXTERNAL_CONTRACT` per `docs/DESIGN.md:620`. Surface classification explicitly documented; synchronized across runtime uncertainty register and behavioral proof register. | debt_closure.impl.decide_surface_and_manifest_authority | **CLOSED**: Classification verified in DESIGN.md public API documentation. |
| AUTH-MCP-FASTMCP | out-of-step-origin | **CLOSED-2026-04-07** | FastMCP authority tuple reconciled per `docs/DESIGN.md:561-578` "FastMCP Translation Boundary" section. Package=`fastmcp>=2.0.0`, runtime import=`from mcp.server.fastmcp import FastMCP`, manifest=implementation-agnostic. Translation boundary explicitly documented; synchronized across runtime uncertainty register and behavioral proof register. | debt_closure.impl.decide_surface_and_manifest_authority | **CLOSED**: Authority tuple verified in DESIGN.md translation boundary documentation. |

## Resolved Blocker Records (Post-Synchronization)

The following blocker families were resolved as part of `reclose.surface.sync_evidence_registers` step execution:

| resolved_family | resolution_date | resolution_basis | artifacts_updated |
|---|---|---|---|
| SURFACE-REENUMERATE | 2026-04-07 | `docs/DESIGN.md:620` explicit public surface classification | runtime_uncertainty_register.md, behavioral_proof_register.md, normalized_blocker_basis.md |
| AUTH-MCP-FASTMCP | 2026-04-07 | `docs/DESIGN.md:561-578` FastMCP Translation Boundary authority tuple | runtime_uncertainty_register.md, behavioral_proof_register.md, normalized_blocker_basis.md |

## Gate Rule

- `gate_open_allowed=false` until downstream reclose verification closes every ADR-006 behavioral blocker row (`R13`, `R42-CONFIG-REMOVE-INFLIGHT`, `R42-DISCONNECT-UNDER-RECOVERY`) in the structured evidence set.
- SURFACE-REENUMERATE and AUTH-MCP-FASTMCP resolved 2026-04-07 via documentation synchronization; these do not block gate-open.
- Supportive structure evidence may accompany a blocker row, but it does not change disposition from `blocking-now` by itself.
- No behavioral blocker family may be softened to non-blocking without explicit non-intersection evidence naming the remaining gates it does not intersect. No such evidence currently exists for the three remaining ADR-006 behavioral blocker families.

## Register Alignment Notes

- `evidence/behavioral_proof_register.md` now treats `R13`, `R42-CONFIG-REMOVE-INFLIGHT`, and `R42-DISCONNECT-UNDER-RECOVERY` as open blocker rows rather than partially-proven success cases.
- `evidence/runtime_uncertainty_register.md` remains the authoritative provenance/disposition ledger for remaining open blocker families.
- **SURFACE-REENUMERATE and AUTH-MCP-FASTMCP closed 2026-04-07**: Both blocker families synchronized across all three registers with reference to `docs/DESIGN.md` authoritative documentation.
- This basis intentionally does not downgrade supporting non-blocker observations such as `UNC-LIVENESS-HEALTHY-NEIGHBOR` or `UNC-CONFIG-MISSING-FAIL-CLOSED`; they remain support evidence only and do not intersect blocker closure unless a future artifact proves otherwise.

## Prior Closure Invalidity

The earlier closure was invalid because it declared success while the blocker-class rows were still open, which violated the hardened rule that blocker rows must be closed rather than merely documented before gate-open.
