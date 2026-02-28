# tela

MCP aggregation gateway. Connects N downstream MCP servers (stdio and SSE)
and exposes them as a single upstream MCP endpoint with role-based profile
filtering.

## Features

- Aggregate multiple MCP servers behind one endpoint
- Profile-based tool filtering (agents see only what they need)
- Capability token authentication (HMAC + TTL) or open mode
- Structured audit logging (L1/L2/L3)
- Hot-reloadable YAML configuration
- Standard MCP stdio transport

## Quick start

```bash
pip install mcp-tela
tela start --config tela.yaml
```

## Configuration

```yaml
servers:
  fs:
    command: "mcp-fs"
    args: ["--root", "/workspace"]
  github:
    url: "sse://localhost:3001"

profiles:
  coder:
    - "fs.*"
    - "sandbox.run"
    - "github.read_pr"
  reviewer:
    - "fs.read_file"
    - "github.*"

auth:
  mode: open        # open | token
  secret: "${TELA_SECRET}"

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

## License

MIT
