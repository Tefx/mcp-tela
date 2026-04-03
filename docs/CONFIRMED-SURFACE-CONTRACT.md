# Confirmed Surface Contract

This document is the canonical wording source for agent-facing tela surface
classification and downstream instruction merge semantics.

## 0. Contract type definitions

```text
SurfaceKind = "tool" | "resource" | "CLI" | "HTTP" | "absent"

SurfaceContract := {
  name: str,
  exact_kind: SurfaceKind,
  canonical_access_path: str,
  capability_wording: str,
  notes: str,
}
```

## 1. Canonical surface matrix

### 1.1 Named `tela.*` built-in surfaces

| Surface name | Exact kind | Canonical access path | Notes |
|---|---|---|---|
| `tela.profiles` | `resource` | MCP resource read of `tela://profiles` / `tela.profiles` | Confirmed supported MCP built-in surface. |
| `tela_list_providers` | `tool` | MCP `tools/call` with `{}` input | Returns list of ProviderInfo: `{name, status, tool_count, tool_names}`. |
| `tela.status` | `absent` | N/A | Do not present as a current MCP tool or MCP resource. Use operator surfaces instead. |
| `tela.connections` | `absent` | N/A | Do not present as a current MCP tool or MCP resource. Use operator surfaces instead. |
| `tela.audit` | `absent` | N/A | Do not present as a current MCP tool or MCP resource. Use operator surfaces instead. |

### 1.2 Operator companion surfaces

| Surface | Exact kind | Canonical purpose |
|---|---|---|
| `tela profiles` | `CLI` | Local operator listing of configured profiles. |
| `tela status` | `CLI` | Local/operator runtime status query. |
| `tela connections` | `CLI` | Local/operator active-connection query. |
| `tela audit` | `CLI` | Local/operator audit query. |
| `GET /status` | `HTTP` | Runtime status endpoint consumed by operator/CLI flows. |
| `GET /health` | `HTTP` | Liveness endpoint. |
| `POST /connect` | `HTTP` | Bridge registration endpoint; non-readiness lifecycle plumbing only. |
| `POST /disconnect` | `HTTP` | Bridge deregistration endpoint. |
| `POST /mcp` | `HTTP` | Streamable HTTP MCP transport endpoint and readiness-gated admission surface. |

### 1.1 Admission boundary freeze

- `POST /mcp` is the only readiness-gated HTTP admission surface in this contract slice.
- `POST /connect` remains connection registration and lifecycle plumbing only.
- `POST /connect` must not be described as readiness truth, a readiness cache, or admission proof for ordinary MCP traffic.

## 2. Tool vs resource rules

### 2.1 MCP tools

- A surface is a `tool` only if it is callable through MCP `tools/call` as an
  explicitly supported built-in tela surface.
- This contract confirms **one current built-in `tela.*` MCP tool**:
  `tela_list_providers`.
- Input: empty object `{}`
- Output: list of `ProviderInfo` objects, each containing `name` (server name),
  `status` (`"connected"` | `"disconnected"` | `"failed"`), `tool_count` (int),
  and `tool_names` (list of post-enforcement-filter exposed tool names).
- Therefore docs, tests, and runtime work must not claim that `tela.status`,
  `tela.connections`, `tela.audit`, or `tela.profiles` are current MCP tools.

### 2.2 MCP resources

- A surface is a `resource` only if it is readable as an MCP resource and is
  explicitly registered under a tela-owned resource name/URI.
- This contract confirms **exactly one current built-in tela MCP resource**:
  `tela.profiles`.
- `tela.profiles` is a resource read surface, not a tool-call surface.

### 2.3 CLI and HTTP surfaces

- CLI and HTTP operator surfaces are real product surfaces, but they are not to
  be described as MCP built-ins unless separately confirmed as `tool` or
  `resource`.
- `tela profiles` (CLI companion), `tela status`, `tela connections`, and
  `tela audit` are confirmed operator surfaces.
- `GET /status` is an operator/runtime HTTP endpoint and must not be renamed in
  docs/tests as `tela.status` MCP access.

## 3. Capability wording

- Approved current capability wording is:
  - `profiles express capability ceilings only`
  - `capabilities` are `family -> posture`
  - built-in operator surfaces must not be described as current runtime-enforced
    `tela_admin` MCP surfaces unless and until explicit MCP support exists

- `tela_admin` is **not approved as current-runtime contract wording** for
  `tela.status`, `tela.connections`, or `tela.audit` because those named MCP
  built-ins are currently absent in this confirmed contract.

- If `tela_admin` is mentioned in migration/spec history, it must be marked as
  historical or future-facing language rather than current confirmed runtime
  behavior.

## 4. Instruction merge ordering

Instruction composition is ordered and non-commutative.

1. Tela top-level gateway instructions, when present, are emitted first.
2. After the gateway instructions, tela appends zero or more downstream server
   sections.
3. Downstream sections are appended in configured server iteration order.
4. For each downstream server:
   - `instructions: false` => no section is appended for that server
   - `instructions: <string>` => append a server section using the explicit
     override string
   - `instructions: null` / omitted => append a server section using the
     downstream server's own advertised instructions, if any
5. When a downstream section is appended and tools are known, an `Available
   tools:` list is appended inside that server's section.

## 5. Instruction conflict semantics (implementation-backed)

- Runtime behavior is append-only composition: gateway block first, then
  downstream sections.
- Runtime does not implement semantic conflict detection or automatic conflict
  resolution for instruction text.
- If downstream text contradicts gateway guidance, both texts are still present
  in the composed output.
- Practical mitigation is explicit configuration: suppress a server section,
  provide a per-server replacement string, or revise contract/docs via explicit
  follow-up spec work.

## 6. Alignment targets

The following files/surfaces must align to this contract:

- `docs/INTERFACES.md`
- `docs/DESIGN.md`
- `README.md`
- `docs/USAGE.md`
- tests that classify agent-facing surfaces
- runtime registration/wiring in `src/tela/shell/gateway.py`
- any tests or docs that currently describe `tela.status`, `tela.connections`,
  or `tela.audit` as built-in MCP surfaces

## 7. Source basis

- Actual current registrations and merge mechanics: `src/tela/shell/gateway.py`
- Actual current surface audit: `evidence/surface_audit_actual_surface.md`
- Prior taxonomy decision artifact: `evidence/taxonomy_decision.md`
