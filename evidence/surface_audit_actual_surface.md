## Surface audit evidence

### Scope

Review-only audit of current tela agent-facing surfaces in the worktree.

### Surface matrix

| Surface | MCP resource | MCP tool | CLI command | HTTP endpoint | Present today? |
|---|---|---|---|---|---|
| `tela_list_providers` | No | Yes | No | No | Verified present |
| `tela_list_profiles` | No | Yes | No | No | Verified present |
| `tela status` | No | No | Yes | `GET /status` | Operator-only |
| `tela connections` | No | No | Yes | via `GET /status` data | Operator-only |
| `tela audit` | No | No | Yes | via `GET /status` data | Operator-only |

### MCP resources verified

- none

### MCP tools verified

- `tela_list_providers`
- `tela_list_profiles`

### Operator surfaces verified

- `tela profiles`
- `tela status`
- `tela connections`
- `tela audit`
- `GET /health`
- `GET /status`
- `POST /connect`
- `POST /disconnect`
- `POST /mcp`
