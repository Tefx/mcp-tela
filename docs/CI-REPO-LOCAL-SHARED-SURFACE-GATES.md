# Repo-Local Opifex Shared-Surface Gates

## Authority basis

These repo-local jobs exist only to verify `mcp-tela` against the canonical and
conformance inputs published by `opifex`:

- `../opifex/design/final-canonical-contract.md`
- `../opifex/conformance/shared_surfaces.yaml`
- `../opifex/conformance/forbidden_vocabulary.yaml`
- `../opifex/conformance/case_matrix/mcp-tela/*`

This repo does not reinterpret shared meaning locally.

## Required green checks

The workflow `.github/workflows/opifex-shared-surface-gates.yml` exposes these
branch-protection-ready job names:

- `mcp-tela / opifex shared-surface expected-red`
- `mcp-tela / opifex shared-surface green`

The expected-red job is intentionally green only when a seeded forbidden-vocabulary
drift fails first. The green job is the steady-state repo-local gate.

## Trusted repo-local commands

Use the same commands locally and in CI:

```bash
uv sync --frozen --group dev
OPIFEX_ROOT=../opifex uv run python scripts/ci/mcp_tela_shared_surface_gate.py expected-red
OPIFEX_ROOT=../opifex uv run python scripts/ci/mcp_tela_shared_surface_gate.py green
```

If `opifex` is checked out elsewhere, point `OPIFEX_ROOT` at that checkout root.

## Shared surfaces covered

The wrapper script verifies the `mcp-tela`-owned shared surfaces from
`shared_surfaces.yaml` and then runs the matching repo-local tests for:

- `tela_initialize_token_mode`
- `tela_tools_call_builtin_tela_list_profiles`
- `tela_tools_call_builtin_tela_list_providers`
- `tela_tools_call_downstream`
- `tela_http_connect`
- `tela_shared_naming_docs`
- `tela_mcp_server_naming`

The gate is intended to fail fast on:

- forbidden shared vocabulary such as `profile_name`, `tools_profile`, and `families`
- dotted or otherwise non-`snake_case` MCP surface names
- schema/vendor drift against `opifex`
- docs/runtime parity drift on the tela-owned shared surfaces above
