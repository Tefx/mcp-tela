# ADR-008: Operator Recovery Exposure

## Status

Accepted for implementation planning.

## Context

P1 operator recovery has two possible exposure postures:

- **A**: expose a remote authenticated explicit recovery mutation with audit evidence and no passive side effects.
- **B**: keep `tela doctor --recover` as the only recovery mutation path and document that remote recovery is absent or rejected.

The relevant boundary is the HTTP/operator surface in `docs/INTERFACES.md` §7.2a: `GET /status` is readiness truth, `POST /connect` is lifecycle plumbing, and admission/retry decisions must not be inferred from passive probes or bridge-local state. `tela doctor` is passive unless `--recover` is supplied.

`opifex` remains the canonical owner for shared PersonaSpec, JobSpec, CapabilityToken, and profile-list contract meaning. This decision must not redefine any of those shapes.

## Decision

Choose **Branch B: CLI-only recovery**.

`tela doctor --recover` remains the only operator recovery mutation path. Remote HTTP/MCP operator clients must either have no recovery endpoint/tool or receive a documented rejection from any attempted remote recovery surface. A later implementation step must add tests proving the remote surface is absent or rejected.

I chose CLI-only recovery because the current hard requirement is authority adjudication, not remote operability. The naive existing path already provides an explicit operator mutation (`doctor --recover`) without adding another remote mutating surface, audit category, or authorization branch.

## Normative Requirements

- **No passive status/probe recovery**: `GET /status`, `tela status --probe`, health checks, and read-only MCP/operator listing calls must observe diagnostics only. They must not clean stale discovery, cold-start, reconnect, or otherwise invoke recovery.
- **No RBAC is introduced**: this decision does not add roles, permissions, or approval layers. Existing bearer-token admission and profile binding remain authentication/admission mechanisms, not a new operator role model.
- **No shared contract mutation**: this decision must not change PersonaSpec, JobSpec, CapabilityToken, or `tela_list_profiles` semantics or payload shapes.
- **Remote recovery posture**: remote recovery is intentionally absent or rejected. If an implementation encounters a proposed remote recovery endpoint/tool, it must fail closed with documented behavior rather than silently recovering.
- **Audit boundary**: since no remote recovery mutation is added, no new remote recovery audit event is required by this ADR. Existing CLI recovery diagnostics remain the mutation evidence for `doctor --recover`.

## Consistency Notes

- `docs/INTERFACES.md` §7.2a keeps `GET /status` as an observation/readiness authority, not a mutation authority. CLI-only recovery preserves that split.
- `POST /connect` remains lifecycle plumbing and cannot be repurposed as recovery.
- The downstream call-path recovery from ADR-006 is failure-triggered tool-call recovery, not an operator status/probe side effect. This ADR does not expand that mechanism.
- Opifex ownership invariants are preserved because this ADR consumes canonical token/profile/persona/job meanings without editing or reinterpreting them.

## Downstream Test and Docs Impacts

Later implementation work must:

- document in `docs/INTERFACES.md` that remote operator recovery is absent or rejected while `tela doctor --recover` remains the recovery mutation path;
- add or update red/green tests proving passive status/probe paths do not call recovery;
- add or update tests proving remote recovery is absent or returns the documented rejection;
- avoid any PersonaSpec, JobSpec, CapabilityToken, or profile-list fixture/schema changes.

## Complexity Cost Receipt

1. **Parts Added**: one ADR decision record only; no code, endpoint, MCP tool, RBAC table, or schema.
2. **Simplest Alternative**: make no new decision record and leave `doctor --recover` as an implicit local convention.
3. **The Defense**: the plan requires a falsifiable posture choice for downstream tests/docs, so an explicit ADR is the smallest durable artifact that prevents workers from inventing remote recovery behavior.
