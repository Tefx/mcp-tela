# Operator P1 type-check and opifex parity criterion

## Accepted criterion

Operator P1 uses a scoped type-check criterion:

```bash
uv run mypy src
```

The full inventory command remains recorded but is **not** the Operator P1 gate
until the out-of-scope test typing families below are closed:

```bash
uv run mypy src tests
```

## Full inventory failure-family register

Provenance: `uv run mypy src tests` on the isolated worktree for
`tela.operator_p1.surfaces.remediate_typecheck_and_opifex_parity` returned
216 errors in 26 test files while `uv run mypy src` returned green.

| family | provenance | disposition | gate intersection | owner/closure |
| --- | --- | --- | --- | --- |
| test import path bootstrap | `tests/test_vendor_schema_parity.py:24 import-not-found` for `opifex_authority` | Excluded from Operator P1 source gate; parity is enforced by runtime pytest gate. | Does not affect runtime shared-surface implementation typing under `src`. | Test-typing cleanup owner; close by packaging or mypy path config for `scripts/ci`. |
| repro/live probe typing | `tests/repro/*` socket/process/fixture optionality and generator annotations | Excluded; repro harness typing backlog. | No intersection with Operator P1 shared-surface runtime contract gate. | Repro harness owner; close by fixture/probe annotation pass. |
| test fake/mock shape typing | `tests/shell/test_http_client.py`, `tests/shell/test_downstream_*`, `tests/shell/test_gateway.py` fake response/session/method assignment errors | Excluded; tests execute green but fake classes are not mypy-shaped. | Runtime contract behavior remains covered by pytest/shared-surface gate. | Test infrastructure owner; close by typed protocol fakes/casts. |
| optional `Result`/model narrowing in tests | `tests/black_box/*`, `tests/shell/test_status_truth_verification.py`, `tests/shell/test_hard_cut_shared_surfaces.py` union-attr/index errors | Excluded; assertion code lacks explicit non-None narrowing. | No source typing intersection; runtime assertions pass. | Test owner; close by asserts/casts before dereference. |
| TypedDict/dataclass construction in tests | `tests/core/test_models.py`, `tests/core/test_tool_prefix_contract.py`, `tests/shell/test_builtin_tools.py` | Excluded; test data needs explicit typed builders. | No source implementation ambiguity. | Test owner; close by typed factories or precise literals. |
| third-party ASGI/TestClient typing | `tests/shell/test_mcp_readiness_gate.py` middleware callable signature | Excluded; runtime compatibility passes. | No Operator P1 shared-surface decision dependency. | Test/middleware typing owner; close by ASGI protocol annotation. |

## OPIFEX parity unblock record

Before fix, repo pin `design/opifex-frozen-authority-packet.json` expected
`981a3294fe363ea137547ffa292829e21206e981` while the available authority
checkout was `450281eae8a481f2f63cf4540112c529cc976548`.

Authority citation:

- `opifex/design/final-canonical-contract.md` says canonical authority defines
  meaning, conformance metadata describes CI, and downstream repositories are
  conformance targets only.
- `opifex/conformance/shared_surfaces.yaml` requires the frozen follow-up packet
  before downstream CI and classifies mcp-tela `shared`, `alternate`, and
  `local_runtime` exposures as switch-blocking.
- `opifex/design/cross-repo-followup-packet.md` says downstream repos consume
  the opifex packet and do not reinterpret or replace its semantics.

Disposition: repo-fixable alignment. The mcp-tela frozen authority pin now
matches the authority checkout at `450281eae8a481f2f63cf4540112c529cc976548`;
the shared-surface gate then validates case matrix coverage, vendor schema
parity, expected-red behavior, and green runtime/contract tests against that
authority checkout.
