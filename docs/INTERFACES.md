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
- `env`

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
      tela_admin: read_only
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
- introspection tools (`tela.status`, `tela.connections`, `tela.audit`, `tela.profiles`)

### 7.1 Introspection Tools

| Tool | Family | Posture | Description |
|------|--------|---------|-------------|
| `tela.status` | `tela_admin` | `read_only` | Gateway runtime status |
| `tela.connections` | `tela_admin` | `read_only` | Active upstream connections |
| `tela.audit` | `tela_admin` | `read_only` | Audit log query |
| `tela.profiles` | `tela_admin` | `read_only` | Profile configuration |

The `tela.` prefix is reserved. Downstream tools with this prefix are rejected
as conflicts.

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
  "tool_call_count": 5
}
```

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

Written atomically by `tela serve` on startup. Deleted on shutdown.
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

### `tela.profiles`

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

The `instructions` field in `ServerConfig` controls how downstream server instructions are merged into the upstream server's instructions:

| Value | Behavior |
|-------|----------|
| `null` (default) | Passthrough: use downstream server's instructions if available |
| `false` | Suppress: exclude this server's instructions entirely |
| string | Override: use the provided string instead of downstream instructions |

**Merged output format:**
```markdown
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
