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

Authorization is a single comparison:

```text
tool_posture <= profile.capabilities[tool.family]
```

with family admission and tool overrides as pre-checks.

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

### `commands/`

CLI entrypoints only.

## Ownership Rules

1. `core/` owns authorization semantics.
2. `shell/` owns transport and process effects.
3. CLI commands delegate; they do not define authorization rules.
4. tela profiles remain capability-only.

## Invariants

- one connection binds to one profile
- profile ceilings are family-based
- classification is concrete-provider aware
- unclassified access is handled conservatively
- approval semantics do not appear in gateway profiles
