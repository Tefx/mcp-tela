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

1. `tela connect` → `POST /connect` → server registers connection
2. Bridge active: stdio ↔ HTTP MCP session
3. `tela connect` exits → `POST /disconnect` → server deregisters
4. Last connection gone + idle timeout → server auto-shuts down (if auto-started)

### Discovery and readiness

Runtime lifecycle/readiness truth comes from the in-process runtime status snapshot
(and operator surfaces derived from it, such as `GET /status`), not from the
lockfile.

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
- reconnect handling may already hold fresh authoritative `raw_tools`; when that payload is present, downstream consumers MUST reuse it and MUST NOT blindly trigger a second enumeration

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

Built-in MCP surface:
- `tela.profiles` — exposed as MCP resource (read via `tela://profiles`)

Operator surfaces (CLI/HTTP, not MCP):
- `tela profiles`, `tela status`, `tela connections`, `tela audit` — accessible via CLI commands or `GET /status`

These are operator-facing surfaces (CLI/HTTP) and are **not** built-in MCP tool
surfaces. `tela.profiles` remains the only built-in tela MCP surface, and it is
a resource read surface (not a tool). These do not belong to a `tela_admin`
capability family.

No built-in `tela.*` MCP tools are currently implemented.

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
- HTTP route handlers
- stdio-HTTP bridge

#### Transports

Upstream (gateway → MCP clients):
- `http`: Streamable HTTP (MCP 2025-03-26+), used by `tela serve`
- stdio bridging is handled by `tela connect` (not the server itself)

Downstream (gateway → MCP servers):
- `command`: stdio subprocess
- `url`: Streamable HTTP (default) or SSE (`transport: sse`)

### `commands/`

CLI entrypoints only:
- `connect_cmd.py`: client entry (auto-discover, auto-start, bridge)
- `serve_cmd.py`: server entry (HTTP gateway, lockfile, idle shutdown)
- `status_cmd.py`, `connections_cmd.py`, `audit_cmd.py`, `profiles_cmd.py`: query commands

## Auth Layers

| Layer | Mechanism | Purpose |
|-------|-----------|---------|
| Lockfile bearer token | Auto-generated per server instance | Protects HTTP endpoints |
| Config `auth.mode: token` | CapabilityToken with HMAC | Binds connection to profile |
| Config `auth.mode: open` | No token needed | Uses default profile |

Both layers are independent and apply simultaneously:

- **Bearer token** (lockfile or `--token`/`TELA_BEARER_TOKEN`): protects the HTTP transport layer
- **Config `auth.mode`** (open/token): controls MCP-level profile binding
- You can have `auth.mode: open` (no CapabilityToken) and still require the bearer token for HTTP access
- You can override the bearer token with `--token` without changing profile authorization

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
- `tela.` MCP surface names are tela-owned; currently `tela.profiles` is the only
  built-in tela MCP surface (resource)
- each server instance stamps audit entries with a unique `instance_id`
- tool metadata (`annotations`, `title`, `output_schema`) is preserved from downstream through upstream
