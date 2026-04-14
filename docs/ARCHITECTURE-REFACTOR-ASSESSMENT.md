# Architecture Refactor Assessment

## Purpose

This document records whether `mcp-tela` should be architecture-refactored,
what is actually wrong today, and what should be simplified first.

Decision: **YES — targeted refactor COMPLETED.**

## Status: COMPLETED ✓

This assessment originally identified simplification opportunities. The refactor
has been executed through several phases. This document now records what was
accomplished and what remains as known non-goals.

## Scope of This Assessment

This is a **post-refactor record** of completed changes.

- Behavior has been preserved; no regressions introduced.
- The refactor focused on deletion, consolidation, and extraction — not new
  abstraction layers.
- All changes were test-driven and verified through the adversary test suite.

## Executive Summary

`mcp-tela` does **not** look like a classic over-engineered DI/factory/hexagonal
 enterprise framework. The original problems were:

1. ~~a small number of shell modules became too large~~ — **ADDRESSED**
   - Recovery logic extracted from `downstream.py` to `_downstream_recovery.py`
   - Connect command split into `connect_bridge.py`, `connect_transport.py`
   - Connection lifecycle extracted to `connection_lifecycle.py`
   - Downstream registry extracted to `downstream_registry.py`
2. ~~runtime state is spread across several module-level singletons~~ — **ADDRESSED**
   - `_session_registry` moved to `gateway_runtime.py`
   - State ownership is now more centralized with explicit runtime accessors
3. ~~some protocol/adapter abstractions are now unused or single-implementation~~ — **ADDRESSED**
   - Removed: `EventEntryAdapter` — was unused
   - Removed: `ConvergencePolicyConsumer` — was unused
   - Collapsed: `SingleServerConvergenceKernel` — single implementation only
4. ~~backward-compatibility shims are lingering past their usefulness~~ — **PARTIALLY ADDRESSED**
   - `commands/start.py` — retained for internal testing (marked deprecated)
   - Import path cleanup: modules now import `Result` directly from `result.py`

## Verified Findings (ORIGINAL — kept for reference)

### Original Shell Module Sizes (Pre-Refactor)

- `src/tela/shell/downstream.py` — ~~1228~~ now ~412 lines
- `src/tela/commands/connect_cmd.py` — ~~1108~~ now 395 lines (after extraction)
- `src/tela/shell/gateway.py` — ~~973~~ now 947 lines
- `src/tela/commands/serve_cmd.py` — ~~658~~ now 331 lines
- `src/tela/shell/upstream.py` — ~~724~~ now 599 lines
- `src/tela/shell/gateway_runtime.py` — ~~574~~ now 841 lines (state centralization)

### Post-Refactor Structure

New modules created to reduce oversized files:

- `src/tela/shell/_downstream_recovery.py` — recovery logic (212 lines)
- `src/tela/shell/downstream_registry.py` — registry ownership (92 lines)
- `src/tela/shell/connection_lifecycle.py` — connection cleanup (74 lines)
- `src/tela/commands/connect_bridge.py` — bridge framing logic
- `src/tela/commands/connect_transport.py` — transport retry logic

### Abstractions Deleted

| Abstraction | Status | Reason |
|-------------|--------|--------|
| `EventEntryAdapter` | **DELETED** | Unused protocol type (no longer in codebase) |
| `ConvergencePolicyConsumer` | **DELETED** | Unused protocol type (no longer in codebase) |
| `SingleServerConvergenceKernel` | **DELETED** | Was a protocol; replaced by concrete `_converge_single_server_update` function |

### State Ownership Consolidated

| State | Original Location | Current Location |
|-------|-------------------|------------------|
| `_session_registry` | `upstream.py` | `gateway_runtime.py` |
| `_registry` | `downstream.py` | `downstream_registry.py` (module) |
| `_clients` | `downstream.py` | `downstream.py` (unchanged) |
| `_recovery_locks` | `downstream.py` | `_downstream_recovery.py` |

### Runtime State Now Centralized In

- `gateway_runtime.py` — owns `_runtime` singleton with:
  - `_runtime.connections`
  - `_runtime.secrets`
  - `_runtime.session_registry` (moved from `upstream.py` to here)
  - `_runtime.reaper` (owned here; lifecycle managed via `get_runtime_reaper()`, `set_runtime_reaper()`)
  - `_runtime.converge_event` (owned here; lifecycle managed via `get_runtime_converge_event()`, `set_runtime_converge_event()`)
  - Plus accessors: `get_runtime_reaper()`, `set_runtime_reaper()`,
    `get_runtime_converge_event()`, `set_runtime_converge_event()`,
    `get_session_registry_snapshot()`, `clear_session_registry()`

### Import Path Cleanup

All production modules now import `Result` directly from `tela.shell.result`:

```python
# Before (through re-export):
from tela.shell.config_loader import Result  # noqa: F401

# After (direct import):
from tela.shell.result import Result
```

`config_loader.py` still exports `Result` for backward compatibility but this
is deprecated and tests should migrate to direct imports.

## What This Project Does **Not** Need (Still True)

- a full rewrite — **not needed**
- a new architecture layer — **not needed**
- more protocols/interfaces for future flexibility — **not needed**
- a repository/service/controller split — **not needed**
- a broader dependency-injection framework — **not needed**
- giant "unified" functions controlled by boolean flags — **rejected**

## Recommended Refactor Order (COMPLETED)

### ✅ Phase 0 — Protect behavior
- Kept adversary suites green
- Ran `uvx invar-tools guard --all` before/after each slice

### ✅ Phase 1 — Delete fake abstractions
- Removed unused protocol types
- Collapsed single-implementation indirections

### ✅ Phase 2 — Centralize state ownership
- `gateway_runtime.py` is the obvious home for runtime facts
- `gateway.py` orchestrates lifecycle without owning excessive singleton state

### ✅ Phase 3 — Shrink the oversized shell files
- Split by **concrete responsibility**, not abstract layering
- Recovery logic, registry, connection lifecycle all extracted

### ⏸️ Phase 4 — Remove compatibility residue when safe
- `commands/start.py` — retained for testing
- Direct `Result` imports — completed
- Trim `gateway.py` re-exports — completed

## Exit Criteria (ACHIEVED)

- ✅ Shell file sizes reduced through extraction (not violation of guard limits)
- ✅ Unused protocols/adapters deleted
- ✅ Single-implementation indirections collapsed
- ✅ Runtime state ownership easier to explain
- ✅ `Result` import paths are direct and consistent
- ✅ Backward-compatibility exports reduced where safe
- ✅ Full guard and adversary tests pass

## Non-Goals (Still Valid)

- ~~flattening the whole project into one file~~ — rejected
- ~~moving shell I/O logic into core~~ — rejected (violates Invar zone rules)
- ~~replacing tested separation with a boolean-driven mega-function~~ — rejected
- ~~reworking public contracts without matching spec updates~~ — rejected

## Working Rule for Follow-Up Changes

> Delete the abstraction first if it has no real second implementation, but do
> not merge distinct behaviors into a single branching monster.

This rule was applied successfully throughout the refactor.

## Summary

The architecture refactor has been completed. The codebase is now:

1. **Smaller in conceptual surface** — deleted unused abstractions
2. **More centralized in state ownership** — runtime facts live in `gateway_runtime.py`
3. **Better factored by responsibility** — oversized modules split by concrete concern
4. **Just as testable** — all adversary suites remain green

No further architectural simplification is planned at this time.
