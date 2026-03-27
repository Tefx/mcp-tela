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
- `url` for Streamable HTTP (default, MCP 2025-03-26+), omitted `transport`
- `url` + `transport: sse` for SSE (legacy)

Minimal stdio example:

```yaml
servers:
  fs:
    command: "mcp-filesystem"
    args: ["--root", "/workspace"]
    family: "filesystem"
```

Minimal Streamable HTTP example:

```yaml
servers:
  github:
    url: "http://localhost:3001/mcp"
    family: "git"
```

Minimal SSE example (legacy):

```yaml
servers:
  legacy-server:
    url: "http://localhost:3001/sse"
    transport: sse
    family: "other"
```

Important notes:

- the YAML key is the server name
- if `family` is omitted, tela uses the server name as the family by convention
- `default_posture` sets the baseline posture for tools from that server
- `tool_overrides` can adjust family or posture for specific tools
- `instructions` controls how server instructions are merged (see below)

#### Instructions configuration

The `instructions` field in a server entry controls how that server's instructions are merged into the upstream server's instructions:

```yaml
servers:
  fs:
    command: "mcp-filesystem"
    args: ["--root", "/workspace"]
    family: "filesystem"
    instructions: false           # Suppress this server's instructions

  github:
    url: "http://localhost:3001/mcp"
    family: "git"
    instructions: |               # Override with custom instructions
      GitHub MCP server providing repository access.

  websearch:
    url: "http://localhost:3002/mcp"
    family: "web"
    # instructions omitted: passthrough downstream instructions
```

| Value | Behavior |
|-------|----------|
| `null` (default) | Use downstream server's instructions if available |
| `false` | Exclude this server's instructions entirely |
| string | Use the provided string instead of downstream instructions |

The merged instructions appear in the upstream server's `instructions` field as Markdown with H2 headers for each server.

**Merge semantics:**
1. Tela top-level gateway instructions are emitted first
2. Downstream sections are appended in configured server iteration order
3. Per-server rules:
   - `instructions: false` → suppress that server's section
   - `instructions: <string>` → use explicit override string
   - `instructions: null` / omitted → use downstream's advertised instructions
4. When tools are known, an `Available tools:` list is appended per server section

**Conflict handling:**
- Current implementation is append-only composition: gateway block, then downstream sections
- No semantic conflict resolver is implemented; contradictory downstream text is preserved as appended text
- To avoid contradictory guidance, use `instructions: false`/`instructions: <string>` or revise docs/spec explicitly

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
    tool_overrides:
      filesystem:
        overrides:
          delete_file: "deny"
      git:
        overrides:
          force_push: "allow"
    default: true
```

Built-in surfaces:
- `tela.profiles` is exposed as an MCP resource (read via resource read, not `tools/call`)
- `tela profiles`, `tela status`, `tela connections`, and `tela audit` are operator-only surfaces accessible via CLI/HTTP
- No `tela.*` built-in MCP tools are currently supported

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

Note: The gateway also auto-generates a per-instance bearer token on every
startup and stores it in the lockfile. When `tela serve` is started manually,
the token is also printed to stderr. When auto-started by `tela connect`, stderr
is not visible — the token is only available in the lockfile. This protects
HTTP endpoints and is independent of config `auth.mode`. Use `--token` on
`tela serve` to set a fixed token, or set `TELA_BEARER_TOKEN`.

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

The server shuts down after 5 minutes of idle time (no connected bridges) by
default. This applies to both auto-started and manually started servers.
Configurable via `--idle-timeout`. Set to `0` to keep the server running
indefinitely:

```bash
tela serve --config tela.yaml --port 8080 --idle-timeout 0
```

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
# prints: tela: bearer token: tela_tok_a1b2c3d4...
```

Copy the printed bearer token and distribute to clients.

Remote clients via `tela connect`:

```json
{
  "mcpServers": {
    "tela": {
      "command": "tela",
      "args": ["connect", "--server", "gateway-host:8080", "--token", "tela_tok_a1b2c3d4..."]
    }
  }
}
```

Or set `TELA_BEARER_TOKEN` as an environment variable instead of `--token`.

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
tela connect [--config path] [--default-profile name] [--server host:port] [--token tok]
```

- `--config`: configuration file path (default: `tela.yaml`)
- `--default-profile`: override the open-mode default profile
- `--server`: explicit server address as `host:port` (e.g. `192.168.1.10:8080`; skip auto-discover/auto-start)
- `--token`: bearer token for remote server auth (or set `TELA_BEARER_TOKEN`)

### `tela serve`

```bash
tela serve [--config path] [--port N] [--host addr] [--default-profile name] [--idle-timeout sec] [--token tok]
```

- `--config`: configuration file path (default: `tela.yaml`)
- `--port`: port to bind (default: `0` for ephemeral)
- `--host`: bind address (default: `127.0.0.1`)
- `--default-profile`: override the open-mode default profile
- `--idle-timeout`: seconds before auto-shutdown on idle (default: `300`, `0` to disable)
- `--token`: fixed bearer token override (default: auto-generated; or set `TELA_BEARER_TOKEN`)

When started manually, the bearer token is printed to stderr for easy copy-paste.
When auto-started by `tela connect`, the token is only stored in the lockfile.

### Query commands

```bash
tela status [--json]
tela profiles [--config path] [--json]
tela connections [--json]
tela audit [--json] [--since ISO-8601] [--limit N]
```

Query commands discover the running server via `~/.tela/gateway.lock`.

## Built-in surfaces

### MCP Resource

- `tela.profiles` — list configured profiles (MCP resource read via `tela://profiles`)
  - **Access:** Resource read, not callable via `tools/call`

### Operator Surfaces (CLI/HTTP)

The following are operator-only surfaces, not MCP built-in tools:

| Surface | Access | Description |
|---------|--------|-------------|
| `tela profiles` | CLI / via `/status` | List configured profiles and capability ceilings |
| `tela status` | CLI / `GET /status` | Uptime, server count, connection count |
| `tela connections` | CLI / via `/status` | Active upstream connections |
| `tela audit` | CLI / via `/status` | Query audit log entries |

**Note:** These surfaces are accessible via CLI or HTTP, not via MCP `tools/call`.

## Tool metadata passthrough

The `tools/list` response includes metadata fields from downstream servers:

| Field | Description |
|-------|-------------|
| `annotations` | MCP tool annotations including `readOnlyHint`, `destructiveHint` |
| `title` | Human-readable tool title |
| `outputSchema` | JSON schema for tool output if provided |

These fields are preserved from the downstream server and used for posture classification (annotations) and client-side display.

## Notification forwarding

When a downstream server sends `notifications/tools/list_changed`, the gateway:
1. Re-enumerates the server's tools
2. Runs conflict detection
3. If no conflicts: notifies all connected upstream clients via MCP `notifications/tools/list_changed`

This is a best-effort mechanism. Notifications may not reach clients on transports that don't support session capture (e.g., some stdio configurations).

## Environment variables

tela supports `${VAR}` and `$VAR` expansion in config values.

Common variables:

- `TELA_SECRET`
- `TELA_SECRET_PREVIOUS`
- `TELA_BEARER_TOKEN`
- `TELA_STATE`
- `HOME`

## Troubleshooting

### `open` mode fails to start cleanly

Check that exactly one profile is suitable as the default, or pass
`--default-profile` explicitly.

### A server is rejected by config validation

Check that each server defines exactly one transport:

- `command` for stdio
- `url` for Streamable HTTP (default) or SSE (`transport: sse`)

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
