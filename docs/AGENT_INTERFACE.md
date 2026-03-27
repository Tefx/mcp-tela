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
| `tela.status` | CLI/HTTP | `tela status` command or `GET /status` endpoint |
| `tela.connections` | CLI/HTTP | `tela connections` command or via `GET /status` |
| `tela.audit` | CLI/HTTP | `tela audit` command or via `GET /status` |

**Important:** Do not attempt to call `tela.status`, `tela.connections`, or `tela.audit` via MCP `tools/call`. These are not MCP tools.

## 3. Resource vs Tool Distinction

### 3.1 MCP Resources (Read-Only)

MCP resources are read via the MCP protocol's resource read mechanism, not via `tools/call`:

- `tela.profiles`: Returns the list of configured profiles with their capabilities.
  - Access: MCP resource read of URI `tela://profiles`
  - **Not callable via `tools/call`**

### 3.2 MCP Tools

tela does not currently expose any built-in MCP tools under the `tela.*` namespace. The `tela.` prefix is reserved for future built-in surfaces.

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

1. **Tela top-level gateway instructions** come first and remain the authoritative global rule set.
2. **Downstream server sections** are appended after the gateway instructions.
3. Downstream sections are appended in configured server iteration order.
4. Per-server rules:
   - `instructions: false` → no section appended for that server
   - `instructions: <string>` → append section using the explicit override string
   - `instructions: null` / omitted → append section using downstream's advertised instructions
5. When a downstream section is appended and tools are known, an `Available tools:` list is included.

### 5.2 Conflict Handling

- Downstream instructions are **subordinate per-server appendices**, not authority over gateway rules.
- Downstream text may add server-specific guidance but must not silently override gateway instructions.
- If downstream instructions conflict with gateway instructions, the **gateway instructions win**.
- Conflicting downstream text must be handled explicitly by:
  - Suppressing that server section (`instructions: false`)
  - Providing an explicit per-server override string
  - Revising the contract in a future explicit spec change
- **Silent override by downstream text is forbidden.**

## 6. HTTP Endpoints (Operator)

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | None | Liveness check |
| `GET /status` | Bearer token | Full runtime status (operator) |
| `POST /connect` | Bearer token | Register bridge connection |
| `POST /disconnect` | Bearer token | Unregister bridge connection |
| `POST /mcp` | Bearer token | MCP Streamable HTTP endpoint |

## 7. Invariants

- `tela.profiles` is a **resource read**, not a tool call
- No built-in `tela.*` MCP tools are currently supported
- `tela.status`, `tela.connections`, `tela.audit` are operator-only (CLI/HTTP)
- Gateway instructions are authoritative; downstream sections are append-only
- The `tela.` prefix is reserved for future built-in surfaces
