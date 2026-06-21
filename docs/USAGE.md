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

Minimal Streamable HTTP example with `headers`:

```yaml
servers:
  resofeed:
    url: "https://resofeed.tefx.one/mcp"
    headers:
      Authorization: "${RESOFEED_AUTHORIZATION}"
      X-Custom-Auth: "fixed-value"
    family: "resofeed"
```

The `headers` field is a `dict[str, str]` representing literal header keys and values. It supports the existing parse-time `${VAR}` / `$VAR` expansion. `headers` is only valid for URL transports (Streamable HTTP and `transport: sse`); it is strictly rejected for stdio `command` servers. This is downstream transit configuration, entirely distinct from the `auth` section (which controls upstream client access to the gateway).

Minimal SSE example (legacy):

```yaml
servers:
  legacy-server:
    url: "http://localhost:3001/sse"
    transport: sse
    family: "other"
```

#### Tool Prefix Configuration

The `tool_prefix` field configures a namespace prefix for all tools from a server:

```yaml
servers:
  fs-prod:
    command: "mcp-filesystem"
    args: ["--root", "/prod"]
    family: "filesystem"
    tool_prefix: "prod_"
  fs-staging:
    command: "mcp-filesystem"
    args: ["--root", "/staging"]
    family: "filesystem"
    tool_prefix: "staging_"
```

With this configuration:
- Both servers export `read_file`, `write_file`, etc.
- Upstream sees `prod_read_file` and `staging_read_file` (no conflict)
- `tool_overrides` in profiles still reference raw downstream names (`read_file`, not `prod_read_file`)

**Important semantics:**

- `tool_overrides` keys remain raw downstream tool names, not prefixed names
- `exclude_tools` keys also match raw downstream tool names, before any prefix is applied
- Final exposed names (after filtering and prefixing) are used for conflict detection
- `tool_prefix: null` (or omitted) preserves backward-compatible behavior (raw names exposed unchanged)
- final exposed MCP tool names must remain snake_case; prefixes like `prod_` are recommended, while dotted exposed names are not valid shared MCP tool names
- `tool_prefix: "tela."` and `tool_prefix: "tela_"` are reserved and rejected at validation/construction
- plain `tool_prefix: "tela"` (no `.` or `_`) remains allowed because it does not enter the reserved `tela.`/`tela_` namespaces and can still produce snake_case names such as `telaread_file`
- Changing `tool_prefix`, `exclude_tools`, or `nested_gateway` counts as a tool-surface change and triggers `tools/list_changed`

Example with two same-raw-name tools exposed with different prefixes:

```yaml
servers:
  git-work:
    command: "mcp-github"
    env:
      GITHUB_TOKEN: "${WORK_GITHUB_TOKEN}"
    family: "git"
    tool_prefix: "work_"
  git-personal:
    command: "mcp-github"
    env:
      GITHUB_TOKEN: "${PERSONAL_GITHUB_TOKEN}"
    family: "git"
    tool_prefix: "personal_"
```

Upstream clients see `work_search_repos` and `personal_search_repos` — both from the same downstream tool, but exposed with distinct prefixes.

#### Tool Filtering and Nested Tela Gateways

Use `exclude_tools` to hide selected tools from one downstream server:

```yaml
servers:
  host_gui:
    url: "http://127.0.0.1:18081/mcp"
    headers:
      Authorization: "Bearer ${HOST_TELA_TOKEN}"
    tool_prefix: "host_"
    exclude_tools:
      - "tela_list_providers"
      - "tela_list_profiles"
    default_posture: "read_write"
```

`exclude_tools` matches raw downstream tool names, not prefixed exposed names. With the configuration above, raw child tools named `tela_list_providers` and `tela_list_profiles` are removed before `host_` is applied, so upstream clients do not see `host_tela_list_providers` or `host_tela_list_profiles`.

For a downstream server that is itself a Tela gateway, prefer the explicit nested mode:

```yaml
servers:
  host_gui:
    url: "http://127.0.0.1:18081/mcp"
    headers:
      Authorization: "Bearer ${HOST_TELA_TOKEN}"
    tool_prefix: "host_"
    nested_gateway: true
    default_posture: "read_write"
```

`nested_gateway: true` declares that the downstream is another Tela gateway. It requires `tool_prefix` and automatically hides the child gateway's Tela built-in introspection tools:

- `tela_list_providers`
- `tela_list_profiles`

The parent gateway's own `tela_list_providers` and `tela_list_profiles` remain visible. If a downstream server exposes child Tela built-ins without a `tool_prefix`, Tela fails closed with `NESTED_TELA_PREFIX_REQUIRED` and tells the operator to add a non-empty `tool_prefix`, use `nested_gateway: true` for Tela children, or use explicit `exclude_tools` for generic filtering. Detection never silently hides tools unless `exclude_tools` or `nested_gateway: true` is configured.

Important notes:

- the YAML key is the server name
- if `family` is omitted, tela uses the server name as the family by convention
- `default_posture` sets the baseline posture for tools from that server
- `tool_overrides` can adjust family or posture for specific tools
- `tool_prefix` prefixes exposed tool names (see below)
- `exclude_tools` hides raw downstream tool names before prefixing
- `nested_gateway: true` marks a downstream Tela gateway and hides child Tela built-ins
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

Built-in MCP surfaces (require admitted session, accept `{}` only):
- `tela_list_profiles` — built-in MCP tool returning profile list with `profile_id`, `capabilities`, and `default`
- `tela_list_providers` — built-in MCP tool returning `provider_name`, caller-bound `profile_id`, `status`, `tool_prefix`, `tool_count`, and `tool_names`

Builtin calls fail closed without an admitted session/connection. Builtin audit
entries and provider visibility both bind to the calling connection's admitted
`profile_id`.

Operator-only surfaces, not MCP:
- CLI: `tela profiles`, `tela status`, `tela status --probe`, `tela status --clients`, `tela connections`, `tela audit`, `tela doctor`, `tela doctor --recover`, `tela stop`
- HTTP: `GET /status`, `GET /operator/probe`, `GET /operator/clients`, `GET /operator/audit`, `GET /operator/authorization/explain`

Important notes:

- in `open` mode, at most one profile may have `default: true`; multi-default shared profile listings are rejected fail-closed with `invalid_default_profile_state`
- custom capability groups are valid, but built-in profiles only cover built-in capability sets
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

Canonical note: in composed deployments, the shared token contract is owned by
`../opifex`. The canonical binding identity is `profile_id`, and shared token
surfaces do not define any alternate binding field. `token_version` is explicit
and required at admission; omission or mismatch is rejected fail-closed.

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

1. Discovers an existing gateway via `~/.tela/gateway.lock` (discovery only—lockfile presence does not imply readiness or downstream server convergence)
2. Auto-starts one if needed (random port, detached process)
3. Bridges stdio ↔ HTTP once the gateway HTTP endpoint is bound and `GET /status` reports readiness

Multiple `tela connect` instances share the same server and are represented as
client-neutral client attachments to one shared runtime. Downstream servers are
spawned once by the gateway process; `tela status --clients` lists the active
attachments without implying that each client owns a separate runtime.

**Note on lockfile semantics**: The lockfile provides endpoint discovery only.
It does not indicate downstream server readiness, tool enumeration completion,
or registry convergence. Use `tela status` to check authoritative runtime state.
`tela connect` must not treat its own local bridge progress as readiness truth;
gateway runtime lifecycle plus `GET /status` remains the sole readiness authority.
When `tela connect` needs to wait on readiness, it must do so by consulting
`GET /status` with status-driven polling (not fixed sleep delays). Retry is allowed only when
the gateway emits the transient non-ready contract for `POST /mcp`. `ready` and
`degraded` are admission-eligible bridge states; degraded mode proceeds against
the partial registry while preserving `degraded_reason` for operator diagnostics.
Persistent `warming` or another non-admission state must end in a clean bounded exit.

**Discovery-before-readiness cold-start**: After `tela connect` discovers the
endpoint via lockfile, the bridge registers via `POST /connect` using canonical
request key `server_name` and then polls `GET /status` for readiness. The
polling is bounded (default 8 polls); if the
gateway does not reach `ready` or `degraded`, the bridge exits cleanly rather
than retrying indefinitely.

**Bridge recovery**: During MCP forwarding, transient connection errors (e.g.,
`Connection refused`, `Connection reset`, `HTTP 503`, readiness timeouts) may
occur. The bridge attempts bounded recovery:
- Classification via `_is_recoverable_error`: transient network errors are
  recoverable; unknown errors are not. Degraded status by itself is not a
  recovery trigger because MCP traffic can continue against the partial registry.
- Up to `--max-recovery-attempts` recovery cycles (default: 3).
- Recovery budgets are per event/request and reset after a request-scoped
  recovery error or a successful forwarded response; unrelated client events do
  not inherit exhausted attempts.
- Each cycle: re-discover via lockfile, re-poll readiness, re-register via
  `POST /connect`, then resume forwarding.
- Recovery is best-effort; exhausted attempts or unrecoverable errors are
  returned as request-level JSON-RPC errors when possible so the provider loop
  remains alive for later requests.

**Client attachment lifecycle**: EOF from the host stdio transport is a normal
client attachment closure. `tela connect` exits, unregisters the bridge, records
`host_transport_closed`, then records provider exit diagnostics. `tela status
--probe` observes the current lockfile endpoint only and does not cold-start an
absent runtime. `tela doctor` is passive without --recover; use `tela doctor --recover`
when you want explicit recovery mutations and recovery events.

**Session reset semantics**: When recovery succeeds and forwarding resumes,
the bridge continues with the same `connection_id`. Downstream sessions are
scoped to the gateway runtime; a bridge reconnect does not reset downstream
state. Upstream MCP clients should treat a bridge exit as a session termination
and re-initialize if they reconnect.

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
`tela connect` and query commands can discover the HTTP endpoint.

**Lockfile semantics**: The lockfile is written after the HTTP server binds
but before downstream servers are fully connected. It provides endpoint
discovery only—not readiness or downstream convergence guarantees.
Bridge registration via `POST /connect` also remains non-authoritative for
readiness; only gateway runtime status is authoritative.

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

### Config hot reload

`tela serve` watches its loaded config file and hot-reloads changes without
dropping upstream client connections.

- polling-based watch on the running server's `config_path`
- applies added/removed/changed downstream server definitions
- re-enumerates tools and sends MCP `notifications/tools/list_changed` on success
- rejects a reload that would introduce tool conflicts; the previous runtime
  state stays in effect

Operational notes:

- if you started tela with `tela serve --config /path/to/tela.yaml`, edits to
  that file are the ones that will be reloaded
- if you use `tela connect` and it reuses an already-running shared gateway,
  that gateway keeps watching the config file it was originally started with
- changing a different local `tela.yaml` does not retarget an existing shared
  gateway; restart with the desired `--config` if you need to change ownership

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
tela connect [--config path] [--default-profile name] [--server host:port] [--token tok] [--max-recovery-attempts N]
```

- `--config`: configuration file path (default: `tela.yaml`)
- `--default-profile`: override the open-mode default profile
- `--server`: explicit server address as `host:port` (e.g. `192.168.1.10:8080`; skip auto-discover/auto-start)
- `--token`: bearer token for remote server auth (or set `TELA_BEARER_TOKEN`)
- `--max-recovery-attempts`: maximum transient error recovery retries (default: `3`)

### `tela serve`

```bash
tela serve [--config path] [--port N] [--host addr] [--default-profile name] [--idle-timeout sec] [--reaper-sweep-interval sec] [--reaper-native-ttl sec] [--reaper-bridge-ttl sec] [--token tok]
```

- `--config`: configuration file path (default: `tela.yaml`)
- `--port`: port to bind (default: `0` for ephemeral)
- `--host`: bind address (default: `127.0.0.1`)
- `--default-profile`: override the open-mode default profile
- `--idle-timeout`: seconds before auto-shutdown on idle (default: `300`, `0` to disable)
- `--reaper-sweep-interval`: override connection-reaper sweep interval seconds (CLI wins over config)
- `--reaper-native-ttl`: override native idle TTL seconds (`0` disables native reaping)
- `--reaper-bridge-ttl`: override bridge idle TTL seconds (`0` disables bridge reaping; default bridge TTL is `900`)
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

### Local control commands

```bash
tela stop
```

`tela stop` uses the same lockfile to discover the local gateway process, sends `SIGTERM`, waits boundedly for exit, and then cleans the lockfile.
`tela stop` exits `0` on confirmed stop and exits `1` for no-running-server or signal/permission failures (with an error message on stderr).

## Built-in surfaces

### MCP Tools

- `tela_list_profiles` — built-in MCP tool returning configured profiles with `profile_id`, `capabilities`, and `default`; requires an admitted session and exact `{}` input
- `tela_list_providers` — built-in MCP tool returning `provider_name`, caller-bound `profile_id`, `status`, `tool_prefix`, `tool_count`, and `tool_names`; requires an admitted session and exact `{}` input

Builtin audit attribution also binds to the calling connection's admitted
`profile_id`.

### Operator Surfaces

The following are operator-only surfaces, not MCP built-in tools.

CLI surfaces:

| Surface | Access | Description |
|---------|--------|-------------|
| `tela profiles` | CLI | List configured profiles and capability ceilings |
| `tela status` | CLI | Uptime, server count, connection count |
| `tela status --probe` | CLI | Observation-only endpoint probe |
| `tela status --clients` | CLI | Read-only attachment registry view |
| `tela connections` | CLI | Active upstream connections |
| `tela audit` | CLI | Query recent audit log entries |
| `tela doctor` | CLI | Observation-only diagnostic |
| `tela doctor --recover` | CLI | Explicit operator recovery |
| `tela stop` | CLI | Send local SIGTERM to the lockfile-discovered gateway process |

HTTP surfaces:

| Surface | Access | Description |
|---------|--------|-------------|
| `GET /status` | HTTP | Runtime status endpoint with recent audit summary |
| `GET /operator/probe` | HTTP | Observation-only current-endpoint snapshot |
| `GET /operator/clients` | HTTP | Read-only attachment registry view |
| `GET /operator/audit` | HTTP | Query paginated audit log entries |
| `GET /operator/authorization/explain` | HTTP | Diagnostic authorization explanation |

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

### Slow or long-running tools

Slow or long-running MCP tools (such as long-polling, large file operations, or human approvals) wait without a bridge-imposed timeout. Slow tools are not bridge recovery failures.

If a tool call is interrupted:
- `MCP_RESPONSE_INTERRUPTED`: The connection was lost after sending an unsafe request (like `tools/call`) and execution is unknown. The request is not automatically replayed to avoid duplicate side effects.
- `MCP_REQUEST_TIMEOUT`: An explicit future deadline expired (not used by default).
- `BRIDGE_RECOVERY_EXHAUSTED`: Only occurs when the bridge exhausts its bounded attempts to recover gateway reachability or start a session, never just because a tool took too long.

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

1. server-level filtering via `exclude_tools` or `nested_gateway: true`
2. family admission
3. tool override check
4. posture ceiling comparison

### Nested Tela server requires a prefix

`NESTED_TELA_PREFIX_REQUIRED` means downstream `tools/list` returned raw `tela_list_providers` or `tela_list_profiles` while `tool_prefix` is omitted or empty. Add a non-empty `tool_prefix` such as `host_`. If the downstream is intentionally another Tela gateway, set `nested_gateway: true`; for generic servers, use explicit `exclude_tools` to hide unwanted raw downstream tools.

### `tela status` shows empty state

Ensure a server is running. Check `~/.tela/gateway.lock` exists and is not
stale. Query commands need a running server to report state.

**Note**: A valid lockfile only proves the gateway HTTP endpoint is discoverable.
Use `tela status` (or `GET /status`) to check authoritative runtime state
including downstream server convergence.

### Server won't start (port conflict)

If using a fixed port (`--port 8080`), check for existing processes on that
port. Use `--port 0` (default) to let the OS assign an available port.

## Validation and testing

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

For CI job names and branch-protection-ready repo-local shared-surface checks,
see `docs/CI-REPO-LOCAL-SHARED-SURFACE-GATES.md`.

## Related files

- `README.md`
- `docs/CI-REPO-LOCAL-SHARED-SURFACE-GATES.md`
- `tela.yaml.example`
- `docs/DESIGN.md`
- `docs/INTERFACES.md`
