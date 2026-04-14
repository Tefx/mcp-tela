# Hard Cut Prep Proof Assets

Status: preparation only. No product code changes.

## Scope Guard

- This file prepares proof assets only.
- It does **not** implement the hard cut.
- Current runtime/tests/docs are expected to stay legacy until a later implementation step flips them.

## Canonical Fixture Sources

- `docs/ADR-007-opifex-canonical-contract-alignment.md`
  - canonical shared token identity is `CapabilityToken.profile_id`
  - `tools_profile` is invalid on shared token surfaces
  - `profile_name` is display-only local vocabulary if retained at all
- `docs/DESIGN.md`
  - token-mode binding uses canonical `profile_id`
- `docs/INTERFACES.md`
  - `tela.profiles` currently documents a migration shape containing `profile_name`
  - this prep slice freezes the stricter hard-cut target fixture as `profile_id + capabilities + default` only

## Expected-Red Targets by Audited Blocker Family

| blocker_family | why it is blocked now | expected-red target to flip later |
|---|---|---|
| `TOKEN-PROFILE-ID` | Current runtime/tests still require `profile_name` in `CapabilityToken` and initialize payloads (`src/tela/core/models.py`, `src/tela/shell/upstream.py`, `tests/integration/test_token_mode_initialize.py`, `tests/repro/test_high.py`). | `CapabilityToken(profile_id=...)` acceptance fixture in `artifacts/hard_cut/canonical_capability_token_fixture.json`; current constructor/initialize wiring should fail red until `profile_id` replaces `profile_name`. |
| `PROFILES-SURFACE-NAME` | Current surface is still `tela.profiles` / `tela://profiles` (`tests/shell/test_surface_contract.py`, `tests/shell/test_gateway.py`, `docs/AGENT_INTERFACE.md`, `docs/INTERFACES.md`, `docs/USAGE.md`). | Contract check for canonical `tela_list_profiles` surface should fail red until legacy resource name/URI are removed. |
| `PROFILES-PAYLOAD-CANONICAL-SHAPE` | Current tests/docs still assert payload entries with `profile_name`, and docs still mention historical `tools` emission (`tests/shell/test_surface_contract.py`, `tests/shell/test_gateway.py`, `docs/INTERFACES.md`, `docs/MIGRATION-003-capability-only-profiles.md`). | Exact fixture in `artifacts/hard_cut/canonical_tela_list_profiles_fixture.json` should fail red until profile listing emits only `profile_id`, `capabilities`, and `default`. |
| `FAIL-CLOSED-LEGACY-ALIASES` | Current migration/docs still tolerate legacy profile vocabulary and `tools` migration aliases; current initialize path explicitly requires `profile_name`. | Rejection checks should fail red until legacy token/profile aliases are rejected instead of accepted or display-normalized. Minimum red target: initialize with only `profile_name`/`tools_profile` must reject rather than bind. |

## Canonical Schema-Derived Fixtures

### CapabilityToken

File: `artifacts/hard_cut/canonical_capability_token_fixture.json`

```json
{
  "token_id": "tok_example_profile_developer",
  "profile_id": "developer",
  "issued_at": "2026-04-15T00:00:00Z",
  "expires_at": "2026-04-15T01:00:00Z",
  "signature": "example_hmac_sha256_signature"
}
```

Hard-cut rule: canonical bound identity is `profile_id`; `profile_name` and
`tools_profile` are not fixture inputs.

### `tela_list_profiles`

File: `artifacts/hard_cut/canonical_tela_list_profiles_fixture.json`

```json
[
  {
    "profile_id": "developer",
    "capabilities": {
      "filesystem": "read_write",
      "git": "read_only"
    },
    "default": true
  },
  {
    "profile_id": "reviewer",
    "capabilities": {
      "filesystem": "read_only",
      "git": "read_only"
    },
    "default": false
  }
]
```

Hard-cut rule: profile listing fixture uses only `profile_id`, `capabilities`,
and `default`.

## Fail-Closed Error Expectations

1. **Token missing `profile_id`** → reject initialize with `INITIALIZE_REJECTED`; do not infer from config default.
2. **Token presents only legacy `profile_name`** → reject initialize with `INITIALIZE_REJECTED`; do not alias to `profile_id`.
3. **Token presents only legacy `tools_profile`** → reject initialize with `INITIALIZE_REJECTED`; do not alias to `profile_id`.
4. **Token signature invalid after canonical `profile_id` serialization** → `INITIALIZE_REJECTED: TOKEN_INVALID`.
5. **Token expired** → `INITIALIZE_REJECTED: TOKEN_EXPIRED`.
6. **Bound `profile_id` missing from runtime config** → `PROFILE_NOT_FOUND`.
7. **Recovery path loses server config during in-flight recovery** → `DOWNSTREAM_UNAVAILABLE` with ADR-006 detail fields preserved, especially `config_missing=true`.
8. **Profile-listing payload contains legacy fields (`profile_name`, `tools`, `families`) or omits canonical required fields** → contract-test failure / release blocker; do not silently emit mixed-shape payloads.

## Rollback + Re-Verify Matrix

| rollback slice | revert action | token re-verify | profile re-verify | tool re-verify |
|---|---|---|---|---|
| token hard cut | Revert `profile_id` token adoption to last pre-cut commit. | Re-run canonical token fixture acceptance/rejection matrix: valid `profile_id`, reject `profile_name`, reject `tools_profile`, reject bad signature/expiry. | Confirm bound profile lookup still fails closed when canonical profile is absent. | Confirm `tools/list` and `tools/call` still require a successfully bound connection before any tool exposure. |
| profile surface rename | Revert `tela_list_profiles` introduction to last pre-cut commit. | Confirm token flow still binds before profile reads are allowed. | Re-run surface audit: old `tela.profiles`/`tela://profiles` restored, canonical `tela_list_profiles` absent, payload diff recorded. | Confirm no accidental registration of profile-listing as a generic callable tool. |
| profile payload hard cut | Revert payload narrowing to last pre-cut commit. | Confirm token still carries only canonical `profile_id` in proof fixtures. | Re-run profile fixture diff: check for reintroduced `profile_name`, `tools`, or `families`; verify `default` semantics unchanged. | Re-run downstream tool visibility smoke to ensure capability ceilings still gate tool exposure for each listed profile. |
| fail-closed alias rejection | Revert alias rejection logic to last pre-cut commit. | Re-run negative initialize matrix and capture any reintroduced acceptance of `profile_name`/`tools_profile`. | Re-run profile-list contract tests and ensure mixed legacy/canonical shapes are not silently accepted. | Re-run ADR-006 fail-closed witness coverage: token/profile rejection, `PROFILE_NOT_FOUND`, and `DOWNSTREAM_UNAVAILABLE` with `config_missing=true`. |

## Legacy Test/Doc Freeze Inventory

### Test files still frozen on legacy vocabulary

- `profile_name` appears in **31** test files:
  - `tests/black_box/test_reaper_black_box.py`
  - `tests/core/test_catalog.py`
  - `tests/core/test_models.py`
  - `tests/core/test_token.py`
  - `tests/integration/test_audit_levels.py`
  - `tests/integration/test_hot_reload.py`
  - `tests/integration/test_token_mode_initialize.py`
  - `tests/repro/conftest.py`
  - `tests/repro/runtime_snapshot.py`
  - `tests/repro/spec_verify_structural_collapse_liveness.py`
  - `tests/repro/test_blockers.py`
  - `tests/repro/test_conn_v2_blackbox.py`
  - `tests/repro/test_connection_count_semantics.py`
  - `tests/repro/test_disconnect_cleanup.py`
  - `tests/repro/test_high.py`
  - `tests/repro/test_low.py`
  - `tests/repro/test_medium.py`
  - `tests/repro/test_runtime_boundary_immutability.py`
  - `tests/repro/test_seam_convergence_proof.py`
  - `tests/repro/test_startup_coord_liveness.py`
  - `tests/shell/test_audit.py`
  - `tests/shell/test_connection_lifecycle.py`
  - `tests/shell/test_connection_reaper.py`
  - `tests/shell/test_gateway.py`
  - `tests/shell/test_gateway_lifecycle_authority.py`
  - `tests/shell/test_http_routes.py`
  - `tests/shell/test_notification_forwarding.py`
  - `tests/shell/test_query_commands_remote_state.py`
  - `tests/shell/test_status_truth_verification.py`
  - `tests/shell/test_surface_contract.py`
  - `tests/shell/test_upstream.py`
- legacy profile-key `tools` appears in **7** test files:
  - `tests/core/test_capability_only_profiles.py`
  - `tests/repro/test_connect_runtime_liveness.py`
  - `tests/repro/test_tool_prefix_blackbox.py`
  - `tests/shell/test_connect_cmd.py`
  - `tests/shell/test_merge_instructions.py`
  - `tests/shell/test_surface_contract.py`
  - `tests/shell/test_upstream.py`
- legacy `families` appears in **2** test files:
  - `tests/core/test_capability_only_profiles.py`
  - `tests/shell/test_upstream.py`
- legacy URI `tela://profiles` appears in **2** test files:
  - `tests/shell/test_gateway.py`
  - `tests/shell/test_surface_contract.py`

### Doc files still frozen on legacy vocabulary

- `profile_name` appears in **5** doc files:
  - `docs/ADR-007-opifex-canonical-contract-alignment.md`
  - `docs/DESIGN.md`
  - `docs/INTERFACES.md`
  - `docs/MIGRATION-003-capability-only-profiles.md`
  - `docs/USAGE.md`
- legacy profile-key `tools` appears in **7** doc files:
  - `docs/ADR-003-gateway-capability-only-profiles.md`
  - `docs/AGENT_INTERFACE.md`
  - `docs/CONFIRMED-SURFACE-CONTRACT.md`
  - `docs/DESIGN.md`
  - `docs/INTERFACES.md`
  - `docs/MIGRATION-003-capability-only-profiles.md`
  - `docs/USAGE.md`
- legacy `families` appears in **3** doc files:
  - `docs/AGENT_INTERFACE.md`
  - `docs/MIGRATION-003-capability-only-profiles.md`
  - `docs/USAGE.md`
- legacy URI `tela://profiles` appears in **5** doc files:
  - `docs/AGENT_INTERFACE.md`
  - `docs/CONFIRMED-SURFACE-CONTRACT.md`
  - `docs/DESIGN.md`
  - `docs/INTERFACES.md`
  - `docs/USAGE.md`

## Notes

- The local repo still contains pre-hard-cut schema/code (`contracts/capability_token.schema.json`, `src/tela/core/models.py`, `src/tela/shell/upstream.py`) that use `tools_profile` or `profile_name`; this prep step intentionally records that drift instead of fixing it.
- The expected-red targets above are deliberately simple: they exist to prove the current tree is still legacy and to anchor the later implementation cutover.
