flowchart TD
  core-foundation["○ Core Foundation (0/6)"]
  config-layer["🔒 Configuration Layer (0/3)"]
  enforcement-chain["🔒 Enforcement Chain (0/7)"]
  mcp-server["🔒 MCP Server Interface (0/4)"]
  downstream-management["🔒 Downstream Server Management (0/4)"]
  auth-token["🔒 Token Authentication (0/3)"]
  auth-open["🔒 Open Mode Authentication (0/2)"]
  audit-logging["🔒 Audit Logging (0/3)"]
  cli-commands["🔒 CLI Commands (0/4)"]
  hot-reload["🔒 Hot Reload (0/3)"]
  meta-handling["🔒 Meta Field Handling (0/2)"]
  integration-tests["🔒 Integration Tests (0/6)"]
  core-foundation --> config-layer
  core-foundation --> enforcement-chain
  core-foundation --> mcp-server
  config-layer --> mcp-server
  enforcement-chain --> mcp-server
  core-foundation --> downstream-management
  config-layer --> downstream-management
  core-foundation --> auth-token
  config-layer --> auth-token
  core-foundation --> auth-open
  config-layer --> auth-open
  core-foundation --> audit-logging
  config-layer --> audit-logging
  config-layer --> cli-commands
  mcp-server --> cli-commands
  downstream-management --> cli-commands
  downstream-management --> hot-reload
  mcp-server --> hot-reload
  config-layer --> hot-reload
  enforcement-chain --> meta-handling
  cli-commands --> integration-tests
  hot-reload --> integration-tests
  meta-handling --> integration-tests
  auth-token --> integration-tests
  auth-open --> integration-tests
  audit-logging --> integration-tests

%% Drill into a phase: uvx vectl dag --phase <id>
