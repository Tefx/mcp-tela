# tela Agent Interface

This document defines the canonical agent-facing interface for the tela MCP gateway.

## 1. Purpose

`tela` is the concrete MCP gateway and authorization layer. It exposes downstream MCP servers through one upstream endpoint and enforces profile binding, family capability ceilings, and concrete tool posture checks.

## 2. MCP Surface Classification

### 2.1 Supported Built-in MCP Surfaces

| Surface | Kind | Access | URI/Name |
|---------|------|--------|----------|
| `tela.profiles` | MCP resource | Resource read | `tela://profiles` (name: `tela.profiles`) |

### 2.2 Operator Surfaces (Not MCP Built-ins)

The following surfaces are **operator-only** and accessible via CLI/HTTP. They are **not** exposed as MCP built-in tools or resources:

| Surface | Kind | Access Method |
|---------|------|---------------|
| `tela profiles` | CLI/HTTP | `tela profiles` command or via `GET /status` (distinct from MCP resource name `tela.profiles`) |
| `tela status` | CLI/HTTP | `tela status` command or `GET /status` endpoint |
| `tela connections` | CLI/HTTP | `tela connections` command or via `GET /status` |
| `tela audit` | CLI/HTTP | `tela audit` command or via `GET /status` |

**Important:** Do not attempt to call `tela profiles`, `tela status`, `tela connections`, or `tela audit` via MCP `tools/call`. These are not MCP tools.

## 3. Resource vs Tool Distinction

### 3.1 MCP Resources (Read-Only)

MCP resources are read via the MCP protocol's resource read mechanism, not via `tools/call`:

- `tela.profiles`: Returns the list of configured profiles with their capabilities.
  - Access: MCP resource read of URI `tela://profiles`
  - **Not callable via `tools/call`**

### 3.2 MCP Tools

tela exposes one built-in MCP tool under the `tela.*` namespace:

- `tela_list_providers` — returns a list of configured servers and their runtime status
  - **Input:** empty object `{}`
  - **Output:** list of `ProviderInfo` objects, each containing:
    - `name` (string): server name as configured in `servers`
    - `status` (string): one of `"connected"`, `"disconnected"`, `"failed"`
    - `tool_count` (int): number of tools exposed by this server after posture filtering
    - `tool_names` (list[str]): post-enforcement-filter exposed tool names

The `tela.` prefix is reserved for built-in surfaces.

## 4. Profile Capability Model

Profiles express capability ceilings only:

```yaml
profiles:
  developer:
    capabilities:
      filesystem: read_write
      git: read_only
    tool_overrides:
      filesystem:
        overrides:
          delete_file: deny
```

Rules:
- Profile authorization is expressed through `capabilities: family -> posture`
- `tool_overrides` may further restrict or selectively expose tools
- No override may exceed the family capability ceiling
- tela profiles do not include `tela_admin` or other reserved families

## 5. Downstream Instruction Merge

### 5.1 Ordering

Instruction composition is ordered and non-commutative:

1. **Tela top-level gateway instructions** are emitted first.
2. **Downstream server sections** are appended after the gateway instructions.
3. Downstream sections are appended in configured server iteration order.
4. Per-server rules:
   - `instructions: false` → no section appended for that server
   - `instructions: <string>` → append section using the explicit override string
   - `instructions: null` / omitted → append section using downstream's advertised instructions
5. When a downstream section is appended and tools are known, an `Available tools:` list is included.

### 5.2 Conflict Semantics (Current Runtime)

- Runtime composition is append-only: gateway block first, then downstream server sections.
- No semantic conflict resolver is implemented for contradictory instruction text.
- Conflicting downstream text is preserved as appended content.
- Mitigation is configuration-based:
  - Suppress a server section (`instructions: false`)
  - Provide an explicit per-server replacement string (`instructions: <string>`)
  - Revise contract/docs in an explicit follow-up spec change

## 6. HTTP Endpoints (Operator)

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | None | Liveness check |
| `GET /status` | Bearer token | Full runtime status (operator) |
| `POST /connect` | Bearer token | Register bridge connection; non-readiness lifecycle plumbing only |
| `POST /disconnect` | Bearer token | Unregister bridge connection |
| `POST /mcp` | Bearer token | MCP Streamable HTTP endpoint; readiness-gated admission surface |

### 6.1 `POST /mcp` transient 503 contract

When the gateway is still `warming`, `POST /mcp` rejects ordinary MCP
admission with HTTP `503` and the machine-readable contract frozen in
`contracts/mcp_admission_transient_503.schema.json`.

Required machine-readable fields:

- `code = "ADMISSION_REJECTED_WARMING"`
- `transient = true`
- `retry.authorized = true`
- `retry.basis = "gateway_signal"`
- `retry.expectation = "bounded"`
- `gateway_state = "warming"`

Consumer rule: retry only when the gateway emits this contract. Do not retry on
bare `503` status alone.

## 7. Invariants

- `tela.profiles` is a **resource read**, not a tool call
- `tela_list_providers` is the only built-in `tela.*` MCP tool
- `tela profiles`, `tela status`, `tela connections`, `tela audit` are operator-only (CLI/HTTP)
- `POST /mcp` is the only readiness-gated HTTP admission surface in the current slice
- `POST /mcp` warming rejection uses `ADMISSION_REJECTED_WARMING` plus explicit machine-readable transient retry authorization
- `POST /connect` is registration/lifecycle plumbing only and must not become readiness truth, readiness cache, or MCP admission proof
- gateway runtime lifecycle plus `GET /status` is the sole readiness authority for agents and bridge flows
- `tela connect` must not create or own readiness state, cached readiness truth, or local lifecycle labels
- `tela connect` readiness waiting must consult `GET /status` with status-driven polling (not fixed sleep intervals)
- retry is authorized only when the gateway emits the transient non-ready contract for `POST /mcp`
- persistent degraded/non-ready authority from `GET /status` must cause a clean bounded exit rather than an unbounded retry loop
- explicit non-goal: no `shutting_down` expansion in this slice
- lockfile discovery is not readiness truth
- Gateway instructions are emitted first; downstream sections are append-only
- The `tela.` prefix is reserved for built-in surfaces
