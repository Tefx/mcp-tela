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

### 1.1 Built-in MCP surfaces

| Surface name | Exact kind | Canonical access path | Notes |
|---|---|---|---|
| `tela_list_providers` | `tool` | MCP `tools/call` with an admitted session/connection and exact `{}` input | Returns list of ProviderInfo: `{provider_name, profile_id, status, tool_prefix, tool_count, tool_names}` filtered by the calling connection's admitted `profile_id`. |
| `tela_list_profiles` | `tool` | MCP `tools/call` with an admitted session/connection and exact `{}` input | Returns list of ProfileInfo: `{profile_id, capabilities, default}` as exact JSON payload content; multi-default payloads fail closed. |

### 1.2 Operator companion surfaces

| Surface | Exact kind | Canonical purpose |
|---|---|---|
| `tela profiles` | `CLI` | Local operator listing of configured profiles. |
| `tela status` | `CLI` | Local/operator runtime status query. |
| `tela connections` | `CLI` | Local/operator active-connection query. |
| `tela audit` | `CLI` | Local/operator audit query. |
| `GET /status` | `HTTP` | Runtime status endpoint consumed by operator/CLI flows. |
| `GET /health` | `HTTP` | Liveness endpoint. |
| `GET /operator/audit` | `HTTP` | Read-only paginated audit projection. |
| `POST /connect` | `HTTP` | Bridge registration endpoint; non-readiness lifecycle plumbing only. |
| `POST /disconnect` | `HTTP` | Bridge deregistration endpoint. |
| `POST /mcp` | `HTTP` | Streamable HTTP MCP transport endpoint and readiness-gated admission surface. |

### 1.1 Admission boundary freeze

- `POST /mcp` is the only readiness-gated HTTP admission surface in this contract slice.
- `POST /connect` remains connection registration and lifecycle plumbing only.
- `POST /connect` must not be described as readiness truth, a readiness cache, or admission proof for ordinary MCP traffic.
- gateway runtime lifecycle plus `GET /status` is the sole readiness authority.
- the bridge must not create or own readiness state, cached readiness truth, or local lifecycle labels.
- lockfile discovery is explicitly not readiness truth.

### 1.2 `POST /mcp` transient warming rejection

- When the gateway is still `warming` during convergence, `POST /mcp` must
  return HTTP `503`.
- The rejection must use error code `ADMISSION_REJECTED_WARMING`.
- The rejection must carry a machine-readable transient marker so retry logic can
  key from gateway signal rather than from bare HTTP status.
- Required machine-readable fields: `code`, `transient`, `retry.authorized`,
  `retry.basis`, `retry.expectation`, and `gateway_state`.
- Retry authorization must not be inferred from bare client guesswork,
  connection timing, or prior `/connect` success.
- The canonical machine-readable schema for that rejection is
  `contracts/mcp_admission_transient_503.schema.json`.
- Retry expectation is `bounded`; this contract does not authorize indefinite
  retry and does not add any new public lifecycle label beyond `warming`.

### 1.3 Bridge consumer-only readiness freeze

- `tela connect` waits for readiness by consulting `GET /status` with status-driven
polling; fixed sleep delays are not an acceptable readiness authority.
- The bridge remains a consumer of readiness truth only and must not create,
cache, or relabel readiness state locally.
- Retry is allowed only when the gateway emits the transient non-ready contract
for `POST /mcp`; other degraded/non-ready observations do not self-authorize
retry.
- If authoritative `GET /status` facts remain degraded or otherwise non-ready
through the bounded wait policy, `tela connect` must exit cleanly and
boundedly (bounded retry/exit behavior).
- Discovery-before-readiness: lockfile may be written before downstream convergence
completes; endpoint discoverability does not imply readiness.

## 2. Tool vs resource rules

### 2.1 MCP tools

- A surface is a `tool` only if it is callable through MCP `tools/call` as an
  explicitly supported built-in tela surface.
- This contract confirms exactly two current built-in MCP tools:
  `tela_list_providers` and `tela_list_profiles`.
- **Input contract:** Both tools accept strictly `{}` (empty object); additional
  properties are rejected (`extra_key`) and non-object payloads are rejected (`wrong_type`)
- **Session contract:** Both tools require an **admitted session/connection** at
  call time; they fail closed if called without one
- there is no builtin-session bypass and no alternate admission path for builtin tools
- `tela_list_providers` output: list of `ProviderInfo` objects, each containing
  `provider_name` (server name), `profile_id` (caller-bound profile truth),
  `status` (`"connected"` | `"disconnected"` | `"failed"`), `tool_prefix`
  (configured prefix or `null`), `tool_count` (int), and `tool_names` (list of
  post-enforcement-filter exposed tool names).
- `tela_list_profiles` output: list of `ProfileInfo` objects, each containing
  `profile_id` (str), `capabilities` (dict of family→posture string), and
  `default` (bool).
- `tela_list_profiles` must return the canonical JSON payload itself, not a
  Python `repr(...)`/stringified approximation.
- more than one `default: true` entry is invalid and must fail closed with
  `invalid_default_profile_state`
- **Provider listing visibility:** Tools are filtered by the calling
  connection's bound `profile_id`; no cross-profile visibility
- **Audit attribution:** Builtin tool calls are attributed to the caller's
  `profile_id`
- **Regression coverage:** `tests/shell/test_gateway.py::test_streamable_http_builtin_call_requires_admitted_session`, `tests/shell/test_gateway.py::test_streamable_http_builtin_call_accepts_only_exact_empty_object`, `tests/shell/test_builtin_tools.py::test_handle_list_providers_uses_bound_connection_profile_in_token_mode`, `tests/integration/test_token_mode_initialize.py::test_handle_initialize_token_mode_rejects_missing_token_version_before_admission`
- Therefore docs, tests, and runtime work must not claim any additional dotted
  MCP surface names beyond the two canonical builtin list tools.

### 2.2 MCP resources

- A surface is a `resource` only if it is readable as an MCP resource and is
  explicitly registered under a tela-owned resource name/URI.
- This contract confirms **zero current built-in tela MCP resources**.
  Shared profile enumeration is available only through the canonical
  `tela_list_profiles` builtin tool.

### 2.3 CLI and HTTP surfaces

- CLI and HTTP operator surfaces are real product surfaces, but they are not to
  be described as MCP built-ins unless separately confirmed as `tool` or
  `resource`.
- `tela profiles` (CLI companion), `tela status`, `tela connections`, and
  `tela audit` are confirmed operator surfaces.
- `GET /status` and `GET /operator/audit` are operator/runtime HTTP endpoints
  and must not be relabeled as dotted MCP surfaces.

## 3. Capability wording

- Approved current capability wording is:
  - `profiles express capability ceilings only`
  - `capabilities` are `family -> posture`
  - built-in operator surfaces must not be described as current runtime-enforced
    `tela_admin` MCP surfaces unless and until explicit MCP support exists

- `tela_admin` is **not approved as current-runtime contract wording** for
  dotted MCP surface names because the confirmed contract exposes no such
  built-ins.

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
- any tests or docs that currently describe dotted MCP labels as built-in MCP
  surfaces

## 7. Source basis

- Actual current registrations and merge mechanics: `src/tela/shell/gateway.py`
- Actual current surface audit: `evidence/surface_audit_actual_surface.md`
- Prior taxonomy decision artifact: `evidence/taxonomy_decision.md`
