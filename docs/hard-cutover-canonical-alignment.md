# Tela Hard-Cutover Canonical Alignment

## Status

Target implementation plan for the no-compatibility cutover.

## Responsibility

`tela` owns:

- profile registry truth
- concrete family/posture enforcement
- canonical token verification against the bound profile identity

It must expose one canonical profile-list surface and one canonical token vocabulary.

## Canonical References

- `/Users/tefx/Projects/opifex/design/final-canonical-contract.md`
- `/Users/tefx/Projects/opifex/design/tela-clean-gateway.md`
- `/Users/tefx/Projects/opifex/contracts/capability_token.schema.json`

## Current Problems

1. profile config still accepts `tools` as an alias for `capabilities`.
2. shared token and connection vocabulary still uses `profile_name`.
3. profile listing is exposed as a resource dependency instead of one canonical MCP tool.
4. profile list payload uses `profile_name`, may emit `families`, and also emits `tools`.
5. builtin tool naming is mixed with other surface conventions across repos.

## Target End State

### Profile config and model

Use only:

- `profile_id`
- `capabilities`

Do not accept:

- `tools`
- `profile_name`

### Token contract

Use the full canonical CapabilityToken shape defined by the umbrella contract
and schema, including:

- `token_id`
- `profile_id`
- `persona_ref`
- `instance_id`
- `max_depth` (optional)
- `token_version`
- `issued_at`
- `expires_at`
- `signature`

Tela extracts and validates the `profile_id` binding from the full canonical
token payload. It must reject tokens that carry `profile_name` or
`tools_profile` instead, failing closed rather than ignoring the alias fields.

Alias rejection semantics:

- rejection happens during canonical token validation before authorization proceeds
- tokens containing both canonical and alias binding fields are rejected; canonical fields do not "win"
- rejection should surface a stable tela-local token-validation error such as `TOKEN_ALIAS_FIELD_PRESENT`
- alias-field rejection must be auditable in gateway logs

These example error labels are tela-local implementation details, not shared
canonical vocabulary.

### Profile listing

Expose exactly one canonical MCP tool:

- `tela_list_profiles`

Return payload:

```json
[
  {
    "profile_id": "gateway/default-readonly",
    "capabilities": {
      "filesystem": "read_only"
    },
    "default": true
  }
]
```

No shared consumer should need the `tela://profiles` resource.

## Detailed Change Plan

### 1. Remove `tools` alias support

Delete:

- alias validation for `tools`
- `ProfileConfig.tools` compatibility property
- config warnings that discuss migration from `tools`
- dual-key output that emits both `capabilities` and `tools`

### 2. Rename shared identity vocabulary to `profile_id`

Update:

- token models
- token signing/verifying helpers
- connection/session/audit models where shared surfaces expose profile identity
- any API surface that returns or accepts the canonical profile identity

### 3. Replace profile resource dependency with a canonical tool

Add a builtin MCP tool:

- `tela_list_profiles`

Its output becomes the only shared profile enumeration surface used by sibling repos.

### 4. Normalize profile list payload shape

Delete payload keys:

- `profile_name`
- `families`
- `tools`

Return only:

- `profile_id`
- `capabilities`
- `default`

`default` is a required boolean that marks the open-mode fallback candidate when
no explicit default profile is supplied. At most one returned profile may carry
`default: true`.

Default semantics:

- zero `default: true` entries is valid and means no open-mode fallback is available
- more than one `default: true` entry is invalid and must fail closed with a stable error such as `INVALID_DEFAULT_PROFILE_STATE`
- the flag is determined by tela-owned profile config and CLI default-profile resolution rules

Schema note:

- the shared `tela_list_profiles` payload is schema-bound by `/Users/tefx/Projects/opifex/contracts/tela_profile_list.schema.json`

### 5. Keep enforcement logic, rename only the shared vocabulary

The concrete authorization logic is already structurally correct:

- resolve family
- resolve posture
- compare against profile capability ceiling

Keep that logic.
Delete only alias and naming drift.

## Files In Scope

- `src/tela/core/models.py`
- `src/tela/core/config.py`
- `src/tela/core/token.py`
- `src/tela/shell/upstream.py`
- `src/tela/shell/gateway.py`
- `src/tela/core/profile_aliases.py`
- `docs/INTERFACES.md`
- `docs/CONFIRMED-SURFACE-CONTRACT.md`
- `docs/USAGE.md`
- `docs/ADR-007-opifex-canonical-contract-alignment.md`
- any docs/examples that still use `profile_name`, `families`, or `tools`

## Deletions

Delete, do not deprecate:

- `tools` profile alias
- `ProfileConfig.tools`
- `tela://profiles` as the required shared profile discovery surface
- `profile_name` on shared token/profile-list surfaces
- `families` on shared profile-list surfaces
- dual output containing both `capabilities` and `tools`
- `src/tela/core/profile_aliases.py` after inventory confirms it exists only to preserve shared alias semantics

## Verification

1. config parsing accepts `capabilities` only.
2. token verification validates all required canonical token fields (`token_id`, `profile_id`, `persona_ref`, `instance_id`, `issued_at`, `expires_at`, `token_version`, `signature`) and validates `max_depth` against schema constraints when present; if `max_depth` is absent, validation proceeds without a token-specific depth restriction.
3. token validation explicitly rejects `profile_name` and `tools_profile` alias fields before authorization proceeds, surfacing a tela-local error such as `TOKEN_ALIAS_FIELD_PRESENT`.
4. `tela_list_profiles` is present as a builtin MCP tool.
5. `tela_list_profiles` returns `profile_id + capabilities + default` only, with no `tools`, `families`, or `profile_name` keys; if downstream state would produce forbidden keys or otherwise violate `/Users/tefx/Projects/opifex/contracts/tela_profile_list.schema.json`, tela fails closed at the gateway boundary with a tela-local error such as `INVALID_PROFILE_LIST_PAYLOAD` rather than emitting them.
6. profile-list capability postures are limited to `none | read_only | read_write | destructive`.
7. audit and enforcement bind to the same canonical `profile_id` vocabulary.

## Dependency Order

This plan lands first in the hard-cut sequence so downstream consumers can move
to the canonical profile-list tool and payload before old surfaces disappear.

## Rollback Procedure

If the hard cut causes a blocking regression, rollback is a git revert of the
cutover commit set followed by rerunning token/profile verification and
interface-contract checks. Rollback does not preserve alias behavior in a new
forward patch; it restores the prior revision cleanly. Post-rollback verification
must confirm the restored revision behaves according to its own contract set.

## Non-Goals

This plan does not redefine:

- PersonaSpec validation authority (`larva` scope)
- runtime approval workflow semantics (`anima` scope)
- `side_effect_policy` handling outside tela-owned shared surfaces

## Failure Conditions

The cutover is incomplete if any of the following remain true:

- config still accepts `tools`
- token verification still expects `profile_name`
- shared consumers still need `tela://profiles`
- profile list output still emits `tools`, `families`, or `profile_name`

## Complexity Cost Receipt

1. **Parts Added**: one builtin MCP tool `tela_list_profiles`
2. **Simplest Alternative**: keep the resource, keep alias support, and let consumers adapt
3. **The Defense**: the naive alternative fails because `tela` is the owner of profile registry truth; if the owner keeps publishing multiple vocabularies, every consumer must stay dirty forever
