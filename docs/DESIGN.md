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
- `url`: SSE (default) or Streamable HTTP (`transport: http`)

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

Both layers are independent and apply simultaneously.

## Ownership Rules

1. `core/` owns authorization semantics.
2. `shell/` owns transport, process effects, and service lifecycle.
3. CLI commands delegate; they do not define authorization rules.
4. tela profiles remain capability-only.

## Invariants

- one connection binds to one profile
- profile ceilings are family-based
- no tool override may elevate access beyond `capabilities[family]`
- classification is concrete-provider aware
- unclassified access is handled conservatively
- approval semantics do not appear in gateway profiles
- `tela.` tool prefix is reserved for introspection tools
- each server instance stamps audit entries with a unique `instance_id`
