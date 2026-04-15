# Independent Audit: Surface Taxonomy Verification

**Step ID:** `agent_iface.surface_taxonomy.verify_surface_conformance`
**Auditor:** Independent Blind Tester
**Date:** 2026-03-27

## Executive Summary

**VERDICT: PASS**

Surface taxonomy remains aligned across runtime, docs, and repro coverage.

---

## Runtime Surface Classification

- MCP built-in tools: `tela_list_providers` and `tela_list_profiles` (registered in `src/tela/shell/builtin_tools.py` and dispatched in `src/tela/shell/gateway.py`).
- MCP resources: none in the `tela.*` namespace (former `tela.profiles` resource replaced by `tela_list_profiles` builtin tool).
- Operator-only surfaces: `tela profiles`, `tela status`, `tela connections`, `tela audit`, and `GET /status`.

---

## README consistency

- README operator summary includes `tela profiles` alongside `tela status`, `tela connections`, and `tela audit`.
- README operator summary uses CLI/HTTP operator names (`tela profiles`) and does not relabel operator surfaces as MCP tool names.

---

## Doc/Runtime Consistency Checks

- `tela_list_profiles` is a builtin MCP tool callable through `tools/call`.
- Former `tela.profiles` resource has been removed; profiles are now listed via `tela_list_profiles` tool.
- Operator surfaces remain CLI/HTTP companions and are not registered as MCP built-ins.
- Instruction text correctly lists builtin tools.

---

## Verification Runs

| Run | Outcome |
|-----|---------|
| `uv run pytest -q tests/shell/test_surface_contract.py` | `30 passed` |
| `uv run pytest -q tests/shell/test_merge_instructions.py` | `14 passed` |
| `uv run pytest -q tests/shell/test_hard_cut_shared_surfaces.py` | `25 passed` |
| `uv run pytest -q tests/shell/test_builtin_tools.py` | `6 passed` |
| `uv run pytest -q tests/repro/test_loop4_remediation.py` | `4 passed` |

**Total:** 79 passed, 0 failed.

---

## Evidence Notes

- MCP builtin tools observed: `tela_list_providers`, `tela_list_profiles`.
- MCP resources observed: none in tela.* namespace.
- CLI surfaces observed: tela profiles, tela status, tela connections, tela audit.
- HTTP surfaces observed: GET /health, GET /status, POST /connect, POST /disconnect, POST /mcp.
- README consistency: operator summary includes tela profiles and keeps CLI/HTTP naming distinct from MCP builtin tool naming.
- Doc/runtime mismatches found: none.

---

status: "SUCCESS"
evidence: |
  Independent verification:
  - MCP builtin tools observed: tela_list_providers, tela_list_profiles
  - MCP resources observed: none in tela.* namespace (former tela.profiles resource replaced by tela_list_profiles builtin tool)
  - CLI surfaces observed: tela profiles, tela status, tela connections, tela audit
  - HTTP surfaces observed: GET /health, GET /status, POST /connect, POST /disconnect, POST /mcp
  - README consistency: operator summary includes tela profiles and keeps CLI/HTTP naming distinct from tela_list_profiles builtin tool naming
  - Doc/runtime mismatches found: none
error: ""