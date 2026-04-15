## [Design] Taxonomy decision

### Decision basis

- [Proven] The current implementation (`evidence/surface_audit_actual_surface.md`) confirms:
  - `tela_list_providers` and `tela_list_profiles` are registered as MCP builtin tools
  - `tela.status`, `tela.connections`, and `tela.audit` exist as CLI/HTTP operator surfaces only
  - No MCP resources in the `tela.*` namespace (former `tela.profiles` resource removed)

- [Proven] The authoritative confirmed contract (`docs/CONFIRMED-SURFACE-CONTRACT.md`) specifies:
  - `tela_list_profiles` exact kind: `tool` (MCP builtin tool callable via `tools/call`)
  - `tela_list_providers` exact kind: `tool` (MCP builtin tool callable via `tools/call`)
  - `tela.status`, `tela.connections`, `tela.audit` exact kind: `absent` (not MCP tools/resources)
  - `tela.profiles` exact kind: `absent` (former resource removed, replaced by `tela_list_profiles`)

- [Proven] Capability control is generic family/posture-based in `src/tela/core/models.py` and `src/tela/core/catalog.py`.
  - `tela_admin` is not a runtime-enforced capability string in the current source.

### Confirmed taxonomy

1. `tela_list_profiles` confirmed kind: MCP builtin tool (callable via tools/call)
   - Access: MCP `tools/call` with `{}` input
   - Returns list of `{profile_id, capabilities, default}` entries
   - Former `tela.profiles` resource has been removed and replaced by this builtin tool

2. `tela.status` confirmed kind: operator-only surface
   - Access: CLI `tela status` or HTTP `GET /status`
   - Not an MCP tool or resource

3. `tela.connections` confirmed kind: operator-only surface
   - Access: CLI `tela connections` (data via `GET /status`)
   - Not an MCP tool or resource

4. `tela.audit` confirmed kind: operator-only surface
   - Access: CLI `tela audit` (data via `GET /status`)
   - Not an MCP tool or resource

5. Capability wording: No `tela_admin` family at runtime
   - Current source uses generic family/posture-based capability control
   - `tela_admin` appears in docs/spec history but is not implementation-verified

### Runtime status

- [Proven] No additional runtime work required for basic taxonomy alignment.
- Current implementation matches the confirmed contract for all four surfaces.
- Docs/spec drift exists in historical documents and has been remediated in current step.

### Explicit alignment achieved

- This decision artifact is updated to reflect the confirmed contract
- `docs/DESIGN.md` updated to remove contradictory MCP tool claims
- `docs/DESIGN.md` instruction section updated to reflect tested ordering behavior

### Blocking spec question(s)

- None. Confirmed contract is sufficient for taxonomy stabilization.

### Certainty

- Surface taxonomy decision: [Proven]
- Capability wording decision: [Proven]
- Runtime alignment status: [Complete]
