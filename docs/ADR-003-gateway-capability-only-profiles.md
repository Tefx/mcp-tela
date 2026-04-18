# ADR-003: Gateway Profiles Express Capability Only

## Status

Accepted

## Scope

This ADR covers the local tela profile model only. Shared token, `_meta`, and
cross-repo contract authority are governed by ADR-007 and `../opifex`.

## Decision

Tela profiles express capability ceilings only.

Canonical profile shape:

```yaml
profiles:
  developer:
    capabilities:
      filesystem: read_write
      git: read_only
    tool_overrides:
      filesystem:
        overrides:
          delete_file: deny
```

The per-call enforcement chain is:

1. family admission
2. per-tool override evaluation
3. posture ceiling comparison

No override may exceed `capabilities[family]`.

## Rationale

- one capability map is simpler than multiple overlapping mutability controls
- the gateway should enforce concrete family/posture ceilings, not workflow policy
- the model is easier to explain, test, and keep aligned with sibling repos

## Consequences

Positive:

- profile behavior is explicit and local
- enforcement stays data-driven and easy to audit
- built-in and custom capability groups share one model

Negative:

- older migration-era examples are no longer valid references
- operators must use the canonical capability map everywhere

## Non-Goals

- human approval workflow semantics
- persona authority
- alternate shared profile vocabularies
