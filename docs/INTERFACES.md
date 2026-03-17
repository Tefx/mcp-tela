# tela -- Interface Specification

For an operator-oriented walkthrough, including stdio vs SSE deployment advice,
client integration patterns, and worked examples, see `docs/USAGE.md`. For a
fully commented configuration template, see `tela.yaml.example`.

## A. CLI Interface

All commands support `--json` for machine-readable output.

### `tela start [--config path] [--port port] [--default-profile name]`

Start the MCP gateway. Reads configuration from `tela.yaml` (or the path
given by `--config`). Runs as an MCP stdio server by default. When `--port`
is provided, additionally listens on SSE at that port.

### `tela status [--json]`

Print gateway status: uptime, connected downstream servers, active
connections, profile count.

### `tela profiles [--json]`

List all configured profiles with their declared tool families/postures and current
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
    url: "http://localhost:3001/sse"

profiles:
  coder:
    tools:
      filesystem: read_write
      shell: read_only
      git: read_only
    side_effect_policy: allow        # allow | read_only (approval gating is anima's job)
  reviewer:
    tools:
      filesystem: read_only
      git: read_only
    side_effect_policy: read_only

auth:
  mode: token          # token | open
  secrets:             # dual-key for zero-downtime rotation
    - "${TELA_SECRET}"              # primary (sign + validate)
    - "${TELA_SECRET_PREVIOUS}"     # secondary (validate only, optional)

audit:
  level: L2            # L1 | L2 | L3
  output: "${TELA_STATE}/audit.jsonl"
```

### Server Configuration: Family Mapping and Tool Overrides

The server name in `tela.yaml` doubles as the **tool family** name by default.
This is the "server-is-family" convention: every tool exposed by a server
belongs to that server's family unless explicitly overridden.

```yaml
servers:
  # server name = tool family (default)
  fs:
    command: "mcp-fs"
    args: ["--root", "/workspace"]

  # explicit family override
  my-custom-fs:
    command: "my-fs-server"
    family: filesystem

  # cross-family server with tool_overrides
  devtools:
    command: "mcp-devtools"
    tool_overrides:
      run_shell:
        family: shell
      git_status:
        family: git

  # SSE downstream
  github:
    url: "http://localhost:3001/sse"
```

Rules:

- **Server-is-family default**: the server name is the tool family name
  unless overridden with `family:` on the server entry. All tools from
  a server belong to that server's family.
- **`family:` override**: when the server name does not match the desired
  family, set `family:` explicitly (e.g. `my-custom-fs` maps to family
  `filesystem`).
- **`tool_overrides` (server level)**: for cross-family servers that expose
  tools belonging to multiple families, individual tools can be reassigned
  to a different family via `tool_overrides`.
- **Tool name conflicts**: if two downstream servers expose tools with the
  same name, tela reports the conflict and exits at startup. No implicit
  resolution (first-wins, auto-prefix) is attempted. The user must resolve
  conflicts explicitly via `tool_overrides` or by renaming at the downstream
  server level.
- **`default_posture`** (per server): the posture assigned to tools from
  this server when no other classification is available. Defaults to `none`
  (deny). Can be set explicitly, e.g. `default_posture: read_only`.

### Profile Configuration: Tool Overrides

Profiles support optional `tool_overrides` for per-tool fine-grained control
beyond family-level postures:

```yaml
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
```

Profile-level `tool_overrides` let you deny (or explicitly allow) specific
tools within a family, overriding the family posture ceiling. In the example
above, the `coder` profile grants `read_write` to the `filesystem` family
but explicitly denies the `delete_file` tool.

### Prebuilt Profile Catalog (v1)

tela may ship a small prebuilt profile catalog for common operator choices.
These names describe behavioral boundaries, not human roles.

| Profile | Intent |
|---------|--------|
| `read_only` | Read local resources only. No mutation, sending, or execution. |
| `fetch_external` | Read local resources and fetch external information without sending externally. |
| `modify_local` | Modify local content or structure without broad external or privileged execution. |
| `send_external` | Send or submit content to external systems without full privileged execution. |
| `orchestrate` | Coordinate multi-step flows across tools and results without full privileged execution. |
| `execute_safe` | Execute ordinary non-privileged actions across local and remote boundaries. |
| `execute_full` | Execute high-risk or privileged actions. Never use as an implicit default. |

These shipped profiles are templates. Deployment-local configuration remains the
runtime source of truth.

#### Capability Comparison

Value definitions:

- `Yes`: core capability of this profile
- `No`: explicitly excluded
- `Optional`: may be included depending on deployment-specific tool configuration
- `Limited`: restricted subset only, not broad or unrestricted access

| Profile | Local read | External fetch | Local modify | External send | Coordination | Execution | Privileged execution |
|---------|------------|----------------|--------------|---------------|-------------------------|------------------|---------------------|
| `read_only` | Yes | No | No | No | No | No | No |
| `fetch_external` | Yes | Yes | No | No | No | No | No |
| `modify_local` | Yes | Optional | Yes | No | No | Limited | No |
| `send_external` | Yes | Yes | Limited | Yes | No | Limited | No |
| `orchestrate` | Yes | Yes | Limited | Limited | Yes | Limited | No |
| `execute_safe` | Yes | Yes | Yes | Limited | Yes | Yes | No |
| `execute_full` | Yes | Yes | Yes | Yes | Yes | Yes | Yes |

#### Boundary Notes

- `modify_local`: may change local content or local structure. It does not imply
  broad external submission or privileged execution.
- `orchestrate`: may sequence tools and combine intermediate results across a
  multi-step flow, but it does not by itself imply full privileged execution.
- `execute_safe`: may run ordinary bounded actions across local and remote
  surfaces, but excludes privileged or high-risk execution.

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

## C. Tool Posture Classification

Tool-level posture classification determines what a tool is allowed to do.
Three sources contribute, evaluated in strict priority order:

1. **`tool_overrides`** (in server config or profile config) -- explicit per-tool
   posture or deny/allow. Highest priority.
2. **MCP tool annotations** (`readOnlyHint`, `destructiveHint`) -- when the
   downstream server provides annotations on its tools, tela uses them for
   automatic classification.
3. **`default_posture`** (on the server config) -- fallback posture for tools
   with no other classification. Defaults to `none` (deny).

When no classification is available for a tool from any source, the tool is
treated as **unclassified**. Unclassified tools are denied by default and
logged as `TOOL_UNCLASSIFIED` in the audit log. This fail-closed behavior
ensures that new or unexpected tools cannot bypass policy silently.

---

## D. Enforcement Layers

CapabilityToken validation is a connection-bind operation, not a per-call one.
During MCP `initialize` / connection establishment, tela validates the token
signature, expiry, and `tools_profile`, then binds the resulting profile to the
connection context. Every `tools/call` request then passes through the
per-call enforcement chain (steps 3-6 of the 7-step model in DESIGN.md):

### 1. Family admission

Does the tool's family exist in the bound profile's `tools` map? If not,
deny with `AUTHZ_DENY`.

### 2. Tool override check

If the profile has a `tool_overrides` entry for this specific tool: apply it.
- `deny`: short-circuit the chain with DENY.
- `allow`: skip steps 3-4 (explicit allow bypasses posture and side-effect checks).
- no override: continue to step 3.

### 3. Posture check

If posture classification is available for the tool: is the tool's posture
<= the profile's posture ceiling for that family? If exceeded, deny with
`AUTHZ_DENY` (posture_exceeded).

If no classification is available: the tool is subject to the server's
`default_posture` (which defaults to `none` = deny).

### 4. Side-effect check

If the tool's effective posture > `read_only` and the profile's
`side_effect_policy` is `read_only`: deny with `AUTHZ_DENY`.

Note: `approval_required` does not exist in tela profiles. Approval gating
is enforced by anima before the call reaches tela.

The tool call is forwarded to the downstream server only if every layer above
permits it.

---

## E. Hot Reload

tela monitors downstream tool list changes at runtime and reloads
configuration without dropping active connections.

### Triggers

- Downstream server emits MCP `notifications/tools/list_changed`
- Downstream server reconnects after a disconnect
- `tela.yaml` configuration change (when config watch is enabled)

### Behavior

1. Re-enumerate the affected server's tool list
2. Re-assign families (server-is-family default + `tool_overrides`)
3. Re-run conflict detection against all other servers' current tool lists
4. **No conflict**: update the resolved tool set, emit
   `notifications/tools/list_changed` to all connected upstream clients
5. **Conflict detected**: reject the change -- keep the previous tool list
   for that server, write `TOOL_CONFLICT` to the audit log as a warning.
   Do not crash, do not disconnect.

### Invariants

- Active upstream connections are never interrupted by a hot reload
- Profile bindings remain stable -- hot reload only affects the resolved tool
  set within existing families
- New tools in an existing family are automatically subject to that family's
  posture ceiling
- New tools in a family not present in a profile are automatically denied by
  family admission (no security gap)
- Startup conflict detection remains fail-fast (exit). Runtime conflict
  detection degrades gracefully (reject change, log warning)

---

## F. MCP Server Interface (upstream)

tela exposes a standard MCP server to upstream clients (agents).

### tools/list

Returns the filtered tool list for the bound profile. Each tool retains
its original JSON Schema from the downstream server.

### tools/call

Forwards the tool call to the appropriate downstream server. If the
requested tool is not compatible with the bound profile declaration,
the call is rejected with `AUTHZ_DENY`.

### tela.profiles

Returns the list of configured profiles with their tool families, postures,
and side-effect policies. Used by nervus for auto-discovery during profile
binding. Returns JSON array of profile objects.

```json
[
  {
    "name": "coder",
    "tools": { "filesystem": "read_write", "shell": "read_only" },
    "side_effect_policy": "allow"
  }
]
```

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
- Downstream server emits `tools/list_changed` (tool list hot reload)
- Configuration hot-reload changes profile definitions

---

## G. Connection Flow (token mode)

```
Client                          tela                      Downstream
  |                              |                            |
  |-- initialize(token) -------->|                            |
  |                              |-- validate(HMAC + expiry   |
  |                              |   + tools_profile)         |
  |                              |-- bind profile context     |
  |                              |   from token               |
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
2. tela validates the token at connection time: HMAC signature, expiry, and
   `tools_profile`.
3. tela binds the corresponding profile from token metadata (`tools_profile` field)
   into the connection context.
4. Client receives `tools/list` containing only tools allowed by the bound profile.
5. On `tools/call`: tela checks the tool is allowed under the bound declaration, forwards
    to the downstream server, returns the result. The token is NOT re-validated
    on each call and is NOT re-sent on each call — it is a per-connection credential.

### Token Delivery

The primary token carrier is explicit connection-time metadata on `initialize`:
`clientInfo.capability_token`.

If framework behavior prevents that carrier, the documented fallback order is:

1. a custom `clientInfo` field preserved through `initialize`
2. post-initialize `tela/authenticate`
3. a stdio-only environment carrier such as `TELA_TOKEN`

Verifying carrier mechanics is implementation work, not a planning blocker.

### Token Structure

```json
{
  "token_id": "tok_a1b2c3d4e5f6",
  "tools_profile": "coder",
  "persona_ref": "code-reviewer@a1b2c3d4",
  "instance_id": "inst_abc123",
  "max_depth": 3,
  "issued_at": "2026-02-28T10:00:00Z",
  "expires_at": "2026-02-28T11:00:00Z",
  "signature": "<HMAC-SHA256>"
}
```

Required fields: `token_id`, `tools_profile`, `issued_at`, `expires_at`, `signature`.
Optional fields: `persona_ref`, `instance_id`, `max_depth`.

tela uses `tools_profile` to bind the connection to a profile, `issued_at`/`expires_at`
for expiry check, and `signature` for HMAC validation during `initialize` /
connection establishment. `persona_ref` and `instance_id` are verified at
connection time against the token payload for identity binding. Per-call `_meta`
fields are recorded in the audit log for correlation, not used for security
verification. Optional fields are recorded in the audit log.

---

## H. Open Mode (standalone, no token)

When `auth.mode` is set to `open`:

- No capability token is required on connect.
- tela still binds every connection to a profile; open mode removes token
  delivery, not profile-based enforcement.
- Profile selection is explicit and local to the tela instance:
  - `--default-profile` CLI flag wins if provided.
  - Otherwise, the profile marked `default: true` in configuration is used.
- Clients do not select profiles in open mode via connection metadata.
- If no explicit default profile is available, or if multiple profiles are
  marked `default: true`, tela rejects the connection instead of guessing.
- tela never implicitly selects a profile by config ordering.
- Suitable for standalone use with Claude Code, Cursor, or any MCP client
  that does not supply tokens, provided the operator configures an explicit
  open-mode profile.

Recommended open-mode defaults:

- safest default: `read_only`
- practical default for day-to-day standalone use: `execute_safe`
- never use as an implicit default: `execute_full`

---

## I. _meta Handling

MCP clients (agents) may include a `_meta` field in tool call arguments
(e.g. conversation ID, request ID). tela handles this transparently:

1. **Audit**: `_meta` fields are recorded in the audit log entry for the
   tool call.
2. **Stripping**: tela unconditionally strips `_meta` from tool call arguments
   before forwarding to downstream servers. `_meta` is an internal opifex
   contract between anima and tela — downstream servers never see it.
3. **Transparency**: Agents do not need to know about `_meta` handling.
   tela handles it silently.

---

## J. Error Codes

| Code | Meaning |
|------|---------|
| `AUTHZ_DENY` | Tool call rejected -- not in bound profile |
| `AUTH_RATE_LIMITED` | Authentication requests are temporarily rate limited |
| `PROFILE_NOT_FOUND` | Requested profile does not exist in configuration |
| `TOKEN_INVALID` | Token HMAC signature verification failed |
| `TOKEN_EXPIRED` | Token `expires_at` is in the past |
| `DOWNSTREAM_ERROR` | Downstream server returned an error |
| `DOWNSTREAM_UNAVAILABLE` | Downstream server is not connected / not responding |
| `TOOL_CONFLICT` | Two downstream servers expose the same tool name (startup: fatal, runtime: warning) |
| `TOOL_UNCLASSIFIED` | Tool has no posture classification from any source; denied by default |

Errors are returned as standard MCP error responses with the code in the
`error.code` field and a human-readable message in `error.message`.

---

## K. State Directory

Default: `~/.tela/` (override with `$TELA_STATE`).

Contents:

| Path | Purpose |
|------|---------|
| `audit.jsonl` | Append-only audit log (configurable via `audit.output`) |

tela is stateless beyond the audit log. Configuration is the source of
truth. The gateway can be restarted at any time without data loss.
