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
- Supports Streamable HTTP and legacy SSE transports
- Ships with a built-in profile catalog for common access patterns

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

## Introspection

The gateway exposes MCP tools for runtime introspection:

- `tela.status` — uptime, server count, connection count
- `tela.connections` — active upstream connections
- `tela.audit` — query audit log
- `tela.profiles` — list configured profiles

These are controlled by the `tela_admin` family in profiles.

## CLI

```text
tela connect [--config path] [--default-profile name] [--server host:port]
tela serve   [--config path] [--port N] [--host addr] [--default-profile name] [--idle-timeout sec]
tela status  [--json]
tela profiles [--config path] [--json]
tela connections [--json]
tela audit   [--json] [--since T] [--limit N]
```

## Core FAQ

### Why `tela connect` instead of `tela start`?

`tela connect` auto-discovers a running gateway or starts one. Multiple clients
share the same gateway, avoiding duplicate downstream processes. Old `tela start`
spawned independent processes per client.

### Can multiple agents share one gateway?

Yes. That's the default behavior. Each `tela connect` bridges to the same shared
`tela serve` instance.

### How does auto-shutdown work?

When the last `tela connect` client disconnects, the auto-started server waits
5 minutes (configurable via `--idle-timeout`) then shuts down. Manually started
servers (`tela serve`) never auto-shutdown.

### How is the gateway protected?

Every `tela serve` instance generates a bearer token stored in the lockfile
(`~/.tela/gateway.lock`). All HTTP endpoints require this token. `tela connect`
reads it automatically. Direct HTTP clients must provide it as an
`Authorization: Bearer <token>` header. This is independent of config
`auth.mode` (which controls MCP-level profile binding).

### What about LAN deployment?

Use `tela serve --host 0.0.0.0 --port 8080` for LAN. Remote clients use
`tela connect --server <ip>:8080` or connect directly via HTTP.

## Testing

```bash
uv run pytest -q
uv run pytest --doctest-modules src/tela/
uv run invar guard --all
```

## Documentation

- `README.md`: project overview and quickstart
- `docs/USAGE.md`: operator guide and deployment patterns
- `tela.yaml.example`: commented configuration template
- `docs/INTERFACES.md`: CLI and config contract surface
- `docs/DESIGN.md`: architecture and implementation detail

## License

MIT
