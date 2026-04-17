# ADR-007: Opifex Canonical Contract Alignment

## Status

Accepted and implemented

## Context

`mcp-tela` participates in a cross-repo contract where `../opifex` defines the
shared meaning for token, profile-list, and `_meta` surfaces.

The risks this ADR closes are:

- local convenience names leaking into shared boundaries
- repo-local docs or snapshots being mistaken for contract authority
- independently distributed tela builds drifting from sibling repos

## Decision

Choose strict canonical alignment.

The architecture is:

- `../opifex/contracts/capability_token.schema.json` is the only canonical
  shared token schema
- local packaged schemas may exist only as read-only mirrors under
  `vendor/opifex/contracts/`
- `profile_id` is the only canonical shared profile-binding identity
- legacy alias fields are invalid on shared token and shared profile-list
  boundaries
- `_meta` remains audit/reference context only and never participates in
  authentication or authorization decisions
- tela verifies, enforces, and audits shared behavior but does not become a
  second contract authority

## Consequences

- repo-facing docs must use canonical shared vocabulary only
- examples and tests must frame retired shared names only as rejection cases,
  never as active input or output shapes
- release/distribution workflows may vendor read-only snapshots, but edit
  authority stays in `../opifex`

## Complexity Cost Receipt

1. **Parts Added**: read-only vendor mirrors and regression tests that enforce parity and authority hygiene
2. **Simplest Alternative**: keep local editable schema copies and explain the preferred source in prose
3. **The Defense**: that still leaves multiple plausible authorities, which is exactly the contract failure this ADR forbids
