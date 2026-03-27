## [Design] Taxonomy decision

### Decision basis

- [Proven] The current product-spec docs consistently describe `tela.status`, `tela.connections`, `tela.audit`, and `tela.profiles` as upstream MCP introspection tools, not merely CLI/operator-only surfaces.
  - `docs/INTERFACES.md:165-177` names an upstream MCP surface with introspection tools and lists all four `tela.*` names in the tool table.
  - `docs/DESIGN.md:114-119` says the server exposes MCP tools for runtime introspection and again lists all four names.
  - `README.md:145-155` describes the same four names as MCP tools.
- [Proven] The audit artifact shows current runtime drift from that intended spec: only `tela.profiles` is presently source-verified, and it is currently implemented as an MCP resource rather than an MCP tool; `tela.status`, `tela.connections`, and `tela.audit` are not source-verified MCP surfaces today (`evidence/surface_audit_actual_surface.md:9-25,41-55`).
- [Proven] The same authoritative docs explicitly use `tela_admin` as the family/capability label for these built-ins (`docs/INTERFACES.md:69-80,170-177`; `docs/DESIGN.md:116-119`; `README.md:147-155`).

### Authoritative intended taxonomy

1. `tela.profiles` intended kind: built-in MCP introspection tool.
   - Rationale: the normative interface spec places it in the introspection tool table, and no product-spec doc presents it as a resource.
   - Consequence: current resource implementation is runtime drift that must be reconciled.

2. `tela.status` intended kind: built-in MCP introspection tool, also exposed via operator HTTP/CLI surfaces.
   - Rationale: spec explicitly lists it as an upstream MCP introspection tool while also documenting `GET /status` and `tela status`.

3. `tela.connections` intended kind: built-in MCP introspection tool, with CLI/operator access allowed as a secondary surface.
   - Rationale: spec explicitly lists it as an upstream MCP introspection tool; CLI existence does not replace that intent.

4. `tela.audit` intended kind: built-in MCP introspection tool, with CLI/operator access allowed as a secondary surface.
   - Rationale: spec explicitly lists it as an upstream MCP introspection tool; CLI existence does not replace that intent.

5. Capability wording: `tela_admin` is explicitly adopted by spec for the introspection family and is allowed in docs/runtime for this surface set.
   - Rationale: the docs are not silent; they repeatedly assign these four surfaces to `tela_admin`.
   - Constraint: this is a spec-adoption decision, not a claim that current source already enforces `tela_admin`.

### Runtime work implication

- [Proven] Runtime work is required.
- Why:
  - `tela.status`, `tela.connections`, and `tela.audit` are intended MCP built-ins by spec but are not source-verified as MCP tool/resource registrations in the audit artifact.
  - `tela.profiles` is intended as an MCP introspection tool by spec but is currently source-verified as an MCP resource.
  - Therefore runtime, tests, and docs must converge toward the chosen spec taxonomy rather than the current mixed implementation.

### Explicit downstream planning constraint

- Until runtime alignment lands, downstream steps must distinguish:
  - **intended taxonomy**: all four `tela.*` surfaces are built-in MCP introspection tools under `tela_admin`
  - **actual current runtime**: only `tela.profiles` is source-verified today, and as a resource

### Blocking spec question(s)

- None. The current authoritative product docs are sufficient to choose the intended taxonomy and capability wording.

### Certainty

- Surface-intent decision: [Proven]
- Capability wording decision: [Proven]
- Runtime-work-required decision: [Proven]
