# tela Usage Guide

## Overview

tela is an MCP aggregation gateway. It sits between one or more downstream MCP
servers and one or more upstream MCP clients, then applies access control,
profile selection, posture enforcement, and audit logging.

Use tela when you want to:

- expose multiple MCP servers as one endpoint
- constrain tool usage by profile
- enforce read-only or destructive ceilings by family
- centralize audit logging
- share a single gateway across multiple agents

## Documentation map

Use the docs in this order:

- `README.md`: quickest way to understand what tela is and how to launch it
- `docs/USAGE.md`: operator guide, deployment patterns, and worked examples
- `tela.yaml.example`: fully commented configuration template
- `docs/INTERFACES.md`: CLI and configuration contract reference
- `docs/DESIGN.md`: architecture and implementation detail

## Mental model

At runtime, tela works like this:

```text
MCP client(s) -> tela -> downstream MCP servers
```

tela does not replace downstream servers. It brokers access to them.

## Installation

Install from the project root:

```bash
pip install -e .
```

If you use `uv`, the same editable install pattern works through your existing
workflow.

## First-time setup

Copy the example configuration and edit it for your environment:

```bash
cp tela.yaml.example tela.yaml
```

The example file is intentionally verbose and should be treated as the primary
operator reference.

## Configuration model

tela reads one YAML file with four top-level sections:

- `servers`
- `profiles`
- `auth`
- `audit`

### `servers`

Each server entry declares one downstream MCP server.

You must use exactly one transport per server:

- `command` for stdio
- `url` for SSE

Minimal stdio example:

```yaml
servers:
  fs:
    command: "mcp-filesystem"
    args: ["--root", "/workspace"]
    family: "filesystem"
```

Minimal SSE example:

```yaml
servers:
  github:
    url: "http://localhost:3001/sse"
    family: "git"
```

Important notes:

- the YAML key is the server name
- if `family` is omitted, tela uses the server name as the family by convention
- `default_posture` sets the baseline posture for tools from that server
- `tool_overrides` can adjust family or posture for specific tools

### `profiles`

Profiles define what a client is allowed to do.

Each profile contains:

- `capabilities`: family -> maximum posture ceiling
- `tool_overrides`: allow or deny specific tools within a family
- `default`: whether the profile is the default in open mode

Example:

```yaml
profiles:
  developer:
    capabilities:
      filesystem: "read_write"
      network: "read_only"
      git: "read_write"
    tool_overrides:
      filesystem:
        overrides:
          delete_file: "deny"
      git:
        overrides:
          force_push: "allow"
    default: true
```

Important notes:

- in `open` mode, one profile should usually have `default: true`
- custom families are valid, but built-in profiles only cover built-in family sets
- `tool_overrides` require the nested `overrides` map

### Built-in profiles

tela ships with seven built-in profile templates:

- `read_only`
- `fetch_external`
- `modify_local`
- `send_external`
- `orchestrate`
- `execute_safe`
- `execute_full`

These are defined in `src/tela/core/catalog.py` and demonstrated in
`tela.yaml.example`.

You can:

- rely on them as defaults
- override them by reusing the same profile name in your config
- define custom profiles alongside them

### `auth`

tela supports two authentication modes.

#### Open mode

```yaml
auth:
  mode: "open"
```

Use open mode only in trusted environments. Any client that can connect can use
tela, subject to profile restrictions.

#### Token mode

```yaml
auth:
  mode: "token"
  secrets:
    - "${TELA_SECRET}"
    - "${TELA_SECRET_PREVIOUS}"
```

Use token mode for shared or production deployments.

Best practices:

- use environment variables for secrets
- rotate keys by keeping the previous validation key in the second slot
- do not commit secrets to source control

#### Token mode end-to-end example

This is a practical pattern for a shared internal deployment.

1. Export the secrets.
2. Start tela in token mode.
3. Point clients at the shared gateway.

Example environment:

```bash
export TELA_SECRET="replace-with-primary-secret"
export TELA_SECRET_PREVIOUS="replace-with-previous-secret"
export TELA_STATE="$HOME/.tela"
```

Example config:

```yaml
servers:
  fs:
    command: "mcp-filesystem"
    args: ["--root", "/shared-workspace"]
    family: "filesystem"
  github:
    url: "http://localhost:3001/sse"
    family: "git"

profiles:
  team_safe:
    capabilities:
      filesystem: "read_write"
      git: "read_only"
    tool_overrides:
      filesystem:
        overrides:
          delete_file: "deny"
    default: true

auth:
  mode: "token"
  secrets:
    - "${TELA_SECRET}"
    - "${TELA_SECRET_PREVIOUS}"

audit:
  level: "L3"
  output: "${TELA_STATE}/audit.jsonl"
```

Start the shared gateway:

```bash
tela start --config tela.yaml --port 8080
```

Operational notes:

- use `token` mode when the gateway is shared by multiple users or agents
- keep the current signing key first and the previous validation key second
- prefer conservative shared profiles and explicit per-tool denies
- use `L3` audit logging when you need stronger operational traceability

Token auth flow:

```text
client -> presents token metadata -> tela
      -> tela validates signature and expiry using configured secrets
      -> tela binds the request to an allowed profile
      -> tela applies posture, tool override, and side-effect checks
      -> tela forwards allowed calls to downstream MCP servers
```

### `audit`

Audit logs are written as JSONL.

```yaml
audit:
  level: "L2"
  output: "${TELA_STATE}/audit.jsonl"
```

Audit levels:

- `L1`: minimal records
- `L2`: standard operational detail
- `L3`: verbose diagnostic detail

## Running tela

### stdio mode

Start tela without `--port` when your MCP client launches it as a child process:

```bash
tela start --config tela.yaml
```

This is the standard local-development setup.

Behavioral note:

- each MCP client usually launches its own `tela` process in stdio mode
- multiple agents can use tela this way, but they normally do not share one process

### SSE mode

Start tela with `--port` when you want a shared gateway process:

```bash
tela start --config tela.yaml --port 8080
```

Use SSE mode when:

- multiple agents should share the same gateway
- you want one long-lived gateway process
- you want to centralize audit and connection state more cleanly

## Client connection patterns

### Pattern 1: stdio client integration

Typical MCP client configuration:

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

This is the easiest integration path.

### Example: Claude Code style stdio configuration

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

This pattern is representative of local MCP hosts that launch child processes.

### Example: generic local MCP host

If your MCP host asks for an executable plus arguments, use:

```text
command: tela
args: start --config tela.yaml
```

If your host supports environment variables, pass token secrets there rather
than hardcoding them into config files.

### Pattern 2: shared gateway over SSE

Start the gateway:

```bash
tela start --config tela.yaml --port 8080
```

Then point multiple clients at the same network endpoint using your client's
SSE or remote MCP configuration model.

The exact client-side format depends on the MCP host application.

### Example: generic SSE client configuration

Many MCP hosts use a URL-based remote server definition. In those cases, point
the client at the shared tela endpoint you started with `--port`.

Conceptually, the configuration looks like this:

```text
name: tela
transport: sse
url: http://localhost:8080/sse
```

If your host supports headers or auth metadata, attach whatever that host uses
to carry your token-mode credentials.

### Example: shared internal gateway pattern

```text
gateway process: tela start --config tela.yaml --port 8080
client endpoint: http://gateway-host:8080/sse
```

This is the recommended shape when multiple agents should share one tela
instance instead of each launching its own stdio child process.

### Practical client guidance

- use `stdio` if your host expects a local command to launch
- use `SSE` if your host can connect to a shared remote MCP endpoint
- prefer `open` mode only for local trusted environments
- prefer `token` mode for shared agent infrastructure
- for quick local setup, the example config's custom `developer` profile is the simplest default
- for policy-centric setups, start from built-in profiles such as `modify_local` or `execute_safe`

## Choosing between stdio and SSE

### Use stdio when

- you are running locally
- you want the simplest setup
- one client owning one gateway process is acceptable

### Use SSE when

- multiple agents should share one tela instance
- you want one operational surface for logs and status
- you are deploying tela as a reusable service

## Multi-agent deployment patterns

### Pattern A: independent local agents

Each agent launches its own `tela` child process.

```text
Agent A -> tela A -> downstream servers
Agent B -> tela B -> downstream servers
Agent C -> tela C -> downstream servers
```

Choose this when:

- agents run on one developer machine
- isolated sessions are desirable
- duplicate downstream connections are acceptable

Operational tradeoff:

- simplest setup
- least shared state
- highest process duplication

### Pattern B: shared team gateway

One SSE gateway is shared by many clients or agents.

```text
Agent A ---\
Agent B ----> shared tela gateway -> downstream servers
Agent C ---/
```

Choose this when:

- multiple agents should see the same gateway surface
- you want centralized audit logs
- you want one place to manage auth and profiles

Operational tradeoff:

- better resource efficiency
- easier shared operations
- requires a more service-oriented deployment model

### Pattern C: mixed mode

It is valid to run both modes at once.

Examples:

- developers use local stdio instances for experimentation
- automation or shared assistants use one SSE deployment

This is often the most practical rollout path.

## Suggested deployment recipes

### Recipe: one developer, one workstation

- auth mode: `open`
- transport: `stdio`
- default profile: custom `developer` profile from `tela.yaml.example`
- audit level: `L2`

### Recipe: shared internal gateway

- auth mode: `token`
- transport: `SSE`
- default profile: conservative shared profile
- audit level: `L3`
- secrets: environment variables only

### Recipe: CI or bot operator

- auth mode: `token`
- transport: usually `SSE`
- profile: purpose-built automation profile
- side effects: explicitly allowed only where needed

## CLI reference

### Start the gateway

```bash
tela start --config tela.yaml
```

Options:

- `--config`: configuration file path, default `tela.yaml`
- `--port`: start SSE transport instead of stdio
- `--default-profile`: override the open-mode default profile selected from config

Examples:

```bash
tela start --config tela.yaml
tela start --config tela.yaml --port 8080
tela start --config tela.yaml --default-profile developer
```

### Inspect gateway state

```bash
tela status
tela status --json
```

### List profiles

```bash
tela profiles --config tela.yaml
tela profiles --config tela.yaml --json
```

### List active connections

```bash
tela connections
tela connections --json
```

### Query the audit log

```bash
tela audit
tela audit --limit 50
tela audit --since "1h"
tela audit --since "2026-01-01T00:00:00Z"
tela audit --json
```

## Environment variables

tela supports `${VAR}` and `$VAR` expansion in config values.

Common variables:

- `TELA_SECRET`
- `TELA_SECRET_PREVIOUS`
- `TELA_STATE`
- `HOME`

Example:

```bash
export TELA_SECRET="replace-with-real-secret"
export TELA_STATE="$HOME/.tela"
```

## Practical setup examples

### Local single-user development

```yaml
servers:
  fs:
    command: "mcp-filesystem"
    args: ["--root", "/workspace"]
    family: "filesystem"

profiles:
  developer:
    capabilities:
      filesystem: "read_write"
    default: true

auth:
  mode: "open"

audit:
  level: "L2"
  output: "./audit.jsonl"
```

Recommended run command:

```bash
tela start --config tela.yaml --default-profile developer
```

### Shared gateway for multiple agents

```yaml
servers:
  fs:
    command: "mcp-filesystem"
    args: ["--root", "/shared-workspace"]
    family: "filesystem"
  github:
    url: "http://localhost:3001/sse"
    family: "git"

profiles:
  developer:
    capabilities:
      filesystem: "read_write"
      git: "read_write"
    default: true

auth:
  mode: "token"
  secrets:
    - "${TELA_SECRET}"

audit:
  level: "L3"
  output: "/var/log/tela/audit.jsonl"
```

Recommended run command:

```bash
tela start --config tela.yaml --port 8080
```

## Core FAQ

### Does stdio mean only one agent can use tela?

No. Multiple agents can use tela in stdio mode, but each client usually starts
its own `tela` child process.

### When should I use stdio?

Use `stdio` when the MCP host launches local child processes and you want the
simplest possible setup.

### When should I use SSE?

Use `SSE` when multiple agents or clients should share one long-lived gateway.

### Which profile naming pattern should I follow?

Use built-in names like `modify_local` and `execute_safe` when you want to stay
close to the built-in catalog. Use custom names like `developer` and
`team_safe` when you are documenting deployment-specific policy intent.

## Troubleshooting

### `open` mode fails to start cleanly

Check that exactly one profile is suitable as the default, or pass
`--default-profile` explicitly.

### A server is rejected by config validation

Check that each server defines exactly one transport:

- `command`
- or `url`

Not both, and not neither.

### A tool is unexpectedly unavailable

Check, in order:

1. family admission
2. tool override check
3. posture ceiling comparison

## Validation and testing

Use these commands from the repository root:

```bash
uv run pytest -q
uv run pytest --doctest-modules src/tela/
uv run invar guard --all
uv run pytest tests/repro/ -q
```

## Related files

- `README.md`
- `tela.yaml.example`
- `docs/DESIGN.md`
- `docs/INTERFACES.md`
