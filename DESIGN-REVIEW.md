# Document Review: DESIGN.md

**Mode**: Deep | **Verdict**: PASS WITH CONDITIONS

<meta_analysis>

- **Type**: Detailed Design / RFC
- **Maturity**: Accepted (pre-implementation, no source code yet)
- **Dimensions Applied**: D1 (Completeness), D2 (Feasibility), D3 (Consistency), D4 (Risk), D5 (Clarity)
- **Experts Consulted**: none (no LLM-agent or SE-radical consultations needed -- the design is conventional proxy architecture)
- **Context Loaded**:
  - `/Users/tefx/Projects/mcp-tela/DESIGN.md` (document under review)
  - `/Users/tefx/Projects/mcp-tela/INTERFACES.md` (source of truth for external interface)
  - `/Users/tefx/Projects/mcp-tela/CLAUDE.md` (project conventions)
  - `/Users/tefx/Projects/mcp-tela/INVAR.md` (invar protocol details)
  - `/Users/tefx/Projects/mcp-tela/pyproject.toml` (project metadata)
  - `/Users/tefx/Projects/mcp-tela/README.md` (public docs)
  - `/Users/tefx/Projects/mcp-tela/docs/dag.md` (vectl phase DAG)
  - `/Users/tefx/Projects/mcp-tela/contracts/errors.yaml` (canonical error codes)
  - `/Users/tefx/Projects/mcp-tela/contracts/capability_token.schema.json` (token schema)
  - `/Users/tefx/Projects/mcp-tela/contracts/meta.schema.json` (meta field schema)
  - `/Users/tefx/Projects/opifex/design/tela-clean-gateway.md` (upstream design doc)
  - `/Users/tefx/Projects/opifex/design/architecture.md` (system architecture)
  - `/Users/tefx/Projects/opifex/contracts/errors.yaml` (canonical error codes -- upstream copy)
  - `/Users/tefx/Projects/opifex/contracts/capability_token.schema.json` (upstream copy)
  - `/Users/tefx/Projects/opifex/contracts/meta.schema.json` (upstream copy)

</meta_analysis>

---

## 1. Executive Summary

* **Issue Count**: BLOCKING: 2 | SHOULD_FIX: 9 | SUGGESTION: 7
* **Top 3 Risks**:
  1. `parse_config` signature omits `env_vars` parameter but its docstring promises env var expansion -- implementers will be confused about where env vars come from (BLOCKING)
  2. The enforcement chain has an ordering conflict between steps 4/5 that makes it ambiguous whether a `tool_overrides: allow` entry can rescue a tool denied by posture check (BLOCKING)
  3. The `make_error` return type references `TelaError` but the exception hierarchy uses `TelaException` -- two parallel error models coexist with no documented relationship (SHOULD_FIX)

---

## 2. Cross-Reference Verification

### Verified (Consistent)

- **`contracts/errors.yaml`** <-> **DESIGN.md Section 10.2**: Error codes and numeric ranges match exactly.
- **`contracts/capability_token.schema.json`** <-> **DESIGN.md `CapabilityToken` model**: Fields, types, required/optional designations all match. The `token_id` pattern `^tok_` is consistent.
- **`contracts/meta.schema.json`** <-> **DESIGN.md `MetaField` model**: Fields and types match. `trace_id` is required in both.
- **INTERFACES.md Section D (Enforcement Layers)** <-> **DESIGN.md Section 4.4**: 7-step chain is consistent between both docs.
- **INTERFACES.md Section B (Configuration)** <-> **DESIGN.md Section 9.1**: Config schema structure is consistent.
- **INTERFACES.md Section E (Hot Reload)** <-> **DESIGN.md Section 8.3**: Hot reload behavior and invariants match.
- **INTERFACES.md Section H (Open Mode)** <-> **DESIGN.md Section 7.2**: Open mode semantics match.
- **opifex `tela-clean-gateway.md`** <-> **DESIGN.md Section 1**: Non-responsibilities, profile-only model, server-is-family convention all align.
- **opifex `architecture.md` Token Structure** <-> **DESIGN.md `CapabilityToken`**: Identical field set and semantics.
- **opifex `architecture.md` Three-Plane Separation** <-> **DESIGN.md Section 1**: tela stays in the Tool Plane; does not absorb Control or Event Plane responsibilities.
- **`pyproject.toml`** <-> **DESIGN.md**: Package name `mcp-tela`, entry point `tela.cli:main`, source path `src/tela/`, dependencies (`fastmcp`, `pydantic`, `click`, `rich`, `pyyaml`) all consistent.

### Conflict

- **opifex `architecture.md` line 53**: Lists `idempotency enforcement` as a tela key feature. **DESIGN.md does not specify idempotency enforcement**. The `_meta.idempotency_key` field is modeled but the meta.schema.json explicitly says "At-most-once enforcement is a future capability (not in v0.1)." This is a documentation discrepancy in the upstream architecture doc, not a DESIGN.md bug, but should be noted.

### Missing

- **`contracts/errors.yaml` referenced as source doc** (`contracts/*`): Found and verified.
- **`opifex/design/tela-clean-gateway.md` referenced as source doc**: Found and verified.
- **`opifex/design/architecture.md` referenced as source doc**: Found and verified.

### Pending Implementation

- All modules described in DESIGN.md (no `src/tela/` directory exists yet).
- `plan.yaml` exists (vectl plan created) but no implementation steps are complete.

---

## 3. Findings

### BLOCKING

| ID | Dimension | Location | Finding | Recommendation |
|----|-----------|----------|---------|----------------|
| B1 | D3 Consistency | Section 4.2, `parse_config` signature (line ~363) | The function signature is `def parse_config(raw: dict) -> TelaConfig` but the docstring says "Expands ${VAR} references from the provided env_vars mapping." There is no `env_vars` parameter. The function is in `core/config.py` which per the Invar rules must have ZERO I/O (so it cannot read `os.environ` itself). Who provides the env vars? The shell layer (`config_loader`) reads them, but it calls `parse_config(raw)` with only one argument. The env var expansion path is broken. | Add `env_vars: dict[str, str]` as a second parameter to `parse_config`, or split `expand_env_vars` into a separate pre-processing step called by `config_loader` before `parse_config`. The docstring-to-signature mismatch must be resolved. |
| B2 | D5 Clarity | Section 4.4, enforcement steps 4 vs 5 (lines ~476-527) | The enforcement chain runs: step 4 (posture check) then step 5 (tool override check). Step 5 says "overriding the posture check result from step 4." But step 4 documentation says "Any DENY short-circuits the chain." If step 4 denies a tool due to posture, step 5 never executes, so `tool_overrides: allow` can never rescue a posture-denied tool. This contradicts the stated purpose of tool overrides ("overriding the posture check result"). INTERFACES.md Section D has the same ambiguity. An implementer cannot determine the correct behavior. | Either: (a) change the chain order so tool override check runs BEFORE posture check, making overrides truly override posture; or (b) explicitly state that overrides can only deny (not allow), and remove the claim that overrides "override the posture check result." The current text is contradictory. |

### SHOULD_FIX

| ID | Dimension | Location | Finding | Recommendation |
|----|-----------|----------|---------|----------------|
| S1 | D3 Consistency | Section 4.8 (line ~722) | `make_error` returns `TelaError` but the exception hierarchy defines `TelaException`. `TelaError` (line ~342) is a Pydantic `BaseModel` used for structured error responses. These are two independent error types with no documented relationship. An implementer will not know when to use `TelaError` (data) vs `TelaException` (exception). | Document explicitly: `TelaException` is for internal control flow (raise/catch); `TelaError` is the wire format for MCP error responses. Add a `to_error() -> TelaError` method on `TelaException`, or a `from_exception(TelaException) -> TelaError` factory. |
| S2 | D1 Completeness | Section 4.1, `ServerConfig` model (line ~216) | `ServerConfig.name` is a field on the model, but in the config YAML (Section 9.1), the server name is the dict key, not a field inside the value. `TelaConfig.servers` is typed as `dict[str, ServerConfig]`. If the YAML parser creates `ServerConfig` from the value dict, `name` will not be populated automatically. The `parse_config` function must inject the dict key into `ServerConfig.name`, but this is not documented. | Add an explicit note in `parse_config` that it must populate `ServerConfig.name` from the dict key. Same applies to `ProfileConfig.name`. |
| S3 | D1 Completeness | Section 6.2, `tools/call` processing step ordering (line ~1144) | The processing order states: (1) extract `_meta`, (2) record `_meta` in audit, (3) strip `_meta`, (4) run enforcement, (5) if deny return error, (6) if allow forward, (7) record audit, (8) return result. Step 2 says "record `_meta` in audit entry" but step 7 also says "record audit entry." This means the audit entry is partially built in step 2 and completed in step 7. The `build_audit_entry` function in Section 4.12 takes `meta` as a parameter, suggesting it is built once, not incrementally. Clarify that `_meta` is extracted in step 1, held in memory, and passed to `build_audit_entry` in step 7. Step 2 should say "hold `_meta` for inclusion in audit entry" not "record." | Reword step 2 to "Hold extracted `_meta` for audit entry" and remove the implication that recording happens twice. |
| S4 | D1 Completeness | Section 4.5, `classify_tool` (line ~567) | The `classify_tool` function returns `None` for unclassified tools, deferring to the caller to apply `default_posture`. But `default_posture` is on `ServerConfig`, and `classify_tool` already receives `server_config` as a parameter. The caller must know to apply `server_config.default_posture` when the return is `None`. Meanwhile, the `resolve_tools` function in Section 4.6 (line ~627) calls both `classify_tool` and `resolve_family` but does not document applying `default_posture`. The `ResolvedTool.posture` is `Posture | None = None`, meaning unclassified tools flow through the system with `None` posture until the enforcement chain handles them. This is a valid design but is confusing: the enforcement chain (step 4) has to know about `default_posture` even though it is a server-level concern, not a profile-level concern. | Document the `default_posture` application point explicitly. Either: (a) apply `default_posture` inside `classify_tool` (making it never return `None`), or (b) add a note in `resolve_tools` that `ResolvedTool.posture = None` means "unclassified, enforcement chain will apply server's `default_posture`." Currently the reader must reconstruct this from scattered hints. |
| S5 | D4 Risk | Section 4.4, `enforce` signature (line ~467) | The `enforce` function takes `token_result: EnforcementResult` to represent "token validation already passed." But `EnforcementResult` carries `verdict`, `denied_by`, and `error_code`. Using the same type for a "pre-check passed" input and a "chain produced this output" result is a type-safety risk. An implementer could accidentally pass a DENY token result and have it silently accepted if they forget to check it. | Consider a dedicated input type (e.g., `TokenValidationResult`) or at minimum document the invariant: "If `token_result.verdict == DENY`, the `enforce` function MUST immediately return that result. Callers MUST NOT call `enforce` with a DENY token result and expect it to be ignored." |
| S6 | D1 Completeness | Section 4.4 (line ~501) | `check_posture` takes `default_posture: Posture` as a parameter. But the `enforce` top-level function does not take `default_posture` as a parameter, nor `server_config`. The enforcement function receives only `tool: ResolvedTool`, `profile: ProfileConfig`, and `token_result`. Where does `default_posture` come from? `ResolvedTool.posture` can be `None`, and the enforcement chain needs the server's `default_posture` to resolve it. | Either: (a) add `default_posture: Posture` as a parameter to `enforce`, or (b) require that `ResolvedTool.posture` is never `None` by the time it reaches `enforce` (resolve it earlier in the pipeline). The current design has a gap where `check_posture` needs data that `enforce` does not receive. |
| S7 | D1 Completeness | Section 4.11, `handle_tools_list` (line ~860) | `handle_tools_list` returns `list[dict]` but does not document the filtering logic inline. Section 6.1 defines four filtering criteria but the function signature has no parameters for the tool set or server config. The connection context provides the profile, but where does the tool list come from? Presumably the `UpstreamHandler` holds a reference to the `DownstreamManager`, but this dependency is not in the listed dependencies for Section 4.11. | The dependency list says `shell/downstream` is a dependency of `shell/upstream` (line ~836, via the module dep diagram). Good. But the `UpstreamHandler` Protocol does not show how it accesses the downstream tool list. Add a note that the implementation must hold a reference to `DownstreamManager` (or the resolved tool registry). |
| S8 | D1 Completeness | Section 12 (Extension Points) | The document mentions `fastmcp>=2.0.0` as a dependency (pyproject.toml) but the design never specifies how fastmcp is used. The MCP server is implemented via fastmcp's API, but the design does not document the integration pattern. Open Question 1 acknowledges this gap for SSE, but even the stdio path is unspecified. For a "single source of truth" design doc, this is a meaningful gap for implementers. | Add a subsection to Section 12 (or a new Section 12.5) documenting the expected fastmcp integration pattern: how the MCP server is created, how tools are registered, how the initialize handler is hooked. Even a brief "fastmcp Integration" section stating "Use `FastMCP` server class, register tools via `@server.tool()`, handle initialize via custom session handler" would reduce ambiguity. |
| S9 | D2 Feasibility | Section 7.1, token in `clientInfo` (line ~1201) | The design specifies that the capability token is passed in `clientInfo.capability_token` during MCP `initialize`. However, the MCP protocol `initialize` request sends `clientInfo` with schema `{ name: string, version: string }`. There is no standard `capability_token` field. fastmcp may or may not support custom fields in `clientInfo`. If it does not, the token delivery mechanism breaks entirely. | Verify that fastmcp allows arbitrary fields in `clientInfo` (MCP spec says `clientInfo` is extensible). If not, document an alternative token delivery mechanism (e.g., custom MCP method `tela.authenticate`, or a query parameter for SSE, or an environment variable). This is the single most important integration risk. |

### SUGGESTION

| ID | Dimension | Location | Finding | Recommendation |
|----|-----------|----------|---------|----------------|
| G1 | D5 Clarity | Section 3, Directory Layout (line ~77) | The directory layout shows `src/tela/core/` and `src/tela/shell/` but the pyproject.toml says `packages = ["src/tela"]`. The Invar rules in CLAUDE.md specify `**/core/**` and `**/shell/**` as path patterns. These are consistent, but the mapping from `core/config.py` (in section headers) to `src/tela/core/config.py` (actual file path) is implicit. | Add a one-line note: "All module references like `core/config` refer to `src/tela/core/config.py`." |
| G2 | D5 Clarity | Section 4.5, `posture_from_annotations` (line ~587) | The mapping `readOnlyHint=False, destructiveHint=False -> READ_WRITE` is surprising. A tool that is explicitly neither read-only nor destructive could reasonably be classified as `READ_WRITE`, but it could also just mean the annotations are uninformative. Consider: what about `readOnlyHint=True, destructiveHint=True`? This contradictory case is not handled. | Add a note for the contradictory case: "If both `readOnlyHint=True` and `destructiveHint=True`: treat as `DESTRUCTIVE` (most restrictive wins)." Also clarify: "If both are `False`, classify as `READ_WRITE` (tool is mutating but not destructive)." |
| G3 | D1 Completeness | Section 4.1, `MetaField` model (line ~276) | The `MetaField` model has typed fields (`trace_id`, `event_id`, etc.) but the `meta.schema.json` contract sets `additionalProperties: false`. This means any extra fields from clients will be rejected. However, the DESIGN.md Section I (`_meta Handling`) says "tela handles this transparently" and does not mention validation. Should tela validate `_meta` against the schema, or accept any dict? | Decide and document: either (a) tela validates `_meta` against the schema (reject unknown fields), or (b) tela accepts any dict as `_meta` and stores it as-is (the schema is informational for producers, not enforced by tela). Option (b) is more resilient. |
| G4 | D5 Clarity | Section 9.1, config YAML (line ~1322) | The SSE URL scheme is shown as `sse://host:port`. The MCP ecosystem uses `http://` or `https://` for SSE endpoints. Clarify whether `sse://` is a tela-specific scheme that gets translated, or whether it should be a standard HTTP(S) URL. | Specify the URL scheme: "SSE URLs use `http://` or `https://`. The `sse://` prefix in examples is shorthand; the implementation should accept both." Or commit to `sse://` as a custom scheme and document the translation. |
| G5 | D1 Completeness | Section 13, Invariants | No invariant covers the relationship between `tools/list` filtering and `tools/call` enforcement. Specifically: if a tool passes `tools/list` filtering, does it always pass `tools/call` enforcement (assuming no concurrent hot reload)? This is a critical consistency property. | Add invariant: "A tool returned by `tools/list` for a given connection MUST pass the enforcement chain when called by that same connection, provided no hot reload has occurred since the `tools/list` response. Violation of this invariant is a security consistency bug." |
| G6 | D5 Clarity | Section 4.12, `build_audit_entry` (line ~937) | The function takes `result: EnforcementResult` but also `latency_ms`, `arguments`, `request_content`, `response_content`. For a denied call, `latency_ms` is meaningless (no downstream call happened) and `response_content` is None. But for an allowed call that hits `DOWNSTREAM_ERROR`, there may be partial results. The doc does not specify what to record in each case. | Add a note: "For denied calls, `latency_ms` is the enforcement chain latency only. `response_content` is None. For downstream errors, `response_content` contains the error from the downstream server." |
| G7 | D1 Completeness | Section 15, Open Questions | Open Question 5 (graceful shutdown) lists shutdown concerns but does not mention what happens to in-flight tool calls. A call that is mid-execution on a downstream server during shutdown could produce an orphaned request. | Add to Open Question 5: "In-flight tool calls during shutdown: should tela wait for active calls to complete (drain), or immediately close connections? Drain timeout?" |

---

## 4. Dimension Summary

| Dimension | Status | Issues | Notes |
|-----------|--------|--------|-------|
| D1: Completeness | Needs Revision | B1, S2, S3, S4, S6, S7, S8, G3, G5, G6, G7 | The module specs are thorough but have gaps at boundaries: where env vars enter `parse_config`, where `default_posture` enters `enforce`, and how `UpstreamHandler` accesses tools. |
| D2: Feasibility | Conditional Pass | S9 | The `clientInfo.capability_token` delivery mechanism is the largest feasibility risk. Everything else is implementable with fastmcp + standard Python. |
| D3: Consistency | Conditional Pass | B1, B2, S1, S5 | Cross-references to INTERFACES.md and opifex docs are highly consistent. Internal consistency has the `parse_config` signature gap, the enforcement ordering contradiction, and the dual error type confusion. |
| D4: Risk | Acceptable | S9, G2 | Security model is well-thought-out (fail-closed, dual-key, token threat model). Main risks are integration-level (fastmcp API assumptions). |
| D5: Clarity | Good | B2, G1, G2, G4, G6 | The document is remarkably clear for its length. Diagrams, tables, and code signatures make it actionable. The enforcement chain ordering is the main clarity failure. |

---

## 5. Advisor Perspectives

No formal expert consultations were required. The following observations apply:

**@software-architect perspective**: The Core/Shell separation is well-enforced by Invar rules and the module dependency diagram is acyclic. The design correctly keeps all pure logic in `core/*` and all I/O in `shell/*`. The Protocol-based interfaces for Shell modules allow easy test-doubling. The separation of `core/enforcement.py` as a pure function taking all inputs is a strong architectural choice.

**@se-expert perspective**: The Invar protocol (pre/post contracts + doctests) is appropriate for the enforcement chain, classification, and family mapping -- these are exactly the kinds of pure functions that benefit most from contracts. The `returns.Result` usage in Shell is consistent with the project's error-handling philosophy.

**@llm-agent-expert perspective**: The token threat model is well-articulated: defending against the agent, not network attackers. The `_meta` stripping invariant is critical and correctly positioned as unconditional. The open mode fallback for standalone Claude Code use is a pragmatic escape hatch.

---

## 6. Non-Goals

This review explicitly did NOT check:

- **Plan quality**: The `plan.yaml` and `docs/dag.md` were read for context but not reviewed for phase ordering or step completeness.
- **Test coverage adequacy**: The testing strategy (Section 14) was noted but individual test case sufficiency was not evaluated.
- **Performance**: No performance modeling or bottleneck analysis was performed. The design does not make performance claims that need validation.
- **Deployment/operational concerns**: No runbook or deployment guide exists to review.
- **fastmcp API compatibility**: Verifying that fastmcp 2.0 actually supports the assumed APIs would require code experimentation, which is outside this review's scope.

---

## 7. Strengths

1. **Exceptional cross-document consistency**: The DESIGN.md is remarkably faithful to both INTERFACES.md and the opifex upstream docs. Error codes, token structures, enforcement semantics, and config schemas all match their source-of-truth contracts.

2. **Clear module boundaries with explicit responsibilities and non-responsibilities**: Every module spec states what it does and what it does not do. This is rare and valuable.

3. **Security-first invariants**: The 5 security invariants (Section 13) are concrete and falsifiable. The fail-closed default, unconditional `_meta` stripping, and "no implicit profile" rule are all the right calls.

4. **Pure core / impure shell separation**: The Invar-enforced architecture means every enforcement decision can be unit-tested without mocking, which is exactly the right approach for a security-critical gateway.

5. **Honest open questions**: Section 15 lists 5 unresolved questions without pretending they are solved. This is better than guessing -- it flags integration risks for implementers.

6. **Complete data model**: Every model used at module boundaries is fully defined with types. `Posture`, `SideEffectPolicy`, `EnforcementVerdict` enums are well-chosen and minimal.

7. **Consistent error model**: The error code range (200-299) is cleanly carved out in the opifex-wide `errors.yaml`, and every error code maps to a specific exception class.

---

## 8. Next Steps

1. [ ] **Fix B1**: Resolve the `parse_config` signature to accept `env_vars` or restructure env expansion to happen in the Shell layer before `parse_config` is called.
2. [ ] **Fix B2**: Resolve the enforcement chain step 4/5 ordering to make the short-circuit vs. override semantics unambiguous. Document whether `tool_overrides: allow` can rescue a posture-denied tool.
3. [ ] **Fix S1**: Document the relationship between `TelaError` (wire format) and `TelaException` (internal exception). Add conversion method.
4. [ ] **Fix S2**: Add note that `parse_config` must inject dict keys into `ServerConfig.name` and `ProfileConfig.name`.
5. [ ] **Fix S6**: Either add `default_posture` to `enforce` parameters, or require `ResolvedTool.posture` to never be `None` by the time it reaches enforcement.
6. [ ] **Fix S9**: Verify fastmcp `clientInfo` extensibility or document alternative token delivery.
7. [ ] **Address remaining SHOULD_FIX items** (S3, S4, S5, S7, S8).
8. [ ] **Consider SUGGESTION items** (G1-G7) during implementation.
