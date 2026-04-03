# ADR-004: Deferred Gateway Startup State Machine

## Status

Deferred — future architecture only. This ADR records a possible Plan C model for a
later redesign. It does **not** change the approved current Plan A implementation
shape described in `docs/DESIGN.md`, and it must not be treated as a required
implementation step now.

Guardrail: the current Full-B contract/readiness slice explicitly excludes any new
public `shutting_down` runtime state, bridge retry logic keyed off
`shutting_down`, or broader teardown-state redesign. It also freezes the
admission boundary so `POST /mcp` remains the readiness-gated admission surface
while `POST /connect` remains registration/lifecycle plumbing only. If
shutdown-state expansion becomes necessary, it must be planned and approved as a
separate future architecture slice.

## Context

`docs/DESIGN.md` establishes the current source-of-truth split:

- lockfile = discovery truth only
- runtime status snapshot / `GET /status` = lifecycle and readiness truth
- downstream registry + reconnect/reload flow = convergence truth

That current Plan A shape remains the active design.

This ADR captures a deferred Plan C option: if startup sequencing later becomes
hard to reason about across discovery, admission, convergence, degradation, and
shutdown, the gateway may adopt an explicit runtime state machine to make those
boundaries first-class.

## Decision

If Plan C is ever adopted, the gateway runtime state should be expressed through
an explicit state machine with the states below. Until then, this ADR is design
inventory only.

### Explicit States

1. `preparing`
   - process exists, startup is in progress
   - bind target may be chosen but is not yet authoritative for client admission
   - downstream convergence is not yet complete

2. `bound_discoverable`
   - network bind and discovery publication succeeded
   - lockfile/endpoint discovery may exist
   - readiness is still not implied

3. `converging`
   - gateway is actively connecting downstreams, enumerating tools, or reconciling registry state
   - runtime truth comes from in-process status, not discovery artifacts

4. `ready`
   - startup invariants are satisfied for normal admission
   - runtime may accept ordinary client connections

5. `degraded`
   - process remains alive, but one or more readiness/convergence invariants are currently violated
   - operator-visible status must expose the degraded condition explicitly

6. `shutting_down`
   - runtime has begun intentional teardown
   - new work should not be admitted except shutdown-safe control paths

7. `stopped`
   - process is no longer serving
   - lockfile/discovery artifacts should no longer be considered live

### Transition Rules

Normal path:

`preparing -> bound_discoverable -> converging -> ready`

Allowed transitions:

- `preparing -> shutting_down`
  - startup aborted intentionally before successful bind/publication
- `bound_discoverable -> converging`
  - endpoint publication completed and convergence work begins
- `bound_discoverable -> degraded`
  - bind/discovery succeeded but required startup follow-up failed or timed out
- `converging -> ready`
  - required convergence invariants completed successfully
- `converging -> degraded`
  - convergence failed partially or exceeded policy threshold
- `ready -> converging`
  - explicit re-convergence event begins (for example reconnect/reload style recovery)
- `ready -> degraded`
  - active runtime loses a required invariant after readiness
- `degraded -> converging`
  - recovery work begins
- `degraded -> ready`
  - degraded condition clears without requiring full restart
- `preparing|bound_discoverable|converging|ready|degraded -> shutting_down`
  - operator stop, idle shutdown, or fatal policy decision initiates teardown
- `shutting_down -> stopped`
  - teardown completes

Disallowed interpretations:

- lockfile publication alone must never imply `ready`
- `ready` must not be inferred from successful bind alone
- a crash must not be modeled as a clean transition to `stopped`

## Source-of-Truth Boundaries

If Plan C is adopted, state ownership should remain split rather than collapsing
all truth into one artifact.

| Concern | Authoritative source | Not authoritative |
| --- | --- | --- |
| Discovery | lockfile / published endpoint metadata | readiness, convergence completion |
| Runtime state machine state | in-process runtime status snapshot | lockfile presence alone |
| Downstream convergence detail | downstream registry and convergence result records | discovery artifacts |
| Connection liveness | connection registry / session tracking | startup state name by itself |
| Process existence after crash | OS/process observation on restart | stale lockfile |

Rationale: this preserves the current Plan A source-of-truth split from
`docs/DESIGN.md` instead of redefining discovery as readiness. The state machine
would summarize lifecycle state, not replace the existing truth boundaries.

## Connection Admission Rules

If Plan C is adopted, admission policy should be state-dependent.

| State | Admission rule |
| --- | --- |
| `preparing` | reject ordinary client admission; startup is not yet discoverable-ready |
| `bound_discoverable` | allow discovery but reject or explicitly defer ordinary client admission until convergence policy says otherwise |
| `converging` | reject new ordinary admission, or admit only if protocol semantics explicitly support waiting/retry against non-ready runtime status |
| `ready` | admit ordinary client connections |
| `degraded` | default deny new ordinary admission; any exception must be explicit and operator-visible |
| `shutting_down` | deny new ordinary admission; allow only teardown-safe control operations if such a surface exists |
| `stopped` | no admission possible |

Rationale: admission must follow authoritative runtime state rather than lockfile
discoverability. This keeps discovery and readiness separate and avoids letting
connection registration become de facto admission proof.

## Interrupt / Crash Semantics

If Plan C is adopted, hard-interrupt and crash handling should be interpreted as
follows:

- intentional stop signal while running
  - transition to `shutting_down`
  - teardown remains best-effort
- interrupt during `preparing` or `converging`
  - may terminate startup without ever reaching `ready`
  - must not publish readiness as a side effect of partial startup
- abrupt crash
  - no guaranteed transition is observed in-process
  - the next observer should treat stale discovery artifacts as suspect until process liveness is revalidated
- restart after crash
  - begins again at `preparing`
  - must reconstruct runtime truth from live process state and downstream recovery, not from stale prior state labels

Rationale: crash semantics are observational, not transactional. A future state
machine must not pretend that every failure produces a clean terminal transition.

## Adoption Triggers

Plan C should be considered only if one or more of the following become true:

1. startup bugs repeatedly come from ambiguity between discoverable and ready
2. operator surfaces need a stable lifecycle vocabulary beyond ad hoc status fields
3. connection admission decisions need explicit lifecycle gating not well served by current status shape
4. recovery/degradation behavior becomes complex enough that implicit state derivation is error-prone
5. implementation evidence shows the current Plan A lifecycle model is no longer sufficient to explain or verify runtime behavior

Non-trigger:

- preference for a more formal model by itself is not enough; Plan A remains the approved current design until concrete failure evidence justifies change

## Consequences

Positive if adopted later:

- clearer operator vocabulary for lifecycle status
- explicit admission semantics tied to runtime truth
- easier distinction between discoverable, converging, ready, and degraded states

Trade-offs if adopted later:

- additional lifecycle coordination complexity
- more status transitions to test and document
- risk of duplicating rather than clarifying truth unless source boundaries remain strict

## Non-Goals

- redefining the approved current Plan A execution steps as mandatory now
- making lockfile discovery authoritative for readiness
- collapsing convergence truth into a single startup artifact
