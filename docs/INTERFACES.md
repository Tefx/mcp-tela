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
tela start [--config path] [--port port] [--default-profile name]
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
- or `url` for SSE

Optional gateway controls:
- `family`
- `default_posture`
- `tool_overrides`

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
