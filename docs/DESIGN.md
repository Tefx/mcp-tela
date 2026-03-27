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
The lockfile contains `pid`, `port`, and `token` for auth.

### Connection lifecycle

1. `tela connect` → `POST /connect` → server registers connection
2. Bridge active: stdio ↔ HTTP MCP session
3. `tela connect` exits → `POST /disconnect` → server deregisters
4. Last connection gone + idle timeout → server auto-shuts down (if auto-started)

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

The server exposes MCP tools for runtime introspection:
- `tela.status`, `tela.connections`, `tela.audit`, `tela.profiles`
- Belong to `tela_admin` family, controlled by profiles
- Also available via HTTP `/status` endpoint for CLI queries

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

The `instructions` field in `ServerConfig` controls how downstream server instructions are merged into the upstream server's instructions.

**Three modes:**

| Value | Behavior |
|-------|----------|
| `None` (default) | Passthrough: use downstream server's instructions if available |
| `False` | Suppress: exclude this server's instructions entirely |
| `str` | Override: use the provided string instead of downstream instructions |

**Merged output format:**
```markdown
## ServerName

<instructions or override>

Available tools:
- tool_1
- tool_2
```

## Invariants

- one connection binds to one profile
- profile ceilings are family-based
- no tool override may elevate access beyond `capabilities[family]`
- classification is concrete-provider aware
- unclassified access is handled conservatively
- approval semantics do not appear in gateway profiles
- `tela.` tool prefix is reserved for introspection tools
- each server instance stamps audit entries with a unique `instance_id`
- tool metadata (`annotations`, `title`, `output_schema`) is preserved from downstream through upstream
