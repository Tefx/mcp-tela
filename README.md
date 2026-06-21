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
- legacy token alias fields are not canonical shared vocabulary
- shared/profile-binding docs use `profile_id` only
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
- `ready` and `degraded` are admission-eligible bridge states; degraded mode uses the partial registry and surfaces `degraded_reason`
- persistent `warming` or another non-admission state must end in a clean bounded exit
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

Downstream server entries may also use `tool_prefix` for namespacing (for example `prod_`, `work_`, or `host_`), `exclude_tools` for raw-name filtering, and explicit `nested_gateway: true` when the downstream server is another Tela gateway.

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

### MCP Tools

- `tela_list_providers` — returns configured servers and their runtime status
- `tela_list_profiles` — returns configured profiles with `profile_id`, `capabilities`, and `default` flags

### Operator Surfaces

The following are operator-only CLI surfaces:

- `tela status` — uptime, server count, connection count
- `tela status --probe` — observation-only endpoint probe
- `tela status --clients` — read-only attachment registry view
- `tela profiles` — configured profile listing
- `tela connections` — active upstream connections
- `tela audit` — query audit log
- `tela doctor` — observation-only diagnostic
- `tela doctor --recover` — explicit operator recovery
- `tela stop` — local process control via lockfile discovery and SIGTERM

The following are operator-only HTTP surfaces:

- `GET /status` — runtime status endpoint
- `GET /operator/probe` — observation-only current-endpoint snapshot
- `GET /operator/clients` — read-only attachment registry view
- `GET /operator/audit` — paginated audit endpoint
- `GET /operator/authorization/explain` — diagnostic authorization explanation

**Note:** These are not MCP built-in tools. Do not call via `tools/call`.

## Features

- **Tool metadata passthrough**: Preserves `annotations`, `title`, and `outputSchema` from downstream servers
- **Tool namespacing and filtering**: `tool_prefix` namespaces downstream tools; `exclude_tools` removes raw downstream tool names before exposure
- **Nested Tela gateways**: explicit `nested_gateway: true` marks a downstream Tela gateway, requires a non-empty prefix such as `host_`, and hides child Tela built-ins while preserving the parent built-ins; omitted `nested_gateway` with a valid prefix preserves prefixed child built-ins unless raw-name `exclude_tools` is configured
- **Instructions merging**: Configurable per-server instructions (`passthrough`, `suppress`, or `override`)
- **Notification forwarding**: Forwards `notifications/tools/list_changed` from downstream to upstream clients

## CLI

Common CLI shape (abbreviated). See `docs/INTERFACES.md` for authoritative
surface semantics and option contracts.

```text
tela connect [--config path] [--default-profile name] [--server host:port] [--token tok]
tela serve   [--config path] [--port N] [--host addr] [--default-profile name] [--idle-timeout sec] [--token tok]
tela status  [--json] [--probe] [--clients]
tela profiles [--config path] [--json]
tela connections [--json]
tela audit   [--json] [--since ISO-8601] [--limit N]
tela doctor  [--json] [--recover]
tela stop
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
uv sync --frozen --group dev
AUTHORITY_REF=$(python - <<'PY'
import json
from pathlib import Path
print(json.loads(Path("design/opifex-frozen-authority-packet.json").read_text())["ref"])
PY
)
test "$(git -C ../opifex rev-parse HEAD)" = "$AUTHORITY_REF"
OPIFEX_ROOT=../opifex uv run python scripts/ci/mcp_tela_shared_surface_gate.py expected-red
OPIFEX_ROOT=../opifex uv run python scripts/ci/mcp_tela_shared_surface_gate.py green
```

See `docs/CI-REPO-LOCAL-SHARED-SURFACE-GATES.md` for the exact branch-protection
job names and the opifex-authoritative repo-local gate scope.

## Documentation

- `README.md`: project overview and quickstart
- `docs/USAGE.md`: operator guide and deployment patterns
- `docs/ARCHITECTURE-REFACTOR-ASSESSMENT.md`: completed refactor record, architecture simplification results
- `tela.yaml.example`: commented configuration template
- `docs/INTERFACES.md`: CLI and config contract surface
- `docs/DESIGN.md`: architecture and implementation detail

## License

MIT
