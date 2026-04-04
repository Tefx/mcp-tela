# Proof-Obligation Closure Contract (ADR-006 debt slice)

Authoritative scope: `debt_closure.contract_test.freeze_closure_contract`

Decision sources:
- Step contract in dispatcher message (required fields, required closure items, and verification constraints).
- `evidence/debt_closure_review_basis.md` (blocker rules, required structured artifacts, and OPEN/BLOCK rule).
- `evidence/review_miss_root_cause_analysis.md` (missed-family obligations and downstream ownership).
- `evidence/reversal_register.md` (re-close proof obligations and remediation ownership).

## Proof-Obligation Contract
| requirement_ref | claim_type | claim | expected_evidence | pass_fail_boundary | unresolved_status_policy | closure_path | gate_decision_basis |
|---|---|---|---|---|---|---|---|
| R13 | behavioral_proof | `_registry_lock` is not held across awaited network I/O in downstream recovery paths. | Named integration test(s) and/or tracing artifact showing lock-state transitions around awaited network calls; exact command lines and full raw output; artifact link in behavioral proof register. | **PASS** only if evidence demonstrates: (1) awaited network operation occurs, (2) lock is released before await begins and remains unheld during await window, (3) concurrent registry access is not blocked by that await window. **FAIL** if any condition is missing, inferred, or contradicted. Static code reading alone cannot pass. | Allowed statuses: `PROVEN`, `NEEDS_TEST`, `UNPROVEN`, `UNCERTAIN_BLOCKING`. Any status except `PROVEN` is blocker-class and forces `BLOCK`. | If unresolved, carry to `debt_closure.runtime_evidence.collect_behavioral_proof` and then `debt_closure.impl.close_runtime_gap_if_exposed` when defect is exposed. | Final gate must cite the behavioral proof register row and explicitly justify why runtime evidence (not prose) satisfies all three PASS conditions. |
| R42 | behavioral_proof | Per-server recovery lock is pruned after config-reload-remove and disconnect scenarios, including in-flight recovery cases. | Runtime witness evidence for removal/disconnect while recovery is in flight; command+output proving `config_missing=true` where applicable and no stale per-server recovery lock after cleanup; artifact link in behavioral proof register. | **PASS** only if evidence demonstrates both paths: (1) config-reload-remove during in-flight recovery, (2) disconnect path under recovery pressure; and in both paths lock cleanup is observed with no stale lock state post-cleanup. **FAIL** if either path is missing or lock cleanup is only inferred from source. | Allowed statuses: `PROVEN`, `NEEDS_TEST`, `UNPROVEN`, `UNCERTAIN_BLOCKING`. Any status except `PROVEN` is blocker-class and forces `BLOCK`. | If unresolved, carry to `debt_closure.runtime_evidence.collect_behavioral_proof` and then `debt_closure.impl.close_runtime_gap_if_exposed` when defect is exposed. | Final gate must cite explicit runtime evidence rows for both scenarios and reject closure if any blocker status remains. |
| UNC-LIVENESS-HEALTHY-NEIGHBOR | uncertainty_question | Healthy-neighbor liveness remains unaffected while failing server recovery is in progress (supporting R13/R42 closure confidence). | Integration evidence that a healthy neighbor continues serving/responding during failing-server recovery test windows; exact commands and outputs; uncertainty register row linking the witness artifact. | **PASS** only if healthy-neighbor requests succeed throughout failing-server recovery window without induced lock starvation symptoms. **FAIL** if not measured or if success is sampled outside recovery window. | Allowed statuses: `RESOLVED_NON_BLOCKING`, `UNCERTAIN_BLOCKING`. Mark `UNCERTAIN_BLOCKING` if evidence is missing or indicates possible cross-server impact; that forces `BLOCK`. | If unresolved, carry to `debt_closure.runtime_evidence.resolve_uncertainty_register`; remediation, if required, routes to `debt_closure.impl.close_runtime_gap_if_exposed`. | Gate decision must state whether this uncertainty intersects downstream closure gates; non-intersection claims must name remaining gates not intersected. |
| UNC-CONFIG-MISSING-FAIL-CLOSED | uncertainty_question | Missing-server path fails closed with `config_missing=true` where closure contract requires it (supports R42 closure semantics). | Runtime evidence from removal/recovery scenarios showing explicit fail-closed behavior and emitted `config_missing=true`; exact commands and full output; uncertainty register row citing artifacts. | **PASS** only if fail-closed response with `config_missing=true` is observed on required path(s) and no permissive success response leaks through. **FAIL** if field missing, ambiguous, or contradicted. | Allowed statuses: `RESOLVED_BLOCKING`, `UNCERTAIN_BLOCKING`. `UNCERTAIN_BLOCKING` forces `BLOCK`. | If unresolved, carry to `debt_closure.runtime_evidence.resolve_uncertainty_register`, then `debt_closure.impl.close_runtime_gap_if_exposed` for implementation remediation. | Gate decision must show this uncertainty is resolved before OPEN, or explicitly BLOCK with owner and re-close proof. |
| SURFACE-REENUMERATE | surface_decision | `re_enumerate()` classification is explicitly one of: supported public surface, framework-only escape hatch, or dead export to remove. | Decision record in runtime uncertainty register and matching supporting artifact: docs/tests for supported public surface; framework-only annotation plus non-public contract text; or removal/refactor evidence for dead export path. | **PASS** only if one classification is selected and matching evidence exists. **FAIL** if unclassified, mixed classifications, or evidence does not match selected classification. | Allowed statuses: `RESOLVED_EXTERNAL_CONTRACT`, `RESOLVED_INTERNAL_ONLY`, `RESOLVED_COMPATIBILITY_SHIM`, `UNCERTAIN_BLOCKING`. `UNCERTAIN_BLOCKING` forces `BLOCK`. | If unresolved or docs cannot support a confident outcome, bind to `debt_closure.impl.decide_surface_and_manifest_authority` before any later gate can OPEN. | Gate basis must include the chosen classification, evidence artifact, and why alternatives were rejected. |
| AUTH-MCP-FASTMCP | manifest_authority_decision | mcp/fastmcp authority is reconciled into one authoritative tuple: declared package authority, canonical import authority, and manifest/header wording authority. | Single authority record citing `pyproject.toml`, runtime import site(s), manifest/header source text, and contract tests that verify the chosen tuple or explicit translation boundary. | **PASS** only if all three authorities are consistent or an explicit translation boundary is documented and verified by tests/docs. **FAIL** if authorities remain split, implied, or undocumented. | Allowed statuses: `RESOLVED_CANONICAL_TUPLE`, `UNCERTAIN_BLOCKING`. `UNCERTAIN_BLOCKING` forces `BLOCK`. | If unresolved or current docs cannot justify one authority tuple, bind to `debt_closure.impl.decide_surface_and_manifest_authority` and block `debt_closure.verify`/`debt_closure.final_review` from OPEN. | Final gate must quote the exact tuple and evidence references; provenance of conflicting artifacts cannot be used as disposition. |

## Runtime Uncertainty Register Schema (required)

This schema is mandatory for downstream artifacts that carry unresolved or resolved uncertainty.

| field | required | description |
|---|---|---|
| `requirement_ref` | yes | Stable ID tying the uncertainty row to contract row(s). |
| `claim` | yes | Exact uncertainty claim under evaluation. |
| `uncertainty_type` | yes | One of: `behavioral_runtime`, `surface_classification`, `authority_reconciliation`, `non_intersection_claim`. |
| `status` | yes | One of: `RESOLVED_NON_BLOCKING`, `RESOLVED_BLOCKING`, `UNCERTAIN_BLOCKING`. |
| `blocking` | yes | Boolean; must be `true` whenever `status=UNCERTAIN_BLOCKING`. |
| `evidence_ref` | yes | Artifact(s) and command output proving current status. |
| `owner_step` | yes when unresolved | Downstream owner for unresolved rows. |
| `required_resolution` | yes | Concrete proof or decision needed to resolve the row. |
| `review_miss_cause` | required for reversed closure items | Why prior review allowed premature closure. |
| `re_close_proof` | required for reversed closure items | Evidence required before re-close is allowed. |
| `remaining_gates_not_intersected` | required when claiming non-intersection | Explicit downstream gates unaffected by this row. |

### Uncertainty status rule
- Any row with `status=UNCERTAIN_BLOCKING` or `blocking=true` forces overall gate `BLOCK`.
- Provenance labels (for example `pre-existing`) are informational only and cannot satisfy disposition.

## Gate Decision Prompts (binding)

1. For each blocker-class row (R13, R42), where is runtime witness evidence proving PASS conditions, and where is the exact command/output citation?
2. For each uncertainty row, does status satisfy schema and closure policy, or is `UNCERTAIN_BLOCKING` still present?
3. For `re_enumerate()`, which single classification is selected and what artifact proves it?
4. For mcp/fastmcp, what is the exact authority tuple, where is it documented, and which tests prove boundary behavior?
5. Do any non-intersection claims omit `remaining_gates_not_intersected`? If yes, gate result is `BLOCK`.
6. Does any decision rely on vague language (`looks safe`, `probably conformant`, `appears fine`)? If yes, gate result is `BLOCK`.
