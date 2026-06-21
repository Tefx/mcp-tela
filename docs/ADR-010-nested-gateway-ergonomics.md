# ADR-010: Nested Tela Gateway Ergonomics

## Status
Accepted

## Context

Tela can be used as a downstream MCP server of another Tela gateway. This is useful for split-runtime deployments, such as a primary gateway in a VM delegating host-only tools to a host-side gateway.

The existing `tool_prefix` mechanism makes nested calls work, but child Tela built-ins such as `tela_list_providers` and `tela_list_profiles` can appear on the parent surface as `host_tela_list_providers` and `host_tela_list_profiles`. When a nested child is configured without `tool_prefix`, startup fails closed through a generic reserved-namespace error.

## Decision Drivers

- Keep the parent gateway as the single visible control surface by default when the user explicitly declares a nested gateway.
- Preserve existing behavior unless configuration requests filtering.
- Keep tool-surface behavior explainable from config, not silent runtime guessing.
- Reuse raw downstream tool-name semantics already used by `tool_overrides`.
- Preserve canonical shared names and payloads for `tela_list_profiles` and `tela_list_providers`.

## Options Considered

### Option A: Generic `exclude_tools`

- **Mechanism**: Per-server `exclude_tools` matches raw downstream tool names before prefixing and registration.
- **Pros**: General, simple, predictable, useful beyond nested Tela.
- **Cons**: Nested Tela users must know which child built-ins to exclude.
- **Fails if**: Nested deployments become common enough that requiring explicit built-in names is too error-prone.

### Option B: Silent nested-Tela auto-detection and auto-hide

- **Mechanism**: If the downstream exposes Tela built-ins, hide them automatically.
- **Pros**: Very convenient for the common nested-Tela case.
- **Cons**: Public tool surface changes based on runtime heuristics that are not visible in config; harder to audit, test, and debug.
- **Fails if**: A downstream intentionally exposes Tela-like tools, detection changes after upgrades, or users need to understand why a tool disappeared.

### Option C: Explicit `nested_gateway: true`

- **Mechanism**: User declares the downstream is another Tela gateway. Tela requires `tool_prefix` and adds child Tela built-ins to the effective exclude set.
- **Pros**: Ergonomic for nested Tela while keeping behavior explicit and testable.
- **Cons**: Adds one Tela-specific config field.
- **Fails if**: The project wants to remain purely generic with no Tela-specific downstream semantics.

## Decision

Use **Option A + Option C** together.

`exclude_tools` is the generic primitive. It matches raw downstream tool names before `tool_prefix` is applied and before the tools enter conflict detection, registry state, profile enforcement, provider metadata, or call routing.

`nested_gateway: true` is the explicit convenience mode for downstream Tela gateways. It requires `tool_prefix` and automatically excludes these raw child tool names:

- `tela_list_providers`
- `tela_list_profiles`

Do **not** silently auto-hide child Tela built-ins based only on detection. Detection may improve diagnostics and suggestions, especially for missing `tool_prefix`, but must not mutate the public tool surface unless `exclude_tools` or `nested_gateway: true` is configured.

## Consequences

- Existing configurations remain compatible when `exclude_tools` and `nested_gateway` are omitted.
- Nested Tela deployments get a concise explicit config.
- Parent Tela built-ins remain visible and gateway-owned.
- Child Tela built-ins are hidden only when configured through `exclude_tools` or `nested_gateway: true`.
- `tela_list_providers.tool_count` and `tool_names` report the filtered exposed tool surface.
- Missing-prefix errors for deterministic nested-Tela triggers should use an actionable `NESTED_TELA_PREFIX_REQUIRED` diagnostic.

## Acceptance Criteria

- `exclude_tools` matches raw downstream names, not prefixed exposed names.
- `nested_gateway: true` requires `tool_prefix`.
- `nested_gateway: true` hides child `tela_list_providers` and `tela_list_profiles` while preserving parent built-ins.
- Omitted `nested_gateway` preserves current behavior, including prefixed child built-ins when a prefix is configured.
- A downstream that exposes `tela_list_providers` or `tela_list_profiles` with omitted or empty `tool_prefix` fails closed with deterministic, actionable `NESTED_TELA_PREFIX_REQUIRED`.
- Reload/re-enumeration treats changes to `exclude_tools` or `nested_gateway` as tool-surface changes.
