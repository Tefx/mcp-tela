# tela -- Design

## Design Boundary

`tela` is the concrete MCP gateway.

Its design scope is limited to:
- downstream server aggregation
- profile-based authorization
- concrete tool classification
- connection binding and audit

Out of scope:
- persona identity
- job/runtime workflow policy
- approval gating
- cross-run agent state

## System Shape

`tela` sits between:
- upstream MCP clients
- downstream MCP providers

and enforces one bound capability profile per connection.

## Runtime Architecture

### Two entry points

| Entry | Process Role | Transport |
|-------|-------------|-----------|
| `tela connect` | Client bridge (stdio ↔ HTTP) | stdio upstream, HTTP to server |
| `tela serve` | Gateway server | HTTP upstream, stdio/HTTP downstream |
| `tela stop` | Local operator control | lockfile discovery → SIGTERM |

### Process model

```text
MCP Client ──stdio──→ tela connect ──HTTP──→ tela serve ──stdio/HTTP──→ downstream servers
```

Multiple `tela connect` instances share one `tela serve`. Downstream servers
are spawned once by the server, not per-client.

### Service discovery

`tela connect` discovers the running server via `~/.tela/gateway.lock`.
If no server is running, it auto-starts one as a detached subprocess.

The lockfile is **discovery truth only**. Its contract is limited to:
- process identity (`pid`)
- bind target / endpoint discoverability (`host`, `port`)
- transport auth bootstrap (`token`)
- startup config ownership (`config_path`)
- instance metadata (`started_at`, `version`)

The lockfile is **not** readiness truth and **not** downstream convergence truth.
Its presence means only that a process advertised an endpoint; it does **not** mean:
- downstream servers are connected
- tool enumeration succeeded
- reconnect convergence completed
- `tela connect` may skip runtime lifecycle checks

Consumers of `tela connect` and `tela serve` MUST NOT treat lockfile discovery as
proof that downstreams are ready.

### Connection lifecycle

1. `tela connect` → `POST /connect` with canonical request key `server_name` → server registers connection and returns `connection_id`
2. Bridge active: stdio ↔ HTTP MCP session; the connect process is recorded as a client-neutral client attachment to the shared runtime
3. Additional `tela connect` processes attach to the same shared runtime and appear together in `tela status --clients`
4. `tela connect` exits or receives host transport EOF → `POST /disconnect` → server deregisters; the client side records `host_transport_closed` before provider-exit diagnostics
5. Last connection gone + idle timeout → server auto-shuts down (if auto-started)

Idle shutdown is a server process lifecycle event, not a command to kill an already-attached host transport. Request-level idle/recovery failures must either recover the next request or fail only that request while keeping the provider loop alive for later client messages. Recovery budgets are per event/request so unrelated events do not accumulate stale exhaustion state.

Operator surfaces preserve the same split: `tela status --probe` observes the current lockfile endpoint and does not cold-start an absent runtime, while `tela doctor` is passive without `--recover`; `tela doctor --recover` is the explicit mutation path that may cold-start and append recovery events.
### Discovery and readiness

Runtime lifecycle/readiness truth comes from the in-process runtime status snapshot
(and operator surfaces derived from it, such as `GET /status`), not from the
lockfile.

**Discovery-before-readiness cold-start semantics**: When `tela connect` performs
lockfile discovery (the default path without `--server`), the lockfile may be
written before downstream convergence completes. The endpoint is discoverable,
but readiness must be verified separately via `GET /status`. Consumers must not
assume endpoint discoverability implies MCP admission readiness.

Admission boundary in this slice:

- `POST /mcp` is the readiness-gated HTTP admission surface during convergence
- `POST /connect` remains registration/lifecycle plumbing for bridge presence only
- `POST /connect` must not become readiness truth, a readiness cache, or admission proof for ordinary MCP traffic

Source-of-truth split:

| Concern | Authoritative source | Explicitly not authoritative |
|---------|----------------------|------------------------------|
| Discovery | `~/.tela/gateway.lock` | runtime readiness, downstream convergence |
| Lifecycle / readiness | runtime status snapshot / `GET /status` | lockfile presence |
| Downstream convergence | downstream registry + reconnect/reload flow | lockfile presence |

Implications:
- readiness/lifecycle checks must read runtime status, not infer from discovery artifacts
- downstream sync state remains separate from lockfile discovery
- a discovered endpoint may still be starting, degraded, or disconnected from downstreams
- `tela connect` is a bridge/transport client and must not create or own readiness state, cached readiness truth, or local lifecycle labels
- bridge-side observations may trigger a runtime status query, but they must not become a second readiness authority
- reconnect handling may already hold fresh authoritative `raw_tools`; when that payload is present, downstream consumers MUST reuse it and MUST NOT blindly trigger a second enumeration
- this approved slice does **not** introduce a new public `shutting_down` lifecycle state in `/status`
- this approved slice does **not** add bridge retry or reconnect policy keyed off a `shutting_down` state label
- if teardown-state vocabulary becomes necessary later, it must be planned as a separate future architecture slice rather than folded into the current readiness/convergence work

**`tela connect` readiness behavior**:
- After `POST /connect` registration, the bridge polls `GET /status` for readiness
- Polling is bounded (`BRIDGE_READINESS_MAX_POLLS = 8`) so recovery keeps waiting through the discovered-gateway readiness window instead of treating lockfile publication as readiness
- If `GET /status` returns `state: "ready"`, the bridge proceeds to MCP forwarding
- If `GET /status` returns `state: "degraded"`, the bridge exits cleanly with error
- If bounded polls exhaust without reaching `ready`, the bridge exits cleanly
- The bridge must not retry indefinitely; bounded exit is required for non-ready authority

**`tela connect` runtime recovery**:

During MCP forwarding, transient connection errors may occur (connection refused,
reset, broken pipe, HTTP 503, readiness timeouts). The bridge implements bounded
recovery:

**Recovery classification** (`_is_recoverable_error`):
- Recoverable: HTTP connection errors, connection refused/reset/aborted, broken pipe,
  timeouts, HTTP 503, readiness query failures
- Non-recoverable: degraded state, unknown error types

**Recovery sequence**:
1. Detect recoverable error during readiness poll or forwarding
2. Check recovery attempts against `--max-recovery-attempts` (default: 3)
3. Re-discover gateway via lockfile (`_recover_gateway`)
4. Re-poll readiness at the recovered endpoint
5. Re-register via `POST /connect` with the same opaque bridge identifier carried in request key `server_name` and echoed back as `connection_id`
6. Resume forwarding MCP frames

**Session semantics**: The bridge maintains the same `connection_id` across
recovery cycles. Downstream sessions are scoped to the gateway runtime, so
bridge recovery does not reset downstream state. Upstream MCP clients should
treat bridge exit as session termination and re-initialize on reconnect.

**Bounded exit**: If recovery is exhausted or the error is non-recoverable,
the bridge exits cleanly with diagnostic logging to stderr. This matches the
interrupt/teardown contract: best-effort cleanup, never block process exit.

Authoritative freeze for downstream work:

- gateway runtime lifecycle plus `GET /status` is the sole readiness authority
- the bridge must not create or own readiness state
- the bridge must not cache readiness truth as its own source of authority
- the bridge must not invent local lifecycle labels that compete with runtime status
- the lockfile remains discovery-only and is explicitly not readiness truth
- any future change to these ownership rules requires a separate architecture slice / ADR

### Startup coordination

Startup coordination is distinct from single-server convergence.

- `connect_all` owns process/session startup across the configured server set.
- Startup coordination may enumerate multiple servers and publish the initial
  registry only after transport validation and conflict checks complete.
- Startup coordination remains outside the single-server convergence kernel in
  the startup-convergence refactor.
- Config-change orchestration may choose to call `disconnect_all` + `connect_all`
  as the safe whole-registry path; that policy decision is not part of the
  single-server convergence kernel.

### Convergence and reload

Runtime event-entry adapters are separate from convergence semantics.

| Boundary | Owns | Explicitly does not own |
|----------|------|-------------------------|
| Event-entry adapters | reconnect triggers, downstream `tools/list_changed`, watcher reload hooks, manual re-enumeration triggers, acquiring fresh `raw_tools` when required | resolve/register/conflict/rollback semantics |
| Single-server convergence kernel | `resolve_tools`, tentative register, conflict detection, rollback, structured convergence result | notify policy, audit policy, startup `connect_all`, trigger discovery |
| Orchestration / adapter policy | whether to notify upstream, whether to write audit warnings, when to choose whole-registry reconnect | core convergence mutation logic |

Freshness rules for `raw_tools`:

- Reconnect path: reuse the fresh `raw_tools` already enumerated after the new
  client handle is established.
- Reload/watcher/manual re-enumeration paths: perform a new enumeration before
  invoking the single-server convergence kernel.

The convergence kernel returns structured results so callers can apply notify and
audit policy without the kernel owning those policies directly.

### Idle shutdown

When a `tela serve` process is auto-started by `tela connect`, it monitors active
connections. After the last connection closes, an idle timer starts (default 300s).
If no new connections arrive before the timeout, the server shuts down.

- Configurable via `--idle-timeout` (default: 300 seconds)
- Applies to both auto-started and manually started servers
- Set to `0` to keep a server running indefinitely
- Shutdown is triggered by the idle manager's timeout, not by lack of downstream activity

### Connection reaper

Connections can leak when a client disconnects without sending `POST /disconnect`
(e.g., crash, network drop, or killed process). The connection reaper is a
background async task that periodically sweeps all runtime connections and removes
orphaned or idle entries.

**Activity tracking:**

Each `ConnectionContext` carries a `last_activity` field (ISO-8601 UTC string,
initially empty). The field is updated on every client interaction via
`touch_connection_activity()` — a thread-safe accessor in `gateway_runtime.py`.

Touch points:
- `_ensure_connection` (gateway.py) — on MCP session initialization
- `handle_tools_call` (upstream.py) — on every tool call
- `handle_tools_list` (upstream.py) — on every tools/list request
- `handle_connect` (http_routes.py) — on bridge connection registration

**Sweep behavior:**

The reaper runs a sweep cycle every `sweep_interval_seconds` (default: 30s).
Each sweep inspects all runtime connections:

1. **Session probe** (`conn_*` connections only): checks whether the upstream
   session is still registered. If the session is gone, the connection is reaped
   immediately via `cleanup_connection_by_id`.
2. **Staleness check** (all connection types): compares `last_activity` (or
   `connected_at` as fallback) against the connection-type-specific idle TTL.
   Connections that exceed the TTL are reaped.

After each reap, `idle_manager.decrement()` is called so the idle shutdown
timer can start when no connections remain.

**Configuration defaults (`ReaperConfig`):**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `sweep_interval_seconds` | 30.0 | How often the reaper runs |
| `native_idle_ttl_seconds` | 120.0 | Idle TTL for native (non-bridge) connections |
| `bridge_idle_ttl_seconds` | 900.0 | Idle TTL for bridge connections (0 = disabled) |

**Lifecycle wiring:**

- The reaper starts after `gateway_converge_startup` completes successfully.
- The reaper stops before `gateway_shutdown` tears down downstreams.
- Both `start()` and `stop()` are idempotent.

### Hard interrupt semantics (SIGINT/SIGTERM)

`tela connect` handles hard interrupts at three lifecycle stages:

**Autostart wait stage**: SIGINT terminates connect immediately without retrying
or waiting for timeout expiry. No attach or disconnect call is required.

**Attach loop stage**: SIGTERM terminates the active bridge loop immediately and
must not continue forwarding MCP frames after the stop signal. Best-effort
disconnect is attempted exactly once after the loop stops.

**Bridge teardown stage**: Hard interrupt during teardown still terminates connect
immediately; cleanup remains best-effort and must not block process exit.

### Token override modes

The bearer token protecting HTTP endpoints can be set via:

1. `--token` CLI option (highest precedence)
2. `TELA_BEARER_TOKEN` environment variable
3. Auto-generated on startup (default)

For remote clients, the lockfile token is read automatically. To skip lockfile
discovery and connect to an explicit server, use `--server host:port` which
requires `--token` or `TELA_BEARER_TOKEN` (lockfile discovery is disabled).

## Core Concepts

### Profiles

Profiles express capability ceilings only:

```yaml
capabilities:
  family: posture
```

### Tool classification

Concrete tool posture comes from:
1. explicit override
2. MCP annotations
3. server `default_posture`

### Authorization

Authorization is enforced in 3 per-call steps:

1. family admission
2. tool override check
3. posture ceiling comparison

Core comparison:

```text
tool_posture <= profile.capabilities[tool.family]
```

There is no separate workflow-policy layer in gateway authorization.

### Introspection

Built-in MCP tools:
- `tela_list_profiles` — MCP tool returning configured profiles with `profile_id`, `capabilities`, and `default`; **requires admitted session**
- `tela_list_providers` — MCP tool returning `provider_name`, caller-bound `profile_id`, `status`, `tool_prefix`, `tool_count`, and `tool_names`; **requires admitted session**

**Canonical builtin semantics:**
- Built-in tools require an admitted session/connection at call time; there is no builtin-session bypass
- Built-in tools accept strictly `{}` (empty object) input; `null`, omitted payloads, and additional properties are rejected
- Provider listing returns tools filtered by the calling connection's admitted `profile_id`
- Audit entries for builtin calls attribute to the calling connection's admitted `profile_id`
- Regression coverage: `tests/shell/test_gateway.py::test_streamable_http_builtin_call_requires_admitted_session`, `tests/shell/test_gateway.py::test_streamable_http_builtin_call_accepts_only_exact_empty_object`, `tests/shell/test_builtin_tools.py::test_handle_list_providers_uses_bound_connection_profile_in_token_mode`, `tests/integration/test_token_mode_initialize.py::test_handle_initialize_token_mode_rejects_missing_token_version_before_admission`

Operator surfaces (CLI/HTTP, not MCP):
- `tela profiles`, `tela status`, `tela connections`, `tela audit` — accessible via CLI commands or `GET /status`; paginated audit history is also exposed at `GET /operator/audit`

These are operator-facing surfaces (CLI/HTTP) and are **not** built-in MCP tool
surfaces. The `tela.` prefix is reserved for built-in surfaces.

## Module Boundaries

### `core/`

Pure gateway logic:
- models
- config validation
- token semantics
- classification
- enforcement
- conflict detection
- family mapping

### `shell/`

I/O and process edges:
- gateway lifecycle
- upstream/downstream MCP interaction
- config loading
- audit output
- reload orchestration
- lockfile management
- connection tracking
- connection reaping (idle/orphan cleanup)
- HTTP route handlers
- stdio-HTTP bridge
- gateway lifecycle state (`gateway_lifecycle.py`)
- connection lifecycle management (`connection_lifecycle.py`)
- MCP admission contract types (`mcp_admission_contract.py`)
- built-in tools for introspection (`builtin_tools.py`)
- downstream recovery coordination (`_downstream_recovery.py`)

#### Transports

Upstream (gateway → MCP clients):
- `http`: Streamable HTTP (MCP 2025-03-26+), used by `tela serve`
- stdio bridging is handled by `tela connect` (not the server itself)

Downstream (gateway → MCP servers):
- `command`: stdio subprocess
- `url`: Streamable HTTP (default) or SSE (`transport: sse`)

#### `transient_types.py`

**Responsibility:** Shared transient connection failure classifiers for gateway retry decisions.

**Public API:**
- `TRANSIENT_CONNECTION_EXCEPTIONS` — tuple of exception types classified as transient: `ConnectionRefusedError`, `ConnectionResetError`, `ConnectionAbortedError`, `BrokenPipeError`, `TimeoutError`
- `TRANSIENT_ERRNOS` — frozenset of numeric errno values for transient failures

**TimeoutError Policy Divergence (C4):** `TimeoutError` is treated as TRANSIENT in the Shell retry path because connection-timeout during gateway convergence is recoverable (the server may still be starting). This differs from Core layer classification where `TimeoutError` may indicate a downstream service timeout that should propagate as permanent failure. The divergence is intentional and reflects different failure modes at each layer.

| Layer | TimeoutError | Rationale |
|-------|-------------|-----------|
| Shell (connect) | TRANSIENT | Gateway may be warming |
| Core (MCP tool) | NON-TRANSIENT | Service timeout |

**Dependencies:** None (stdlib only). Consumed by `http_client.py` for transient classification.

### `commands/`

CLI entrypoints only:
- `connect_cmd.py`: client entry (auto-discover, auto-start, bridge)
- `serve_cmd.py`: server entry (HTTP gateway, lockfile, idle shutdown)
- `status_cmd.py`, `connections_cmd.py`, `audit_cmd.py`, `profiles_cmd.py`: query commands

#### `http_client.py`

**Responsibility:** Shared HTTP request retry/backoff skeleton — centralizes urllib request construction, transient retry, and 503 retry logic for connect bridge HTTP call sites.

**Public API:**
- `retry_http_request(...) -> Result[HTTPResponse, str]` — execute HTTP request with configurable retry on transient connection errors and 503 responses
- `_is_transient_url_error(exc: urllib_error.URLError) -> bool` — classify URLError as transient (connection refused/reset/broken pipe) or non-transient

**Ownership:** Owns only request/retry/backoff/result skeleton. Response parsing, bearer header construction, session management, and error semantics remain caller responsibilities.

**Dependencies:**
- Upstream: `urllib.request`, `urllib.error`, `http.client`, `time`
- Downstream: consumed by `connect_cmd.py` for `_post_mcp_message` and `_post_json` transient retry paths

## Auth Layers

| Layer | Mechanism | Purpose |
|-------|-----------|---------|
| Lockfile bearer token | Auto-generated per server instance | Protects HTTP endpoints |
| Config `auth.mode: token` | CapabilityToken with HMAC | Verifies canonical token fields (including explicit `token_version`) and binds connection to canonical `profile_id` |
| Config `auth.mode: open` | No token needed | Binds the connection to one explicit local default `profile_id` |

Both layers are independent and apply simultaneously:

- **Bearer token** (lockfile or `--token`/`TELA_BEARER_TOKEN`): protects the HTTP transport layer
- **Config `auth.mode`** (open/token): controls MCP-level profile binding via CapabilityToken
- You can have `auth.mode: open` (no CapabilityToken) and still require the bearer token for HTTP access
- You can override the bearer token with `--token` without changing profile authorization

Canonical contract note:

- `../opifex` owns the shared CapabilityToken and `_meta` contracts
- `token_version` is **explicit and required** at admission; no default or fallback
- token-mode binding uses canonical `profile_id`; legacy shared token aliases are rejected fail-closed
- `tela` is the verifier/enforcer and audit sink for those shared surfaces, not a second contract authority

## Ownership Rules

1. `core/` owns authorization semantics.
2. `shell/` owns transport, process effects, and service lifecycle.
3. CLI commands delegate; they do not define authorization rules.
4. tela profiles remain capability-only.

## Resolved Tool Model

`ResolvedTool` is the gateway's canonical tool representation after family mapping and classification. It includes both processed fields and passthrough metadata from downstream servers.

**Core fields:**
| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Tool name (unique across all servers) |
| `server_name` | str | Source downstream server |
| `family` | str | Assigned family for authorization |
| `posture` | Posture \| None | Computed posture level |
| `schema_` | dict | Tool input schema |
| `description` | str | Tool description |

**Metadata passthrough fields:**
| Field | Type | Description |
|-------|------|-------------|
| `annotations` | dict \| None | MCP tool annotations (`readOnlyHint`, `destructiveHint`, etc.) |
| `title` | str \| None | Human-readable tool title |
| `output_schema` | dict \| None | Tool output schema if provided |

## Session Capture and Notification Forwarding

The gateway implements MCP `notifications/tools/list_changed` forwarding from downstream servers to upstream clients.

**Session Capture:**
- Upstream MCP sessions are captured during handler registration in `gateway.py`
- Stored in a thread-safe registry (`connection_id` → session)
- Sessions are released on disconnect

**Notification Flow:**
1. Downstream server sends `notifications/tools/list_changed`
2. Gateway triggers hot reload re-enumeration via `on_tools_changed()`
3. On successful reload, notifications are broadcast to all captured upstream sessions
4. Each notification is sent via `session.send_tool_list_changed()`

**Fallback behavior:**
- If no session is captured (e.g., stdio transports), notifications are skipped silently
- Failed notifications remove stale sessions from the registry

## ServerConfig Instructions Field

The `instructions` field in `ServerConfig` controls how downstream server instructions are appended to the upstream server's instructions.

**Three modes:**

| Value | Behavior |
|-------|----------|
| `None` (default) | Passthrough: use downstream server's instructions if available |
| `False` | Suppress: exclude this server's instructions entirely |
| `str` | Override: use the provided string instead of downstream instructions |

**Merge semantics:**
1. Gateway instructions are emitted first.
2. Downstream server sections are appended in configured order.
3. When a downstream section is appended and tools are known, an `Available tools:` list is appended inside that server's section.

**Merged output format:**
```markdown
## ServerName

<instructions or override>

Available tools:
- tool_1
- tool_2
```

**Conflict handling:**
- Runtime composition is append-only and does not perform semantic conflict resolution.
- Contradictory downstream text is preserved as appended text.
- Mitigate contradictions by suppressing a section, providing a per-server replacement string, or revising contract/docs explicitly.

## Invariants

- one connection binds to one profile
- profile ceilings are family-based
- no tool override may elevate access beyond `capabilities[family]`
- classification is concrete-provider aware
- unclassified access is handled conservatively
- approval semantics do not appear in gateway profiles
- built-in MCP tools owned by tela are `tela_list_profiles` and
  `tela_list_providers`
- each server instance stamps audit entries with a unique `instance_id`
- tool metadata (`annotations`, `title`, `output_schema`) is preserved from downstream through upstream

## Shell Module Responsibilities

### `result.py`

**Responsibility:** Canonical `Result[T, E]` type for all shell I/O boundaries.

**Public API:**
- `Result(Generic[T, E])` — frozen dataclass with `.value`, `.error`, `.is_ok`, `.is_err`

**Ownership:** Stateless; defines a type only.

**Dependencies:** None (stdlib only).

**Concurrency:** Immutable (frozen dataclass); inherently thread-safe.

---

### `config_loader.py`

**Responsibility:** Shell-level config file I/O — reads YAML from disk, delegates parsing/validation to `core.config`, and returns a runtime-ready `TelaConfig`.

**Public API:**
- `load_config(path: Path | None = None, default_profile: str | None = None) -> Result[TelaConfig, str]`

**Ownership:**
- Reads: filesystem (`tela.yaml`), `os.environ` (for `${VAR}` expansion).
- Mutates: nothing. Returns a fresh `TelaConfig` on each call.

**Dependencies:**
- Upstream (reads from): `yaml`, `os.environ`, filesystem.
- Downstream (delegates to): `tela.core.config.parse_config`, `validate_config`, `resolve_open_mode_default_profile`.
- Re-exports: `Result` from `tela.shell.result`.

**Concurrency:** Stateless function; safe to call from any thread. File reads are not locked — concurrent calls reading the same file are safe (read-only).

---

### `gateway_runtime.py`

**Responsibility:** Locked mutable runtime state for the gateway process. All public accessors follow a strict boundary policy: DATA READ returns deep-copied snapshots, OPERATION acquires the lock and performs work on the live service without leaking references, WRITE acquires the lock for the full mutation.

**Public API:**

| Function | Signature | Kind |
|----------|-----------|------|
| `get_runtime_config` | `() -> Result[TelaConfig \| None, str]` | DATA READ |
| `set_runtime_config` | `(config: TelaConfig \| None) -> None` | WRITE |
| `is_runtime_running` | `() -> Result[bool, str]` | DATA READ |
| `get_runtime_connections_snapshot` | `() -> Result[list[ConnectionContext], str]` | DATA READ |
| `add_runtime_connection` | `(ctx: ConnectionContext) -> None` | WRITE |
| `remove_runtime_connection` | `(connection_id: str) -> Result[bool, str]` | WRITE |
| `clear_runtime_connections` | `() -> None` | WRITE |
| `set_runtime_running` | `(running: bool) -> None` | WRITE |
| `increment_tool_calls` | `() -> None` | WRITE |
| `get_runtime_secrets` | `() -> Result[list[str], str]` | DATA READ |
| `set_runtime_secrets` | `(secrets: list[str]) -> None` | WRITE |
| `set_runtime_total_tool_calls` | `(count: int) -> None` | WRITE |
| `get_runtime_status_snapshot` | `() -> Result[RuntimeStatusSnapshot, str]` | SNAPSHOT |
| `get_expected_bearer_token` | `() -> Result[str \| None, str]` | DATA READ |
| `is_upstream_server_initialized` | `() -> Result[bool, str]` | OPERATION |
| `get_upstream_http_app` | `() -> Result[Starlette, str]` | OPERATION |
| `get_upstream_log_level` | `() -> Result[str, str]` | OPERATION |
| `with_upstream_server` | `(fn: Callable[[FastMCP], T]) -> Result[T, str]` | OPERATION (test-only) |
| `set_upstream_server` | `(server: FastMCP \| None) -> None` | WRITE |
| `touch_connection_activity` | `(connection_id: str, timestamp: str) -> Result[bool, str]` | WRITE |
| `get_upstream_server` | `() -> None` | Removed (raises RuntimeError) |
| `capture_session` | `(connection_id: str, session: UpstreamSession) -> Result[None, str]` | WRITE |
| `release_session` | `(connection_id: str) -> Result[None, str]` | WRITE |
| `get_captured_session` | `(connection_id: str) -> Result[UpstreamSession, str]` | OPERATION |
| `get_connection_id_for_session` | `(session: UpstreamSession) -> Result[str, str]` | OPERATION |
| `get_session_registry_snapshot` | `() -> Result[dict[str, UpstreamSession], str]` | DATA READ |
| `clear_session_registry` | `() -> None` | WRITE |
| `get_runtime_reaper` | `() -> Result[Any, str]` | OPERATION |
| `set_runtime_reaper` | `(reaper: Any) -> None` | WRITE |
| `get_runtime_converge_event` | `() -> Result[asyncio.Event \| None, str]` | OPERATION |
| `set_runtime_converge_event` | `(event: asyncio.Event \| None) -> None` | WRITE |

**Types:**
- `GatewayRuntime` — mutable dataclass holding all runtime fields.
- `RuntimeStatusSnapshot` — frozen dataclass for atomic status reads.
- `RuntimeTruthContract`, `RuntimeTruthPlane` — declarative source-of-truth contracts.

**Ownership:**
- Mutates: `_runtime` (module-level `GatewayRuntime` singleton).
- Reads: `_runtime` fields under `_runtime_lock`.

**Dependencies:**
- Upstream: `tela.core.models` (ConnectionContext, TelaConfig), `mcp.server.fastmcp` (FastMCP), `starlette` (Starlette).
- Downstream: consumed by `gateway.py`, `http_routes.py`, `upstream.py`, `reload.py`, `connection_reaper.py`.

**FastMCP Translation Boundary:**

FastMCP appears under multiple authorities. This section reconciles those authorities.

| Authority Layer | Value | Role |
|-----------------|-------|------|
| Package declaration | `fastmcp>=2.0.0` (pyproject.toml) | Package distribution name for dependency management |
| Runtime import authority | `from mcp.server.fastmcp import FastMCP` (gateway.py, gateway_runtime.py) | Internal tela shell import path — the canonical import used within tela's shell modules |
| Manifest/header authority | Implementation-agnostic (surface_instructions.py) | User-facing docs must not prescribe an import path — they describe capability, not implementation |

**Authority decision (AUTH-MCP-FASTMCP):**
- The `fastmcp` package provides FastMCP through both `from fastmcp import FastMCP` (public API) and `from mcp.server.fastmcp import FastMCP` (internal path in FastMCP v2+).
- Tela's shell modules use `from mcp.server.fastmcp import FastMCP` as the internal implementation path.
- This internal path is correct for FastMCP v2+ and does not indicate a missing dependency.
- Manifests, instructions, and user-facing docs must not claim that `from fastmcp import FastMCP` is the canonical tela import — runtime uses `mcp.server.fastmcp`.
- Tests that import FastMCP for fixture/test purposes may use either path depending on context; runtime code uses `mcp.server.fastmcp`.

**Concurrency:** All public accessors acquire `_runtime_lock` (`threading.RLock`). The lock is reentrant. Returned snapshots share no mutable state with the runtime. The `FastMCP` reference never escapes the lock — only operation results are returned.

---

### `gateway.py`

**Responsibility:** Gateway lifecycle orchestration — start (load config, connect downstreams, create upstream MCP server, wire handlers), shutdown (disconnect downstreams, release sessions), and runtime status/connections queries.

**Public API:**
- `GatewayStartupConfig` — frozen dataclass: `transport`, `port`, `auth_mode`, `default_profile`, `host`.
- `bind_gateway_startup(runtime: RuntimeBindingContract, config: TelaConfig | None = None) -> Result[GatewayStartupConfig, str]`
- `gateway_start(config: GatewayStartupConfig, tela_config: TelaConfig | None = None, tool_lists: dict | None = None, expected_bearer_token: str | None = None) -> Result[None, str]`
- `gateway_shutdown() -> Result[None, str]`
- `gateway_status() -> Result[GatewayStatus, str]`
- `gateway_connections() -> Result[list[ConnectionContext], str]`
- `gateway_reload_config_from_disk(config_path: Path, default_profile: str | None) -> Result[None, str]`

**Ownership:**
- Mutates: `_runtime` (via `gateway_runtime` accessors) during start/shutdown.
- Reads: `_runtime` for status queries; downstream registry for tool/connection state.
- Wires: upstream MCP handlers (`_wire_upstream_handlers`), HTTP routes (`_register_http_routes`), profiles resource (`_register_profiles_resource`), reload notifications (`_wire_reload_notifications`).

**Dependencies:**
- Upstream: `tela.core.models`, `mcp.server.fastmcp`, `starlette`.
- Downstream (delegates to): `downstream.connect_all/disconnect_all`, `upstream.handle_*`, `http_routes.handle_*`, `audit.audit_init/audit_close`, `config_loader.load_config`, `surface_instructions.*`, `reload.set_notify_callback`.


**Concurrency:** Startup and shutdown are single-threaded (called once from CLI entry). HTTP route handlers run in the Starlette/uvicorn event loop and acquire `_runtime_lock` for state access. The `_ensure_connection` callback runs per-request under the MCP server's handler context.

---

### `downstream.py`

**Responsibility:** Downstream server management — connect/disconnect lifecycle, tool call forwarding, event-entry adapters for reconnect and `tools/list_changed` notifications, and module-level client/registry ownership.

**Public API:**
- `connect_all(servers: dict[str, ServerConfig], tool_lists: dict | None = None) -> Result[None, str]`
- `disconnect_all() -> Result[None, str]`
- `call_tool(server_name: str, tool_name: str, arguments: dict) -> Result[dict, TelaError]`
- `get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]`
- `get_tool_server(tool_name: str) -> Result[str | None, str]`
- `get_server_instructions() -> Result[dict[str, str], str]`
- `re_enumerate(server_name: str) -> Result[list[ResolvedTool], str]` — **Supported public surface** (SURFACE-REENUMERATE resolved)
- `get_registry() -> DownstreamRegistry`

**Ownership:**
- Mutates: `_clients` (dict of connected client handles), `_server_instructions` (dict of server instructions), `_registry` (DownstreamRegistry singleton).
- All mutations guarded by `_registry_lock` (`asyncio.Lock`).

**Dependencies:**
- Upstream: `tela.core.conflict.detect_conflicts`, `tela.core.family.resolve_tools`, `tela.core.models`.
- Downstream (delegates to): `downstream_clients._open_client_for_server`, `downstream_clients._enumerate_tools`, `downstream_registry.DownstreamRegistry`.
- Cross-module: `reload.on_tools_changed`, `reload.on_server_reconnect` (lazy imports to avoid cycles).

**Concurrency:** `_registry_lock` is an `asyncio.Lock` protecting `_clients`, `_registry`, and `_server_instructions`. All connect/disconnect/re-enumerate operations acquire this lock. `call_tool` acquires the lock briefly to look up the client handle, then releases before the downstream RPC call. Event-entry adapters (`_handle_reconnect`, `_handle_tools_list_changed`) also acquire the lock.

#### ADR-006 Downstream Recovery Behavior

**Scope:** Steady-state tool-call recovery for transient downstream disconnects.

Recovery is failure-triggered: `call_tool` attempts the downstream call immediately; only when the failure proves the client is locally disconnected before dispatch does recovery activate. Recovery is per-server, with one automatic retry maximum.

**Recovery eligibility classifier** (`_is_recovery_eligible_exception`):
- Eligible: `RuntimeError("Client is not connected...")` or `RuntimeError("Server session was closed unexpectedly")`
- Ineligible: `TimeoutError`, `BrokenPipeError`, `ConnectionResetError`, or any downstream application error
- Unknown exceptions fail closed (no retry)

**Recovery sequence** (`_recover_server_client`):
1. Acquire per-server recovery lock (`_recovery_locks`) to serialize concurrent recovery for the same server
2. Re-read runtime config after lock acquisition; fail with `details.config_missing=true` if server removed
3. Open fresh client session via `_open_client_for_server`
4. Enumerate tools via `_enumerate_client_tools`
5. Route through single-server convergence kernel (`on_server_reconnect`)
6. On convergence success: swap new client into `_clients`, close old handle best-effort
7. Return to caller for single retry

**Timeout budget:** 15 seconds total (`_RECOVERY_TIMEOUT_SECONDS`), covering lock wait, reconnect, enumeration, convergence, and the single retry. Timeout exhaustion sets `details.recovery_stage="recovery_timeout"`.

**Locking rules:**
- Per-server recovery locks are created lazily per server name
- `_registry_lock` is released before waiting on recovery locks or performing I/O
- Re-acquired only for swap/register phases through convergence path
- Locks are pruned when server is permanently removed or `disconnect_all()` tears down state

**Error details keys** (`TelaError.details`):
- `server_name`: required
- `recovery_attempted`: required (bool)
- `recovery_eligible`: required (bool)
- `recovery_stage`: one of `"not_attempted"`, `"reconnect_started"`, `"reconnect_succeeded"`, `"convergence_rejected"`, `"retry_failed"`, `"recovery_timeout"`, `"classifier_unknown"`
- `config_missing`: optional, true when server removed from runtime config during recovery
- `underlying_error`: required, string representation of original failure

**Structured recovery diagnostics** (`_emit_recovery_diagnostic`):
Events: `downstream_recovery_started`, `downstream_recovery_succeeded`, `downstream_recovery_rejected`, `downstream_recovery_exhausted`, `downstream_recovery_classifier_unknown`
Required fields: `event`, `level` (INFO/WARNING), `server_name`, `tool_name` (optional), `elapsed_ms`, `recovery_stage`, `underlying_error` (optional for INFO), `request_id` (optional)

**Shared recovery primitive:** Both message-handler reconnect flow and call-triggered recovery flow use `_recover_server_client` as the single recovery authority. This ensures consistent convergence semantics regardless of trigger source.

---

### `downstream_clients.py`

**Responsibility:** Transport-level client lifecycle primitives — opening stdio/SSE/Streamable HTTP sessions, transport mode validation, and tool enumeration via MCP `tools/list`.

**Public API:**
- `_ClientHandle` — dataclass: `session: ClientSession`, `stack: AsyncExitStack`, `instructions: str | None`.
- `_validate_transport_mode(server_name: str, server_config: ServerConfig) -> Result[None, str]`
- `_open_client_for_server(server_name: str, server_config: ServerConfig, message_handler: MessageHandlerFnT | None = None) -> Result[_ClientHandle, str]`
- `_enumerate_tools(session: ClientSession) -> Result[list[dict], str]`

**Ownership:** Stateless — creates and returns client handles. Does not own module-level state.

**Dependencies:**
- Upstream: `mcp.client.session`, `mcp.client.stdio`, `mcp.client.sse`, `mcp.client.streamable_http`.
- Downstream: none. Consumed by `downstream.py`.

**Concurrency:** Stateless functions; each call creates its own `AsyncExitStack`. Safe to call concurrently (e.g., `asyncio.gather` in `connect_all`).

---

### `downstream_registry.py`

**Responsibility:** In-memory registry of resolved tools from downstream servers. Provides lookup by tool name and server name, snapshot/restore for atomic rollback during convergence.

**Public API:**
- `DownstreamRegistry` class:
  - `register(server_name: str, tools: list[ResolvedTool]) -> None`
  - `unregister(server_name: str) -> None`
  - `get_all_tools() -> dict[str, list[ResolvedTool]]`
  - `get_tool_server(tool_name: str) -> str | None`
  - `get_tool(tool_name: str) -> ResolvedTool | None`
  - `snapshot() -> tuple[dict[str, list[ResolvedTool]], dict[str, str]]`
  - `restore(snap: ...) -> None`
  - `clear() -> None`

**Ownership:**
- Mutates: `_tools_by_server` (server→tools mapping), `_tool_to_server` (tool→server flat lookup).
- Registry keys are final exposed upstream names (`ResolvedTool.name`).

**Dependencies:**
- Upstream: `tela.core.models.ResolvedTool`.
- Downstream: none. Consumed by `downstream.py` and `reload.py`.

**Concurrency:** Not internally synchronized. All access is externally guarded by `downstream._registry_lock` (`asyncio.Lock`).

---

### `reload.py`

**Responsibility:** Hot reload orchestration — single-server convergence kernel (resolve/register/conflict/rollback), config-change handling (`on_config_changed`), and upstream notification dispatch after successful updates.

**Public API:**
- `set_notify_callback(callback: NotifyCallback | None) -> Result[None, str]`
- `on_tools_changed(server_name: str, server_config: ServerConfig, new_tool_list: list[dict]) -> Result[None, str]`
- `on_server_reconnect(server_name: str, server_config: ServerConfig, tool_list: list[dict]) -> Result[None, str]`
- `on_config_changed(new_config: TelaConfig) -> Result[None, str]`

**Types:**
- `SingleServerConvergenceResult` — frozen dataclass with `disposition`, `trigger`, `server_name`, `rollback_applied`, `resolved_tool_names`, `conflicts`. (Note: this was previously an interface protocol; now a concrete result type.)
- `ConvergenceConflictNote` — frozen dataclass with `tool_name`, `servers`.
- `ConvergenceTrigger` — literal type: `reconnect | reload | watcher | manual_reenumeration`.
- `NotifyCallback` — `Callable[[str], Awaitable[None]]` for upstream notification dispatch.

**Ownership:**
- Mutates: downstream registry (via convergence kernel), runtime config (via `set_runtime_config`).
- Owns: `_notify_callback` (module-level callback reference), `_converge_single_server_update` function (single-server convergence logic).

**Dependencies:**
- Upstream: `tela.core.conflict`, `tela.core.family`, `tela.core.models`.
- Cross-module: `downstream._registry_lock`, `downstream.get_registry`, `downstream.connect_all/disconnect_all`, `gateway_runtime.get_runtime_config/set_runtime_config`, `audit.audit_write/build_audit_entry`.

**Concurrency:** Convergence kernel acquires `downstream._registry_lock` for the full resolve/register/conflict/rollback cycle. `_notify_callback` is a module-level reference set once during startup and cleared during shutdown — no lock protects it (single-writer pattern).

---

### `upstream.py`

**Responsibility:** Upstream MCP handlers (initialize, tools/list, tools/call) with enforcement, session capture/notification for `tools/list_changed`, and profile listing.

**Public API:**
- `handle_initialize(client_info: dict) -> Result[ConnectionContext, str]`
- `handle_tools_list(connection: ConnectionContext) -> Result[list[dict], str]`
- `handle_tools_call(connection: ConnectionContext, tool_name: str, arguments: dict) -> Result[dict, TelaError]`
- `handle_profiles_list() -> Result[list[dict], str]`
- `capture_session(connection_id: str, session: UpstreamSession) -> Result[None, str]`
- `release_session(connection_id: str) -> Result[None, str]`
- `get_captured_session(connection_id: str) -> Result[UpstreamSession, str]`
- `get_connection_id_for_session(session: UpstreamSession) -> Result[str, str]`
- `find_connection_for_session(session: UpstreamSession, connections: list[ConnectionContext]) -> Result[ConnectionContext, str]`
- `notify_tools_changed(connection: ConnectionContext, tools_digest: str) -> Result[None, str]`
- `resolve_initialize_profile_binding(...) -> Result[InitializeProfileBinding, str]`

**Types:**
- `UpstreamSession` — runtime-checkable protocol with `send_tool_list_changed()`.
- `InitializeContext` — frozen dataclass with `connection_metadata`.

**Ownership:**
- Session registry is owned by `gateway_runtime.py` (not this module); delegates to `capture_session`/`release_session`/`get_captured_session` accessors.
- Mutates: runtime connections (via `add_runtime_connection`), tool call counter (via `increment_tool_calls`).
- Reads: runtime config, runtime secrets, downstream registry.

**Dependencies:**
- Upstream: `tela.core.models`, `tela.core.token.resolve_token_init_binding`.
- Cross-module: `downstream.call_tool/get_all_tools/get_registry`, `gateway_runtime.*`, `upstream_utils.*`, `idle_shutdown.get_idle_manager`.

**Concurrency:** Session registry is protected by `gateway_runtime._runtime_lock`. Session capture uses first-binding semantics — re-capture of the same session is idempotent; a different session on an already-bound connection_id is rejected. All runtime state access goes through locked `gateway_runtime` accessors.

---

### `upstream_utils.py`

**Responsibility:** Pure/synchronous helpers for upstream tool filtering, `_meta` stripping/holding for audit correlation, and enforcement bridging. Extracted from `upstream.py` to stay under DX line-count thresholds.

**Public API:**
- `filter_tools_for_profile(all_tools: dict[str, list[ResolvedTool]], profile: ProfileConfig, server_default_postures: dict[str, Posture]) -> Result[list[ResolvedTool], str]`
- `strip_meta(arguments: dict) -> Result[tuple[dict, dict | None], str]`
- `enforce_tool_call(tool_name: str, tool: ResolvedTool, profile: ProfileConfig, default_posture: Posture) -> Result[EnforcementResult, str]`

**Ownership:** Stateless; no module-level state.

**Dependencies:**
- Upstream: `tela.core.enforcement.enforce`, `tela.core.models`.
- Downstream: none. Consumed by `upstream.py`.

**Concurrency:** Pure functions; inherently thread-safe.

---

### `http_routes.py`

**Responsibility:** HTTP route handler implementations for all gateway HTTP endpoints (`/health`, `/status`, `/operator/audit`, `/connect`, `/disconnect`). Separated from route mounting (which lives in `gateway.py`).

**Public API:**
- `handle_health() -> Result[HealthResponse, str]`
- `handle_status(request_token: str, expected_token: str) -> Result[StatusResponse, str]`
- `handle_operator_audit(cursor: str | None, limit: int | None) -> Result[AuditPage, str]`
- `handle_connect(request_token: str, expected_token: str, payload: ConnectRequest) -> Result[Mapping[str, object], str]`
- `handle_disconnect(request_token: str, expected_token: str, payload: DisconnectRequest) -> Result[Mapping[str, object], str]`

**Ownership:**
- Mutates: runtime connections (via `add_runtime_connection`, `remove_runtime_connection`).
- Reads: runtime config, runtime status snapshot, downstream tools, audit entries.

**Dependencies:**
- Upstream: `tela.core.models`, `tela.core.contracts`.
- Cross-module: `gateway_runtime.*`, `downstream.get_all_tools`, `http_auth.validate_bearer_token`, `upstream.release_session`, `audit.get_audit_entries`.

**Concurrency:** Handler functions are synchronous (called from Starlette route adapters in `gateway.py`). All runtime state access goes through locked `gateway_runtime` accessors.

---

### `http_auth.py`

**Responsibility:** HTTP bearer token authentication — constant-time token validation, raw bearer string parsing, and raw ASGI middleware that enforces bearer auth on all routes except `GET /health`.

**Public API:**
- `extract_bearer_from_header_value(value: str) -> str | None` — parse `Bearer <token>` from an `Authorization` header value; returns token string or `None` if not present/invalid
- `validate_bearer_token(request_token: str, expected_token: str) -> Result[None, str]`
- `BearerAuthMiddleware(app: ASGIApp, get_expected_token: Callable[[], str | None])` — raw ASGI middleware class.

**Bearer parsing (C1):** `extract_bearer_from_header_value` is the canonical shared bearer parser. It centralizes common string parsing logic (split on whitespace, validate prefix, return token) and preserves adapter-local error handling at each extraction point.

**Ownership:** Stateless (middleware instance holds references to app and token getter but no mutable state).

**Dependencies:**
- Upstream: `hmac` (stdlib), `tela.shell.result.Result`.
- Downstream: none. Consumed by `serve_cmd.py` (middleware wrapping), `http_routes.py` (direct validation), and `gateway_http_auth.py` (Starlette adapter).

**Concurrency:** `validate_bearer_token` is a pure function. `BearerAuthMiddleware` is stateless per-request — safe for concurrent ASGI invocation. `get_expected_token` callback must be thread-safe (satisfied by `gateway_runtime.get_expected_bearer_token`).

---

### `gateway_http_auth.py`

**Responsibility:** Starlette-level bearer token extraction from HTTP `Authorization` headers. Thin adapter between Starlette `Request` and the shell auth contract.

**Public API:**
- `extract_bearer_token(request: Request) -> Result[str, str]`

**Ownership:** Stateless.

**Dependencies:**
- Upstream: `starlette.requests.Request`, `tela.shell.result.Result`.
- Downstream: none. Consumed by `gateway.py` route adapters.

**Concurrency:** Pure function; inherently thread-safe.

---

### `idle_shutdown.py`

**Responsibility:** Connection-count tracking and idle-timer-based graceful shutdown for the HTTP gateway. Triggers a shutdown callback when all connections close and the idle timeout expires.

**Public API:**
- `IdleShutdownManager` class:
  - `increment() -> Result[None, str]` — new connection arrived; cancel idle timer.
  - `decrement() -> Result[None, str]` — connection closed; start idle timer if count reaches 0.
  - `reset() -> Result[None, str]` — cancel timer, reset count (used during shutdown).
  - Properties: `timeout_seconds`, `connection_count`, `is_shutdown_disabled`.
- `init_idle_manager(timeout_seconds: float, shutdown_callback: Callable) -> Result[IdleShutdownManager, str]`
- `shutdown_idle_manager() -> Result[None, str]`
- `get_idle_manager() -> IdleShutdownManager | None`

**Ownership:**
- Mutates: `_manager` (module-level singleton), `_connection_count`, `_idle_handle` (asyncio task).
- Module-level singleton pattern with exactly-once initialization guard.

**Dependencies:**
- Upstream: `asyncio`, `tela.shell.result.Result`.
- Downstream: none. Consumed by `gateway.py` (connect/disconnect routes) and `serve_cmd.py` (initialization).

**Concurrency:** All mutable state protected by `asyncio.Lock` (not `threading.Lock` — this is asyncio-only). The idle timer is an `asyncio.Task` that sleeps and fires the shutdown callback on expiry; new connections cancel the task.

---

### `connection_reaper.py`

**Responsibility:** Background sweep of idle and orphaned upstream connections. Periodically inspects all runtime connections and removes those whose upstream session is gone or whose idle TTL has been exceeded.

**Public API:**
- `ReaperConfig` — frozen dataclass: `sweep_interval_seconds`, `native_idle_ttl_seconds`, `bridge_idle_ttl_seconds`.
- `ReaperSweepOutcome` — frozen dataclass: `checked`, `reaped_session_gone`, `reaped_stale`, `errors`.
- `ConnectionReaper` class:
  - `start() -> Result[None, str]` — start background sweep task (idempotent).
  - `stop() -> Result[None, str]` — stop background sweep task (idempotent).
  - `sweep() -> Result[ReaperSweepOutcome, str]` — execute a single sweep cycle.

**Ownership:**
- Reads: runtime connections (via `get_runtime_connections_snapshot`), session registry (via `get_captured_session`).
- Mutates: runtime connections (via `cleanup_connection_by_id`), idle manager count (via `idle_manager.decrement`).

**Dependencies:**
- Upstream: `asyncio`, `tela.shell.gateway_runtime`, `tela.shell.upstream`, `tela.shell.idle_shutdown`, `tela.shell.connection_lifecycle`.
- Downstream: none. Consumed by `gateway.py` (lifecycle wiring).

**Concurrency:** The sweep loop is a single `asyncio.Task`. All runtime state access goes through locked gateway_runtime accessors. The reaper does not hold any long-lived locks across sweep iterations.

---

### `startup_coordinator.py`

**Responsibility:** Race-sensitive startup arbitration for `tela connect` — lockfile discovery with config ownership matching, single-leader autostart locking (per resolved config path via `fcntl.flock`), and follower wait/attach behavior.

**Public API:**
- `discover_or_autostart(*, config_path: str, default_profile: str | None, read_lockfile: ReadLockfile, wait_for_live_lockfile: WaitForLiveLockfile, autostart_serve: AutostartServe, lockfile_wait_timeout_seconds: float) -> Result[LockfileData, str]`

**Constants:**
- `STARTUP_LOCK_DIR` = `~/.tela`
- `FOLLOWER_WAIT_POLL_SECONDS` = `0.1`
- `RACE_WAIT_SECONDS` = `0.3`
- `START_RACE_RETRIES` = `3`

**Ownership:**
- Mutates: filesystem (startup lock files in `~/.tela/startup.<hash>.lock`), stale lockfile cleanup.
- Reads: lockfile via injected `read_lockfile` callback.

**Dependencies:**
- Upstream: `fcntl`, `hashlib`, `tela.core.models.LockfileData`, `tela.shell.lockfile.delete_lockfile`.
- Downstream: none. Consumed by `connect_cmd.py`.

**Concurrency:** Uses OS-level `fcntl.flock(LOCK_EX | LOCK_NB)` for non-blocking startup leadership arbitration. Only one process per config path can hold the startup lock. Followers poll with `FOLLOWER_WAIT_POLL_SECONDS` intervals. The coordinator is synchronous (blocking) — it runs before the asyncio event loop starts.

---

### `lockfile.py`

**Responsibility:** Shell-level lockfile and bearer token contracts — atomic lockfile write/read/delete at `~/.tela/gateway.lock`, stale PID detection, and bearer token generation.

**Public API:**
- `write_lockfile(data: LockfileData) -> Result[None, str]`
- `read_lockfile() -> Result[LockfileData, str]`
- `delete_lockfile() -> Result[None, str]`
- `generate_bearer_token() -> Result[str, str]`
- `is_stale(lockfile: LockfileData) -> bool`

**Constants:**
- `LOCKFILE_PATH` = `~/.tela/gateway.lock`
- `LOCKFILE_DIRECTORY_MODE` = `0o700`
- `LOCKFILE_FILE_MODE` = `0o600`

**Ownership:**
- Mutates: filesystem (`~/.tela/gateway.lock`).
- Reads: filesystem, PID liveness (`os.kill(pid, 0)`).

**Dependencies:**
- Upstream: `secrets`, `os`, `tela.core.models.LockfileData`, `tela.core.contracts`.
- Downstream: none. Consumed by `startup_coordinator.py`, `serve_cmd.py`.

**Concurrency:** Atomic writes via temp-file + `os.rename`. No module-level mutable state. PID liveness checks are safe for concurrent calls. No internal locking — external coordination (e.g., `fcntl.flock` in `startup_coordinator.py`) prevents concurrent writes.

---

### `audit.py`

**Responsibility:** Audit log writer and reader — entry construction with level-based field filtering, in-memory FIFO storage (bounded deque), optional JSONL file persistence, and query/read functionality.

**Public API:**
- `build_audit_entry(level: AuditLevel, connection: ConnectionContext, tool_name: str, server_name: str, result: EnforcementResult, ...) -> Result[AuditEntry, str]`
- `audit_init(config: AuditConfig) -> Result[None, str]`
- `audit_write(entry: AuditEntry) -> Result[None, str]`
- `audit_close() -> Result[None, str]`
- `audit_query(since: str | None = None, limit: int = 100) -> Result[list[AuditEntry], str]`
- `get_audit_entries() -> Result[list[AuditEntry], str]`
- `clear_audit_entries() -> None`

**Ownership:**
- Mutates: `_audit_entries` (module-level `deque[AuditEntry]`, maxlen=10000), `_audit_log_path` (optional JSONL path).
- Reads: `_audit_entries` for query.

**Dependencies:**
- Upstream: `tela.core.models` (AuditEntry, AuditLevel, AuditConfig, ConnectionContext, EnforcementResult, MetaField).
- Downstream: none. Consumed by `upstream.py` (tool call audit), `reload.py` (conflict warnings), `http_routes.py` (status query).

**Concurrency:** `_audit_lock` is an `asyncio.Lock` protecting `_audit_entries` and `_audit_log_path`. `build_audit_entry` is a pure function (no lock needed). `get_audit_entries` and `clear_audit_entries` are synchronous and access the deque directly — safe only when called from the same event loop or when no concurrent writes are in progress.

---

### `surface_instructions.py`

**Responsibility:** Authoritative runtime instruction text for tela-owned surfaces, and composition of gateway + downstream instruction sections.

**Public API:**
- `get_gateway_surface_instructions() -> Result[str, str]`
- `compose_gateway_and_downstream(gateway_instructions: str, downstream_instructions: str | None) -> Result[str, str]`

**Ownership:** Stateless; returns constant or composed text.

**Dependencies:**
- Upstream: `tela.shell.result.Result`.
- Downstream: none. Consumed by `gateway.py` during upstream server creation.

**Concurrency:** Pure functions; inherently thread-safe.

---

### `gateway_lifecycle.py`

**Responsibility:** Lifecycle authority surface for status/readiness facts.
Single authority for lifecycle/discovery/readiness facts consumed by gateway
status surfaces. This module provides the authoritative status facts that
bridge consumers query before admitting MCP traffic.

**Public API:**
- `LifecycleStatusFacts` — frozen dataclass with runtime status snapshot facts.
- `get_lifecycle_status_facts() -> Result[LifecycleStatusFacts, str]`

**Ownership:**
- Reads: `gateway_runtime.get_runtime_status_snapshot()`, `downstream.get_all_tools()`.
- Stateless aggregator — computes status facts from runtime + registry state.

**Dependencies:**
- Upstream: `tela.shell.gateway_runtime`, `tela.shell.downstream`.
- Downstream: none. Consumed by `gateway.py` and HTTP routes for status responses.

**Concurrency:** Pure function computing facts from runtime snapshots — inherently thread-safe.

---

### `connection_lifecycle.py`

**Responsibility:** Shared cleanup authority for connection teardown paths.
Centralizes per-connection runtime/session cleanup so disconnect and shutdown
callers apply the same cleanup semantics keyed by `connection_id`.

**Public API:**
- `ConnectionCleanupOutcome` — frozen dataclass with cleanup result.
- `cleanup_connection_by_id(connection_id: str) -> Result[ConnectionCleanupOutcome, str]`

**Ownership:**
- Mutates: runtime connections (via `remove_runtime_connection`), session registry (via `release_session`).
- Idempotent by design — repeated calls for the same ID are safe.

**Dependencies:**
- Upstream: `tela.shell.gateway_runtime` (release_session, remove_runtime_connection).
- Downstream: none. Consumed by `http_routes.py`, `gateway.py`, `connection_reaper.py`.

**Concurrency:** Calls locked gateway_runtime accessors — safe for concurrent invocation.

---

### `_downstream_recovery.py`

**Responsibility:** Downstream recovery, call-path, and event-handler coordination.
Extracted from `tela.shell.downstream` to keep startup/connect-disconnect logic
below maintainability limits. Recovery functions access shared downstream state
via lazy imports to avoid circular module dependencies.

This module is an **internal implementation detail** — `tela.shell.downstream`
re-exports its public entry points (`call_tool`) and internal hooks
(`_handle_reconnect`, `_handle_tools_list_changed`, `_recover_server_client`).

**Public API (re-exported by downstream.py):**
- `call_tool(server_name, tool_name, arguments) -> Result[dict, TelaError]`
- `_recover_server_client(...) -> Result[_ClientHandle, str]` — internal recovery primitive
- `_handle_reconnect(...) -> None` — reconnect event handler
- `_handle_tools_list_changed(...) -> None` — tools/list_changed event handler

**Constants:**
- `_RECOVERY_TIMEOUT_SECONDS = 15.0`
- Recovery stage strings: `_RECOVERY_STAGE_NOT_ATTEMPTED`, `_RECOVERY_STAGE_RECONNECT_STARTED`, etc.

**Ownership:**
- Accesses: downstream `_clients`, `_recovery_locks` (via lazy import).
- Stateless recovery primitives orchestrated by downstream.py event adapters.

**Dependencies:**
- Upstream: `tela.core.errors`, `tela.core.models`, `tela.shell.result`.
- Downstream: accessed via lazy import from `tela.shell.downstream` to avoid cycles.

**Concurrency:** Per-server recovery locks serialize concurrent recovery for
the same server. `_registry_lock` is released before waiting on recovery locks.
