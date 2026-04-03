# tela -- Interface Specification

For operator walkthroughs and deployment examples, see `docs/USAGE.md`.

## 1. Purpose

`tela` is the concrete MCP gateway and authorization layer.

It exposes downstream MCP servers through one upstream endpoint and enforces:
- profile binding
- family capability ceilings
- concrete tool posture checks

It does not own:
- persona identity
- runtime approval workflow
- runtime read-only mode semantics

## 2. CLI Surface

```text
tela connect [--config path] [--default-profile name] [--server host:port] [--token tok]
tela serve   [--config path] [--port N] [--host addr] [--default-profile name] [--idle-timeout sec] [--token tok]
tela status  [--json]
tela profiles [--config path] [--json]
tela connections [--json]
tela audit   [--json] [--since ISO-8601] [--limit N]
```

`tela connect` is the client entry point (stdio bridge with auto-discover/auto-start).
`tela serve` is the server entry point (HTTP gateway).
Query commands (`status`, `connections`, `audit`) discover the running server via
`~/.tela/gateway.lock` and query over HTTP.

## 3. Configuration Contract

Top-level sections:
- `servers`
- `profiles`
- `auth`
- `audit`

### 3.1 Servers

Each server defines one downstream MCP provider.

Required transport choice:
- `command` for stdio
- `url` for Streamable HTTP (default, MCP 2025-03-26+), omitted `transport`
- `url` + `transport: sse` for SSE (legacy)

Optional gateway controls:
- `family`
- `default_posture`
- `tool_overrides`
- `tool_prefix`
- `env`

#### `tool_prefix` contract:
- type is `str | None` (default: `None`)
- when set, all tools from this server are exposed upstream with the prefix prepended
- `tool_overrides` remain keyed by raw downstream tool names, not prefixed names
- conflict detection uses final exposed names (after prefix is applied)
- `tool_prefix="tela."` is reserved and rejected at validation
- omitted `tool_prefix` preserves backward-compatible behavior (raw names unchanged)
- prefix-only changes count as tool-surface changes (trigger reload/re-enumeration)

`env` contract:
- type is `dict[str, str]` (`VAR_NAME -> value`)
- omitted `env` defaults to `{}`
- explicit `env: {}` is equivalent to omitting `env`
- parser accepts `${VAR}` placeholders in env values and resolves them from parse-time environment input
- unresolved `${VAR}` placeholders are rejected during parse as configuration errors

### 3.2 Profiles

Profiles are capability ceilings only.

```yaml
profiles:
  developer:
    capabilities:
      filesystem: read_write
      git: read_only
    tool_overrides:
      filesystem:
        overrides:
          delete_file: deny
```

Normative rules:
- profile authorization is expressed through `capabilities: family -> posture`
- `tool_overrides` may further restrict or selectively expose tools
- no override may exceed the family capability ceiling
- tela profiles do not include approval or runtime side-effect policy

### 3.3 Authentication

Supported modes:
- `open`
- `token`

In token mode, a CapabilityToken binds the connection to one profile.

### 3.4 Audit

Audit logging is configured independently of authorization semantics.

Levels: `L1` (minimal), `L2` (standard), `L3` (verbose diagnostic).

Each `AuditEntry` includes:
- `timestamp`, `level`, `instance_id`, `connection_id`, `profile_name`
- `tool_name`, `server_name`, `verdict`, `denied_by`, `error_code`
- `latency_ms`, `param_hash` (L2+), `request_content`/`response_content` (L3)
- `meta` (trace fields from `_meta` argument)

`instance_id` is generated per `tela serve` invocation and identifies the
server instance that produced each entry.

## 4. Posture Model

Posture ordering:

```text
none < read_only < read_write < destructive
```

Meaning:
- `none`: denied
- `read_only`: observation only
- `read_write`: mutating but not destructive
- `destructive`: high-risk or irreversible operations

## 5. Tool Classification

Concrete tool posture is determined by tela from, in priority order:
1. explicit server/tool override
2. MCP tool annotations (`readOnlyHint`, `destructiveHint`) when present
3. server `default_posture`

If no valid classification is available, tela falls back to conservative denial
according to gateway configuration.

## 6. Enforcement Model

### 6.1 Connection bind

At connection establishment, tela binds the session to exactly one profile.

In token mode, the binding comes from the token `profile_name`.
In open mode, the binding comes from one explicit local default profile.

### 6.2 Per-call authorization

Per-call authorization is:

1. family admission
2. tool override application
3. posture comparison against the bound family ceiling

Core rule:

```text
allow iff tool_posture <= profile.capabilities[tool.family]
```

There is no separate side-effect policy layer in tela.
Approval and runtime read-only behavior are owned by the runtime layer.

## 7. MCP Surface

### Upstream behavior

The upstream MCP surface exposes:
- filtered `tools/list`
- authorized `tools/call`
- resource read: `tela.profiles`

### 7.1 MCP Resources

| Resource | Name | Purpose |
|----------|------|---------|
| `tela://profiles` | `tela.profiles` | Profile configuration (read via MCP resource read) |

The `tela.` prefix is reserved. Downstream tools with this prefix are rejected
as conflicts.

### 7.1a Built-in surfaces summary

| Surface | Kind | Access method | Status |
|---------|------|---------------|--------|
| `tela.profiles` | MCP resource | `tela://profiles` (resource read) | Supported |
| `tela profiles` | CLI/HTTP | `tela profiles` or `GET /status` (distinct from MCP resource name `tela.profiles`) | Operator-only (not MCP built-in) |
| `tela status` | CLI/HTTP | `tela status` or `GET /status` | Operator-only (not MCP built-in) |
| `tela connections` | CLI/HTTP | `tela connections` or via `/status` | Operator-only (not MCP built-in) |
| `tela audit` | CLI/HTTP | `tela audit` or via `/status` | Operator-only (not MCP built-in) |

### 7.2 HTTP Endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | None | Liveness check: `{"status":"ok","pid":N}` |
| `GET /status` | Bearer token | Full runtime status |
| `POST /connect` | Bearer token | Register bridge connection |
| `POST /disconnect` | Bearer token | Unregister bridge connection |
| `POST /mcp` | Bearer token | MCP Streamable HTTP endpoint |

### 7.2.2 Tool Metadata Passthrough

The `tools/list` response includes metadata fields preserved from downstream servers:

| Field | Type | Description |
|-------|------|-------------|
| `annotations` | dict \| null | MCP tool annotations including `readOnlyHint`, `destructiveHint`, `openWorldHint` |
| `title` | string \| null | Human-readable tool title |
| `outputSchema` | dict \| null | JSON schema for tool output if provided |

These fields are passed through unmodified from the downstream server and used for:
- Posture classification (annotations)
- Tool display (title)
- Client-side validation (outputSchema)

### 7.2.1 `GET /status` Response Schema

The status endpoint returns a `StatusResponse` containing gateway runtime state. Response fields have the following guarantees:

**Authoritative Fields** (directly from runtime state):
| Field | Type | Semantics |
|-------|------|-----------|
| `uptime_seconds` | float | Gateway process uptime in seconds |
| `server_count` | int | Number of configured downstream servers |
| `connected_servers` | list[str] | Server names currently connected |
| `active_connections` | int | **Numeric count** of active upstream connections |
| `profile_count` | int | Number of configured profiles |
| `total_tool_calls` | int | Cumulative tool calls since startup |
| `connections` | list[ConnectionContext] | **Structural collection** of connection contexts |
| `audit_entries` | list[AuditEntry] | Recent audit log entries (limit 100) |
| `state` | str | Lifecycle state: `warming`, `ready`, or `degraded` |
| `degraded_reason` | str \| null | Machine-readable reason when `state == "degraded"` |
| `discovery_source` | str \| null | How endpoint was resolved: `lockfile`, `autostart`, `explicit_server`, `startup_follower` |
| `config_path` | str \| null | Config path owned by running gateway |
| `requested_config_path` | str \| null | Config path requested by this query |
| `config_mismatch` | bool | Whether requested config differs from running gateway |

**Lifecycle States**:
- `warming`: Gateway HTTP endpoint is bound but downstream server connections are still initializing
- `ready`: Downstream servers are connected and tool registry is converged
- `degraded`: Gateway is reachable but not all downstream servers are connected

**Current-slice exclusion**:
- this contract does **not** add a public `shutting_down` value to `GET /status`
- bridge retry/admission behavior in the current slice must be keyed from the existing runtime snapshot semantics above, not from a new teardown state label
- any future teardown-state expansion must be planned as a separate architecture slice before this schema changes

**Count-vs-Collection Semantics**:
- `active_connections` is an **int count** for numeric comparisons (e.g., `active_connections >= 1`)
- `connections` is a **list** of `ConnectionContext` objects for structural inspection
- These fields are logically related but semantically distinct: `len(connections)` should equal `active_connections` in steady state, but only `active_connections` is authoritative for count semantics

**ConnectionContext** (structural):
```json
{
  "connection_id": "bridge_abc123",
  "profile_name": "developer",
  "connected_at": "2026-03-25T12:00:00Z",
  "tool_call_count": 5,
  "last_activity": "2026-03-25T12:05:00Z"
}
```

`last_activity` is an ISO-8601 UTC timestamp updated on each client interaction
(tool calls, tool list requests, connection registration). Empty string when no
activity has been recorded since connection establishment.

**AuditEntry** (structural):
```json
{
  "timestamp": "2026-03-25T12:00:00Z",
  "level": "L2",
  "event": "tool_call",
  "connection_id": "bridge_abc123",
  "tool_name": "filesystem/read_file",
  "details": {}
}
```

**Field Presence Guarantees**:
- All fields in `StatusResponse` are **guaranteed present** (non-null) in successful responses
- List fields (`connections`, `audit_entries`, `connected_servers`) are guaranteed present and may be empty (`[]`)
- `active_connections` is guaranteed to be the integer count (never null or omitted)
- Nullable fields (`degraded_reason`, `discovery_source`, etc.) are guaranteed present but may be `null`

### 7.3 Lockfile Contract

Location: `~/.tela/gateway.lock`

```json
{
  "pid": 12345,
  "host": "127.0.0.1",
  "port": 49152,
  "token": "bearer-token-here",
  "started_at": "2026-03-22T10:00:00Z",
  "config_path": "/path/to/tela.yaml",
  "version": "0.1.0"
}
```

**Critical semantics**: The lockfile is **discovery truth only**, written atomically
by `tela serve` after HTTP server bind but before downstream server startup completes.

**What the lockfile means**:
- An HTTP endpoint is bound and discoverable
- Bearer token for HTTP auth is available
- Config ownership is recorded (for concurrent startup coordination)

**What the lockfile does NOT mean**:
- Downstream servers are connected (check `connected_servers` in status)
- Tool enumeration succeeded (check runtime status)
- Registry convergence completed (check runtime status)
- Ready to serve tool calls (check `state` field in status)

Stale detection via PID liveness check.

**Extra Field Handling**: Extra fields in the lockfile are **accepted** and
silently ignored. Only the 7 required fields above are guaranteed to be present
in the parsed `LockfileData`. This follows Pydantic's default `extra="ignore"`
behavior. Clients should not rely on presence of extra fields.

### 7.4 Bearer Token

Every `tela serve` instance auto-generates a bearer token on startup using
`secrets.token_urlsafe(32)`. The token is:

- printed to stderr when started manually (not visible when auto-started by
  `tela connect`, since stderr is redirected)
- stored in the lockfile `token` field
- required on all HTTP endpoints except `GET /health`
- validated via constant-time comparison (`hmac.compare_digest`)

Override with `--token` or `TELA_BEARER_TOKEN` for fixed tokens in
automation/CI. Local `tela connect` reads the token from the lockfile
automatically. Remote clients pass it via `--token` or `TELA_BEARER_TOKEN`.

This bearer token is independent of config `auth.mode` (which controls
MCP-level profile binding via CapabilityToken).

### `tela.profiles` Resource

`access: read` via MCP resource read (not `tools/call`).

Migration payload shape (backward-compatible window):

```json
[
  {
    "profile_name": "developer",
    "tools": {
      "filesystem": "read_write",
      "git": "read_only"
    },
    "capabilities": {
      "filesystem": "read_write",
      "git": "read_only"
    },
    "default": false
  }
]
```

Post-migration payload shape (canonical):

```json
[
  {
    "profile_name": "developer",
    "capabilities": {
      "filesystem": "read_write",
      "git": "read_only"
    },
    "default": false
  }
]
```

Historical/migration notes:
- `tools` is emitted only during the migration window for backward compatibility
- `side_effect_policy` is not part of `tela.profiles` output in the target model

### 7.3 Session and Notification Forwarding

The gateway implements MCP `notifications/tools/list_changed` forwarding from downstream servers to upstream clients.

**Session Capture:**
- Upstream MCP sessions are captured during handler registration and stored in a thread-safe registry (`connection_id` → session)
- Sessions are released automatically on disconnect

**Notification Flow:**
1. Downstream server sends `notifications/tools/list_changed`
2. Gateway triggers hot reload re-enumeration
3. On successful reload, notifications are broadcast to all captured upstream sessions
4. Each notification is sent via `session.send_tool_list_changed()`

**Fallback:**
- If no session is captured (e.g., stdio transports), notifications are skipped silently with a debug log
- Failed notifications remove stale sessions from the registry

### 7.4 Instructions Configuration

The `instructions` field in `ServerConfig` controls how downstream server instructions are merged into the upstream server's instructions.

**Merge semantics:**
1. Tela top-level gateway instructions, when present, are emitted first.
2. After the gateway instructions, tela appends zero or more downstream server sections.
3. Downstream sections are appended in configured server iteration order.
4. For each downstream server:
   - `instructions: false` → no section is appended for that server
   - `instructions: <string>` → append a server section using the explicit override string
   - `instructions: null` / omitted → append a server section using the downstream server's own advertised instructions, if any
5. When a downstream section is appended and tools are known, an `Available tools:` list is appended inside that server's section.

**Conflict handling:**
- Runtime composition is append-only: gateway block first, then downstream sections.
- Runtime does not implement semantic conflict detection/resolution for instruction text.
- Contradictory downstream text remains present as appended content.
- Mitigation is explicit configuration or spec/doc revision: suppress a section, provide per-server replacement text, or make an explicit follow-up contract change.

**Configuration table:**

| Value | Behavior |
|-------|----------|
| `null` (default) | Passthrough: use downstream server's instructions if available |
| `false` | Suppress: exclude this server's instructions entirely |
| string | Override: use the provided string instead of downstream instructions |

**Merged output format:**
```markdown
<tela gateway instructions here>

## ServerName

<instructions or override>

Available tools:
- tool_1
- tool_2
```

## 8. Invariants

- tela is profile-only
- tela does not consume PersonaSpec or JobSpec directly
- tela enforces capability ceilings, not workflow policy
- runtime approval and temporary read-only execution remain outside the gateway
- tool metadata fields (`annotations`, `title`, `outputSchema`) are preserved from downstream through upstream
- notification forwarding operates on best-effort basis with graceful degradation for transports that don't support session capture

## 9. Downstream fastmcp Client Contract

This section defines the shell-side integration contract for connecting to
downstream MCP providers via `mcp.client`/fastmcp session clients.

### 9.1 `connect_all` transport behavior

`connect_all(servers)` iterates configured servers and selects exactly one
connection mode per server:

- stdio server contract: `ServerConfig.command` is required; client connect uses
  `command`, `args`, and `env` from config.
- Streamable HTTP server contract: `ServerConfig.url` is required; client connect uses
  `url`. This is the default when `transport` is omitted.
- SSE server contract: `ServerConfig.url` is required and
  `ServerConfig.transport` must be `"sse"`; client connect uses `sse_client`.
- mixed transport fields (`command` and `url` both set) are invalid and must be
  rejected as a config/runtime contract violation.

Per-server session results are stored in `_clients` only after successful session
establishment.

### 9.2 `_clients` mapping shape

Runtime mapping contract:

```text
_clients: dict[str, ClientSession]
```

- key: canonical `server_name` from `servers` mapping.
- value: connected downstream client session object used for `tools/list`,
  `tools/call`, and re-enumeration.
- invariant: key exists iff that server is currently connected.

### 9.3 Session lifecycle contract

- startup (`connect_all`): establish sessions for all servers, then enumerate and
  register tools.
- failure during startup: close any sessions opened in the same call and leave
  `_clients` empty (no partial connected state).
- shutdown (`disconnect_all`): close all sessions best-effort, clear `_clients`,
  and clear resolved tool registry.
- reload/re-enumeration: session identity in `_clients` is reused when transport
  endpoint is unchanged; replaced only when reconnect is required.

### 9.4 Error handling contract

- transport/session establishment errors are surfaced as structured
  `TelaError(code="DOWNSTREAM_CONNECT_FAILED", ...)` with server context.
- tool invocation on missing/unconnected server returns
  `TelaError(code="DOWNSTREAM_NOT_CONNECTED", ...)`.
- downstream tool execution failures are surfaced as
  `TelaError(code="DOWNSTREAM_TOOL_CALL_FAILED", ...)` and include server/tool
  context in `details`.
- `connect_all` rejects tool-name conflicts across servers and tears down opened
  sessions before returning conflict error.

### 9.5 Retry and Reconnection Semantics

#### Startup: downstream connect failure

`connect_all` uses `asyncio.gather` to connect all configured servers
concurrently. If **any** server fails to connect or enumerate tools:

- All already-opened client handles are closed (best-effort).
- The downstream registry is cleared.
- `connect_all` returns an error immediately.
- **No retry or backoff is attempted.** Startup is fail-fast.

There is no partial-success mode at startup: either all servers connect and
enumerate successfully, or the entire downstream layer remains unconnected.

#### Steady state: auto-reconnect on disconnect

When a connected downstream server disconnects (the MCP client session raises
an `Exception` in the message handler), the gateway automatically attempts
reconnection via `_handle_reconnect`:

1. Opens a new client session to the same server (`_open_client_for_server`).
2. On success: swaps the new client handle into `_clients` and closes the old
   handle best-effort.
3. Re-enumerates tools from the new session.
4. Routes the fresh tool list through the single-server convergence kernel
   (`on_server_reconnect`) which resolves, registers, and checks for conflicts.
5. On conflict: the previous tool list is preserved (rollback), a
   `TOOL_CONFLICT` audit warning is written, and the reconnect update is rejected.
6. On success: the registry is updated, and upstream clients are notified via
   `tools/list_changed`.

**No backoff or retry limit.** If the reconnect attempt fails (connection or
enumeration), a warning is logged and no further automatic retry is scheduled.
The server remains disconnected until the next event triggers reconnection
(e.g., another disconnect exception, config reload, or manual re-enumeration).

#### Hot reload: reconnect behavior on config change

When `on_config_changed` detects server additions, removals, or configuration
changes:

1. Computes the diff between old and new server sets (added, removed, changed).
2. If any servers were removed or changed: calls `disconnect_all()` followed by
   `connect_all(new_config.servers)` — a full reconnect cycle.
3. No per-server incremental reconnect is supported. The full
   `disconnect_all` + `connect_all` path is the safe path that preserves
   conflict-detection invariants across the entire server set.

**No retry on reload failure.** If `connect_all` fails during reload, the error
is returned to the caller. The previous server connections are already torn down.

#### Manual reconnect

There is no dedicated manual reconnect CLI command or HTTP endpoint. Operators
can trigger reconnection by:

- Restarting `tela serve`.
- Modifying the config file (if a file watcher is configured).
- Calling `gateway_reload_config_from_disk` programmatically.

### 9.6 Connection Reaper

The connection reaper is a background async task that periodically removes
orphaned and idle upstream connections.

**Public types (`shell/connection_reaper.py`):**

| Type | Kind | Fields |
|------|------|--------|
| `ReaperConfig` | frozen dataclass | `sweep_interval_seconds: float = 30.0`, `native_idle_ttl_seconds: float = 120.0`, `bridge_idle_ttl_seconds: float = 300.0` |
| `ReaperSweepOutcome` | frozen dataclass | `checked: int`, `reaped_session_gone: list[str]`, `reaped_stale: list[str]`, `errors: list[str]` |
| `ConnectionReaper` | class | `start()`, `stop()`, `sweep()` — all return `Result` |

**Activity tracking (`shell/gateway_runtime.py`):**

| Function | Signature | Purpose |
|----------|-----------|---------|
| `touch_connection_activity` | `(connection_id: str, timestamp: str) -> Result[bool, str]` | Update `last_activity` for a connection under `_runtime_lock` |

**Model field (`core/models.py`):**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ConnectionContext.last_activity` | `str` | `""` | ISO-8601 UTC timestamp of last client interaction |

**Sweep behavior:**

1. Session probe (`conn_*` only): if the upstream session is no longer in the
   session registry, the connection is reaped.
2. Staleness check (all types): if `last_activity` (or `connected_at` fallback)
   exceeds the connection-type TTL, the connection is reaped.
3. After each reap, `idle_manager.decrement()` is called to maintain the
   connection count for idle shutdown.

**Lifecycle:**

- Starts after `gateway_converge_startup` succeeds.
- Stops before `gateway_shutdown` tears down downstreams.

## 10. Operational Limits and Constraints

### 10.1 Audit

| Limit | Value | Enforced | Source |
|-------|-------|----------|--------|
| In-memory buffer max entries | 10,000 | Yes — `deque(maxlen=10000)` | `shell/audit.py` |
| Query result limit (default) | 100 | Yes — `audit_query(limit=100)` | `shell/audit.py` |
| Query result limit (max) | No limit enforced | — | Caller may pass any `limit` value |
| Status endpoint audit entries | 100 (most recent) | Yes — uses `get_audit_entries()` then `[-100:]` in status | `shell/http_routes.py` |

### 10.2 Configuration

| Limit | Value | Enforced | Source |
|-------|-------|----------|--------|
| Max servers | No limit enforced | — | Pydantic `dict` with no max size |
| Max profiles | No limit enforced | — | Pydantic `dict` with no max size |
| Server name length | No limit enforced | — | YAML dict key, no validation |
| Profile name length | No limit enforced | — | YAML dict key, no validation |
| Tool override count per server | No limit enforced | — | Pydantic `dict` with no max size |
| Env vars per server | No limit enforced | — | Pydantic `dict` with no max size |

### 10.3 Runtime

| Limit | Value | Enforced | Source |
|-------|-------|----------|--------|
| Max upstream connections | No limit enforced | — | `_runtime.connections` is an unbounded `list` |
| Idle timeout range | `[0, ∞)` float seconds | `0` disables auto-shutdown | `shell/idle_shutdown.py` |
| Idle timeout default | 300 seconds | Yes — CLI `--idle-timeout` default | `cli.py` |
| Max tool calls counter | No limit enforced | — | `_runtime.total_tool_calls` is an unbounded `int` |
| Reaper sweep interval | 30.0 seconds | Yes — `ReaperConfig.sweep_interval_seconds` | `shell/connection_reaper.py` |
| Native idle TTL | 120.0 seconds | Yes — `ReaperConfig.native_idle_ttl_seconds` | `shell/connection_reaper.py` |
| Bridge idle TTL | 300.0 seconds (0 = disabled) | Yes — `ReaperConfig.bridge_idle_ttl_seconds` | `shell/connection_reaper.py` |

### 10.4 Tool Registry

| Limit | Value | Enforced | Source |
|-------|-------|----------|--------|
| Max tools per server | No limit enforced | — | `_tools_by_server` is an unbounded `list` |
| Total tools across servers | No limit enforced | — | `_tool_to_server` is an unbounded `dict` |
| Tool name length | No limit enforced | — | String key, no validation |
| Tool enumeration timeout | No limit enforced | — | Relies on MCP client session timeout |
| Tool enumeration pagination | Automatic | Yes — follows `nextCursor` until `None` | `downstream_clients.py` |

### 10.5 HTTP / Transport

| Limit | Value | Enforced | Source |
|-------|-------|----------|--------|
| Request body max size | No limit enforced | — | Starlette/uvicorn defaults apply |
| Bearer token length | ≥43 chars (generated) | Post-condition enforced on generation | `shell/lockfile.py` |
| Bearer token entropy | 32 bytes (`secrets.token_urlsafe(32)`) | Yes | `shell/lockfile.py` |
| Startup leadership retries | 3 | Yes — `START_RACE_RETRIES` | `shell/startup_coordinator.py` |
| Follower poll interval | 0.1 seconds | Yes — `FOLLOWER_WAIT_POLL_SECONDS` | `shell/startup_coordinator.py` |
| Race wait window | 0.3 seconds | Yes — `RACE_WAIT_SECONDS` | `shell/startup_coordinator.py` |

### 10.6 Lockfile

| Limit | Value | Enforced | Source |
|-------|-------|----------|--------|
| Lockfile directory permissions | `0o700` | Yes | `shell/lockfile.py` |
| Lockfile file permissions | `0o600` | Yes | `shell/lockfile.py` |
| Stale detection | PID liveness via `os.kill(pid, 0)` | Yes | `shell/lockfile.py` |

## 6.3 Error Response Semantics

### Authorization denial (MCP enforcement chain)

When the 3-step enforcement chain denies a tool call, the error is returned
as a `TelaError` via MCP error semantics:

| Denial reason | `denied_by` | `error_code` | Message template |
|--------------|-------------|--------------|------------------|
| Family not in profile capabilities | `family_admission` | `AUTHZ_DENY` | `"Family '{family}' is not admitted by profile '{profile_name}'"` |
| Tool explicitly denied by override | `tool_override` | `AUTHZ_DENY` | `"Tool '{tool_name}' explicitly denied by profile override"` |
| Posture exceeds family ceiling | `posture_ceiling` | `AUTHZ_DENY` | `"Tool posture {posture} exceeds ceiling {ceiling}"` |
| Unclassified tool with `default_posture=none` | `posture_ceiling` | `TOOL_UNCLASSIFIED` | `"Tool is unclassified and server default_posture is NONE"` |

The enforcement result is wrapped in a `TelaError(code=..., message=...)` and
raised as a `RuntimeError` by the upstream MCP handler. The MCP framework
serializes this as a standard MCP error response.

### Missing/invalid bearer token (HTTP)

| Condition | HTTP status | Response body |
|-----------|-------------|---------------|
| Missing `Authorization` header | 401 | `{"error": "AUTH_INVALID_TOKEN: bearer token validation failed"}` |
| `Authorization` header without `Bearer ` prefix | 401 | `{"error": "AUTH_INVALID_TOKEN: bearer token validation failed"}` |
| Empty token after `Bearer ` prefix | 401 | `{"error": "AUTH_INVALID_TOKEN: bearer token validation failed"}` |
| Token mismatch (constant-time comparison) | 401 | `{"error": "AUTH_INVALID_TOKEN: bearer token validation failed"}` |
| Token unavailable during startup race | 401 | `{"error": "AUTH_INVALID_TOKEN: bearer token validation failed"}` |

The `BearerAuthMiddleware` (raw ASGI) handles auth for all HTTP routes except
`GET /health`. It uses `hmac.compare_digest` for constant-time comparison.

### Other HTTP error codes

| Condition | HTTP status | Error prefix |
|-----------|-------------|--------------|
| Invalid request payload (JSON parse / validation) | 400 | `INVALID_REQUEST:` |
| Connection not found on disconnect | 404 | `CONNECTION_NOT_FOUND:` |
| Gateway not started | 503 | `GATEWAY_NOT_STARTED:` |

### Connection limits

No connection limit is enforced. The `_runtime.connections` list is unbounded.
Any number of upstream bridge connections may register concurrently.

### 7.5 Tool Enumeration Failure Modes

#### Startup enumeration failure

During `connect_all`, tool enumeration is attempted for every configured server
after transport connection succeeds. If enumeration fails for any server:

- The client handle for that server is closed (best-effort via `aclose()`).
- All other already-opened handles are also closed.
- The downstream registry is cleared.
- `connect_all` returns `DOWNSTREAM_CONNECT_FAILED` error.

**No partial registry.** Startup is all-or-nothing. Either all servers connect
and enumerate successfully, or the gateway starts with an empty tool registry
and reports `warming` lifecycle state.

#### Runtime `tools/list_changed` handling

When a downstream server sends `notifications/tools/list_changed`:

1. The event-entry adapter (`_handle_tools_list_changed`) re-enumerates the
   server's tools via `_enumerate_client_tools`.
2. If re-enumeration fails: a warning is logged; the previous tool list for
   that server is preserved. No registry mutation occurs.
3. If re-enumeration succeeds: the fresh tool list is routed through the
   single-server convergence kernel (`on_tools_changed`).
4. **Conflict detection:** The kernel tentatively registers the new tools, then
   runs `detect_conflicts` against the full registry (all servers).
   - On conflict: registry is rolled back to the pre-update snapshot (atomic
     rollback via `snapshot()`/`restore()`). A `TOOL_CONFLICT` audit warning
     is written. The update is rejected. Previous tools remain.
   - On no conflict: registry is updated. Upstream clients are notified.
5. **Atomic swap:** Registration uses `unregister(server_name)` then
   `register(server_name, new_tools)` within the `_registry_lock`. This is
   effectively an atomic replace for one server's tool set.

#### Unclassified tool behavior

Tools that cannot be classified (no explicit override, no MCP annotations, and
`default_posture = none`) receive `posture = None` in the `ResolvedTool`. At
enforcement time:

- **Posture:** The enforcement chain uses `default_posture` as fallback. If
  `default_posture` is `none`, the tool is denied with error code
  `TOOL_UNCLASSIFIED`.
- **Visibility:** Unclassified tools **are** registered in the downstream
  registry and **are** visible in the full tool list. However, they are
  filtered out of `tools/list` responses by `filter_tools_for_profile` when
  the enforcement check returns `DENY`.
- **Audit:** Denied unclassified tool calls produce audit entries with
  `denied_by="posture_ceiling"` and `error_code="TOOL_UNCLASSIFIED"`.

## 3.4a Audit Entry Value Enumerations

### `verdict`

| Value | Meaning |
|-------|---------|
| `allow` | Tool call was authorized and forwarded to downstream |
| `deny` | Tool call was rejected by the enforcement chain |

Type: `EnforcementVerdict` enum (`str`). Exactly two values.

### `denied_by`

Populated only when `verdict == "deny"`. Identifies which enforcement step
rejected the call:

| Value | Enforcement step | Description |
|-------|-----------------|-------------|
| `family_admission` | Step 1 | Tool's family is not in the profile's `capabilities` map |
| `tool_override` | Step 2 | Profile has an explicit `deny` override for this tool |
| `posture_ceiling` | Step 3 | Tool's posture exceeds the profile's family ceiling, or tool is unclassified with `default_posture=none` |
| `tool_conflict` | System | Used in audit warnings for tool-name conflicts during reload/reconnect (not a per-call denial) |

When `verdict == "allow"`, `denied_by` is `null`.

### `error_code`

Populated only when `verdict == "deny"`. Machine-readable error classification:

| Value | Meaning | Used by |
|-------|---------|---------|
| `AUTHZ_DENY` | Authorization denied by policy | `family_admission`, `tool_override`, `posture_ceiling` |
| `TOOL_UNCLASSIFIED` | Tool has no posture and server `default_posture` is `none` | `posture_ceiling` |
| `TOOL_CONFLICT` | Tool name conflict across servers | Reload/reconnect conflict warnings |

When `verdict == "allow"`, `error_code` is `null`.

### Additional error codes (non-audit, MCP/HTTP surface)

| Error code | Surface | Condition |
|------------|---------|-----------|
| `GATEWAY_NOT_STARTED` | MCP + HTTP | Gateway runtime not initialized |
| `TOOL_NOT_FOUND` | MCP | Tool name not in downstream registry |
| `PROFILE_NOT_FOUND` | MCP | Bound profile not in runtime config |
| `DOWNSTREAM_UNAVAILABLE` | MCP | Downstream server not connected or call failed |
| `DOWNSTREAM_ERROR` | MCP | Downstream server returned `isError: true` |
| `DOWNSTREAM_CONNECT_FAILED` | Startup | Transport connection or enumeration failed |
| `INITIALIZE_REJECTED` | MCP | Token validation failed or no default profile |
| `AUTH_INVALID_TOKEN` | HTTP | Bearer token validation failed |
| `CONNECTION_NOT_FOUND` | HTTP | Disconnect for unknown connection_id |
| `INTERNAL_ERROR` | MCP | Unexpected internal failure |
| `SESSION_ALREADY_BOUND` | Internal | Session capture attempted on already-bound connection |
| `SESSION_NOT_FOUND` | Internal | No captured session for connection_id |
| `SESSION_NOT_REGISTERED` | Internal | Reverse lookup failed for session |
| `NOTIFICATION_SEND_FAILED` | Internal | Failed to send `tools/list_changed` to upstream |
| `CONFIG_FILE_MISSING` | Startup | Config file not found at path |
| `CONFIG_FILE_READ_ERROR` | Startup | OS error reading config file |
| `CONFIG_PARSE_ERROR` | Startup | YAML parse or Pydantic validation error |
| `CONFIG_ENV_UNSET` | Startup | `${VAR}` placeholder references unset env var |
| `PROFILE_NOT_FOUND` | Startup | CLI `--default-profile` names unknown profile |
| `OPEN_MODE_DEFAULT_PROFILE_MISSING` | Startup | Open mode with no default profile |
| `OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS` | Startup | Multiple profiles marked `default: true` |
| `TOKEN_INVALID` | MCP | CapabilityToken HMAC validation failed |
| `TOKEN_EXPIRED` | MCP | CapabilityToken past expiry |
| `LOCKFILE_READ_ERROR` | Discovery | Lockfile missing or unreadable |
| `LOCKFILE_STALE` | Discovery | Lockfile PID not alive |
| `LOCKFILE_PARSE_ERROR` | Discovery | Lockfile JSON invalid |
| `LOCKFILE_WAIT_TIMEOUT` | Discovery | Timed out waiting for lockfile |
| `DISCOVERY_FAILED` | Discovery | Could not discover or auto-start server |
| `IDLE_MANAGER_ALREADY_INITIALIZED` | Startup | `init_idle_manager` called twice |
| `CONNECTION_COUNT_UNDERFLOW` | Internal | Disconnect without matching connect |
| `AUDIT_INIT_ERROR` | Startup | Cannot create audit log directory |
| `AUDIT_WRITE_ERROR` | Runtime | Cannot write to audit JSONL file |
| `AUDIT_QUERY_ERROR` | Runtime | Invalid timestamp format in query |

## 3.1a Config Loading Edge Cases

### ENV placeholder expansion

`${VAR}` placeholders in server `env` values are expanded at parse time using
the process environment (`os.environ`):

- **Syntax:** `${VAR_NAME}` and `$VAR_NAME` are both supported.
- **Unset variables:** Unresolved placeholders raise
  `ConfigContractError(code="CONFIG_ENV_UNSET")` — they are **not** silently
  ignored or left as literal strings. This is a hard startup error.
- **Special characters:** Variable names follow standard shell naming
  (`[A-Za-z_][A-Za-z0-9_]*`). The expansion uses regex matching, so variable
  names with special characters in braces are matched greedily.
- **Nested expansion:** Not supported. `${${VAR}}` is not valid.
- **Empty value:** If the env var is set but empty (`VAR=""`), the empty string
  is substituted. This is not an error.
- **Recursive expansion in env values:** Expansion is applied recursively to
  all string values in the YAML object graph, not just `env` fields. Any
  string value in the config containing `${VAR}` will be expanded.

### File-not-found behavior

- **Missing config file:** Returns `Result(error="CONFIG_FILE_MISSING: configuration file not found at {path}")`.
- **Default path:** When no `--config` is specified, defaults to `tela.yaml`
  in the current working directory.
- **OS read errors:** Returns `Result(error="CONFIG_FILE_READ_ERROR: {exc}")`.

### YAML parse errors

- **Invalid YAML:** Returns `Result(error="CONFIG_PARSE_ERROR: invalid YAML: {exc}")`.
- **Non-mapping top level:** Returns `Result(error="CONFIG_PARSE_ERROR: top-level YAML document must be a mapping")`.
- **Empty file:** `yaml.safe_load` returns `None`, which is treated as `{}`
  (empty config). This produces a valid `TelaConfig` with defaults.

### Unknown field handling

Unknown fields in the YAML config are handled by Pydantic's default behavior:

- **`TelaConfig`:** Unknown top-level keys are silently ignored (Pydantic
  default `extra="ignore"`).
- **`ServerConfig`:** Unknown server fields are silently ignored.
- **`ProfileConfig`:** Unknown profile fields are silently ignored.
- **`AuthConfig`:** Unknown auth fields are silently ignored.
- **`AuditConfig`:** Unknown audit fields are silently ignored.

No warning is emitted for unknown fields. This is by design for forward
compatibility — newer config versions may add fields that older `tela`
versions should ignore.

### Config reload trigger conditions

Config reload (`on_config_changed`) is triggered by:

1. `gateway_reload_config_from_disk` — the production runtime callback for
   file-watcher integrations.
2. Programmatic calls to `on_config_changed(new_config)` with a new
   `TelaConfig` object.

Config reload **does not** watch the filesystem automatically. A file-watcher
must be configured externally to call the reload callback. There is no polling
mechanism built into `tela serve`.
