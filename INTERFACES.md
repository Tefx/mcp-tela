# tela -- Interface Specification

## A. CLI Interface

All commands support `--json` for machine-readable output.

### `tela start [--config path] [--port port]`

Start the MCP gateway. Reads configuration from `tela.yaml` (or the path
given by `--config`). Runs as an MCP stdio server by default. When `--port`
is provided, additionally listens on SSE at that port.

### `tela status [--json]`

Print gateway status: uptime, connected downstream servers, active
connections, profile count.

### `tela profiles [--json]`

List all configured profiles with their tool glob patterns and current
resolved tool counts.

### `tela connections [--json]`

List active upstream connections: connection id, bound profile, connected
since, tool call count.

### `tela audit [--json] [--since time] [--limit n]`

Query the audit log. `--since` accepts ISO-8601 timestamps or relative
durations (e.g. `1h`, `30m`). `--limit` caps the number of returned
entries (default 100).

---

## B. Configuration (tela.yaml)

```yaml
servers:
  # stdio downstream -- tela spawns and manages the process
  fs:
    command: "mcp-fs"
    args: ["--root", "/workspace"]

  # SSE downstream -- tela connects as a client
  github:
    url: "sse://localhost:3001"

profiles:
  # Each profile is a list of tool glob patterns.
  # Patterns are matched against "<server_name>.<tool_name>".
  coder:
    - "fs.*"
    - "sandbox.run"
    - "github.read_pr"
  reviewer:
    - "fs.read_file"
    - "github.*"

auth:
  mode: token          # token | open
  secret: "${TELA_SECRET}"   # HMAC secret for capability token validation

audit:
  level: L2            # L1 | L2 | L3
  output: "${TELA_STATE}/audit.jsonl"
```

### Audit Levels

| Level | Recorded |
|-------|----------|
| L1    | tool name, result status, latency |
| L2    | L1 + parameter hash |
| L3    | L2 + full request/response content (opt-in via `--unsafe`) |

### Environment Variable Expansion

All string values in the config support `${VAR}` expansion from the
process environment. Unset variables cause a startup error.

---

## C. MCP Server Interface (upstream)

tela exposes a standard MCP server to upstream clients (agents).

### tools/list

Returns the filtered tool list for the bound profile. Each tool retains
its original JSON Schema from the downstream server.

### tools/call

Forwards the tool call to the appropriate downstream server. If the
requested tool is not in the bound profile, the call is rejected with
`AUTHZ_DENY`.

### notifications/tools/list_changed

Emitted when the available tool set changes. Payload:

```json
{
  "profile_name": "coder",
  "token_id": "tok_abc123",
  "tools_digest": "sha256:..."
}
```

Triggers:
- Profile switch on an existing connection
- Downstream server connects/disconnects (changing the resolved tool set)
- Configuration hot-reload changes profile definitions

---

## D. Connection Flow (token mode)

```
Client                          tela                      Downstream
  |                              |                            |
  |-- connect(token) ----------->|                            |
  |                              |-- validate(HMAC + expiry)  |
  |                              |-- bind profile from        |
  |                              |   token.tools_profile      |
  |<-- tools/list (filtered) ----|                            |
  |                              |                            |
  |-- tools/call(tool, args) --->|                            |
  |                              |-- check profile allows --->|
  |                              |-- forward(tool, args') --->|
  |                              |<-- result -----------------|
  |<-- result -------------------|                            |
```

1. Client sends MCP `initialize` with the capability token in
   `clientInfo.capability_token` (the full CapabilityToken JSON object).
2. tela validates the token: HMAC signature check, expiry check.
3. tela reads `token.tools_profile` and binds the corresponding profile.
4. Client receives `tools/list` containing only tools allowed by the profile.
5. On `tools/call`: tela checks the tool is in the bound profile, forwards
   to the downstream server, returns the result. The token is NOT
   re-sent on each call — it is a per-connection credential.

### Token Structure

```json
{
  "token_id": "tok_a1b2c3d4e5f6",
  "tools_profile": "coder",
  "persona_ref": "code-reviewer@a1b2c3d4",
  "instance_id": "inst_abc123",
  "budget": 100000,
  "max_depth": 3,
  "issued_at": "2026-02-28T10:00:00Z",
  "expires_at": "2026-02-28T11:00:00Z",
  "signature": "<HMAC-SHA256>"
}
```

Required fields: `token_id`, `tools_profile`, `issued_at`, `expires_at`,
`signature`. Optional fields: `persona_ref`, `instance_id`, `budget`,
`max_depth`. tela currently uses `tools_profile` (for profile binding),
`issued_at`/`expires_at` (for expiry check), and `signature` (for HMAC
validation). Optional fields are recorded in the audit log but not
enforced by tela in v0.1.

---

## E. Open Mode (standalone, no token)

When `auth.mode` is set to `open`:

- No capability token is required on connect.
- The profile is determined by the `profile` field in connection metadata,
  or falls back to the first profile defined in configuration.
- Suitable for standalone use with Claude Code, Cursor, or any MCP client
  that does not supply tokens.

### Comma-Separated Profile Values

In open mode, `profile` metadata may contain comma-separated values (e.g., `"filesystem,shell"`).
tela resolves this by merging the tool patterns from all named profiles:

```yaml
profiles:
  filesystem:
    - "fs.*"
  shell:
    - "shell.*"
```

When `profile: "filesystem,shell"`, the bound profile becomes the union of both:
`["fs.*", "shell.*"]`.

**Backward Compatibility:** Single profile names (non-comma) continue to work unchanged.
tela gracefully handles both formats without requiring configuration changes.

---

## F. _meta Handling

MCP clients (agents) may include a `_meta` field in tool call arguments
(e.g. conversation ID, request ID). tela handles this transparently:

1. **Audit**: `_meta` fields are recorded in the audit log entry for the
   tool call.
2. **Forwarding**: Before forwarding to a downstream server, tela checks
   whether the downstream tool's input schema includes `_meta`. If it does
   not, tela strips `_meta` from the arguments before forwarding.
3. **Transparency**: Agents do not need to know whether a downstream server
   supports `_meta`. tela handles it silently.

---

## G. Error Codes

| Code | Meaning |
|------|---------|
| `AUTHZ_DENY` | Tool call rejected -- not in bound profile |
| `PROFILE_NOT_FOUND` | Requested profile does not exist in configuration |
| `TOKEN_INVALID` | Token HMAC signature verification failed |
| `TOKEN_EXPIRED` | Token `expires_at` is in the past |
| `DOWNSTREAM_ERROR` | Downstream server returned an error |
| `DOWNSTREAM_UNAVAILABLE` | Downstream server is not connected / not responding |

Errors are returned as standard MCP error responses with the code in the
`error.code` field and a human-readable message in `error.message`.

---

## H. State Directory

Default: `~/.tela/` (override with `$TELA_STATE`).

Contents:

| Path | Purpose |
|------|---------|
| `audit.jsonl` | Append-only audit log (configurable via `audit.output`) |

tela is stateless beyond the audit log. Configuration is the source of
truth. The gateway can be restarted at any time without data loss.
