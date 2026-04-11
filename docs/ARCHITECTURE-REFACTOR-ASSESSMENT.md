# Architecture Refactor Assessment

## Purpose

This document records whether `mcp-tela` should be architecture-refactored,
what is actually wrong today, and what should be simplified first.

Decision: **yes, targeted refactor is warranted; full rewrite is not.**

The project already has strong behavior coverage, but the shell/runtime surface
 has accumulated oversized modules, singleton-heavy state, and a few leftover
 abstraction layers that no longer earn their keep.

## Scope of This Assessment

This is a **docs-first assessment** before runtime changes.

- No behavior changes are introduced by this document.
- No architectural rewrite is proposed.
- The goal is to make later simplification explicit, bounded, and test-driven.

## Executive Summary

`mcp-tela` does **not** look like a classic over-engineered DI/factory/hexagonal
 enterprise framework. The main problem is different:

1. a small number of shell modules became too large,
2. runtime state is spread across several module-level singletons,
3. some protocol/adapter abstractions are now unused or single-implementation,
4. backward-compatibility shims are lingering past their usefulness,
5. Invar guard pressure is already signaling that the current structure is too
   expensive to maintain.

So the right move is **selective demolition**: delete weak abstractions, shrink
 oversized modules, and centralize state ownership without inventing new layers.

## Verified Findings

The findings below were verified from the repository state and guard output.

### 1. Shell modules are too large

Verified in source:

- `src/tela/shell/downstream.py` — 1228 lines
- `src/tela/commands/connect_cmd.py` — 1108 lines
- `src/tela/shell/gateway.py` — 973 lines
- `src/tela/commands/serve_cmd.py` — 658 lines
- `src/tela/shell/upstream.py` — 724 lines
- `src/tela/shell/gateway_runtime.py` — 574 lines

Verified in `uvx invar-tools guard --all` output:

- `downstream.py` exceeds shell file-size limits
- `connect_cmd.py`, `gateway.py`, and `serve_cmd.py` trigger large-function or
  size warnings
- the project currently has too many unaddressed shell-complexity warnings

### 2. Some abstractions are now weak or unnecessary

Verified in source search:

- `EventEntryAdapter` in `src/tela/shell/downstream.py` is defined but not used
- `ConvergencePolicyConsumer` in `src/tela/shell/reload.py` is defined but not used
- `SingleServerConvergenceKernel` in `src/tela/shell/reload.py` currently has a
  single concrete implementation (`_RegistrySingleServerConvergenceKernel`)

These are good deletion candidates unless a second real implementation appears.

### 3. Runtime state ownership is fragmented

Verified in source:

- `src/tela/shell/gateway_runtime.py` owns `_runtime`
- `src/tela/shell/upstream.py` owns `_session_registry`
- `src/tela/shell/downstream.py` owns `_registry`, `_clients`,
  `_server_instructions`, `_recovery_locks`
- `src/tela/shell/gateway.py` still owns `_startup_manifest`, `_reaper`, and
  `_converge_event`

This is workable, but expensive: lifecycle reasoning is spread across multiple
 files instead of one obvious runtime authority.

### 4. Import and compatibility residue is still present

Verified in source:

- `src/tela/shell/config_loader.py` re-exports `Result` from
  `src/tela/shell/result.py`
- most modules still import `Result` through `config_loader`, not `result`
- `src/tela/commands/start.py` is deprecated but still retained for testing and
  legacy wiring coverage
- `src/tela/shell/gateway.py` re-exports many `gateway_runtime` symbols for
  backward compatibility

This is not a correctness bug, but it keeps the dependency graph noisier than
 it needs to be.

### 5. Test safety net is already strong enough for controlled refactor

Verified in repository structure:

- `tests/shell/` — 34 files
- `tests/integration/` — 6 files
- `tests/repro/` — 18 files
- `tests/core/` — 12 files
- `tests/black_box/` — 1 file

Important adversary suites for future refactor work:

- `tests/shell/test_gateway.py`
- `tests/shell/test_downstream.py`
- `tests/shell/test_connect_cmd.py`
- `tests/shell/test_reload.py`
- `tests/integration/test_end_to_end.py`
- `tests/repro/test_runtime_boundary_immutability.py`
- `tests/repro/test_startup_coord_liveness.py`
- `tests/repro/test_connect_runtime_liveness.py`

## What This Project Does **Not** Need

The current evidence does **not** justify:

- a full rewrite,
- a new architecture layer,
- more protocols/interfaces for future flexibility,
- a repository/service/controller split,
- a broader dependency-injection framework,
- giant "unified" functions controlled by boolean flags.

If a simplification requires `is_admin`, `skip_db`, `is_reconnect`,
`is_manual_reenumeration`, or similar branching flags to merge distinct flows,
that merge should be rejected.

## Recommended Refactor Order

### Phase 0 — Protect behavior

Before touching runtime code:

1. keep the adversary suites above green,
2. run `uvx invar-tools guard --all` before and after each refactor slice,
3. prefer small deletions with immediate verification over one large rewrite.

### Phase 1 — Delete fake abstractions

First simplification targets:

- remove unused protocol types:
  - `EventEntryAdapter`
  - `ConvergencePolicyConsumer`
- collapse single-implementation protocol indirection where no second
  implementation exists:
  - `SingleServerConvergenceKernel`

Expected payoff:

- fewer conceptual layers,
- less reader indirection,
- simpler reload/downstream mental model.

### Phase 2 — Centralize state ownership

Target shape:

- `gateway_runtime.py` should be the obvious home for runtime facts,
- `gateway.py` should orchestrate lifecycle rather than also owning more
  singleton state than necessary,
- stateful shells should expose a small number of explicit mutation points.

This is a **state ownership reduction**, not an invitation to add more wrapper
 classes.

### Phase 3 — Shrink the oversized shell files

Priority order:

1. `downstream.py`
2. `connect_cmd.py`
3. `gateway.py`
4. `serve_cmd.py`

Important constraint: split by **concrete responsibility**, not by abstract
 layering. For example:

- downstream connection lifecycle,
- downstream recovery path,
- downstream registry access,
- connect bridge framing/transport,
- serve process lifecycle/watchers.

Do **not** split into extra interface packages or adapter forests.

### Phase 4 — Remove compatibility residue when safe

Candidates:

- direct `Result` imports from `tela.shell.result`
- trim `gateway.py` re-exports once callers no longer need them
- eventually retire `commands/start.py` when tests stop depending on legacy
  startup wiring

## Suggested Exit Criteria

The architecture refactor can be considered successful when most of the
 following are true:

- no shell file exceeds its guard limit,
- unused protocols/adapters are deleted,
- single-implementation indirections are collapsed,
- runtime state ownership is easier to explain in one page,
- `Result` import paths are direct and consistent,
- backward-compatibility exports are reduced,
- full guard and adversary tests pass.

## Non-Goals

This assessment explicitly does **not** recommend:

- flattening the whole project into one file,
- moving shell I/O logic into core,
- replacing tested separation with a boolean-driven mega-function,
- reworking public contracts without matching spec and test updates.

## Working Rule for Follow-Up Changes

Use this rule for each follow-up patch:

> Delete the abstraction first if it has no real second implementation, but do
> not merge distinct behaviors into a single branching monster.

That is the safest path to a smaller architecture here.
