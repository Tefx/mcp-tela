# Independent Gate Audit: tela.operator_p1.scope_decision.gate

## refs Read Confirmation (MANDATORY)

- `evidence/p1_operator_scope_decision_authority_and_source_discovery.md` — read lines 3-40; key passages: planning-only record, tela ownership/non-ownership boundary, five P1 targets with source locations, and source map for status/audit/clients/probe/recovery/authorization.
- `docs/ADR-008-operator-recovery-exposure.md` — read lines 1-54; key passages: decision chooses **B: CLI-only recovery**, no passive recovery, no RBAC, no shared contract mutation, remote recovery absent or rejected.
- `docs/INTERFACES.md` — read purpose, HTTP endpoints, status response schema, and audit limits; key passages: tela is gateway/authorization layer, `GET /status` readiness authority, `POST /connect` lifecycle plumbing, status `audit_entries` limit 100, audit query default 100/no max.
- `docs/DESIGN.md` — read design boundary and ownership rules; key passages: tela owns downstream aggregation/profile authorization/tool classification/connection binding/audit; core owns auth semantics; shell owns transport/process lifecycle; CLI delegates.

## Gate Review Report

- P1 checklist complete: yes
- Recover decision selected: B CLI-only
- Source discovery adequate: yes
- Non-goals preserved: no RBAC, no PersonaSpec/JobSpec/CapabilityToken changes, no passive recovery, /status.audit_entries remains recent summary
- Blockers: none
- Gate decision: OPEN

## Notes

- Non-blocking observation: `docs/ADR-008-operator-recovery-exposure.md` cites `docs/INTERFACES.md` §7.2a, but the current `docs/INTERFACES.md` table of contents contains §7.2 and §7.2.2, not §7.2a. The controlling statements are still present under §7.2/§7.2.2, and the source-discovery evidence explicitly records this mismatch, so this is not blocking for the scope-decision gate.
- Verification run: full `invar_guard` completed with ok=true, errors=0, warnings=50, infos=20, files_checked=65.
