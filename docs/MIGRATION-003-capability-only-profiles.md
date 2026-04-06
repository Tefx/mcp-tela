# Migration: Capability-Only Tela Profiles

## Goal

Move tela profiles from mixed posture + side-effect configuration to a single
capability map.

## Legacy Model

```yaml
profiles:
  reviewer:
    tools:
      filesystem: read_write
      git: read_only
    side_effect_policy: read_only
```

## Target Model

```yaml
profiles:
  reviewer:
    capabilities:
      filesystem: read_only
      git: read_only
```

Profiles in the target model express capability ceilings only.

## Field Replacement

| Legacy field | Target field / behavior |
|--------------|-------------------------|
| `tools` | `capabilities` |
| `side_effect_policy: read_only` | cap family ceilings to `read_only` |
| `side_effect_policy: allow` | drop the field; keep family ceilings |
| `side_effect_policy: approval_required` | remove from tela; runtime approval belongs to anima |

## Override Rule

Migration must preserve the target invariant:

```text
no tool override may exceed capabilities[family]
```

If a legacy override would exceed the migrated family ceiling, migration must:
- downgrade it
- remove it
- or reject the migration with an explicit error

## Code Changes

The following code changes are required to implement capability-only profiles:

### 1. ProfileConfig Model (`src/tela/core/models.py`)

- Rename `ProfileConfig.tools` field to `capabilities`
- Remove `side_effect_policy` field
- Keep `tool_overrides` field unchanged

### 2. Profile Catalog (`src/tela/core/catalog.py`)

- Rename `tools` to `capabilities` in all built-in profiles
- Remove `side_effect_policy` from all built-in profiles
- Apply capping rules: `read_only` profiles get `read_only` ceilings on all families

### 3. Config Parser (`src/tela/core/config.py`)

- Parse `capabilities` key instead of `tools`
- Handle backward compatibility: accept `tools` as alias for `capabilities` during migration
- Remove `side_effect_policy` parsing

### 4. Enforcement Layer (`src/tela/core/enforcement.py`)

- Simplify enforcement chain from 4 steps to 3 steps:
  1. Family admission
  2. Tool override check
  3. Posture ceiling comparison
- Remove `check_side_effect` function and `side_effect_policy` handling

## Dual-Format Support

During the migration window, tela supports dual-format configuration:

### Input Backward Compatibility

The config parser accepts both legacy and new formats:

```yaml
# Legacy format (accepted during migration)
profiles:
  reviewer:
    tools:
      filesystem: read_write
    side_effect_policy: read_only

# New format (canonical)
profiles:
  reviewer:
    capabilities:
      filesystem: read_only
```

When parsing legacy format:
1. `tools` is treated as alias for `capabilities`
2. `side_effect_policy: read_only` triggers ceiling capping on all families
3. `side_effect_policy: allow` is silently dropped
4. `side_effect_policy: approval_required` causes parse error with clear message

### Output Format

Canonical `tela.profiles` MCP response shape:

```json
{
  "profile_id": "developer",
  "profile_name": "Developer",
  "capabilities": { "filesystem": "read_only" }
}
```

`profile_id` is the stable canonical identity. `profile_name`, when present, is
display-only local vocabulary. Legacy `tools` output is not canonical.

## tool_overrides Section

The `tool_overrides` field in profiles remains unchanged in syntax but changes in semantics:

### Syntax (Unchanged)

```yaml
profiles:
  developer:
    capabilities:
      filesystem: read_write
    tool_overrides:
      filesystem:
        overrides:
          delete_file: deny
          dangerous_operation: deny
```

### Semantics Change

In the target model, tool overrides cannot elevate access beyond the family ceiling:

**Legacy behavior (removed):**
- `tool_overrides` could allow tools even if `side_effect_policy: read_only` was set
- This created confusing dual-layer authorization

**Target behavior:**
- `tool_overrides` can only restrict (deny) or selectively allow within the family ceiling
- An override to `allow` a tool is only valid if `tool_posture <= capabilities[family]`
- An override attempting to grant `read_write` access when family ceiling is `read_only` must be rejected

### Migration Rule for tool_overrides

When migrating profiles with `tool_overrides`:

1. Calculate the effective family ceiling after `side_effect_policy` capping
2. For each override, check if the implied posture exceeds the family ceiling
3. If exceeded, apply one of:
   - **Downgrade**: Change override to match family ceiling posture
   - **Remove**: Delete the override entry
   - **Reject**: Fail migration with explicit error message

Example:

```yaml
# Legacy profile
profiles:
  reviewer:
    tools:
      filesystem: read_write
    side_effect_policy: read_only
    tool_overrides:
      filesystem:
        overrides:
          edit_file: allow  # implies read_write posture

# After migration (downgrade strategy)
profiles:
  reviewer:
    capabilities:
      filesystem: read_only  # capped from read_write
    tool_overrides:
      filesystem:
        overrides:
          edit_file: deny  # downgraded: cannot allow read_write in read_only family
```

## Target Enforcement Model

After migration, tela authorization is:

1. family admission
2. tool override application within the family ceiling
3. posture comparison against `capabilities[family]`

There is no side-effect policy layer in the target gateway model.

## Final State

The target tela profile model is capability-only.
Legacy `tools` and `side_effect_policy` are historical input shapes, not active
architecture.
