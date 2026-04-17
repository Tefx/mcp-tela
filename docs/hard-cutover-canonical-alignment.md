# Tela Hard-Cut Canonical Alignment

## Status

Complete

## Authority Chain

- shared contract authority: `../opifex`
- packaged local mirror: `vendor/opifex/contracts/` (read-only)
- tela runtime responsibility: verify and enforce the canonical shared contract

## Final Canonical State

### CapabilityToken boundary

- runtime validates the full canonical CapabilityToken shape
- shared binding identity is `profile_id`
- legacy alias fields are rejected fail-closed and audited

### Shared profile-list boundary

- shared profile enumeration is exposed only through `tela_list_profiles`
- payload shape is exactly:

```json
[
  {
    "profile_id": "developer",
    "capabilities": {
      "filesystem": "read_only"
    },
    "default": true
  }
]
```

- payload validation is fail-closed
- more than one `default: true` entry is rejected

### Local config surface

- local profile config accepts canonical posture strings
- local config remains capability-only
- no legacy shared vocabulary is accepted back into runtime parsing

## Verification Summary

Alignment is complete only when all of the following remain true:

1. CapabilityToken validation stays fully canonical
2. shared profile enumeration stays canonical and fail-closed
3. only one schema authority chain remains: `../opifex` → read-only vendor mirror
4. docs, tests, and examples use canonical shared vocabulary only

## Complexity Cost Receipt

1. **Parts Added**: none beyond the minimal vendor mirror, runtime validation, and regression tests required for conformance
2. **Simplest Alternative**: keep multiple shared vocabularies and multiple schema copies
3. **The Defense**: that would recreate contract drift and a second truth source, which the hard cut explicitly forbids
