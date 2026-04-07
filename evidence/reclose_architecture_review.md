# Architecture Review: reclose.deep_review.architecture_review

## Scope

- blocker ledger as control-plane truth
- runtime proof vs static proof distinctions
- module-level mutable state discipline for `_recovery_locks`
- no new authority split from FastMCP translation boundary resolution

## Findings

### Source-of-truth assessment

- `evidence/normalized_blocker_basis.md` is the authoritative blocker-class ledger and explicitly defines the gate rule (`gate_open_allowed=true`) from blocker-family dispositions rather than from prose summaries.
- `evidence/behavioral_proof_register.md` remains the runtime-proof source for blocker families that require executable witnesses.
- `evidence/runtime_uncertainty_register.md` mirrors the same blocker-family dispositions and preserves provenance-vs-disposition separation.
- Result: no hidden truth split was found between the blocker ledger and the runtime-proof register.

### Mutable-state assessment

- `src/tela/shell/downstream.py` keeps `_recovery_locks` module-owned beside `_clients` and `_registry`, so recovery lock lifecycle stays in the same control plane as downstream session ownership.
- Production mutation points are disciplined through `_registry_lock`: creation/acquire in `_acquire_recovery_lock`, prune in `_prune_recovery_lock_if_unused`, and bulk cleanup in `disconnect_all`.
- Recovery code releases `_registry_lock` before awaited transport work and uses per-server recovery locks for contention control, which preserves the R13 and R42 concurrency boundaries.

### Authority-boundary assessment

- `docs/DESIGN.md` and `docs/INTERFACES.md` now express one FastMCP translation boundary tuple: package authority=`fastmcp>=2.0.0`, runtime import authority=`mcp.server.fastmcp`, manifest authority=implementation-agnostic.
- `src/tela/shell/gateway_runtime.py` keeps FastMCP as runtime-owned state and only exposes operation results, not the live server reference.
- Result: the translation-boundary resolution consolidates authority instead of creating a new split.

## Verdict

PASS
