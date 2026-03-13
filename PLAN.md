# tela

| Status | Phase | Name | Progress |
|--------|-------|------|----------|
| ○ pending | core-foundation | Core Foundation | 0/6 (0%) |
| 🔒 locked | config-layer | Configuration Layer | 0/3 (0%) |
| 🔒 locked | enforcement-chain | Enforcement Chain | 0/7 (0%) |
| 🔒 locked | mcp-server | MCP Server Interface | 0/4 (0%) |
| 🔒 locked | downstream-management | Downstream Server Management | 0/4 (0%) |
| 🔒 locked | auth-token | Token Authentication | 0/3 (0%) |
| 🔒 locked | auth-open | Open Mode Authentication | 0/2 (0%) |
| 🔒 locked | audit-logging | Audit Logging | 0/3 (0%) |
| 🔒 locked | cli-commands | CLI Commands | 0/4 (0%) |
| 🔒 locked | hot-reload | Hot Reload | 0/3 (0%) |
| 🔒 locked | meta-handling | Meta Field Handling | 0/2 (0%) |
| 🔒 locked | integration-tests | Integration Tests | 0/6 (0%) |

## ○ core-foundation — Core Foundation (0/6, 0%)

- ○ **core.errors** Error codes module
  Implement core error types from contracts/errors.yaml. AUTHZ_DENY (200), 
PROFILE_NOT_FOUND (201), TOKEN_INVALID (202), TOKEN_EXPIRED (203), TOOL_CONFLICT
(204), TOOL_UNCLASSIFIED (205), DOWNSTREAM_ERROR (210), DOWNSTREAM_UNAVAILABLE 
(211). Pure logic with dataclass contracts.
- ○ **core.posture** Posture classification
  Implement posture enum (none, read_only, read_write) with comparison 
operators. Posture check: is tool posture <= profile posture ceiling? Pure 
logic.
- ○ **core.profiles** Profile models
  ProfileSpec dataclass: name, tools (family->posture map), tool_overrides 
(family->tool->posture), side_effect_policy (allow/read_only). Validation logic.
- ○ **core.token** Capability token model
  CapabilityToken dataclass: token_id, tools_profile, persona_ref (opt), 
instance_id (opt), max_depth (opt), issued_at, expires_at, signature. Required 
vs optional field validation.
- ○ **core.server-config** Server configuration model
  ServerConfig dataclass: command/url, args (opt), family (opt), tool_overrides 
(opt), default_posture (opt=none). Server-is-family convention implementation.
- ○ **core.tool** Tool metadata model
  ToolMetadata dataclass: name, family, posture (opt), annotations (opt). Family
resolution: server-is-family default, tool_overrides, explicit family field. 
Posture classification: tool_overrides > MCP annotations > default_posture.

## 🔒 config-layer — Configuration Layer (0/3, 0%)

- ○ **config.load** Configuration loader
  Load tela.yaml, parse servers/profiles/auth/audit sections. Shell module for 
file I/O.
- ○ **config.env-expansion** Environment variable expansion
  Expand all  references in config values. Fail on unset variables with clear 
error messages.
- ○ **config.validation** Configuration validation
  Validate profile names are unique, default profile exists if open mode, at 
least one server defined.

## 🔒 enforcement-chain — Enforcement Chain (0/7, 0%)

- ○ **enforce.layer1-token** Layer 1: Token validation
  Verify HMAC signature (dual-key: try primary, then secondary), check 
expires_at not in past. If invalid, deny with TOKEN_INVALID or TOKEN_EXPIRED. 
Pure logic.
- 🔒 **enforce.layer2-profile** Layer 2: Profile lookup
  Resolve tools_profile from token to ProfileSpec in config. If not found, deny 
with PROFILE_NOT_FOUND.
- 🔒 **enforce.layer3-family** Layer 3: Family admission
  Check if tool's family exists in bound profile's tools map. If not, deny with 
AUTHZ_DENY.
- 🔒 **enforce.layer4-posture** Layer 4: Posture check
  If posture classification available: is tool posture <= profile posture 
ceiling for that family? If exceeded, deny with AUTHZ_DENY. If unclassified: 
subject to default_posture (deny by default).
- 🔒 **enforce.layer5-override** Layer 5: Tool override check
  If profile has tool_overrides for this specific tool: apply it (deny or 
allow), overriding posture check result.
- 🔒 **enforce.layer6-sideeffect** Layer 6: Side-effect check
  If tool's effective posture > read_only and profile's side_effect_policy is 
read_only: deny with AUTHZ_DENY.
- 🔒 **enforce.layer7-effective** Layer 7: Effective policy
  Combine all layers results. Tool call forwarded to downstream only if every 
layer permits it. Pure logic for policy decision.

## 🔒 mcp-server — MCP Server Interface (0/4, 0%)

- ○ **mcp.tools-list** tools/list handler
  Return filtered tool list for bound profile. Each tool retains original JSON 
Schema from downstream.
- 🔒 **mcp.tools-call** tools/call handler
  Forward tool call to downstream server after enforcement chain check. Strip 
_meta before forwarding. Return result or error.
- ○ **mcp.profiles-list** tela.profiles handler
  Return list of configured profiles with tool families, postures, and 
side_effect_policy. Used by nervus for auto-discovery.
- ○ **mcp.notifications** notifications/tools/list_changed
  Emit notification when available tool set changes: profile switch, downstream 
connect/disconnect, downstream tools/list_changed, config hot-reload. Payload: 
profile_name, token_id, tools_digest.

## 🔒 downstream-management — Downstream Server Management (0/4, 0%)

- ○ **downstream.spawn-stdio** Spawn stdio downstream servers
  Start downstream MCP servers via stdio transport. Manage process lifecycle 
(spawn, monitor, cleanup). Shell module with process I/O.
- 🔒 **downstream.connect-sse** Connect SSE downstream servers
  Connect to downstream MCP servers via SSE transport. Handle reconnection 
logic. Shell module with network I/O.
- 🔒 **downstream.enumerate** Enumerate downstream tool lists
  Call tools/list on each downstream server. Resolve family for each tool 
(server-is-family default + tool_overrides). Pure logic for family resolution.
- 🔒 **downstream.conflict** Tool conflict detection
  Detect tool name conflicts across downstream servers. Fail-fast at startup 
(exit). Runtime: reject change, log warning, keep previous tool list. Pure logic
for conflict detection.

## 🔒 auth-token — Token Authentication (0/3, 0%)

- ○ **auth.hmac** HMAC signature validation
  Implement HMAC-SHA256 signature validation. Dual-key rotation: try primary 
secret for sign+validate, try secondary for validate-only. Pure logic.
- 🔒 **auth.expiry** Token expiry checking
  Check expires_at is not in the past. Include issued_at for correlation. Pure 
logic.
- 🔒 **auth.binding** Profile binding
  Bind token's tools_profile field to ProfileSpec in config. Verify persona_ref 
and instance_id against token payload at connection time. Shell module for 
config lookup.

## 🔒 auth-open — Open Mode Authentication (0/2, 0%)

- ○ **open.default-profile** Default profile selection
  When auth.mode=open, select default profile marked default:true in config or 
from --default-profile CLI flag. Reject if no default configured. Shell module 
for config lookup.
- 🔒 **open.connection-meta** Connection metadata handling
  Process connection metadata for profile selection in open mode. Shell module 
for I/O.

## 🔒 audit-logging — Audit Logging (0/3, 0%)

- ○ **audit.levels** Audit level definitions
  L1: tool name, result status, latency. L2: L1 + parameter hash. L3: L2 + full 
request/response (opt-in).
- 🔒 **audit.write** JSONL audit writer
  Append-only audit log as JSONL. Configurable output path. Include _meta fields
for correlation. Shell module for file I/O.
- 🔒 **audit.query** Audit query CLI
  tela audit [--json] [--since time] [--limit n]. Relative durations (1h, 30m). 
Shell for CLI and query.

## 🔒 cli-commands — CLI Commands (0/4, 0%)

- ○ **cli.start** tela start command
  Start MCP gateway. Read config, spawn downstream servers, listen on stdio or 
SSE port if --port specified. Shell module for CLI and I/O.
- ○ **cli.status** tela status command
  Print gateway status: uptime, connected downstream servers, active 
connections, profile count. Support --json for machine-readable output.
- ○ **cli.profiles** tela profiles command
  List configured profiles with tool families, postures, side effect policies, 
and resolved tool counts. Support --json output.
- ○ **cli.connections** tela connections command
  List active upstream connections: connection id, bound profile, connected 
since, tool call count. Support --json output.

## 🔒 hot-reload — Hot Reload (0/3, 0%)

- ○ **reload.tool-list** Tool list hot reload
  Handle downstream notifications/tools/list_changed. Re-enumerate tool list, 
re-assign families, re-run conflict detection. No interruptions to active 
connections.
- 🔒 **reload.config** Config hot reload
  Monitor tela.yaml for changes. Reload config, update profiles, re-bind 
connections if needed. Active connections preserved.
- 🔒 **reload.conflict-runtime** Runtime conflict handling
  On runtime tool conflict: reject change, keep previous tool list, write 
TOOL_CONFLICT warning to audit log. Do not crash, do not disconnect. Contrast 
with startup fail-fast.

## 🔒 meta-handling — Meta Field Handling (0/2, 0%)

- ○ **meta.strip** _meta field stripping
  Unconditionally strip _meta from tool call arguments before forwarding to 
downstream servers. _meta is internal opifex contract, never seen by downstream.
Pure logic.
- 🔒 **meta.audit** _meta audit recording
  Record _meta fields in audit log entry for tool call correlation. Shell module
for audit I/O.

## 🔒 integration-tests — Integration Tests (0/6, 0%)

- ○ **test.token-flow** Token authentication flow
  End-to-end test: client connects with valid token, receives filtered 
tools/list, tool calls are authorized correctly.
- 🔒 **test.open-flow** Open mode flow
  End-to-end test: client connects without token, default profile applied, tool 
calls work correctly.
- 🔒 **test.enforcement** Enforcement chain integration
  End-to-end test: all 7 enforcement layers work in sequence. Token validation →
profile lookup → family admission → posture check → override → side-effect → 
forward.
- 🔒 **test.downstream** Downstream server lifecycle
  End-to-end test: spawn stdio downstream, connect SSE downstream, enumerate 
tools, handle disconnect, cleanup.
- 🔒 **test.reload** Hot reload integration
  End-to-end test: downstream emits tools/list_changed, tool list updates, 
conflict detected, config hot-reload, connection preserved.
- 🔒 **test.conflict-detector** Conflict detection integration
  End-to-end test: two downstream servers expose same tool name -> startup 
fails. Runtime conflict -> warning and reject.

