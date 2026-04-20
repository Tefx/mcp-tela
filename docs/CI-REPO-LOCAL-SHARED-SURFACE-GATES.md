# Repo-Local Opifex Shared-Surface Gates

## Authority basis

These repo-local jobs exist only to verify `mcp-tela` against the canonical and
conformance inputs published by `opifex` at the frozen ref pinned in
`design/opifex-frozen-authority-packet.json`:

- `opifex/design/final-canonical-contract.md`
- `opifex/conformance/shared_surfaces.yaml`
- `opifex/conformance/forbidden_vocabulary.yaml`
- the `opifex` `case_matrix` files referenced by `shared_surfaces.yaml`

This repo does not reinterpret shared meaning locally. The workflow and gate
script consume the frozen pin recorded in
`design/opifex-frozen-authority-packet.json` and derive repo-local scope from
`conformance/shared_surfaces.yaml` plus the referenced
`conformance/case_matrix/mcp-tela/*` files.

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
AUTHORITY_REF=$(python - <<'PY'
import json
from pathlib import Path
print(json.loads(Path("design/opifex-frozen-authority-packet.json").read_text())["ref"])
PY
)
test "$(git -C ../opifex rev-parse HEAD)" = "$AUTHORITY_REF"
OPIFEX_ROOT=../opifex uv run python scripts/ci/mcp_tela_shared_surface_gate.py expected-red
OPIFEX_ROOT=../opifex uv run python scripts/ci/mcp_tela_shared_surface_gate.py green
```

If `opifex` is checked out elsewhere, point `OPIFEX_ROOT` at that pinned checkout root.

## Shared surfaces covered

The wrapper script verifies the `mcp-tela`-owned shared surfaces by reading the
authoritative `shared_surfaces.yaml` entries, filtering by `owner_repo`,
deriving switch-blocking scope from `gate_policy.switch_blocking.by_exposure`,
and then resolving each owned surface's referenced `case_matrix` files under
`opifex`. That authority-derived scope, not a local allowlist, determines which
shared surfaces the repo-local gate must cover.

The gate is intended to fail fast on:

- forbidden shared vocabulary such as `profile_name`, `tools_profile`, and `families`
- dotted or otherwise non-`snake_case` MCP surface names
- schema/vendor drift against `opifex`
- docs/runtime parity drift on the tela-owned shared surfaces above
