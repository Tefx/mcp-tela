## [Design] Taxonomy decision

### Decision basis

- [Proven] The current implementation (`evidence/surface_audit_actual_surface.md`) confirms:
  - Only `tela.profiles` is registered as an MCP resource (not an MCP tool)
  - `tela.status`, `tela.connections`, and `tela.audit` exist as CLI/HTTP operator surfaces only
  - No `tela.*` MCP tools are currently registered

- [Proven] The authoritative confirmed contract (`docs/CONFIRMED-SURFACE-CONTRACT.md`) specifies:
  - `tela.profiles` exact kind: `resource` (MCP resource read via `tela://profiles`)
  - `tela.status`, `tela.connections`, `tela.audit` exact kind: `absent` (not MCP tools/resources)

- [Proven] Capability control is generic family/posture-based in `src/tela/core/models.py` and `src/tela/core/catalog.py`.
  - `tela_admin` is not a runtime-enforced capability string in the current source.

### Confirmed taxonomy

1. `tela.profiles` confirmed kind: MCP resource (read-only, not callable tool)
   - Access: MCP resource read of `tela://profiles`
   - # SPEC QUESTION: Future work may consider promoting to MCP tool if needed; currently resource-only

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
