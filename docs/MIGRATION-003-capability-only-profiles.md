# Migration: Capability-Only Tela Profiles

## Goal

Move tela profiles from mixed posture + side-effect configuration to a single
capability map per ADR-003.

## Old Model

```yaml
profiles:
  reviewer:
    tools:
      filesystem: read_write
      git: read_only
    side_effect_policy: read_only
```

## New Model

```yaml
profiles:
  reviewer:
    capabilities:
      filesystem: read_only
      git: read_only
```

A profile with only `capabilities` and no `tool_overrides` is valid.
A profile with an empty `capabilities: {}` map is valid — it admits no tool
families and effectively denies all tool calls.

## Mapping Rule

If an old profile used `side_effect_policy: read_only`, cap every family posture
at `read_only` during migration.

Examples:

| Old | New |
|-----|-----|
| `filesystem: read_write` + `read_only` policy | `filesystem: read_only` |
| `git: read_only` + `read_only` policy | `git: read_only` |
| `network: destructive` + `read_only` policy | `network: read_only` |

If an old profile used `side_effect_policy: allow`, keep the family ceilings and
drop the field.

If an old profile used `side_effect_policy: approval_required`, do not encode
that in tela. Keep the family ceilings unchanged and move approval behavior to
anima runtime controls.

### tool_overrides Interaction with Capping

During migration, `tool_overrides` must preserve the target invariant:
no override may exceed the family capability ceiling.

If a legacy profile would produce an `allow` override above the migrated family
ceiling, migration must do one of:

- downgrade the override to fit the family ceiling
- remove the override
- fail migration with an explicit operator-facing error

Migration tooling SHOULD warn whenever an override becomes incompatible with the
post-migration family ceiling.

## Enforcement Model After Migration

Initialize-time preconditions (unchanged):
- Token validation (token mode only)
- Profile lookup and binding

Per-call enforcement chain (3 steps):

1. **Family admission** — is the tool's family present in `capabilities`?
2. **Tool override** — does a per-tool deny/allow override apply?
3. **Posture ceiling** — `tool_posture <= capabilities[family]`?

There is no separate side-effect check in the target design.

## Code Changes Required

| File | Change |
|------|--------|
| `src/tela/core/models.py` | Remove `SideEffectPolicy` enum. Remove `side_effect_policy` field from `ProfileConfig`. Rename `tools` field to `capabilities`. |
| `src/tela/core/enforcement.py` | Remove `check_side_effect()`. Update `enforce()` to skip side-effect step. Update all functions reading `profile.tools` to `profile.capabilities`. Update all doctests. |
| `src/tela/core/config.py` | `parse_config()` accepts both `tools:` and `capabilities:` in YAML. Apply ceiling capping when `side_effect_policy` is present. Emit deprecation warning for `tools:` key. |
| `src/tela/core/catalog.py` | Update all 7 builtin profiles: rename `tools` to `capabilities`, remove `side_effect_policy`. Apply read_only capping to `read_only` and `fetch_external` profiles. |
| `src/tela/shell/upstream.py` | `handle_profiles_list()` emits `capabilities` (and `tools` during transition). |
| `src/tela/shell/upstream_utils.py` | `filter_tools_for_profile()` and `enforce_tool_call()` read `capabilities`. |
| `docs/DESIGN.md` | Update enforcement chain (17+ locations), profile examples. |
| `docs/INTERFACES.md` | Update profile schema (7 locations), `tela.profiles` response format. |
| `docs/USAGE.md` | Update profile examples (5 locations). |
| `README.md` | Update any profile examples. |
| `tela.yaml.example` | Update all profile examples (10 locations). |
| `tela.yaml` | Update local test config. |
| `src/tela/core/catalog.py` | All 7 builtin profiles. |

## Dual-Format Support (Migration Period)

During migration:

1. Config parser accepts `capabilities:` (canonical) OR `tools:` (deprecated),
   not both simultaneously in the same profile.
2. Presence of `tools:` key emits a deprecation warning to stderr.
3. If `side_effect_policy` is present alongside `tools:`, ceiling capping is
   applied per the mapping rule above, then `side_effect_policy` is stripped.
4. If `side_effect_policy` is present alongside `capabilities:`, it is an error
   (new format must not carry the old policy field).
5. `tela.profiles` API response emits both `"tools"` and `"capabilities"` keys
   during migration. After cleanup, only `"capabilities"` is emitted.
6. Normalized internal representation always uses `capabilities`.

## Rollout Recommendation

1. Support both `tools` and `capabilities` during migration.
2. Normalize emitted/inspected profiles to `capabilities`.
3. Translate `side_effect_policy: read_only` into capped family ceilings.
4. Deprecate and remove `side_effect_policy` from profile schema and docs.
5. After one release cycle, remove `tools:` key support entirely.
