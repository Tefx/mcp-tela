# tela Agent Interface

This document defines the canonical agent-facing interface for the tela MCP gateway.

## 1. Purpose

`tela` is the concrete MCP gateway and authorization layer. It exposes downstream MCP servers through one upstream endpoint and enforces profile binding, family capability ceilings, and concrete tool posture checks.

## 2. MCP Surface Classification

### 2.1 Supported Built-in MCP Surfaces

| Surface | Kind | Access | Name |
|---------|------|--------|------|
| `tela_list_providers` | MCP tool | `tools/call` with `{}` | `tela_list_providers` |
| `tela_list_profiles` | MCP tool | `tools/call` with `{}` | `tela_list_profiles` |

### 2.2 Operator Surfaces (Not MCP Built-ins)

The following surfaces are **operator-only**. They are **not** exposed as MCP built-in tools or resources.

CLI surfaces:

| Surface | Kind | Access Method |
|---------|------|---------------|
| `tela profiles` | CLI | `tela profiles`; not an MCP resource |
| `tela status` | CLI | `tela status`; not an MCP resource |
| `tela status --probe` | CLI | Observation-only endpoint probe; not an MCP resource |
| `tela status --clients` | CLI | Read-only attachment registry view; not an MCP resource |
| `tela connections` | CLI | `tela connections`; not an MCP resource |
| `tela audit` | CLI | `tela audit`; not an MCP resource |
| `tela doctor` | CLI | Observation-only diagnostic; not an MCP resource |
| `tela doctor --recover` | CLI | Explicit operator recovery; not an MCP resource |
| `tela stop` | CLI | Local process control via lockfile discovery and SIGTERM; not an MCP resource |

Operator HTTP surfaces:

| Surface | Kind | Access Method |
|---------|------|---------------|
| `GET /status` | HTTP | Runtime status endpoint; not an MCP resource |
| `GET /operator/probe` | HTTP | Observation-only current-endpoint snapshot; not an MCP resource |
| `GET /operator/clients` | HTTP | Read-only attachment registry view; not an MCP resource |
| `GET /operator/audit` | HTTP | Paginated operator audit endpoint; not an MCP resource |
| `GET /operator/authorization/explain` | HTTP | Diagnostic authorization explanation; not an MCP resource |

HTTP transport and bridge endpoints:

| Surface | Kind | Access Method |
|---------|------|---------------|
| `GET /health` | HTTP | Liveness endpoint; not an MCP resource |
| `POST /connect` | HTTP | Bridge registration endpoint; not an MCP resource |
| `POST /disconnect` | HTTP | Bridge deregistration endpoint; not an MCP resource |
| `POST /mcp` | HTTP | Streamable HTTP MCP transport endpoint, not a named MCP built-in tool or resource |

**Important:** Do not attempt to call operator CLI commands or HTTP endpoints via MCP `tools/call`. These are not named MCP tools.

## 3. Resource vs Tool Distinction

### 3.1 MCP Resources (Read-Only)

There are currently no built-in MCP resources. Profile information is available
via the `tela_list_profiles` built-in tool.

### 3.2 MCP Tools

The parent tela gateway exposes exactly two gateway-owned built-in MCP tools:

- `tela_list_providers` — returns a list of configured servers and their runtime status
  - **Input:** empty object `{}`
  - **Output:** list of `ProviderInfo` objects, each containing:
    - `provider_name` (string): server name as configured in `servers`
    - `profile_id` (string): the admitted caller profile that filtered visibility
    - `status` (string): one of `"connected"`, `"disconnected"`, `"failed"`
    - `tool_prefix` (string | null): configured prefix applied to exposed tool names
    - `tool_count` (int): number of tools exposed by this server after server-level filtering and posture filtering
    - `tool_names` (list[str]): post-filter/post-enforcement exposed tool names
  - **Ordering:** provider entries are sorted by `provider_name`; each `tool_names` list is sorted by exposed tool name

- `tela_list_profiles` — returns a list of configured profiles with their capabilities
  - **Input:** empty object `{}`
  - **Output:** list of `ProfileInfo` objects, each containing:
    - `profile_id` (string): canonical profile identifier
    - `capabilities` (dict): family → posture mapping
    - `default` (bool): whether this is the default profile
  - **Ordering:** profile entries are sorted by `profile_id`; capability families are emitted in sorted family-name order

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
- tela profiles do not include `tela_admin` or other reserved capability groups

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

## 6. HTTP Endpoints

### 6.1 Operator HTTP endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /status` | Bearer token | Full runtime status (operator) |
| `GET /operator/probe` | Bearer token | Observation-only current-endpoint snapshot |
| `GET /operator/clients` | Bearer token | Read-only attachment registry view |
| `GET /operator/audit` | Bearer token | Read-only paginated audit projection |
| `GET /operator/authorization/explain` | Bearer token | Diagnostic authorization explanation |

### 6.2 Liveness, bridge, and transport endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | None | Liveness check |
| `POST /connect` | Bearer token | Register bridge connection; non-readiness lifecycle plumbing only |
| `POST /disconnect` | Bearer token | Unregister bridge connection |
| `POST /mcp` | Bearer token | MCP Streamable HTTP endpoint; readiness-gated admission surface |

### 6.3 `POST /mcp` transient 503 contract

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

- `tela_list_profiles` is the **canonical built-in MCP tool** for listing profiles (not a resource)
- parent `tela_list_providers` and `tela_list_profiles` are the only gateway-owned built-in MCP tools provided by tela itself
- `tela profiles`, `tela status`, `tela status --probe`, `tela status --clients`, `tela connections`, `tela audit`, `tela doctor`, `tela doctor --recover`, and `tela stop` are operator-only CLI surfaces
- `GET /status`, `GET /operator/probe`, `GET /operator/clients`, `GET /operator/audit`, and `GET /operator/authorization/explain` are operator-only HTTP surfaces
- `POST /mcp` is the only readiness-gated HTTP admission surface in the current slice
- `POST /mcp` warming rejection uses `ADMISSION_REJECTED_WARMING` plus explicit machine-readable transient retry authorization
- `POST /connect` is registration/lifecycle plumbing only and must not become readiness truth, readiness cache, or MCP admission proof
- gateway runtime lifecycle plus `GET /status` is the sole readiness authority for agents and bridge flows
- `tela connect` must not create or own readiness state, cached readiness truth, or local lifecycle labels
- `tela connect` readiness waiting must consult `GET /status` with status-driven polling (not fixed sleep intervals)
- retry is authorized only when the gateway emits the transient non-ready contract for `POST /mcp`
- `ready` and `degraded` are admission-eligible bridge states; degraded mode uses the partial registry and keeps `degraded_reason` visible for diagnostics
- persistent `warming` or another non-admission state from `GET /status` must cause a clean bounded exit rather than an unbounded retry loop
- explicit non-goal: no `shutting_down` expansion in this slice
- lockfile discovery is not readiness truth
- Gateway instructions are emitted first; downstream sections are append-only
- The reserved tela-owned prefixes remain unavailable to downstream tool prefixes
