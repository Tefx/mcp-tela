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
tela start [--config path] [--port port] [--transport {stdio,sse,http}] [--default-profile name]
tela status [--json]
tela profiles [--config path] [--json]
tela connections [--json]
tela audit [--json] [--since T] [--limit N]
```

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
- `url` for SSE (legacy, default when `transport` is omitted)
- `url` + `transport: http` for Streamable HTTP (MCP 2025-03-26+)

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
    tool_overrides:
      filesystem:
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
- profile inspection via `tela.profiles`

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

## 8. Invariants

- tela is profile-only
- tela does not consume PersonaSpec or JobSpec directly
- tela enforces capability ceilings, not workflow policy
- runtime approval and temporary read-only execution remain outside the gateway

## 9. Downstream fastmcp Client Contract

This section defines the shell-side integration contract for connecting to
downstream MCP providers via `mcp.client`/fastmcp session clients.

### 9.1 `connect_all` transport behavior

`connect_all(servers)` iterates configured servers and selects exactly one
connection mode per server:

- stdio server contract: `ServerConfig.command` is required; client connect uses
  `command`, `args`, and `env` from config.
- SSE server contract: `ServerConfig.url` is required; client connect uses `url`.
  This is the default when `transport` is omitted.
- Streamable HTTP server contract: `ServerConfig.url` is required and
  `ServerConfig.transport` must be `"http"`; client connect uses
  `streamable_http_client`.
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
