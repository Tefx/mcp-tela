# tela Usage Guide

## Overview

tela is an MCP aggregation gateway. It sits between one or more downstream MCP
servers and one or more upstream MCP clients, then applies access control,
profile selection, posture enforcement, and audit logging.

Use tela when you want to:

- expose multiple MCP servers as one endpoint
- constrain tool usage by profile
- enforce read-only or destructive ceilings by family
- centralize audit logging
- share a single gateway across multiple agents without duplicating downstream processes

## Documentation map

- `README.md`: quickest way to understand what tela is and how to launch it
- `docs/USAGE.md`: operator guide, deployment patterns, and worked examples
- `tela.yaml.example`: fully commented configuration template
- `docs/INTERFACES.md`: CLI and configuration contract reference
- `docs/DESIGN.md`: architecture and implementation detail

## Mental model

```text
MCP client ──stdio──→ tela connect ──HTTP──→ tela serve ──stdio/HTTP──→ downstream servers
```

`tela connect` is what your MCP host launches. It bridges stdio to a shared
`tela serve` gateway. Multiple clients share one gateway — downstream servers
are spawned once.

## Installation

```bash
pip install -e .
```

## First-time setup

```bash
cp tela.yaml.example tela.yaml
```

The example file is intentionally verbose and should be treated as the primary
operator reference.

## Configuration model

tela reads one YAML file with four top-level sections:

- `servers`
- `profiles`
- `auth`
- `audit`

### `servers`

Each server entry declares one downstream MCP server.

You must use exactly one transport per server:

- `command` for stdio
- `url` for SSE (legacy, default when `transport` is omitted)
- `url` + `transport: http` for Streamable HTTP (MCP 2025-03-26+)

Minimal stdio example:

```yaml
servers:
  fs:
    command: "mcp-filesystem"
    args: ["--root", "/workspace"]
    family: "filesystem"
```

Minimal SSE example (legacy):

```yaml
servers:
  github:
    url: "http://localhost:3001/sse"
    family: "git"
```

Minimal Streamable HTTP example:

```yaml
servers:
  github:
    url: "http://localhost:3001/mcp"
    transport: http
    family: "git"
```

Important notes:

- the YAML key is the server name
- if `family` is omitted, tela uses the server name as the family by convention
- `default_posture` sets the baseline posture for tools from that server
- `tool_overrides` can adjust family or posture for specific tools

### `profiles`

Profiles define what a client is allowed to do.

Each profile contains:

- `capabilities`: family -> maximum posture ceiling
- `tool_overrides`: allow or deny specific tools within a family
- `default`: whether the profile is the default in open mode

Example:

```yaml
profiles:
  developer:
    capabilities:
      filesystem: "read_write"
      network: "read_only"
      git: "read_write"
      tela_admin: "read_only"
    tool_overrides:
      filesystem:
        overrides:
          delete_file: "deny"
      git:
        overrides:
          force_push: "allow"
    default: true
```

The `tela_admin` family controls access to introspection tools
(`tela.status`, `tela.connections`, `tela.audit`, `tela.profiles`).

Important notes:

- in `open` mode, one profile should usually have `default: true`
- custom families are valid, but built-in profiles only cover built-in family sets
- `tool_overrides` require the nested `overrides` map

### Built-in profiles

tela ships with seven built-in profile templates:

- `read_only`
- `fetch_external`
- `modify_local`
- `send_external`
- `orchestrate`
- `execute_safe`
- `execute_full`

These are defined in `src/tela/core/catalog.py` and demonstrated in
`tela.yaml.example`.

### `auth`

tela supports two authentication modes.

#### Open mode

```yaml
auth:
  mode: "open"
```

Use open mode only in trusted environments.

#### Token mode

```yaml
auth:
  mode: "token"
  secrets:
    - "${TELA_SECRET}"
    - "${TELA_SECRET_PREVIOUS}"
```

Use token mode for shared or production deployments.

Note: The gateway also generates a per-instance bearer token (stored in the
lockfile) to protect HTTP endpoints. This is independent of config `auth.mode`.

### `audit`

Audit logs are written as JSONL.

```yaml
audit:
  level: "L2"
  output: "~/.tela/audit.jsonl"
```

Audit levels:

- `L1`: minimal records
- `L2`: standard operational detail
- `L3`: verbose diagnostic detail

Each audit entry includes an `instance_id` field identifying which server
instance produced it.

## Running tela

### `tela connect` (recommended for most users)

```bash
tela connect --config tela.yaml
```

This is the standard entry point. Your MCP host launches this as a child
process. Under the hood it:

1. Checks `~/.tela/gateway.lock` for a running server
2. Auto-starts one if needed (random port, detached process)
3. Bridges stdio ↔ HTTP

Multiple `tela connect` instances share the same server. Downstream servers
are spawned once.

MCP host configuration:

```json
{
  "mcpServers": {
    "tela": {
      "command": "tela",
      "args": ["connect", "--config", "tela.yaml"]
    }
  }
}
```

### `tela serve` (explicit server)

```bash
tela serve --config tela.yaml --port 8080
tela serve --config tela.yaml --host 0.0.0.0 --port 8080     # LAN
```

Use when you need explicit control over host/port. Writes a lockfile so
`tela connect` and query commands can discover it.

Direct HTTP client configuration:

```json
{
  "mcpServers": {
    "tela": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

### Auto-shutdown

When `tela connect` auto-starts a server, it shuts down after 5 minutes of
idle time (no connected bridges). Configurable via `--idle-timeout`. Set to `0`
to disable.

Manually started servers (`tela serve`) never auto-shutdown.

## Client connection patterns

### Pattern 1: local development (recommended)

```json
{
  "mcpServers": {
    "tela": {
      "command": "tela",
      "args": ["connect", "--config", "tela.yaml"]
    }
  }
}
```

Multiple Claude Code / OpenCode instances share one auto-managed gateway.

### Pattern 2: shared gateway (LAN or team)

Start the server explicitly:

```bash
tela serve --config tela.yaml --host 0.0.0.0 --port 8080
```

Clients connect via HTTP:

```json
{
  "mcpServers": {
    "tela": {
      "type": "http",
      "url": "http://gateway-host:8080/mcp"
    }
  }
}
```

Or via `tela connect` with explicit server:

```json
{
  "mcpServers": {
    "tela": {
      "command": "tela",
      "args": ["connect", "--server", "gateway-host:8080"]
    }
  }
}
```

### Practical client guidance

- use `tela connect` as the default — it handles everything automatically
- use `tela serve` when you need fixed host/port for LAN or CI
- prefer `open` mode only for local trusted environments
- prefer `token` mode for shared infrastructure

## Multi-agent deployment patterns

### Pattern A: shared local gateway (default)

Multiple agents share one auto-managed tela.

```text
Agent A ──connect──┐
Agent B ──connect──┤──→ tela serve (auto) ──→ downstream servers (1 copy each)
Agent C ──connect──┘
```

This is the default behavior. No manual server management needed.

### Pattern B: shared team gateway

One explicit gateway for a team.

```text
Agent A ──HTTP──┐
Agent B ──HTTP──┤──→ tela serve (manual, LAN) ──→ downstream servers
Agent C ──HTTP──┘
```

Use `tela serve --host 0.0.0.0 --port 8080` with token auth.

### Pattern C: mixed mode

- developers use `tela connect` locally
- CI/shared agents connect directly to a team gateway via HTTP

## Suggested deployment recipes

### Recipe: one developer, one workstation

- auth mode: `open`
- entry: `tela connect --config tela.yaml`
- default profile: custom `developer` profile
- audit level: `L2`

### Recipe: shared internal gateway

- auth mode: `token`
- entry: `tela serve --host 0.0.0.0 --port 8080`
- default profile: conservative shared profile
- audit level: `L3`
- secrets: environment variables only

### Recipe: CI or bot operator

- auth mode: `token`
- entry: direct HTTP to `tela serve`
- profile: purpose-built automation profile

## CLI reference

### `tela connect`

```bash
tela connect [--config path] [--default-profile name] [--server host:port]
```

- `--config`: configuration file path (default: `tela.yaml`)
- `--default-profile`: override the open-mode default profile
- `--server`: explicit server address as `host:port` (e.g. `192.168.1.10:8080`; skip auto-discover/auto-start)

### `tela serve`

```bash
tela serve [--config path] [--port N] [--host addr] [--default-profile name] [--idle-timeout sec]
```

- `--config`: configuration file path (default: `tela.yaml`)
- `--port`: port to bind (default: `0` for ephemeral)
- `--host`: bind address (default: `127.0.0.1`)
- `--default-profile`: override the open-mode default profile
- `--idle-timeout`: seconds before auto-shutdown on idle (default: `300`, `0` to disable)

### Query commands

```bash
tela status [--json]
tela profiles [--config path] [--json]
tela connections [--json]
tela audit [--json] [--since T] [--limit N]
```

Query commands discover the running server via `~/.tela/gateway.lock`.

## Introspection

The gateway exposes MCP tools for runtime queries:

| Tool | Description |
|------|-------------|
| `tela.status` | Uptime, server count, connection count |
| `tela.connections` | Active upstream connections |
| `tela.audit` | Query audit log entries |
| `tela.profiles` | List configured profiles |

These belong to the `tela_admin` family. Add `tela_admin: "read_only"` to a
profile's capabilities to grant access. Profiles without `tela_admin` cannot
see these tools.

## Environment variables

tela supports `${VAR}` and `$VAR` expansion in config values.

Common variables:

- `TELA_SECRET`
- `TELA_SECRET_PREVIOUS`
- `TELA_STATE`
- `HOME`

## Troubleshooting

### `open` mode fails to start cleanly

Check that exactly one profile is suitable as the default, or pass
`--default-profile` explicitly.

### A server is rejected by config validation

Check that each server defines exactly one transport:

- `command` for stdio
- `url` for SSE (default) or Streamable HTTP (`transport: http`)

Not both `command` and `url`, and not neither.

### A tool is unexpectedly unavailable

Check, in order:

1. family admission
2. tool override check
3. posture ceiling comparison

### `tela status` shows empty state

Ensure a server is running. Check `~/.tela/gateway.lock` exists and is not
stale. Query commands need a running server to report state.

### Server won't start (port conflict)

If using a fixed port (`--port 8080`), check for existing processes on that
port. Use `--port 0` (default) to let the OS assign an available port.

## Validation and testing

```bash
uv run pytest -q
uv run pytest --doctest-modules src/tela/
uv run invar guard --all
```

## Related files

- `README.md`
- `tela.yaml.example`
- `docs/DESIGN.md`
- `docs/INTERFACES.md`
