# Repo-Local Tela Contract Gates

## Authority basis

Tela owns its contracts. The gate requires only this checkout and the project's
development dependencies:

- `contracts/capability_token.schema.json`
- `contracts/tela_profile_list.schema.json`
- `contracts/errors.yaml`
- `docs/INTERFACES.md`
- `docs/CONFIRMED-SURFACE-CONTRACT.md`

No sibling checkout, external Git pin, or authority environment variable is
required. Historical Opifex references in archived evidence are provenance only.

## Required green checks

The workflow `.github/workflows/shared-surface-gates.yml` exposes:

- `mcp-tela / shared-surface expected-red`
- `mcp-tela / shared-surface green`

The expected-red job succeeds only when seeded forbidden-vocabulary drift fails
the documentation check. It restores the original README in a `finally` block.
The green job runs the contract and runtime groups declared by
`scripts/ci/mcp_tela_shared_surface_gate.py`.

If branch protection still requires the retired workflow's Opifex-prefixed job
names, replace those required checks with the names above in repository settings.

## Local commands

Run expected-red separately from other tests: it temporarily edits README.

```bash
uv sync --frozen --group dev
uv run python scripts/ci/mcp_tela_shared_surface_gate.py expected-red
uv run python scripts/ci/mcp_tela_shared_surface_gate.py green
uv run pytest -q
uv run invar guard --all
```

## Coverage

- JSON schema validity and representative agreement with token and profile-list
  runtime validation, including missing fields, extra keys, types and enums
- fail-closed admission and profile-list handling across runtime paths
- MCP tool naming, token binding and audit semantics
- documentation/runtime surface parity

Profile-list runtime validation additionally rejects multiple default entries;
that cross-entry constraint remains a runtime check.
