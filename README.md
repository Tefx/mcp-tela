# tela

MCP aggregation gateway. Connects downstream MCP servers and exposes them as a
single upstream MCP endpoint with profile-based tool filtering and policy
enforcement.

## Features

- Aggregate multiple MCP servers behind one endpoint
- Server-is-family tool mapping with per-tool overrides
- Per-tool-family posture filtering and side-effect policy enforcement
- Capability token authentication (HMAC + TTL, dual-key rotation) or open mode
- Structured audit logging (L1/L2/L3)
- Hot reload of downstream tool lists and configuration
- Standard MCP stdio transport

## Quick start

```bash
pip install -e .
tela start --config tela.yaml
```

## Configuration

```yaml
servers:
  fs:
    command: "mcp-fs"
    args: ["--root", "/workspace"]
  my-custom-fs:
    command: "my-fs-server"
    family: filesystem            # explicit family override
  github:
    url: "http://localhost:3001/sse"

profiles:
  coder:
    tools:
      filesystem: read_write
      shell: read_only
      git: read_only
    tool_overrides:
      filesystem:
        delete_file: deny
    side_effect_policy: allow
  reviewer:
    tools:
      filesystem: read_only
      git: read_only
    side_effect_policy: read_only

auth:
  mode: open        # open | token
  secrets:
    - "${TELA_SECRET}"            # primary
    - "${TELA_SECRET_PREVIOUS}"   # secondary (optional, for rotation)

audit:
  level: L2         # L1 | L2 | L3
  output: "${TELA_STATE}/audit.jsonl"
```

## Usage with Claude Code

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "tela": {
      "command": "tela",
      "args": ["start", "--config", "tela.yaml"]
    }
  }
}
```

## CLI

```
tela start [--config path] [--port port]   Start the gateway
tela status [--json]                        Show gateway status
tela profiles [--json]                      List configured profiles
tela connections [--json]                   List active connections
tela audit [--json] [--since T] [--limit N] Query audit log
```

## Testing

```bash
uv run pytest -q
uv run pytest --doctest-modules src/tela/
uv run invar guard --all
uv run pytest tests/repro/ -q
```

`tests/repro/` is the executable regression suite. If a legacy workflow refers
to `tests/blind/`, use `tests/repro/` as the canonical fallback path.

## License

MIT
