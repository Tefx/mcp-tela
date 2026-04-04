# Debt-Closure Review Hardening Basis

## Scope

This artifact is the binding review basis for the `debt_closure` phase group.
It exists to prevent the review miss recorded in `evidence/review_miss_root_cause_analysis.md` from recurring.

Controlling evidence:

- `evidence/review_miss_root_cause_analysis.md`
- `evidence/reversal_register.md`
- `evidence/ADR-006-deep-conformance-audit.md`

## Closure Criteria

### R13

- `R13` remains blocker-class until a structured **behavioral proof register** row marks it `PROVEN` with runtime witness evidence for "no `_registry_lock` held during awaited network I/O".
- Static inspection may support analysis, but it cannot retire `R13` by itself.
- Any gate decision basis that reports `R13` as `NEEDS_TEST`, `UNPROVEN`, or `UNCERTAIN_BLOCKING` MUST end in `BLOCK`, never `OPEN`.

### R42

- `R42` remains blocker-class until a structured **behavioral proof register** row marks it `PROVEN` with runtime witness evidence for lock cleanup when config reload removes a server during in-flight recovery.
- "Code path exists" or "cleanup helper called" is insufficient closure.
- Any gate decision basis that reports `R42` as `NEEDS_TEST`, `UNPROVEN`, or `UNCERTAIN_BLOCKING` MUST end in `BLOCK`, never `OPEN`.

### re_enumerate surface clarity

- Any debt-closure review touching public-surface claims MUST carry a structured decision for `re_enumerate()` in the **runtime uncertainty register** or equivalent contract artifact.
- Allowed states are only:
  - `RESOLVED_EXTERNAL_CONTRACT`
  - `RESOLVED_INTERNAL_ONLY`
  - `RESOLVED_COMPATIBILITY_SHIM`
  - `UNCERTAIN_BLOCKING`
- If the symbol remains unclassified, the gate result is `BLOCK`.

### mcp/fastmcp manifest clarity

- Any debt-closure review touching dependency, manifest, or runtime import authority MUST emit one authoritative tuple in the **final gate decision basis**:
  - declared package authority
  - canonical import authority
  - manifest/header wording authority
- If those authorities disagree and the translation boundary is not explicitly recorded, the gate result is `BLOCK`.

## Required Structured Artifacts

Every downstream debt-closure gate review MUST consume and cite these exact artifact types:

1. **Behavioral proof register**
   - Required purpose: enumerate blocker-class behavioral obligations and their proof status.
   - Minimum fields: `requirement`, `classification`, `status`, `evidence_artifact`, `reviewer_disposition`, `remaining_gap`.
   - Binding rule: any blocker-class row with `status` in `{NEEDS_TEST, UNPROVEN, UNCERTAIN_BLOCKING}` forces overall `BLOCK`.

2. **Runtime uncertainty register**
   - Required purpose: enumerate unresolved runtime or contract ambiguities that can still invalidate closure.
   - Minimum fields: `claim`, `uncertainty_type`, `status`, `blocking`, `owner_phase`, `required_resolution`.
   - Binding rule: any row marked `blocking=true` or `status=UNCERTAIN_BLOCKING` forces overall `BLOCK`.

3. **Final gate decision basis**
   - Required purpose: record the reviewer's final OPEN-vs-BLOCK decision from the two registers above.
   - Minimum fields: `gate`, `inputs_reviewed`, `blocking_rows`, `non_intersection_claims`, `decision`, `decision_rationale`.
   - Binding rule: this artifact is invalid if it says `OPEN` while either required register still contains unresolved blocker rows.

No later debt-closure gate may substitute narrative prose for these structured artifacts.

## Gate-Review Checklist

The gate reviewer MUST reject closure if any answer below is "no":

1. Does the behavioral proof register explicitly list `R13` and `R42`?
2. Are all blocker-class behavioral obligations either `PROVEN` or explicitly carried as `BLOCK`?
3. Is there no blocker-class row still marked `NEEDS_TEST`, `UNPROVEN`, or `UNCERTAIN_BLOCKING`?
4. Does the runtime uncertainty register classify `re_enumerate()` rather than leaving it implicit?
5. Does the final gate decision basis name one authority tuple for package/import/manifest treatment of FastMCP?
6. Does every non-intersection claim name the remaining gates it does not intersect?
7. If a prior closure was reversed, does the re-close packet include `review_miss_cause`, `remediation_owner`, and re-close proof?

If any checklist item fails, the required outcome is `BLOCK`.

## OPEN vs BLOCK Decision Rule

- **OPEN** is permitted only when all blocker-class behavioral obligations are `PROVEN` and all blocking runtime uncertainties are resolved in the cited structured artifacts.
- **BLOCK** is required when any blocker-class behavioral obligation remains `NEEDS_TEST`, `UNPROVEN`, or `UNCERTAIN_BLOCKING`.
- **BLOCK** is required when any uncertainty artifact still carries unresolved public-surface or authority-boundary ambiguity with downstream closure impact.
- A reviewer may not downgrade blocker-class evidence gaps to informational debt inside a passing decision basis.

## Non-Intersection Claim Rule

Any artifact that says an issue is "non-intersection" or "does not block this step" MUST also name the remaining gates it does not intersect.

Minimum required shape:

- `claim`
- `why_not_intersecting`
- `remaining_gates_not_intersected`
- `residual_risk`

Claims that omit `remaining_gates_not_intersected` are review-incomplete and force `BLOCK`.

## Review-Depth Guidance for debt_closure

### debt_closure.impl

- Review depth: authority reconciliation only.
- Required focus: dependency/import/manifest authority alignment and explicit non-intersection claims.
- Not sufficient for closure: implementation-only reasoning that does not update the required structured artifacts.

### debt_closure.contract_test

- Review depth: contract-surface classification and proof that documented public helpers are either intentionally public or intentionally non-public.
- Required focus: `re_enumerate()` classification and downstream contract evidence.
- Not sufficient for closure: "not in named surface matrix" without explicit retained classification.

### debt_closure.runtime_evidence

- Review depth: live behavioral proof for blocker-class runtime obligations.
- Required focus: `R13` and `R42` runtime witness evidence.
- Not sufficient for closure: code reading, helper existence, or source-only reasoning.

### All downstream closure gates

- Review depth: recompute closure from the structured artifacts, not from local optimism.
- Required focus: verify no blocker row survives in either register before permitting `OPEN`.
- Not sufficient for closure: carrying forward an earlier pass state after a reversal or after unresolved blocker evidence is merely documented.

## Post-Hoc Reversal Rule

If an earlier closure is reversed after review, the next re-close attempt MUST include:

- `review_miss_cause`
- `remediation_owner`
- `re_close_proof`

This rule is binding for all post-hoc reversals in the `debt_closure` phase group. A re-close packet missing any of the three fields is incomplete and forces `BLOCK`.

## Reviewer Prompts

- **gate-reviewer**: "Which blocker-class rows remain unresolved, and where do the required registers prove they are resolved rather than merely described?"
- **wiring-auditor**: "Do the declared package, canonical import, and manifest/header authorities name one consistent FastMCP tuple, or is the boundary still split?"
- **spec-verifier**: "Does every documented public helper, especially `re_enumerate()`, have an explicit contract classification and matching proof artifact?"

## Anti-Regression Rules

1. No debt-closure review may emit `OPEN` while any blocker-class behavioral obligation remains `NEEDS_TEST`, `UNPROVEN`, or `UNCERTAIN_BLOCKING`.
2. Static inspection can support `R13` or `R42`, but runtime witness evidence is required to close them.
3. All downstream closure gates must cite the behavioral proof register, runtime uncertainty register, and final gate decision basis by name.
4. Every non-intersection claim must enumerate the remaining gates it does not intersect.
5. Public-surface ambiguity is blocker-class when a helper is documented as public but not explicitly classified.
6. Dependency/manifest authority remains blocker-class until package, import, and manifest authority are reconciled in one recorded tuple.
7. Post-hoc reversals require `review_miss_cause`, `remediation_owner`, and `re_close_proof` before a re-close can be reviewed.
