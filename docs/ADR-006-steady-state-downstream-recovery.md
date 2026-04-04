# ADR-006: Failure-Triggered Recovery for Steady-State Downstream Tool Calls

## Status

Accepted

## Context

`tela` currently assumes an already-connected downstream session at steady state.
That assumption breaks when an agent stays alive but sends no MCP requests for a
long interval.

Observed and documented current behavior:

- `call_tool` uses the cached downstream client handle directly and returns
  `DOWNSTREAM_UNAVAILABLE` when the handle is absent or the underlying client
  reports a not-connected failure.
- steady-state auto-reconnect today is event-driven from the downstream message
  handler, not from the tool-call path itself.
- the connection reaper removes idle upstream runtime connections after
  connection-type TTLs.
- `tela serve` may also auto-shutdown after its idle timeout when no active
  connections remain.

This means request silence is currently treated too much like departure, even
though silence is epistemically ambiguous: an agent may be busy with a long task,
may be paused, may have died, or may simply not need `tela` for several minutes.

Heartbeat-based presence would clarify that ambiguity, but the gateway cannot
rely on clients to send heartbeats. Therefore the steady-state tool-call path
must become self-healing without requiring any new client behavior.

## Decision Drivers

- Preserve tool availability after long idle periods without requiring client
  heartbeats
- Keep the healthy path fast; no per-call preflight probe
- Reuse existing downstream convergence and registry-update patterns instead of
  inventing a second recovery architecture
- Avoid duplicate side effects from unsafe automatic retries
- Keep failure semantics bounded and falsifiable

## Options Considered

### Option A: Failure-triggered per-server recovery plus a single safe retry
- **Mechanism**: attempt the downstream call immediately; only when the failure
  proves the client is locally disconnected before dispatch, reconnect that one
  server, re-enumerate through the existing single-server convergence path, then
  retry the same tool call once.
- **Pros**: zero steady-state probe cost; preserves existing `reload`
  convergence authority; works without heartbeat; keeps the recovery blast
  radius to one server.
- **Cons**: the first call after an idle disconnect becomes slower; requires a
  careful retry-eligibility classifier.
- **Fails if**: retry is attempted for ambiguous mid-flight failures and causes
  duplicate side effects, or if recovery bypasses the existing convergence path
  and leaves the registry/client handle out of sync.

### Option B: Preflight ping/probe before every call
- **Mechanism**: check downstream liveness before each tool call and reconnect
  proactively when the probe fails.
- **Pros**: can detect broken sessions before the real tool invocation.
- **Cons**: adds latency to every healthy call; duplicates liveness logic on the
  hot path; still does not answer whether the agent is actually gone.
- **Fails if**: steady-state latency becomes visibly worse for ordinary calls.

### Option C: Presence/lease protocol as the primary fix
- **Mechanism**: require clients to send heartbeat or lease-renewal messages so
  the gateway can distinguish idle from departed agents.
- **Pros**: improves presence semantics and runtime introspection.
- **Cons**: not enforceable for all agents; does not itself heal a broken tool
  call unless recovery is also implemented.
- **Fails if**: clients omit heartbeats or if availability still depends on
  strict heartbeat compliance.

## Decision

Choose **Option A**. I chose it because it solves the user-visible
availability problem without adding cost to every healthy call and without
depending on client behavior that the gateway cannot enforce.

Implementation status:

- downstream recovery policy: specified here, pending implementation follow-through
- reaper policy surface and defaults: implemented
  - runtime config exposes dedicated `reaper` settings
  - `tela serve` exposes CLI overrides with CLI precedence over config
  - `native_idle_ttl_seconds = 0` and `bridge_idle_ttl_seconds = 0` both disable
    idle reaping for that connection class
  - default `bridge_idle_ttl_seconds` is `900.0`

Normative boundary for this slice:

- request silence MUST be treated as **unknown presence**, not as authoritative
  client departure
- the gateway MUST NOT require heartbeat support to preserve steady-state tool
  availability
- `call_tool` MUST remain probe-free on the healthy path
- automatic retry MUST be limited to failures that prove the current downstream
  client is unusable before the tool call could have safely completed
- recovery MUST be **per target server**, not a global `connect_all`
- recovery MUST reuse the existing single-server reconnect/convergence pattern
  rather than inventing a second registry-update authority
- automatic retry MUST happen at most once per original tool call
- if recovery is exhausted or not eligible, the outward error remains
  `DOWNSTREAM_UNAVAILABLE`

## Consequences

- Healthy calls keep current latency characteristics.
- The first call after an idle disconnect pays a recovery cost.
- `tela` becomes resilient to silent-but-still-alive agents without claiming it
  can prove that those agents remained alive.
- Presence semantics remain intentionally weak: this ADR improves availability,
  not online/offline truth.

## Detailed Contract

### Scope

This ADR covers **downstream steady-state tool-call recovery** only.

In scope:

- `shell/downstream.py` tool-call path
- reuse of existing reconnect + enumeration + convergence behavior
- retry eligibility rules
- caller-visible latency/error contract

Out of scope:

- heartbeat or lease protocols
- changing `tela connect` client behavior
- redesigning idle shutdown semantics
- proving agent presence or absence

### Ownership and Module Boundaries

#### `shell/downstream.py`

Owns the recovery orchestration for steady-state calls.

Required shape:

- `call_tool` remains the public entry point
- a shared internal recovery primitive is extracted so both:
  - message-handler reconnect flow
  - call-triggered recovery flow
  use the same reconnect authority

Interface anchor for this slice:

```python
async def call_tool(
    server_name: str,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]: ...
```

Internal recovery primitive interface:

```python
async def _recover_server_client(
    server_name: str,
    *,
    deadline_monotonic: float,
) -> Result[None, TelaError]: ...
```

Contract for `_recover_server_client`:

- MUST be the single shared recovery authority used by both message-handler
  reconnect flow and call-triggered recovery flow
- MUST acquire or run under the per-server recovery lock before transport work
  begins
- MUST re-read runtime config after lock acquisition and fail with
  `details.config_missing = true` if the target server no longer exists
- MUST open transport, enumerate tools, and route convergence through
  `shell/reload.py`
- MUST NOT mutate `_clients` or registry state ad hoc outside the existing
  convergence path
- MUST return `Result(value=None)` only after the refreshed client/registry state
  is committed by convergence
- MUST return `Result(error=TelaError(...))` for every recovery failure path and
  set `TelaError.details.recovery_stage` accordingly

Caller rule after `_recover_server_client` returns success:

- the caller MUST re-read `_clients` / registry state rather than assuming a
  stale local handle remains valid

#### `shell/downstream_clients.py`

Remains the transport-opening authority. It continues to own:

- transport validation
- stdio/SSE/Streamable HTTP session creation
- downstream `tools/list` enumeration

It does **not** own recovery policy.

#### `shell/reload.py`

Remains the single-server convergence authority for accepted reconnect payloads.
Recovery must route fresh tool data through this module instead of mutating the
registry ad hoc from the call path.

#### `shell/connection_reaper.py`

Remains a garbage-collection mechanism for stale upstream runtime entries.
It is not upgraded into a presence detector by this ADR.

### Related Reaper Corrections

This ADR also standardizes the reaper policy surface because the current idle
failure chain is influenced by both downstream disconnects and upstream
connection cleanup.

#### Configuration exposure

Reaper settings MUST be externally configurable through **both**:

- runtime configuration file (`tela.yaml` / runtime config model)
- CLI flags on `tela serve`

The externally visible reaper settings are:

- `sweep_interval_seconds`
- `native_idle_ttl_seconds`
- `bridge_idle_ttl_seconds`

The runtime configuration model MUST expose these under a dedicated `reaper`
section so the config-file contract has a single obvious owner.

For implementation handoff, this means the runtime config schema needs an
explicit reaper-bearing field or nested section rather than ad hoc startup-only
wiring.

Illustrative config shape:

```yaml
reaper:
  sweep_interval_seconds: 60.0
  native_idle_ttl_seconds: 0      # disable native idle reaping
  bridge_idle_ttl_seconds: 900.0
```

Field expectations for the first implementation slice:

- `sweep_interval_seconds`: optional, default `30.0`, must be `>= 0`
- `native_idle_ttl_seconds`: optional, default `120.0`, must be `>= 0`
- `bridge_idle_ttl_seconds`: optional, default `900.0`, must be `>= 0`

CLI flags and config-file fields MUST describe the same semantics. If both are
provided, CLI values MUST take precedence over config-file values. The command
surface that owns startup wiring MUST document that precedence explicitly.

Illustrative CLI shape:

```bash
tela serve \
  --reaper-sweep-interval 60 \
  --reaper-native-ttl 0 \
  --reaper-bridge-ttl 300
```

#### Public disable semantics

`0` is a public contract value meaning **disable idle reaping for that
connection class**.

This applies equally to:

- `bridge_idle_ttl_seconds = 0`
- `native_idle_ttl_seconds = 0`

The gateway MUST NOT assign different disable semantics to bridge and native
idle TTLs.

TTL change semantics for the first implementation slice:

- changing a TTL value affects the next reaper sweep cycle; it does not trigger
  immediate connection cleanup or immediate connection resurrection
- setting either TTL to `0` prevents future idle-based cleanup for both existing
  and future connections of that class from the next sweep onward
- config reload may change the effective TTL policy, but the reaper remains the
  authority that observes and applies that policy on its normal sweep cadence

#### Surface distinction

The following controls MUST remain distinct in both code and docs:

- `--idle-timeout`: process-level idle shutdown for the whole gateway
- reaper TTL settings: per-connection stale/idle cleanup policy

`--idle-timeout` MUST NOT be treated as an alias or hidden override for reaper
TTL policy.

#### Verification additions for reaper policy

Implementation is incomplete unless tests also prove:

- native TTL `0` disables native reaping
- bridge TTL `0` disables bridge reaping
- CLI wiring can override reaper defaults
- config-file wiring can override reaper defaults
- docs distinguish gateway idle shutdown from per-connection reaping

### Recovery Eligibility

Automatic retry is allowed only for **recovery-eligible disconnect-class
failures**.

Eligible failure class:

- the target server has no active client handle in `_clients`
- the current client handle reports an explicit local not-connected / closed /
  uninitialized state before a valid downstream response could have been
  produced from that handle

Ineligible failure class:

- timeout after dispatch is plausibly underway
- connection reset / EOF / broken pipe where downstream execution may already
  have started
- downstream tool returned an ordinary application/tool error
- authorization, posture, or registry lookup failures upstream of transport

If classification is ambiguous, the call MUST fail rather than retry.

### Recovery Eligibility Contract

The first implementation slice MUST treat only the following conditions as
recovery-eligible without further inference:

| Condition source | Eligible | Rationale |
|---|---|---|
| `_clients[server_name]` has no active handle | Yes | No downstream dispatch can occur without a client handle. |
| `RuntimeError("Client is not connected. Use the 'async with client:' context manager first.")` surfaced by the FastMCP client | Yes | Current client library documents this as a disconnected local client state. |
| `RuntimeError("Server session was closed unexpectedly")` surfaced by the FastMCP client context | Yes | Current client wrapper converts closed session state into an explicit local runtime error. |
| `TimeoutError` / `asyncio.TimeoutError` | No | Dispatch may already be underway; duplicate side effects cannot be excluded. |
| `BrokenPipeError`, `ConnectionResetError`, EOF-like transport interruption after call submission | No | Mid-flight ambiguity; retry safety is not provable locally. |
| `McpError`, `ToolError`, or any downstream application/tool error payload | No | These are not liveness failures. |
| Any unknown exception class or unknown `RuntimeError` message | No | Unknown implies ambiguous; ambiguity fails closed. |

This classifier is intentionally conservative. Expanding the eligible set requires
new evidence from the concrete client transport stack and must be documented in a
follow-up ADR or spec update before implementation broadens retry scope.

The current classifier is intentionally coupled to the present FastMCP client
surface. If a future FastMCP upgrade changes these runtime error strings or
exception types, unknown variants MUST fail closed until the classifier is
updated explicitly.

### Recovery Sequence

For one original tool call targeting one downstream server:

1. Attempt the normal downstream call with the currently mapped server/tool.
2. If the call succeeds, return normally.
3. If the failure is not recovery-eligible, return the original failure as
   `DOWNSTREAM_UNAVAILABLE`.
4. If the failure is recovery-eligible:
   - serialize recovery for that server so concurrent callers do not stampede
   - after the per-server recovery lock is acquired, re-read runtime config for
     that server; if the server no longer exists in runtime config, stop and
     return `DOWNSTREAM_UNAVAILABLE` with `details.config_missing = true`
   - open a fresh client session for that server using the configured transport
   - enumerate the fresh tool set from that session
   - pass the fresh tool payload through the existing single-server reconnect
     convergence path
   - only after convergence accepts the reconnect result, retry the original
     tool call once
5. If the retry succeeds, return that result.
6. If recovery or the single retry fails, return `DOWNSTREAM_UNAVAILABLE`.

Any failure from the single retry is terminal for that original user call. The
implementation MUST NOT reclassify the retry failure and MUST NOT start a second
recovery attempt.

Clarification: "single safe retry" means **one original call attempt plus at
most one automatic retry after a successful recovery**. A second recovery or a
second retry for the same original user call is forbidden.

If convergence rejects the reconnect payload (for example due to
`TOOL_CONFLICT`), the recovered client handle is treated as unusable for this
request, the call does not proceed to retry, and the outward failure remains
`DOWNSTREAM_UNAVAILABLE` with rejection context in diagnostics.

### Concurrency Contract

- Recovery serialization is **per server**, not global for all servers.
- Ordinary calls to other connected servers MUST continue while one server is
  recovering.
- Concurrent calls to the same recovering server MAY wait behind the recovery
  lock and then use the refreshed client handle once recovery completes.
- A second automatic recovery attempt for the same original call is forbidden.

Locking rules:

- call-triggered recovery and message-handler-triggered recovery for the same
  server MUST share the same per-server recovery lock
- the per-server recovery lock map is owned by `shell/downstream.py`
- lock instances SHOULD be created lazily per server name and removed when that
  server is permanently removed from runtime config or `disconnect_all()` tears
  down downstream state
- `_registry_lock` remains the authority for reading/writing `_clients` and
  registry state, but MUST NOT be held while waiting on network or transport I/O
- recovery MUST NOT hold `_registry_lock` while awaiting a per-server recovery
  lock, opening a transport, or enumerating tools
- allowed ordering is:
  1. briefly read current client handle under `_registry_lock`
  2. release `_registry_lock`
  3. acquire the per-server recovery lock if recovery is needed
  4. re-check recovery eligibility after the lock is acquired so a stale caller
     does not reconnect a server that another path already healed
  5. perform reconnect/enumeration without `_registry_lock`
  6. reacquire `_registry_lock` only for swap/register phases routed through the
      existing convergence flow

Stale-caller and lock-wait behavior:

- the total recovery timeout budget for one original user call starts when the
  call is classified as recovery-eligible
- waiting to acquire the per-server recovery lock consumes that same timeout
  budget
- after acquiring the per-server recovery lock, a waiting caller MUST re-read:
  - runtime config for target-server existence and material config drift
  - `_clients` / registry state for whether a healthy client now exists
- if a healthy client now exists, the stale caller MUST skip reconnect work and
  proceed directly to the single allowed retry using the refreshed mapping
- if the server was removed or materially changed during lock wait, the stale
  caller MUST fail closed with `DOWNSTREAM_UNAVAILABLE`; use
  `details.config_missing = true` when the server no longer exists
- if the timeout budget is exhausted before the caller acquires the lock or
  before recovery completes, the call MUST fail with
  `details.recovery_stage = "recovery_timeout"`
- the first implementation slice defines no separate global cap on concurrent
  waiters for a single server; bounded behavior is provided by the shared
  per-call timeout budget rather than by queue-size policy

This preserves current global-registry ownership while avoiding deadlock between
ordinary callers, reconnect flows, and registry mutation.

### Config-Reload Concurrency Contract

Config reload wins over in-flight recovery.

- recovery MUST use the latest runtime-config view available after the per-server
  recovery lock is acquired
- if the target server no longer exists in runtime config, recovery MUST abort
  and return `DOWNSTREAM_UNAVAILABLE`
- if the target server's config changes materially during an in-flight recovery,
  the recovered handle from the stale config MUST NOT be swapped into `_clients`
- in any remove-or-change race between config reload and recovery, the reload
  path is authoritative and recovery MUST fail closed rather than revive stale
  transport state

This avoids TOCTOU ambiguity by preferring present config truth over recovery
work started from an older view.

### Caller-Visible Behavior

- Healthy path: unchanged latency envelope.
- Recovered path: caller may observe one slower call, but no new protocol step
  is exposed to the agent.
- Exhausted recovery: caller receives the same surface-level error family
  (`DOWNSTREAM_UNAVAILABLE`) rather than a second public recovery-specific API.

This keeps reconnection an internal concern while still making it observable via
latency and logs.

### Error Model

Outward error compatibility is preserved:

- success after recovery returns the ordinary tool payload
- exhausted recovery returns `TelaError(code="DOWNSTREAM_UNAVAILABLE", ... )`

Required diagnostic enrichment for exhausted recovery:

- whether recovery was attempted
- target server name
- whether failure occurred before retry or during retry

This ADR does **not** require a new public error code.

### Error Payload Contract

When recovery is attempted or considered, `TelaError.details` MUST use these
field names so diagnostics remain stable across implementations:

```python
{
    "server_name": str,  # required
    "recovery_attempted": bool,  # required
    "recovery_stage": (
        "not_attempted"
        | "reconnect_started"
        | "convergence_rejected"
        | "retry_failed"
        | "recovery_timeout"
    ),  # required when recovery_attempted is true
    "recovery_eligible": bool,  # required
    "config_missing": bool,  # optional when config lookup was not reached
    "underlying_error": str,  # required
}
```

Exact message text remains implementation-local, but these keys anchor the
diagnostic contract.

Illustrative exhausted-recovery payload:

```python
TelaError(
    code="DOWNSTREAM_UNAVAILABLE",
    message="Downstream server 'fs' is not connected",
    details={
        "server_name": "fs",
        "recovery_attempted": True,
        "recovery_stage": "recovery_timeout",
        "recovery_eligible": True,
        "config_missing": False,
        "underlying_error": "RuntimeError: Client is not connected. Use the 'async with client:' context manager first.",
    },
)
```

Illustrative convergence-rejected payload:

```python
TelaError(
    code="DOWNSTREAM_UNAVAILABLE",
    message="Downstream server 'fs' could not be recovered",
    details={
        "server_name": "fs",
        "recovery_attempted": True,
        "recovery_stage": "convergence_rejected",
        "recovery_eligible": True,
        "config_missing": False,
        "underlying_error": "TOOL_CONFLICT during on_server_reconnect",
    },
)
```

### Recovery Timeout Contract

Recovery MUST be bounded by a finite timeout budget.

- the budget applies to reconnect + enumeration + convergence for one recovery
  attempt
- timeout exhaustion MUST fail the original request as
  `DOWNSTREAM_UNAVAILABLE`
- timeout exhaustion MUST set `details.recovery_stage = "recovery_timeout"`
- the initial timeout budget for the first implementation slice is
  `15.0` seconds
- the timeout value is an internal operational constant for this slice, not a
  new public CLI/API surface

The `15.0` second budget is chosen to stay bounded while leaving headroom above
the repo's existing `10.0` second HTTP control-plane timeout
(`src/tela/commands/connect_cmd.py:HTTP_TIMEOUT_SECONDS`) for reconnect,
enumeration, and convergence work. Future adjustment is allowed only with test
evidence and an explicit doc update. The selected value SHOULD be justified by:

- existing downstream connect/initialize defaults in the client stack
- observed enumeration cost in focused tests
- enough headroom to avoid flaking on healthy reconnects without turning one
  recovery into an operator-invisible hang

Timeout scope clarifications:

- the `15.0` second budget applies per original call / per recovery attempt for
  one target server
- the budget includes lock wait, reconnect, initialization, enumeration, and
  convergence
- there is no separate global timeout shared across unrelated servers; server A
  recovery budget does not consume server B recovery budget
- this ADR defines no explicit tool-count cap; if unusually large tool
  enumeration exhausts the budget, the correct contract outcome is
  `recovery_timeout`

### Observability

The gateway MUST emit structured diagnostics for:

- recovery started
- recovery succeeded
- recovery rejected by convergence/conflict checks
- recovery exhausted

This is required so operators can distinguish:

- healthy calls
- self-healed calls
- permanently unavailable downstreams

Structured recovery diagnostics contract:

```python
{
    "event": (
        "downstream_recovery_started"
        | "downstream_recovery_succeeded"
        | "downstream_recovery_rejected"
        | "downstream_recovery_exhausted"
        | "downstream_recovery_classifier_unknown"
    ),  # required
    "level": ("INFO" | "WARNING"),  # required
    "server_name": str,  # required
    "tool_name": str | None,  # optional when tool context is unavailable
    "elapsed_ms": float,  # required; use 0.0 if emitted at start
    "recovery_stage": (
        "reconnect_started"
        | "reconnect_succeeded"
        | "convergence_rejected"
        | "retry_failed"
        | "recovery_timeout"
        | "classifier_unknown"
    ),  # required
    "underlying_error": str | None,  # required on WARNING events
    "request_id": str | None,  # optional when surrounding call path has one
}
```

Event semantics:

- `downstream_recovery_started` / `reconnect_started` => `level = "INFO"`
- `downstream_recovery_succeeded` / `reconnect_succeeded` => `level = "INFO"`
- `downstream_recovery_rejected` / `convergence_rejected` => `level = "WARNING"`
- `downstream_recovery_exhausted` with `retry_failed` or `recovery_timeout` =>
  `level = "WARNING"`
- `downstream_recovery_classifier_unknown` / `classifier_unknown` =>
  `level = "WARNING"`

When eligibility classification encounters an unknown exception class or an
unknown disconnect-shaped runtime error, the gateway MUST emit a `WARNING`
diagnostic containing the exception class name and a stable representation of the
error text so classifier drift can be detected and updated deliberately.

Suggested levels:

- `INFO` for recovery start/success
- `WARNING` for convergence rejection, timeout, or exhausted recovery

### Verification Expectations

The implementation slice is complete only when tests cover at least:

1. healthy call path remains single-attempt and probe-free
2. explicit not-connected failure triggers one per-server recovery
3. recovery reuses the existing reconnect/convergence path rather than direct
   registry mutation
4. concurrent calls to different servers are not blocked by one server's
   recovery
5. ambiguous transport failures do not auto-retry
6. exhausted recovery returns `DOWNSTREAM_UNAVAILABLE`
7. successful recovery is visible only as added latency / diagnostics, not as a
   new client protocol step
8. config-reload/remove races beat in-flight recovery and do not revive stale
   handles
9. structured recovery diagnostics include the required fields and severity
   class
10. stale callers waiting on the per-server recovery lock either reuse the
    recovered client, fail `config_missing`, or time out under the shared budget
11. TTL `0` policy changes take effect on the next reaper sweep for both existing
    and future connections of that class

Illustrative test shapes:

- `test_call_tool_healthy_path_does_not_trigger_recovery`
- `test_call_tool_without_client_handle_triggers_single_server_recovery`
- `test_runtimeerror_client_not_connected_is_recovery_eligible`
- `test_timeout_error_is_not_recovery_eligible`
- `test_concurrent_recovery_same_server_shares_one_reconnect_path`
- `test_recovery_for_server_a_does_not_block_healthy_server_b_calls`
- `test_convergence_rejection_returns_downstream_unavailable_without_retry`
- `test_recovery_timeout_returns_downstream_unavailable_with_timeout_stage`
- `test_config_reload_change_wins_over_inflight_recovery`
- `test_recovery_logs_emit_required_structured_fields`
- `test_stale_waiter_reuses_recovered_client_without_second_reconnect`
- `test_stale_waiter_removed_server_returns_config_missing`
- `test_lock_wait_consumes_recovery_timeout_budget`
- `test_reaper_ttl_zero_takes_effect_next_sweep_for_existing_connections`

## Implementation Handoff

**Design scope**: steady-state downstream tool-call recovery without heartbeat.

**Key deliverables**:

- this ADR as the recovery policy authority
- one shared internal recovery primitive owned by `shell/downstream.py`
- preserved reuse of `shell/downstream_clients.py` for transport/session open
  and `shell/reload.py` for convergence

**Suggested implementation order**:

1. extract the shared internal reconnect/recover primitive in `downstream.py`
   so message-handler reconnect and call-triggered recovery share one authority
2. add safe retry classification to the `call_tool` path
3. wire reaper settings through config model + `tela serve` CLI with matching
   semantics, including `native_idle_ttl_seconds = 0` disable behavior
4. add per-server recovery serialization and targeted tests
5. add diagnostics proving when recovery happened and why it stopped

**Watch for**:

- do not retry ambiguous mid-flight failures
- do not bypass reconnect convergence by mutating `_registry` directly from the
  call path
- do not pay probe cost on every healthy tool call

## Open Questions

- None for this ADR slice.
