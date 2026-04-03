# ADR-005: Gateway Runtime Is the Sole Readiness Authority

## Status

Accepted

## Context

The Full-B contract/readiness slice needs a frozen boundary so downstream phases
cannot drift readiness ownership into `tela connect` or other discovery
artifacts.

Existing approved design already separates concerns:

- lockfile = discovery truth only
- gateway runtime lifecycle snapshot / `GET /status` = lifecycle and readiness truth
- bridge registration via `POST /connect` = connection/lifecycle plumbing only

What remained insufficiently explicit was the negative boundary around the
bridge. Without that freeze, downstream planning could still reinterpret bridge
state, cached status, or local labels as readiness authority.

## Decision Drivers

- Preserve one readiness authority for downstream implementers
- Prevent readiness drift into `tela connect`
- Keep discovery and admission semantics separable
- Make planning language falsifiable for later phases

## Options Considered

### Option A: Gateway runtime as sole readiness authority
- **Mechanism**: Treat gateway runtime lifecycle plus `GET /status` as the only
  authoritative readiness source; require bridge code to relay/query that truth
  instead of creating local readiness state.
- **Pros**: Keeps authority singular, preserves existing lockfile/discovery
  split, prevents bridge-local readiness drift.
- **Cons**: Bridge flows may need an extra status read instead of inferring from
  local events.
- **Fails if**: Any downstream design lets `tela connect`, `POST /connect`, or
  lockfile contents stand in for readiness truth.

### Option B: Dual authority split between gateway runtime and bridge-local state
- **Mechanism**: Let bridge-side connection progress or cached labels supplement
  runtime readiness.
- **Pros**: May look simpler for bridge-local control flow.
- **Cons**: Competing truth sources, ambiguous admission semantics, lockfile and
  bridge events can be misread as readiness.
- **Fails if**: Different surfaces disagree about whether the gateway is ready.

## Decision

Choose **Option A**. [Proven] This matches the approved source-of-truth split
already documented in `docs/DESIGN.md` and `docs/ADR-004-gateway-startup-state-machine.md`.
I chose it because a single readiness authority is the only option that keeps
discovery, bridge lifecycle plumbing, and admission semantics from collapsing
into one another.

Normative freeze for downstream phases:

- gateway runtime lifecycle plus `GET /status` is the sole readiness authority
- `tela connect` must not create, own, or persist readiness state
- `tela connect` must not cache readiness truth as an authoritative substitute
- `tela connect` must not invent local lifecycle labels that compete with
  gateway runtime status
- `tela connect` readiness waiting must consult `GET /status` rather than rely
  on fixed sleep intervals or bridge-local lifecycle inference
- retries are authorized only when the gateway emits an explicit transient
  non-ready contract signal; degraded/non-ready status alone does not grant
  retry permission
- if authoritative runtime status remains degraded or otherwise non-ready past
  the bounded wait policy, `tela connect` must exit cleanly and boundedly
- `POST /connect` remains registration/lifecycle plumbing only
- lockfile remains discovery-only and is explicitly not readiness truth

## Consequences

- Downstream work must key readiness decisions from gateway runtime status, not
  bridge-local observations
- Bridge code may report or relay readiness facts, but it does not own them
- Planning and implementation text that assigns readiness ownership to the
  bridge is out of contract and must be rejected
