# Document Re-Review: DESIGN.md (Delta Review)

**Mode**: Delta | **Verdict**: PASS WITH CONDITIONS

<meta_analysis>

- **Type**: Detailed Design / RFC
- **Maturity**: Accepted (pre-implementation, no source code yet)
- **Dimensions Applied**: D1 (Completeness), D2 (Feasibility), D3 (Consistency), D5 (Clarity)
- **Experts Consulted**: none
- **Context Loaded**:
  - `/Users/tefx/Projects/mcp-tela/DESIGN.md` (document under review)
  - `/Users/tefx/Projects/mcp-tela/DESIGN-REVIEW.md` (previous review)
  - `/Users/tefx/Projects/mcp-tela/INTERFACES.md` (source of truth for external interface)
  - `/Users/tefx/Projects/mcp-tela/CLAUDE.md` (project conventions)
  - `/Users/tefx/Projects/mcp-tela/contracts/errors.yaml` (canonical error codes)
  - `/Users/tefx/Projects/mcp-tela/contracts/capability_token.schema.json` (token schema)
  - `/Users/tefx/Projects/mcp-tela/contracts/meta.schema.json` (meta field schema)

</meta_analysis>

---

## 1. Executive Summary

* **Issue Count**: BLOCKING: 0 | SHOULD_FIX: 1 | SUGGESTION: 1
* **Previous Findings**: RESOLVED: 17 | PARTIALLY_RESOLVED: 1 | UNRESOLVED: 0
* **Top 3 Risks**:
  1. INTERFACES.md Section D still uses the old enforcement step ordering (posture=4, override=5) while DESIGN.md uses the new ordering (override=4, posture=5). Implementers referencing INTERFACES.md will build the wrong chain. (SHOULD_FIX -- external document, but tela's own source doc)
  2. INTERFACES.md Section B still uses `sse://` URL scheme in examples while DESIGN.md has been corrected to `http://`/`https://`. (SUGGESTION -- same external consistency concern)
  3. `meta.schema.json` specifies `additionalProperties: false` but DESIGN.md explicitly says "tela accepts any dict as _meta." These are intentionally decoupled (schema is informational), but the tension is now documented. No action needed.

---

## 2. Previous Finding Resolution

### BLOCKING Findings

| ID | Status | Evidence |
|----|--------|----------|
| B1 | **RESOLVED** | `parse_config` signature at line 373 now reads `def parse_config(raw: dict, env_vars: dict[str, str]) -> TelaConfig`. Docstring at lines 376-378 documents env var expansion via the mapping and dict key injection for `ServerConfig.name`/`ProfileConfig.name`. `config_loader.load_config` at lines 1038-1041 documents reading `os.environ` and passing to `parse_config(raw, env_vars)`. Startup sequence at line 1322 shows `parse_config(raw_yaml, env_vars)`. The env var flow is now complete and unambiguous. |
| B2 | **RESOLVED** | Enforcement chain steps have been reordered. Step 4 is now "Tool override check" (line 495), step 5 is now "Posture check" (line 499). The `enforce` docstring at lines 504-507 explicitly states: "Tool overrides (step 4) intentionally run BEFORE posture check (step 5) so that an explicit `allow` override can rescue a tool that would otherwise fail posture or side-effect checks." The `check_tool_override` function at lines 529-531 documents that `allow` returns `EnforcementResult(ALLOW)` and "caller skips posture and side-effect checks -- explicit allow bypasses both." Invariant 11 at lines 1639-1642 reflects the new ordering: "(1-token, 2-profile, 3-family, 4-tool override, 5-posture, 6-side-effect, 7-final). An explicit `allow` override at step 4 skips steps 5-6." The `enforce` signature at line 484 now includes `default_posture: Posture` (also resolves S6). All consistent. |

### SHOULD_FIX Findings

| ID | Status | Evidence |
|----|--------|----------|
| S1 | **RESOLVED** | `TelaException` docstring at lines 738-746 now explicitly documents the relationship: "TelaException is raised/caught internally for control flow [...] TelaError is the Pydantic model used as the MCP wire format for error responses sent to upstream clients." The `to_error()` method is defined at lines 751-753 with signature `def to_error(self) -> TelaError`. |
| S2 | **RESOLVED** | `parse_config` docstring at lines 377-378 now states: "Injects dict keys into ServerConfig.name and ProfileConfig.name (since YAML dict keys are not part of the value objects)." |
| S3 | **RESOLVED** | `handle_tools_call` step 1 at line 923 now reads "Extract _meta from arguments, hold for audit." Step 2 at line 1205 reads "Hold extracted `_meta` in memory for inclusion in audit entry (step 7)." Step 7 at line 1209 reads "Build and write audit entry via `build_audit_entry()`, passing the held `_meta`." No more ambiguity about double-recording. |
| S4 | **RESOLVED** | `resolve_tools` docstring at lines 663-666 now includes an explicit note: "ResolvedTool.posture may be None (unclassified) when neither tool_overrides nor MCP annotations provide a posture. This is intentional: the enforcement chain receives the server's default_posture separately and applies it during posture comparison (see enforce() and check_posture())." The flow is now documented end-to-end. |
| S5 | **RESOLVED** | `enforce` docstring at lines 488-489 now includes the precondition: "Precondition: token_result.verdict MUST be ALLOW. Callers MUST NOT call enforce with a DENY token_result -- reject the connection instead." |
| S6 | **RESOLVED** | `enforce` signature at line 484 now includes `default_posture: Posture`. The `check_posture` function at line 542 also takes `default_posture: Posture`. The data flow diagram at lines 1350-1351 shows `default_posture = server_config.default_posture` being passed to `enforce()`. |
| S7 | **RESOLVED** | Implementation note at lines 882-885 now states: "The concrete `UpstreamHandler` must hold a reference to `DownstreamManager` (for accessing the resolved tool registry and forwarding tool calls) and to the `AuditWriter` (for recording audit entries). These dependencies are injected at construction time by `shell/gateway`." |
| S8 | **RESOLVED** | Section 12.5 "fastmcp Integration" added at lines 1586-1603. Documents: server creation (`FastMCP` instance in `shell/gateway`), tool registration (dynamic, delegating to `handle_tools_call`), initialize handler hook, transport configuration (stdio default + SSE via `--port`), notification API, and the boundary rule (fastmcp confined to `shell/`). |
| S9 | **RESOLVED** | Token delivery footnote at lines 1282-1293 now addresses the extensibility concern. Documents that MCP `clientInfo` is extensible (no `additionalProperties: false`), acknowledges the fastmcp risk, and provides a priority-ordered fallback list: (1) custom `clientInfo` field if fastmcp preserves it, (2) custom MCP method `tela/authenticate`, (3) environment variable for stdio-only. States: "The implementation phase MUST verify fastmcp behavior and select the appropriate mechanism." |

### SUGGESTION Findings

| ID | Status | Evidence |
|----|--------|----------|
| G1 | **RESOLVED** | Path convention note at lines 153-154: "All module references like `core/config` in section headers refer to `src/tela/core/config.py` on disk." |
| G2 | **RESOLVED** | `posture_from_annotations` at lines 612-613 now handles the contradictory case: "readOnlyHint=True, destructiveHint=True -> DESTRUCTIVE (most restrictive wins)" and clarifies "readOnlyHint=False, destructiveHint=False -> READ_WRITE (mutating but not destructive)." |
| G3 | **RESOLVED** | `MetaField` model docstring at lines 281-286 now includes an explicit validation policy: "tela accepts any dict as _meta from tool call arguments and stores it as-is. The MetaField model is used for typed access to known fields only. Unknown fields are preserved in the audit entry but not validated. The meta.schema.json contract is informational for producers, not enforced by tela." |
| G4 | **RESOLVED** | `ServerConfig.url` comment at line 224 now reads "SSE server (http:// or https:// URL)." Config schema at line 1394 uses `url: "http://host:port/sse"` with comment "SSE: endpoint URL (standard HTTP/HTTPS)." All `sse://` references in DESIGN.md have been removed. |
| G5 | **RESOLVED** | Invariant 13 added at lines 1645-1648: "A tool returned by `tools/list` for a given connection MUST pass the enforcement chain when called by that same connection, provided no hot reload has occurred since the `tools/list` response. Violation of this invariant is a security consistency bug." |
| G6 | **RESOLVED** | `build_audit_entry` docstring at lines 1005-1011 now specifies field semantics by outcome: denied calls (enforcement latency only, `response_content` is None), allowed calls with downstream error (`response_content` contains downstream error), allowed calls with success (`response_content` contains downstream response, L3 only). |
| G7 | **RESOLVED** | Open Question 5 at lines 1713-1715 now includes: "In-flight tool calls during shutdown: should tela wait for active calls to complete (drain with timeout), or immediately close connections? Drain timeout value?" |

---

## 3. Cross-Reference Verification

### Verified (Consistent)

- **`contracts/errors.yaml`** <-> **DESIGN.md Section 10.2**: Error codes and numeric ranges match exactly. All 8 tela error codes (200-211) present in both.
- **`contracts/capability_token.schema.json`** <-> **DESIGN.md `CapabilityToken` model**: Fields, types, required/optional designations, `token_id` pattern `^tok_` all match. `additionalProperties: false` in schema.
- **`contracts/meta.schema.json`** <-> **DESIGN.md `MetaField` model**: Fields and types match. `trace_id` required in both. The `additionalProperties: false` in the schema vs DESIGN.md's "accept any dict" policy is now explicitly documented as intentional (schema is informational, tela does not enforce it).
- **DESIGN.md Section 4.4** <-> **DESIGN.md Section 13 (Invariant 11)**: Both specify the updated ordering: 1-token, 2-profile, 3-family, 4-tool override, 5-posture, 6-side-effect, 7-final. Both document allow-override skipping steps 5-6.
- **DESIGN.md Section 4.2** <-> **DESIGN.md Section 4.13** <-> **DESIGN.md Section 8.1**: The `parse_config(raw, env_vars)` signature is consistent across the function spec, the `config_loader` spec, and the startup sequence diagram.
- **DESIGN.md Section 12.5** <-> **DESIGN.md Section 4.11** <-> **DESIGN.md Section 4.9**: fastmcp integration pattern is consistent with the gateway, upstream handler, and extension points sections.

### Conflict

- **INTERFACES.md Section D** <-> **DESIGN.md Section 4.4 / Invariant 11**: INTERFACES.md still lists step 4 as "Posture check" and step 5 as "Tool override check" (lines 205-217). DESIGN.md has reversed these (step 4 = tool override, step 5 = posture). This is a direct contradiction between the two documents. DESIGN.md cites INTERFACES.md as a source document.
- **INTERFACES.md Section B** <-> **DESIGN.md Section 9.1**: INTERFACES.md uses `sse://localhost:3001` in server config examples (lines 47, 102). DESIGN.md has been corrected to use `http://`/`https://`. Minor inconsistency.

### Pending Implementation

- All modules described in DESIGN.md (no `src/tela/` directory exists yet).

---

## 4. Findings

### BLOCKING

None.

### SHOULD_FIX

| ID | Dimension | Location | Finding | Recommendation |
|----|-----------|----------|---------|----------------|
| N1 | D3 Consistency | INTERFACES.md Section D, lines 205-217 | INTERFACES.md still has the pre-fix enforcement ordering: step 4 = "Posture check", step 5 = "Tool override check." DESIGN.md (the fix for B2) reversed these to step 4 = "Tool override check", step 5 = "Posture check." Since INTERFACES.md is listed as a source document for DESIGN.md, and implementers may reference either document, this creates a contradiction. An implementer reading INTERFACES.md will build the chain with the old ordering where `allow` overrides cannot rescue posture-denied tools. | Update INTERFACES.md Section D to match DESIGN.md's new ordering: step 4 = Tool override check, step 5 = Posture check. Add the same note about `allow` overrides skipping posture and side-effect checks. |

### SUGGESTION

| ID | Dimension | Location | Finding | Recommendation |
|----|-----------|----------|---------|----------------|
| N2 | D3 Consistency | INTERFACES.md Section B, lines 47, 102 | INTERFACES.md config examples still use `sse://localhost:3001` for SSE server URLs. DESIGN.md was corrected (G4 fix) to use standard `http://`/`https://` URLs. | Update INTERFACES.md examples to use `http://localhost:3001` for consistency. |

---

## 5. Dimension Summary

| Dimension | Status | Issues | Notes |
|-----------|--------|--------|-------|
| D1: Completeness | Pass | -- | All previous completeness gaps (S2, S3, S4, S6, S7, S8, G3, G5, G6, G7) are resolved. Module specs now have explicit notes for all boundary concerns. |
| D2: Feasibility | Conditional Pass | -- | S9 is resolved with documented fallback mechanisms. The fastmcp verification is correctly deferred to implementation time with a clear priority list. |
| D3: Consistency | Conditional Pass | N1, N2 | DESIGN.md internal consistency is now excellent. The remaining consistency issue is between DESIGN.md and its source document INTERFACES.md, which was not updated alongside the DESIGN.md fixes. |
| D5: Clarity | Pass | -- | All previous clarity issues (B2, G1, G2, G4, G6) are resolved. The enforcement chain documentation is now unambiguous. Docstrings provide clear guidance for implementers. |

---

## 6. Quality of Fixes Assessment

The fixes are well-executed. Specific observations:

1. **B1 fix (env_vars parameter)**: Clean. The fix touches all three locations that needed updating: the function signature, the config_loader docstring, and the startup sequence diagram. No loose ends.

2. **B2 fix (enforcement chain reordering)**: Thorough. The reordering is reflected in (a) the `enforce` function steps 1-7, (b) the `enforce` docstring rationale, (c) the `check_tool_override` return semantics, (d) invariant 11, (e) the data flow diagram in Section 8.2. The only gap is INTERFACES.md (see N1).

3. **S1 fix (TelaException/TelaError relationship)**: Well-documented. The docstring is explicit about when to use each, and `to_error()` provides the conversion path.

4. **S8 fix (fastmcp integration)**: The Section 12.5 addition is appropriately scoped -- enough detail for implementers without over-constraining, and correctly states that fastmcp is confined to `shell/` modules.

5. **S9 fix (clientInfo extensibility)**: Pragmatic. The footnote approach with priority-ordered fallbacks is the right level of specificity for a design document. The "MUST verify" language appropriately assigns implementation-time responsibility.

6. **G2 fix (contradictory annotations)**: The "most restrictive wins" rule is a sound security default.

7. **G3 fix (meta validation policy)**: The explicit statement that tela accepts any dict and the schema is informational resolves the ambiguity cleanly. This is the right call for a proxy that should be resilient.

---

## 7. Regression Check

No regressions detected. Specifically verified:

- The B2 fix (enforcement reordering) did not break any other enforcement references in the document. All mentions of the chain (Section 4.4, Section 6.2, Section 8.2, Section 10.1, Section 13 invariant 11) use the updated ordering.
- The B1 fix (env_vars parameter) did not create inconsistencies with the `expand_env_vars` function, which also takes `env_vars: dict[str, str]`.
- The S6 fix (default_posture in enforce) is consistent with the B2 fix -- the `enforce` signature now correctly receives `default_posture` which is used by the posture check in the reordered chain.
- The G4 fix (SSE URL scheme) did not leave any `sse://` remnants in DESIGN.md.
- The new Section 12.5 does not create any dependency from `core/` to fastmcp -- it explicitly states "fastmcp specifics are confined to `shell/` modules -- `core/` never imports or references fastmcp."

---

## 8. Non-Goals

This re-review explicitly did NOT check:

- **INTERFACES.md as a standalone document**: Only cross-referenced against DESIGN.md. A full review of INTERFACES.md would be a separate task.
- **New findings beyond the scope of the previous review**: This is a delta review focused on fix verification. The original review dimensions were not re-run from scratch.
- **Plan quality or implementation progress**: No `plan.yaml` or `docs/dag.md` review.
- **Test coverage adequacy**: Testing strategy (Section 14) was not re-evaluated.
- **Upstream opifex docs**: The opifex source documents were not re-checked (they were verified consistent in the first review and no opifex changes were reported).

---

## 9. Next Steps

1. [ ] **Fix N1 (SHOULD_FIX)**: Update INTERFACES.md Section D to match DESIGN.md's enforcement chain ordering (swap steps 4 and 5).
2. [ ] **Fix N2 (SUGGESTION)**: Update INTERFACES.md Section B to use `http://`/`https://` URLs instead of `sse://`.
3. [ ] Proceed to implementation. DESIGN.md is internally consistent and ready for use as the implementation blueprint.
