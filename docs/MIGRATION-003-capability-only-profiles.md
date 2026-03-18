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
