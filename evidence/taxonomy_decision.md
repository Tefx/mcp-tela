## [Design] Taxonomy decision

### Decision basis

- Built-in MCP tools are exactly `tela_list_providers` and `tela_list_profiles`
- Built-in MCP resources are absent
- Operator surfaces remain `tela profiles`, `tela status`, `tela connections`,
  and `tela audit`

### Confirmed taxonomy

1. `tela_list_profiles` is the canonical built-in MCP profile-list surface
2. `tela_list_providers` is the canonical built-in MCP provider-list surface
3. operator surfaces stay CLI/HTTP only and are not relabeled as MCP tools

### Runtime status

- Runtime, docs, and tests now use the same surface taxonomy
- No additional surface-classification debt remains in this slice

### Certainty

- Surface taxonomy decision: [Proven]
- Runtime alignment status: [Complete]
