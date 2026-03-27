## Surface audit evidence

### Scope

Authoritative review-only audit of actual tela agent-facing surfaces in the current worktree.

### Surface matrix

| Surface | MCP resource | MCP tool | CLI command | HTTP endpoint | Present today? | Source-backed notes |
|---|---|---|---|---|---|---|
| `tela.profiles` | Yes | No explicit registration found | `tela profiles` | No dedicated `tela.profiles` HTTP endpoint | Verified present | Registered via `@upstream_server.resource("tela://profiles", name="tela.profiles", ...)` in `src/tela/shell/gateway.py:317-323`; enabled during startup in `src/tela/shell/gateway.py:586-588`. CLI command exists in `src/tela/cli.py:120-133,215-224`. |
| `tela.status` | No explicit registration found | No explicit registration found | `tela status` | `GET /status` | Present only as CLI + HTTP, not verified as MCP surface | No `name="tela.status"`, no `@...resource`, and no explicit MCP tool registration were found in `src/**/*.py`; `GET /status` is mounted in `src/tela/shell/gateway.py:129-145`; CLI command is wired in `src/tela/cli.py:110-119,182-188`. |
| `tela.connections` | No explicit registration found | No explicit registration found | `tela connections` | No dedicated `tela.connections` HTTP endpoint; data piggybacks on `GET /status` | Present only as CLI, not verified as MCP surface | No `name="tela.connections"` or explicit MCP registration found in `src/**/*.py`; CLI command is wired in `src/tela/cli.py:135-145,225-231`; command reads `connections` from `/status` payload in `src/tela/commands/connections_cmd.py:36-55` and `src/tela/commands/remote_state.py:57-123`. |
| `tela.audit` | No explicit registration found | No explicit registration found | `tela audit` | No dedicated `tela.audit` HTTP endpoint; data piggybacks on `GET /status` | Present only as CLI, not verified as MCP surface | No `name="tela.audit"` or explicit MCP registration found in `src/**/*.py`; CLI command is wired in `src/tela/cli.py:147-166,232-240`; command reads `audit_entries` from `/status` payload in `src/tela/commands/audit_cmd.py:48-73` and `src/tela/commands/remote_state.py:93-123`. |

### MCP resources verified

- `tela.profiles` only.
- Proof: `src/tela/shell/gateway.py:317-323` registers resource URI `tela://profiles` with MCP resource name `tela.profiles`.

### MCP tools verified

- No explicit built-in `tela.*` MCP tool registrations were found.
- Upstream MCP wiring currently exposes generic `tools/list` and `tools/call` handlers in `src/tela/shell/gateway.py:372-412`, but these proxy downstream tools rather than registering built-in `tela.status`, `tela.connections`, or `tela.audit` tools.

### CLI-only surfaces verified

- `tela status` (`src/tela/cli.py:110-119,182-188`; implementation in `src/tela/commands/status_cmd.py:12-51`)
- `tela connections` (`src/tela/cli.py:135-145,225-231`; implementation in `src/tela/commands/connections_cmd.py:13-55`)
- `tela audit` (`src/tela/cli.py:147-166,232-240`; implementation in `src/tela/commands/audit_cmd.py:15-95`)
- `tela profiles` also exists as CLI, but unlike the three above it is additionally verified as an MCP resource.

### HTTP-only/operator surfaces verified

- `GET /health` (`src/tela/shell/gateway.py:121-127`; handler contract in `src/tela/shell/http_routes.py:38-57`)
- `GET /status` (`src/tela/shell/gateway.py:129-145`; handler contract in `src/tela/shell/http_routes.py:67-124`)
- `POST /connect` (`src/tela/shell/gateway.py:146-175`; handler contract in `src/tela/shell/http_routes.py:135-187`)
- `POST /disconnect` (`src/tela/shell/gateway.py:176-206`; handler contract in `src/tela/shell/http_routes.py:198-255`)
- `POST /mcp` is documented as the Streamable HTTP endpoint in `docs/INTERFACES.md:182-190`, but this audit did not locate an explicit custom-route registration because it is expected to come from FastMCP transport wiring rather than local `custom_route(...)` code.

### Unverified/absent surfaces

- `tela.status` as MCP tool/resource: unverified/absent in current source audit.
- `tela.connections` as MCP tool/resource: unverified/absent in current source audit.
- `tela.audit` as MCP tool/resource: unverified/absent in current source audit.
- No dedicated HTTP endpoints named for `tela.profiles`, `tela.connections`, or `tela.audit` were found.

### Capability string audit (`tela_admin` or replacement)

- `tela_admin` appears only in documentation notes as disallowed/historical wording (for example `docs/CONFIRMED-SURFACE-CONTRACT.md`, `docs/AGENT_INTERFACE.md`, `docs/DESIGN.md`) and was not found in executable source under `src/**/*.py`.
- Current executable capability control is family/posture-based in generic form, not hard-coded to `tela_admin`:
  - profiles are canonical `capabilities` maps in `src/tela/core/models.py:162-215`
  - builtin profiles use arbitrary family names like `filesystem`, `network`, `orchestration`, `execution` in `src/tela/core/catalog.py:22-80`
  - family resolution is `tool override -> server family -> server name` in `src/tela/core/family.py:27-60`
- Therefore `tela_admin` is currently a documented/spec capability label, not a verified runtime capability string enforced by code.

### Instruction-merge fact audit

- Downstream instruction merging is real and source-backed: `_merge_downstream_instructions` in `src/tela/shell/gateway.py:209-273` merges per-server instructions into the upstream FastMCP server instructions.
- Merge modes are source-backed in executable code, not only docs:
  - `None`: passthrough downstream instructions (`src/tela/shell/gateway.py:253-258`)
  - `False`: suppress server instructions (`src/tela/shell/gateway.py:253-254`)
  - `str`: override downstream instructions (`src/tela/shell/gateway.py:255-256`)
- Merged instructions are supplied to `FastMCP(..., instructions=merged_instructions)` in `src/tela/shell/gateway.py:290-308`.

### Source lines / commands used

- Commands:
  - `git status --short --branch`
  - `grep` searches for `tela.(profiles|status|connections|audit|admin)` across `src/**/*.py`
  - `grep` searches for `resource(`, `tool(`, `custom_route(`, `list_tools(`, `call_tool(` across `src/**/*.py`
  - `grep` searches for `tela_admin` across `**/*.{py,md,yaml,yml,json}`
- Primary source files read:
  - `src/tela/shell/gateway.py`
  - `src/tela/shell/http_routes.py`
  - `src/tela/shell/upstream.py`
  - `src/tela/cli.py`
  - `src/tela/commands/status_cmd.py`
  - `src/tela/commands/connections_cmd.py`
  - `src/tela/commands/audit_cmd.py`
  - `src/tela/commands/profiles_cmd.py`
  - `src/tela/commands/remote_state.py`
  - `src/tela/core/models.py`
  - `src/tela/core/catalog.py`
  - `src/tela/core/family.py`
  - `docs/INTERFACES.md`
  - `docs/DESIGN.md`
  - `docs/ADR-003-gateway-capability-only-profiles.md`
  - `docs/MIGRATION-003-capability-only-profiles.md`

### Resulting risks / open questions

- Prior doc-drift finding has been remediated in current contract/docs: `tela.status`, `tela.connections`, and `tela.audit` are documented as operator-only (not built-in MCP tools/resources).
- Follow-up work should continue to treat `tela.status`, `tela.connections`, and `tela.audit` as unverified-or-absent MCP built-ins unless later source adds explicit MCP tool/resource registration.
- `tela_admin` should not be treated as implementation-verified capability taxonomy without either code adoption or an explicit architecture/spec decision; current runtime enforcement is generic family/posture based.
