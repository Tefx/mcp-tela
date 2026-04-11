# tela

tela is an MCP aggregation gateway. It connects multiple downstream MCP servers
and exposes them as one upstream MCP endpoint with profile-based tool filtering,
policy enforcement, and audit logging.

## What tela does

- Aggregates multiple MCP servers behind one endpoint
- Applies family-based posture ceilings and per-tool overrides
- Supports open mode or token-based authentication
- Emits structured audit logs at `L1`, `L2`, or `L3`
- Auto-discovers and auto-starts a shared gateway (`tela connect`)
- Supports Streamable HTTP (default) and legacy SSE transports
- Ships with a built-in profile catalog for common access patterns

## Canonical contract note

In composed deployments, `../opifex` is the canonical authority for shared
CapabilityToken, `_meta`, and shared error semantics.

- shared token binding identity is `profile_id`
- `tools_profile` is not canonical shared vocabulary
- `profile_name`, if shown locally, is a display label only
- `_meta.trace_id` and related canonical `_meta` fields are audit carriers;
  tela records them and strips `_meta` before forwarding downstream

## Quick start

```bash
pip install -e .
cp tela.yaml.example tela.yaml
```

Wire your MCP host to launch tela:

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

That's it. `tela connect` auto-starts a shared gateway on a random port and
bridges stdio to it. Multiple clients share one gateway — downstream servers
are only spawned once.

## Two commands

| Command | Role | When to use |
|---------|------|-------------|
| `tela connect` | Client entry point (stdio bridge) | MCP host config, local development |
| `tela serve` | Server entry point (HTTP gateway) | LAN deployment, manual server management |

### `tela connect` (most common)

```bash
tela connect --config tela.yaml
```

What it does:
1. Checks if a shared gateway is already running (via `~/.tela/gateway.lock`)
2. If not, auto-starts one in the background
3. Bridges stdio ↔ HTTP so your MCP host sees a normal stdio server

Readiness boundary:
- the lockfile is discovery-only, not readiness truth
- gateway runtime lifecycle plus `GET /status` is the sole readiness authority
- `tela connect` must not create or own readiness state, cached readiness truth, or local lifecycle labels
- readiness waiting must consult `GET /status` with bounded polling (not fixed sleep delays)
- retry is allowed only when the gateway emits a transient non-ready contract signal
- persistent degraded/non-ready authority must end in a clean bounded exit
- discovery-before-readiness: lockfile may exist before downstream convergence completes

### `tela serve` (explicit server)

```bash
tela serve --config tela.yaml --port 8080                    # fixed port
tela serve --config tela.yaml --host 0.0.0.0 --port 8080     # LAN deployment
```

Use `tela serve` when you need explicit control over host/port, or for shared
LAN deployments.

## Architecture

```text
Claude Code A ──stdio──→ tela connect ──┐
Claude Code B ──stdio──→ tela connect ──┤── HTTP ──→ tela serve (shared)
OpenCode C   ──stdio──→ tela connect ──┘              ├──stdio──→ fs (1 copy)
                                                       ├──stdio──→ git (1 copy)
                                                       └──stdio──→ larva (1 copy)
```

Multiple clients share one gateway. Downstream servers are spawned once, not
per-client. The gateway auto-starts on first `tela connect` and auto-shuts down
when all connections close (5-minute idle timeout).

## Configuration at a glance

tela reads a single YAML config file. The top-level sections are:

- `servers`: downstream MCP servers to connect to
- `profiles`: access control rules for clients
- `auth`: `open` or `token` mode
- `audit`: log verbosity and output path

Minimal example:

```yaml
servers:
  fs:
    command: "mcp-filesystem"
    args: ["--root", "/workspace"]
    family: "filesystem"

profiles:
  developer:
    capabilities:
      filesystem: "read_write"
    default: true

auth:
  mode: "open"

audit:
  level: "L2"
  output: "~/.tela/audit.jsonl"
```

See `tela.yaml.example` for the full commented reference.

## Connecting MCP clients

### stdio (via `tela connect`)

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

### HTTP (direct to `tela serve`)

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

## Built-in Surfaces

### MCP Resource

- `tela.profiles` — list configured profiles (MCP resource read via `tela://profiles`)
  - **Access:** Resource read, not `tools/call`

### Operator Surfaces (CLI/HTTP)

The following are operator-only surfaces, accessible via CLI commands or HTTP endpoints:

- `tela status` — uptime, server count, connection count (CLI/HTTP)
- `tela profiles` — configured profile listing (CLI/HTTP)
- `tela connections` — active upstream connections (CLI/HTTP)
- `tela audit` — query audit log (CLI/HTTP)

**Note:** These are not MCP built-in tools. Do not call via `tools/call`.

## Features

- **Tool metadata passthrough**: Preserves `annotations`, `title`, and `outputSchema` from downstream servers
- **Instructions merging**: Configurable per-server instructions (`passthrough`, `suppress`, or `override`)
- **Notification forwarding**: Forwards `notifications/tools/list_changed` from downstream to upstream clients

## CLI

```text
tela connect [--config path] [--default-profile name] [--server host:port] [--token tok]
tela serve   [--config path] [--port N] [--host addr] [--default-profile name] [--idle-timeout sec] [--token tok]
tela status  [--json]
tela profiles [--config path] [--json]
tela connections [--json]
tela audit   [--json] [--since ISO-8601] [--limit N]
```

## Core FAQ

### Why two commands?

`tela connect` and `tela serve` separate concerns:
- `tela connect` is the client bridge — it auto-discovers or auto-starts a shared gateway
- `tela serve` is the actual gateway — it runs downstream servers and handles HTTP

Multiple `tela connect` instances share one `tela serve`, avoiding duplicate
downstream processes and centralized configuration.

### Can multiple agents share one gateway?

Yes. That's the default behavior. Each `tela connect` bridges to the same shared
`tela serve` instance.

### How does auto-shutdown work?

When all clients disconnect, the server waits 5 minutes (configurable via
`--idle-timeout`) then shuts down. This applies to both auto-started and
manually started servers. Use `--idle-timeout 0` to keep a server running
indefinitely.

### How is the gateway protected?

Every `tela serve` instance auto-generates a bearer token and stores it in the
lockfile (`~/.tela/gateway.lock`). When started manually, the token is also
printed to stderr. When auto-started by `tela connect`, stderr is not visible —
the token is only available in the lockfile. All HTTP endpoints require this
token. `tela connect` reads it automatically from the lockfile. Remote clients
pass it via `--token` or `TELA_BEARER_TOKEN`. Use `--token` on `tela serve` to
set a fixed token for automation/CI. This is independent of config `auth.mode`
(which controls MCP-level profile binding).

### What about LAN deployment?

```bash
tela serve --host 0.0.0.0 --port 8080
# prints: tela: bearer token: tela_tok_a1b2c3d4...
```

Copy the printed token and give it to remote clients:

```bash
tela connect --server 192.168.1.10:8080 --token "tela_tok_a1b2c3d4..."
# or
TELA_BEARER_TOKEN="tela_tok_a1b2c3d4..." tela connect --server 192.168.1.10:8080
```

## Testing

```bash
uv run pytest -q
uv run pytest --doctest-modules src/tela/
uv run invar guard --all
```

## Documentation

- `README.md`: project overview and quickstart
- `docs/USAGE.md`: operator guide and deployment patterns
- `docs/ARCHITECTURE-REFACTOR-ASSESSMENT.md`: verified architecture debt,
  simplification targets, and safe refactor order
- `tela.yaml.example`: commented configuration template
- `docs/INTERFACES.md`: CLI and config contract surface
- `docs/DESIGN.md`: architecture and implementation detail

## License

MIT
