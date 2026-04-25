# P1 Operator Scope Decision: Authority and Source Discovery

## Scope decision

This is a planning-only record. No implementation was performed.

Tela owns the gateway/profile authorization boundary, audit records, bridge/client diagnostic attachment records, status/doctor operator diagnostics, and enforcement of canonical `CapabilityToken.profile_id` against configured tela profiles. Tela does not own `PersonaSpec`, `JobSpec`, agent runtime control, runtime approval workflow, or runtime read-only semantics.

No RBAC is added or planned. The existing authorization source remains profile capabilities plus tool classification/posture enforcement.

## Canonical authority read

- `../opifex/design/final-canonical-contract.md` — Ownership Matrix: `PersonaSpec` is owned by opifex/larva and not directly consumed by tela; profile registry is owned by tela; `CapabilityToken` is opifex/nervus issued and tela-verified; runtime mutation/read-only policy is `JobSpec.runtime_controls` and applied by anima, not tela; anima P0 operator controls are opifex/anima, not tela.
- `../opifex/design/final-canonical-contract.md` — Hard Invariants: one shared persona capability field is `capabilities`; one gateway profile identity/binding field is `profile_id`; shared MCP surfaces are snake_case without alias payload keys; `tela_list_profiles` is an MCP tool; anima switches do not mutate `PersonaSpec`, `CapabilityToken`, `JobSpec`, or tela profile bindings.
- `docs/INTERFACES.md` — §7.2a requested by task, but no heading or text matching `7.2a` exists in this worktree. Nearest controlling passages read: §7.2 HTTP Endpoints says `GET /status` is runtime readiness authority and `POST /connect` is lifecycle plumbing only; §7.2.2 `GET /status` Response Schema defines authoritative status fields; §10.1 Audit defines audit limit behavior.
- `docs/INTERFACES.md` — §1 Purpose: tela is the MCP gateway/authorization layer and does not own persona identity, runtime approval workflow, or runtime read-only mode semantics.
- `docs/DESIGN.md` — Design Boundary and Ownership Rules: tela scope is downstream aggregation, profile authorization, tool classification, connection binding, and audit; `core/` owns authorization semantics; `shell/` owns transport/process lifecycle; CLI commands delegate rather than define authorization rules.

## P1 target checklist

- `audit_query`: clarify cursor/limit semantics without changing audit meaning. Current source is `src/tela/shell/audit.py::audit_query(since, limit)` plus `src/tela/commands/audit_cmd.py::audit_command/_run_audit_command/_filter_entries`; docs show default query limit 100 and no max limit.
- `active_probe`: keep active probing explicit and passive status non-mutating. Current source is `src/tela/commands/status_cmd.py::status_command/_probe_status_runtime` and `src/tela/commands/doctor_cmd.py::_probe_doctor_runtime`; docs say `status --probe` checks only the current lockfile endpoint and does not cold-start.
- `client_attachments`: surface attachment diagnostics from existing ADR-008 registry state, not from readiness guesses. Current source is `src/tela/shell/adr008_registry_events.py::{read_attachment_registry, upsert_client_attachment, append_runtime_event, read_runtime_events}`, `src/tela/commands/status_cmd.py::_read_status_attachments`, and `src/tela/commands/doctor_cmd.py::_read_doctor_attachment_summary`.
- `recover_decision`: keep mutation behind explicit doctor recovery and bounded bridge recovery; status remains read-only. Current source is `src/tela/commands/doctor_cmd.py::_recover_doctor_runtime`, `src/tela/commands/connect_bridge.py::{is_recoverable_error,recover_gateway,_recover_bridge_transport_state,_run_bridge_cycle}`, and `src/tela/core/adr008_status.py::{classify_status_recoverability,make_status_recommendation}`.
- `authorization_explain`: explain the existing 3-step profile authorization decision without adding RBAC or alternate policy layers. Current source is `src/tela/core/enforcement.py::{check_family_admission,check_tool_override,check_posture,enforce}`, `src/tela/shell/upstream.py::handle_tools_call`, and `docs/INTERFACES.md` §6.2 plus Authorization denial error semantics.

## Source map

- status: `src/tela/commands/status_cmd.py::StatusDiscovery`, `ProbeObservation`, `ADR008StatusResult`, `status_command`, `_read_status_discovery`, `_read_status_attachments`, `_probe_status_runtime`; `src/tela/shell/http_routes.py::handle_status`; `src/tela/shell/gateway_runtime.py::get_runtime_status_snapshot`; `src/tela/core/adr008_status.py::classify_shared_runtime_state`, `classify_status_recoverability`, `make_status_recommendation`.
- audit: `src/tela/shell/audit.py::build_audit_entry`, `audit_init`, `audit_write`, `audit_query`, `_get_audit_entries`; `src/tela/commands/audit_cmd.py::audit_command`, `_run_audit_command`, `_filter_entries`; `src/tela/shell/upstream.py::_audit_tool_call`, `_audit_initialize_rejection`.
- clients: `src/tela/commands/connect_cmd.py::connect_command`, `_resolve_endpoint`, `_resolve_connect_token`, `_resolve_client_kind`; `src/tela/commands/connect_bridge.py::run_bridge`, `_heartbeat_attachment_best_effort`, `_record_runtime_event_best_effort`, `_register_bridge_connection`, `_teardown_bridge_connection`; `src/tela/shell/adr008_registry_events.py::read_attachment_registry`, `write_attachment_registry`, `upsert_client_attachment`, `append_runtime_event`, `read_runtime_events`; `src/tela/shell/connection_lifecycle.py::cleanup_connection_by_id`.
- probe: `src/tela/commands/status_cmd.py::_probe_status_runtime`; `src/tela/commands/doctor_cmd.py::_probe_doctor_runtime`; `src/tela/commands/remote_state.py::query_remote_state`, `_fetch_status_payload`; `src/tela/commands/connect_bridge.py::_get_gateway_status`, `_wait_for_gateway_readiness`.
- recovery: `src/tela/commands/doctor_cmd.py::_recover_doctor_runtime`; `src/tela/commands/connect_bridge.py::is_recoverable_error`, `recover_gateway`, `_recover_bridge_transport_state`, `_recover_inflight_transport`, `_run_bridge_cycle`; `src/tela/shell/_downstream_recovery.py::_recover_server_client`, `_acquire_recovery_lock`, `_prune_recovery_lock_if_unused`, `_emit_recovery_diagnostic`, `call_tool`; `src/tela/shell/downstream.py::connect_all`, `disconnect_all`, `re_enumerate`.
- authorization: `src/tela/core/enforcement.py::posture_le`, `check_family_admission`, `check_tool_override`, `check_posture`, `enforce`; `src/tela/core/token.py`; `src/tela/shell/upstream.py::resolve_initialize_profile_binding`, `handle_initialize`, `handle_tools_list`, `handle_tools_call`, `_extract_capability_token`, `_invalid_reserved_client_info_key`; `src/tela/shell/http_auth.py`; `src/tela/shell/gateway_http_auth.py`.

## Minimal complexity receipt

1. Parts added: one planning evidence file only.
2. Simplest alternative: no file, only final chat evidence.
3. Defense: the task requires a produced P1 target checklist and exact source discovery record; a single evidence file makes the result reviewable and deletable without changing code.
