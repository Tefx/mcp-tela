# Independent Audit: Surface Taxonomy Verification

**Step ID:** `agent_iface.surface_taxonomy.verify_surface_conformance`
**Auditor:** Independent Blind Tester
**Date:** 2026-03-27

## Executive Summary

**VERDICT: PASS**

All externally visible surfaces conform to documented taxonomy. No doc/runtime mismatches found.

---

## 1. MCP Tools Observed

| Tool Name | Status | Evidence |
|-----------|--------|----------|
| `tela.profiles` | **NOT A TOOL** | Confirmed: registered as MCP resource, NOT as tool |
| `tela.status` | **ABSENT** | No `@upstream_server.tool("tela.status")` anywhere |
| `tela.connections` | **ABSENT** | No `@upstream_server.tool("tela.connections")` anywhere |
| `tela.audit` | **ABSENT** | No `@upstream_server.tool("tela.audit")` anywhere |

**Source:** `src/tela/shell/gateway.py:320-331` registers `@upstream_server.resource()` for `tela.profiles`. No `@upstream_server.tool()` calls for any `tela.*` name.

---

## 2. MCP Resources Observed

| Resource URI | Name | Registered | Evidence |
|--------------|------|------------|----------|
| `tela://profiles` | `tela.profiles` | YES | `gateway.py:320-331` |
| Other `tela://*` | - | NO | Only one resource registration found |

**Registration Code:**
```python
@upstream_server.resource(
    "tela://profiles",
    name="tela.profiles",
    description="List configured tela profiles.",
    mime_type="application/json",
)
def _profiles_resource() -> str:
    result = handle_profiles_list()
    # ...
```

---

## 3. CLI Surfaces Observed

| CLI Command | Implementation | Status |
|-------------|----------------|--------|
| `tela profiles` | `profiles_command()` via `cli.py:215-224` | OPERATOR-ONLY |
| `tela status` | `status_command()` via `cli.py:182-188` | OPERATOR-ONLY |
| `tela connections` | `connections_command()` via `cli.py:225-231` | OPERATOR-ONLY |
| `tela audit` | `audit_command()` via `cli.py:232-240` | OPERATOR-ONLY |
| `tela connect` | `connect_command()` via `cli.py:203-214` | Client entry |
| `tela serve` | `serve_command()` via `cli.py:189-202` | Server entry |

**Source:** `src/tela/cli.py` subparser definitions (lines 110-166).

---

## 4. HTTP Surfaces Observed

| Endpoint | Auth | Purpose | Evidence |
|----------|------|---------|----------|
| `GET /health` | None | Liveness | `gateway.py:112-118` |
| `GET /status` | Bearer | Runtime status (operator) | `gateway.py:120-135` |
| `POST /connect` | Bearer | Bridge registration | `gateway.py:137-165` |
| `POST /disconnect` | Bearer | Bridge deregistration | `gateway.py:167-197` |
| `POST /mcp` | Bearer | MCP Streamable HTTP | FastMCP app handler |

**Source:** `src/tela/shell/gateway.py:92-198` (`_register_http_routes()`).

---

## 5. Doc/Runtime Mismatches Found

**NONE.**

All documentation correctly states:

| Doc Claim | Runtime Reality | Match |
|-----------|-----------------|-------|
| `tela.profiles` is MCP resource | `@upstream_server.resource()` | ✅ |
| `tela.status/connections/audit` are NOT MCP tools | No `@upstream_server.tool()` for these | ✅ |
| Operator surfaces are CLI/HTTP, not MCP | CLI commands + HTTP routes exist, no MCP registration | ✅ |
| Guidance distinguishes resource reads from tool calls | `"Do not use tools/call for tela.profiles"` | ✅ |
| `tela.` prefix reserved, rejected for downstream tools | `conflict.py:PREFIX_VIOLATION` check | ✅ |

---

## 6. Instruction Merge Observations

**Gateway Instructions (first block in composed output):**
```
# tela gateway surface contract

Gateway rules for tela-owned surfaces:
- Built-in MCP resource: `tela.profiles` (read via `tela://profiles`).
- Built-in MCP tools: none.
- Operator-only surfaces (not MCP built-ins): `tela profiles`, `tela status`, `tela connections`, `tela audit`, and `GET /status`.
- Do not use `tools/call` for `tela.profiles`; use resource read.
```

**Downstream Sections (appended after gateway):**
- Ordering: Configured server dictionary order (Python 3.7+ preserves insertion order)
- Suppression: `instructions: false` → no section
- Override: `instructions: <string>` → use override text
- Passthrough: `instructions: null` or omitted → downstream's advertised instructions

**Conflict Handling:**
- Downstream sections are appended after the gateway block
- No semantic conflict resolver is implemented for contradictory instruction text
- Conflicting downstream text is preserved as appended content
- Tests verify ordering and non-resolution behavior via explicit string assertions

**Test Evidence:**
- `tests/shell/test_merge_instructions.py`: 14/14 pass
- `tests/shell/test_surface_contract.py`: 26/26 pass
- `tests/shell/test_gateway.py::test_fastmcp_profiles_resource_registered`: PASS

---

## 7. Tests Verified

| Test Suite | Tests | Pass | Fail |
|------------|-------|------|------|
| `test_surface_contract.py` | 26 | 26 | 0 |
| `test_merge_instructions.py` | 14 | 14 | 0 |
| `test_gateway.py::test_fastmcp_profiles_resource_registered` | 1 | 1 | 0 |

**Total:** 41 tests, 41 pass, 0 fail.

---

## 8. Conflict Detection Code

**Source:** `src/tela/core/conflict.py:33-37`

```python
RESERVED_PREFIX = "tela."
"""Prefix reserved for tela-owned surfaces."""

INTROSPECTION_TOOLS = ("tela.profiles",)
"""Currently supported built-in tela MCP surface names."""
```

**Detection:** `detect_conflicts()` raises `PREFIX_VIOLATION` for any downstream tool with `tela.*` prefix.

---

## 9. Final Pass/Fail Rationale

**PASS.** All findings are consistent:

1. **Resource vs Tool Classification:** `tela.profiles` is correctly registered as an MCP **resource** (read via `tela://profiles`), not a tool callable via `tools/call`.

2. **Absent MCP Surfaces:** `tela.status`, `tela.connections`, `tela.audit` are correctly **absent** from MCP registration. They exist only as CLI commands and HTTP endpoints (`GET /status`, etc.).

3. **Doc/Runtime Alignment:** Documentation (`docs/AGENT_INTERFACE.md`, `docs/CONFIRMED-SURFACE-CONTRACT.md`, `README.md`) accurately reflects runtime reality.

4. **Instruction Merge Semantics:** Gateway text is emitted first and downstream text is appended in order; semantic conflict resolution is not implemented.

5. **Guidance Clarity:** Runtime instructions clearly state: "Do not use `tools/call` for `tela.profiles`; use resource read."

6. **Surface Classification Tests:** All 41 tests pass, asserting the canonical surface matrix matches documentation.

---

## 10. Evidence Artifacts

**Code Files:**
- `src/tela/shell/gateway.py:315-331` — Resource registration
- `src/tela/shell/gateway.py:92-198` — HTTP route registration
- `src/tela/cli.py:110-166` — CLI subparsers
- `src/tela/core/conflict.py:33-37` — Reserved prefix
- `src/tela/shell/surface_instructions.py:8-21` — Gateway instructions

**Test Files:**
- `tests/shell/test_surface_contract.py` — Canonical surface matrix assertions
- `tests/shell/test_merge_instructions.py` — Merge ordering tests
- `tests/shell/test_gateway.py:497-533` — Resource registration verification

**Docs:**
- `docs/AGENT_INTERFACE.md` — Agent-facing interface
- `docs/CONFIRMED-SURFACE-CONTRACT.md` — Canonical surface contract
- `docs/INTERFACES.md` — CLI/config contract

---

## 11. Loop2 Delta (vs prior remediation loop)

- Removed overclaim wording that implied an implemented "gateway wins" semantic conflict resolver.
- Reframed instruction behavior everywhere in this artifact as implementation-backed ordering + append-only composition.
- Replaced placeholder/weak conflict-semantics coverage in `tests/shell/test_surface_contract.py` with concrete assertions that:
  - gateway text is emitted before downstream sections
  - contradictory downstream text is preserved in composed output (no semantic conflict resolution)

---

## Conclusion

Loop2 remediation aligned docs/evidence with implementation-backed instruction semantics: ordered append-only composition without a semantic conflict resolver.

---

status: "SUCCESS"
evidence: |
  Independent verification:
  - MCP tools observed: None (zero built-in tela.* MCP tools)
  - MCP resources observed: tela.profiles (via tela://profiles) — one resource, correctly registered
  - CLI surfaces observed: tela profiles, status, connections, audit — all operator-only
  - HTTP surfaces observed: GET /health (no auth), GET /status (bearer), POST /connect (bearer), POST /disconnect (bearer), POST /mcp (bearer)
  - Doc/runtime mismatches found: NONE
  - Instruction merge observations: gateway-first ordering and append-only composition verified; contradictory downstream text is preserved (no semantic conflict resolver)
  - Final pass/fail rationale: PASS — all runtime surfaces match documented taxonomy; resource/tool classification correct; operator surfaces correctly NOT registered as MCP built-ins
error: ""
