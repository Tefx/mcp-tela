# Remediation Guardrails Test Seam Inventory

## Scope anchor

This register inventories the exact test families and harness seams that protect the remediation tranche defined in `docs/remediation_guardrails_scope_register.md`.

## Coverage register

### downstream_wrapper_removal

- **Must-run tests**
  - `tests/shell/test_downstream_runtime_connection.py::test_connect_all_enumerates_mocked_session`
  - `tests/shell/test_downstream_runtime_connection.py::test_connect_all_opens_downstreams_in_parallel`
  - `tests/shell/test_downstream_runtime_connection.py::test_connect_all_closes_successful_handles_when_parallel_peer_fails`
  - `tests/shell/test_downstream_runtime_connection.py::test_connect_all_uses_sse_transport_when_url_set`
  - `tests/shell/test_downstream_runtime_connection.py::test_re_enumerate_updates_tool_list_from_session`
  - `tests/shell/test_downstream_runtime_connection.py::test_message_handler_routes_reconnect_exception`
  - `tests/shell/test_downstream.py::test_handle_reconnect_calls_enumerate_once`
  - `tests/shell/test_downstream.py::test_handle_reconnect_passes_enumerated_tools_to_on_server_reconnect`
  - `tests/shell/test_downstream.py::test_handle_reconnect_swaps_client_before_enumeration`
  - `tests/shell/test_downstream.py::test_recover_server_client_fails_closed_when_server_removed_mid_recovery`
  - `tests/shell/test_downstream.py::test_recover_server_client_config_remove_cleans_stale_client_and_lock`
  - `tests/shell/test_downstream.py::test_recover_server_client_releases_registry_lock_around_network_io`
  - `tests/shell/test_downstream.py::test_recover_server_client_success_closes_replaced_client`
  - `tests/shell/test_downstream.py::test_recover_server_client_rejects_material_config_change_before_swap`
- **Current monkeypatch seams**
  - `downstream._open_client_for_server` is monkeypatched heavily across the above tests and is the seam most likely to break if passthrough wrappers are removed or renamed.
  - No current test monkeypatches `downstream._validate_transport_mode`; transport validation only has indirect coverage, so that seam is comparatively under-protected.
- **Highest-risk failure mode**
  - Wrapper removal silently breaks the monkeypatch target name or bypasses reconnect ordering/cleanup behavior, causing tests to pass only on direct `tool_lists=` paths while real reconnect/re-enumeration paths regress.

### error_semantics_foundation

- **Must-run tests**
  - `tests/core/test_classification.py` (entire file)
  - `tests/core/test_errors.py::test_auth_rate_limited_constant_exists`
  - `tests/shell/test_http_routes.py::TestHandleStatus::test_handle_status_rejects_invalid_token`
  - `tests/shell/test_http_routes.py::TestHandleConnect::test_handle_connect_rejects_invalid_token`
  - `tests/shell/test_http_routes.py::TestHandleDisconnect::test_handle_disconnect_rejects_invalid_token`
  - `tests/shell/test_http_routes.py::TestBearerTokenUsage::test_all_handlers_use_validate_bearer_token`
  - `tests/shell/test_http_routes.py::TestBearerTokenUsage::test_validate_bearer_token_uses_hmac_compare_digest`
  - `tests/shell/test_http_auth.py::test_validate_bearer_token_rejects_invalid_token`
  - `tests/shell/test_http_auth.py::test_middleware_401_response_is_valid_json`
  - `tests/shell/test_http_auth.py::test_middleware_rejects_lowercase_bearer_scheme`
  - `tests/shell/test_http_auth.py::test_middleware_rejects_uppercase_bearer_scheme`
  - `tests/shell/test_http_auth.py::test_middleware_rejects_bearer_without_space`
  - `tests/shell/test_http_auth.py::test_middleware_rejects_bearer_with_tab_separator`
  - `tests/integration/test_mcp_auth_wire.py::test_post_mcp_without_bearer_token_returns_401`
  - `tests/integration/test_mcp_auth_wire.py::test_post_mcp_with_wrong_token_returns_401`
  - `tests/integration/test_mcp_auth_wire.py::test_post_mcp_with_lowercase_bearer_returns_401`
  - `tests/integration/test_mcp_auth_wire.py::test_post_mcp_with_uppercase_bearer_returns_401`
  - `tests/integration/test_mcp_auth_wire.py::test_post_mcp_with_bearer_no_space_returns_401`
  - `tests/integration/test_mcp_auth_wire.py::test_post_mcp_with_bearer_tab_separator_returns_401`
- **Literal audits to preserve before/after refactor**
  - `rg "AUTH_INVALID_TOKEN|DOWNSTREAM_CONNECT_FAILED|INITIALIZE_REJECTED|MISSING_TOKEN" tests src/tela`
  - `rg "status_code == 401|startswith\(\"AUTH_INVALID_TOKEN" tests`
- **Highest-risk failure mode**
  - Error/helper centralization changes public string prefixes or 401 classification paths, breaking exact/startswith assertions and producing route-vs-middleware drift that black-box auth tests catch late.

### token_precedence_refactor

- **Must-run tests**
  - `tests/shell/test_connect_cmd.py::test_connect_token_override_priority_cli_env_lockfile`
  - `tests/shell/test_connect_cmd.py::test_connect_server_path_requires_token_or_env`
  - `tests/shell/test_connect_cmd.py::test_connect_server_path_uses_env_token`
  - `tests/shell/test_serve_cmd.py::test_token_override_priority_cli_over_env_over_generated`
  - `tests/shell/test_serve_cmd.py::test_serve_lockfile_written_then_deleted`
- **Highest-risk failure mode**
  - A shared helper over-normalizes the two surfaces: `connect` loses lockfile fallback / missing-token behavior, or `serve` stops generating a token when CLI+env are absent.

### gateway_auth_skeleton_refactor

- **Must-run tests**
  - `tests/shell/test_gateway.py::test_streamable_http_surface_mounts_liveness_routes_and_auth_boundary`
  - `tests/shell/test_http_routes.py::TestHandleStatus::test_handle_status_rejects_invalid_token`
  - `tests/shell/test_http_routes.py::TestHandleStatus::test_handle_status_accepts_valid_token_when_gateway_not_started`
  - `tests/shell/test_http_routes.py::TestHandleStatus::test_handle_status_returns_status_when_gateway_started`
  - `tests/shell/test_http_routes.py::TestHandleConnect::test_handle_connect_rejects_invalid_token`
  - `tests/shell/test_http_routes.py::TestHandleConnect::test_handle_connect_rejects_when_gateway_not_started`
  - `tests/shell/test_http_routes.py::TestHandleConnect::test_handle_connect_is_not_readiness_gated_while_warming`
  - `tests/shell/test_http_routes.py::TestHandleConnect::test_handle_connect_accepts_when_ready`
  - `tests/shell/test_http_routes.py::TestHandleDisconnect::test_handle_disconnect_rejects_invalid_token`
  - `tests/shell/test_http_routes.py::TestHandleDisconnect::test_handle_disconnect_rejects_when_gateway_not_started`
  - `tests/shell/test_http_routes.py::TestHandleDisconnect::test_handle_disconnect_removes_connection`
  - `tests/shell/test_http_routes.py::TestHandleDisconnect::test_handle_disconnect_fails_for_nonexistent_connection`
  - `tests/shell/test_http_auth.py::test_http_auth_gate_rejects_missing_authorization_header`
  - `tests/shell/test_http_auth.py::test_http_auth_gate_rejects_malformed_bearer_prefix`
  - `tests/shell/test_http_auth.py::test_http_auth_gate_bypasses_health_path`
- **Highest-risk failure mode**
  - Extracting a shared route skeleton accidentally changes per-route branching: `/connect` becomes readiness-gated, `/status` loses gateway-not-started semantics, `/disconnect` loses connection-not-found behavior, or the `/health` auth exemption leaks into protected routes.

## Inventory notes

- The monkeypatch-heavy downstream seam is real and concentrated around `_open_client_for_server`.
- The `_validate_transport_mode` wrapper has no direct monkeypatch-backed test seam today; that is a coverage gap, not a reason to broaden this tranche.
- The route/auth tranche is protected at three layers: unit route handlers (`test_http_routes.py`), middleware semantics (`test_http_auth.py`), and mounted/wired behavior (`test_gateway.py`, `test_mcp_auth_wire.py`).
