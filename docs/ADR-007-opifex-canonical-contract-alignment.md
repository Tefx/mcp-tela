# ADR-007: Tela Contract Ownership

## Status

Accepted; revised after Tela became independent and Opifex was retired.
The filename is retained for existing references to this decision.

## Context

Tela previously consumed schemas and conformance metadata from an external
Opifex checkout. That repository is retired. Tela development, tests and CI
must work from this checkout alone.

## Decision

- Tela owns the schemas in `contracts/capability_token.schema.json` and
  `contracts/tela_profile_list.schema.json`.
- `docs/INTERFACES.md` defines admission, `_meta`, forwarding and operator
  semantics; `docs/CONFIRMED-SURFACE-CONTRACT.md` defines surface classification.
- `contracts/errors.yaml` defines error semantics.
- `profile_id` remains the canonical profile-binding identity.
- Legacy alias fields remain invalid on token and profile-list boundaries.
- `_meta` remains audit/reference context only and does not participate in
  authentication or authorization decisions.
- Schema changes require matching runtime validation, behavioral tests and
  interface documentation in the same change.

## Consequences

No sibling repository, external authority pin, or environment variable is
required to run tests or CI. The former vendor mirrors are the editable
Tela-owned contracts; duplicate schema copies are removed.

The repo-local gate runs schema/runtime agreement checks, boundary rejection
tests and documentation parity checks. Historical evidence retains its original
references and does not impose current external prerequisites.
