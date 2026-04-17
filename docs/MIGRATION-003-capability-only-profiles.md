# Migration: Capability-Only Tela Profiles

## Status

Complete

## Canonical End State

Tela profile configuration is now capability-only.

Canonical example:

```yaml
profiles:
  reviewer:
    capabilities:
      filesystem: read_only
      git: read_only
    tool_overrides:
      filesystem:
        overrides:
          delete_file: deny
```

## Active Rules

- profile input uses `capabilities`
- posture values on the local config surface accept canonical strings such as
  `none`, `read_only`, `read_write`, and `destructive`
- `tool_overrides` may restrict or selectively allow only within the family
  ceiling
- there is no secondary mutability layer in gateway profiles

## Migration Closure

- migration-only input forms are retired
- profile serialization and shared profile listing use the same canonical
  capability vocabulary
- current tests should target the canonical profile shape only

## Verification Pointers

- `tests/core/test_capability_only_profiles.py`
- `tests/core/test_hard_cut_vocab.py`
- `tests/shell/test_surface_contract.py`
