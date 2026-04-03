# Full-B readiness open questions resolution

## Scope

This artifact resolves only the remaining plan-shaping questions for Full-B
readiness using the accepted architect review as the controlling source.

## Controlling sources

1. `docs/ADR-005-readiness-authority-boundary.md`
2. `docs/CONFIRMED-SURFACE-CONTRACT.md`
3. `docs/INTERFACES.md` §7.2, §7.2.1, §7.2.2
4. `docs/AGENT_INTERFACE.md` §6.1, §7
5. `docs/DESIGN.md` §Discovery and readiness

## Resolved questions

### 1. Readiness authority

- **Decision**: gateway runtime lifecycle plus `GET /status` is the sole readiness authority.
- **Why**: `docs/ADR-005-readiness-authority-boundary.md` freezes a single readiness authority and rejects bridge-local or discovery-derived substitutes.
- **Downstream contract obligation**:
  - Any bridge, agent, or operator contract that answers readiness questions must key from runtime status / `GET /status`.
  - No downstream step may treat lockfile discovery, successful `/connect`, or bridge-local progress as readiness proof.
- **Verification obligation**:
  - Check docs/tests/spec text for explicit `/status` readiness authority wording.
  - Reject any downstream deliverable that adds a second readiness source.

### 2. `/connect` boundary

- **Decision**: `POST /connect` remains registration/lifecycle plumbing only.
- **Why**: `docs/ADR-005-readiness-authority-boundary.md`, `docs/CONFIRMED-SURFACE-CONTRACT.md`, and `docs/INTERFACES.md` all freeze `/connect` as non-readiness plumbing.
- **Downstream contract obligation**:
  - `/connect` may register bridge presence and lifecycle context only.
  - `/connect` must not be described as readiness truth, readiness cache, or MCP admission proof.
- **Verification obligation**:
  - Check downstream docs/tests for any wording that derives readiness from `/connect`.
  - Reject any implementation or test plan that uses `/connect` success as ordinary MCP admission authorization.

### 3. Public lifecycle vocabulary in this slice

- **Decision**: this Full-B slice excludes any new public `shutting_down` state.
- **Why**: `docs/DESIGN.md` and `docs/INTERFACES.md` explicitly exclude a public `shutting_down` value from the current `/status` contract; `docs/ADR-004-gateway-startup-state-machine.md` is deferred and future-only.
- **Downstream contract obligation**:
  - Do not add `shutting_down` to `/status`, bridge retry policy, or admission semantics in this slice.
  - Do not invent any new public lifecycle label to stand in for teardown semantics.
- **Verification obligation**:
  - Reject downstream specs/tests that require `shutting_down` in current-slice status payloads.
  - Reject any retry or reconnect rule keyed off a new teardown-state label.

### 4. `/mcp` transient not-ready contract

- **Decision**: the transient not-ready `POST /mcp` contract is required and machine-readable.
- **Why**: `docs/CONFIRMED-SURFACE-CONTRACT.md`, `docs/INTERFACES.md` §7.2.1, `docs/AGENT_INTERFACE.md` §6.1, and `contracts/mcp_admission_transient_503.schema.json` all require gateway-authored machine-readable retry authorization during `warming`.
- **Downstream contract obligation**:
  - `POST /mcp` warming rejection must remain HTTP `503` with `ADMISSION_REJECTED_WARMING`.
  - Consumers must key retry authorization from machine-readable fields, not from bare `503` alone.
  - `gateway_state` remains `warming`; this contract does not authorize new lifecycle labels.
- **Verification obligation**:
  - Validate the JSON schema artifact exists and remains the canonical shape.
  - Reject downstream plans that model retry from HTTP status alone or that omit `code`, `transient`, `retry.authorized`, `retry.basis`, `retry.expectation`, or `gateway_state`.

## Surviving ambiguity / conflict check

### Apparent conflict: deferred ADR-004 vs current-slice contract

- **Observed text**: `docs/ADR-004-gateway-startup-state-machine.md` documents a future `shutting_down` state in a deferred Plan C state machine.
- **Controlling source for Full-B**: the current-slice contract is controlled by `docs/ADR-005-readiness-authority-boundary.md`, `docs/DESIGN.md`, and `docs/INTERFACES.md`, all of which explicitly exclude new public `shutting_down` state in this slice.
- **Resolution**: ADR-004 is design inventory for a future slice only; it is not authority for current Full-B deliverables.
- **Planner implication**: downstream work may cite ADR-004 only as future context, never as justification for adding current-slice lifecycle labels or admission behavior.

## Planner-ready summary

- `/status` is the only readiness authority.
- `/connect` is registration/lifecycle plumbing only.
- No new public `shutting_down` state belongs in this slice.
- `/mcp` warming rejection must stay machine-readable and retry-authorizing by contract.
