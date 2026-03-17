# ADR-003: Gateway Profiles Express Capability Only

## Status

Accepted

## Context

tela is the concrete MCP gateway. Its job is to authorize tool visibility and
tool calls against concrete downstream providers.

The previous design added `side_effect_policy` to profiles. That overloaded
gateway profiles with a second mutability concept even though profiles already
contained per-family posture ceilings.

## Decision

tela profiles will express **capability only**.

Target profile shape:

```yaml
profiles:
  developer:
    capabilities:
      filesystem: read_write
      git: read_only
    tool_overrides:
      filesystem:
        delete_file: deny
```

The core authorization primitive becomes:

```text
tool_posture <= profile.capabilities[tool.family]
```

Family admission and tool overrides remain as pre-checks. The per-call
enforcement chain after migration has 3 steps:

1. family admission — is the tool's family in the profile?
2. tool override — does a per-tool deny/allow override apply?
3. posture ceiling — `tool_posture <= capabilities[family]`?

No tool override may elevate access beyond `capabilities[family]`.

Token validation and profile lookup remain initialize-time preconditions,
not per-call enforcement steps.

The `tools` key in profile configuration is renamed to `capabilities`.
The `ProfileConfig.tools` field in code is renamed to `capabilities`.
`tool_overrides` remains optional and unchanged.

### `tela.profiles` API Response

The `tela.profiles` MCP response key changes from `"tools"` to
`"capabilities"`. The `"side_effect_policy"` field is dropped from the
response. During migration, both `"tools"` and `"capabilities"` are emitted
for backward compatibility. After migration, only `"capabilities"` is emitted.

Approval and runtime read-only mode are explicitly out of scope for tela.

## Rationale

- gateway authorization should be a concrete capability ceiling, not a workflow
  policy surface
- `approval_required` cannot be implemented statelessly at the gateway
- a second mutability knob duplicates the posture lattice and confuses operators

## Consequences

Positive:

- profile mental model becomes simpler
- enforcement chain becomes easier to explain
- opifex profile matching only compares one capability map

Negative:

- existing docs and config examples using `side_effect_policy` need migration
- any current reliance on explicit profile-level read-only policy must be
  represented through posture ceilings instead
- downstream consumers of `tela.profiles` API must update from `"tools"` to
  `"capabilities"` key (nervus, opifex)

## Non-Goals

- tela does not become responsible for human approval workflows
- tela does not interpret persona identity or job runtime policy
