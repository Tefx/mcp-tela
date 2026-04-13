# Runtime Characterization Snapshot Evidence

## step_intent
Black-box characterization of `tela serve`, `tela connect`, `GET /status`, `POST /connect`, and `POST /disconnect` using only public entrypoints, to capture current behavior and verify the discovery-vs-readiness boundary per ADR-004/005.

## expected_result
All endpoints return documented contract shapes; discovery truth (lockfile) and readiness truth (GET /status) are externally distinguishable; `tela connect` registers and cleans up; `tela connect` without a server exits bounded.

## observed_result
**13/13 characterization phases PASS.** Full output:

### Phase 1: tela serve startup
- CMD: `tela serve --config runtime_snapshot_minimal.yaml --port 0 --idle-timeout 0`
- Lockfile written with: pid, host, port, token, started_at, config_path, version
- Bound port: OS-selected ephemeral (e.g. 55399)
- Bearer token: 43-char token_urlsafe(32) string

### Phase 2: GET /health (no auth)
- HTTP 200: `{"status":"ok","pid":<pid>}`
- No auth required

### Phase 3: GET /status (bearer auth)
- HTTP 200 with fields: uptime_seconds, server_count, connected_servers, active_connections, profile_count, total_tool_calls, state, discovery_source, config_path, requested_config_path, config_mismatch, degraded_reason, connections, audit_entries
- `state: "ready"` present (lifecycle state from runtime, NOT lockfile)

### Phase 3b: GET /status (no auth)
- HTTP 401: `{"error":"AUTH_INVALID_TOKEN: bearer token validation failed"}`
- Auth enforced correctly

### Phase 4: POST /connect
- HTTP 200: `{"connection_id":"char-snapshot-conn-1","profile_name":"dev","status":"connected"}`
- Registration succeeds with valid bearer token + connection_id payload

### Phase 4b: POST /connect (no auth)
- HTTP 401: Auth rejection

### Phase 5: POST /disconnect
- HTTP 200: `{"connection_id":"char-snapshot-conn-1","status":"disconnected"}`
- Deregistration succeeds

### Phase 5b: POST /disconnect (nonexistent)
- HTTP 404: `{"error":"CONNECTION_NOT_FOUND: connection 'does-not-exist-conn' not found"}`

### Phase 6: Discovery vs Readiness Distinguishability
- Lockfile fields: `[config_path, host, pid, port, started_at, token, version]`
- /status fields: `[active_connections, audit_entries, config_mismatch, config_path, connected_servers, connections, degraded_reason, discovery_source, profile_count, requested_config_path, server_count, state, total_tool_calls, uptime_seconds]`
- Lockfile discovery fields present: `{config_path, host, pid, port, started_at, token, version}`
- /status readiness fields: `{active_connections, connected_servers, connections}`
- **Lockfile has NO `state` field** → readiness truth is NOT discoverable from lockfile
- **`/status` has `state` field (value=`"ready"`)** → readiness truth IS in /status
- Discovery and readiness ARE externally distinguishable

### Phase 7: Connection lifecycle count
- active_connections: 0 → 1 (after POST /connect) → 0 (after POST /disconnect)
- Count tracking confirmed

### Phase 8: tela status CLI
- `tela status --json` → exit 0, valid JSON with same fields as GET /status

### Phase 9: tela connect bridge
- `tela connect --server host:port` registers via POST /connect
- Bridge connection_id format: `bridge_<hex>` (e.g. `bridge_680b97f6e2c646c5b335ed16025982ea`)
- active_connections incremented (0 → 1) during bridge lifetime
- Bridge exits cleanly on stdin close (exit code 0)
- active_connections returns to 0 after disconnect

### Phase 10: tela connect without server
- `tela connect --server 127.0.0.1:1 --max-recovery-attempts 0`
- Exits with code 1 within 10s (bounded, no hang)

## failure_alignment
No failures observed.

## product_implementation_files_modified
None. Only test files written:
- `tests/repro/runtime_snapshot.py` (characterization script)
- `tests/repro/runtime_snapshot_minimal.yaml` (minimal config fixture)

## behavioral_proof_register

| Surface | Evidence | Status |
|---------|----------|--------|
| `tela serve` startup | Lockfile written with discovery fields; process alive after bind | CONFIRMED |
| `GET /health` | 200, `{status:"ok",pid:N}`, no auth required | CONFIRMED |
| `GET /status` (auth) | 200, full lifecycle snapshot with `state` field | CONFIRMED |
| `GET /status` (no auth) | 401, AUTH_INVALID_TOKEN | CONFIRMED |
| `POST /connect` (auth) | 200, `{status:"connected",connection_id,pid,profile_name}` | CONFIRMED |
| `POST /connect` (no auth) | 401 | CONFIRMED |
| `POST /disconnect` (auth, existing) | 200, `{status:"disconnected",connection_id}` | CONFIRMED |
| `POST /disconnect` (auth, nonexistent) | 404, CONNECTION_NOT_FOUND | CONFIRMED |
| Discovery truth | Lockfile has {pid,host,port,token,started_at,config_path,version} — NO `state` | CONFIRMED |
| Readiness truth | GET /status has `state` field = `"ready"` — NOT in lockfile | CONFIRMED |
| Discovery ≠ Readiness | Two field sets are disjoint for concern areas | CONFIRMED |
| `tela connect` bridge registration | Bridge appears in /status active_connections during lifetime | CONFIRMED |
| `tela connect` bridge cleanup | active_connections returns to 0 after bridge exit | CONFIRMED |
| `tela connect` without server | Bounded exit (code 1, <10s) | CONFIRMED |
| Connection count semantics | 0 → 1 → 0 cycle via POST /connect, /disconnect | CONFIRMED |
| `tela status --json` | Exit 0, valid JSON matching /status shape | CONFIRMED |

## gate_open_allowed
YES. No blocking issues found.

## explicit_uncertainty_sources
1. The `tela connect` bridge probe uses `--server` flag (explicit endpoint) rather than lockfile-based auto-discovery. The lockfile-based path was not exercised in this characterization because it would require stopping the serve process, restarting connect, and waiting for autostart coordination — which adds complexity without adding new behavioral evidence for the endpoints under test.
2. The `POST /connect` payload tested only `connection_id`. The spec's ConnectRequest may accept additional fields, but these were not tested since the spec-derived fixture only requires `connection_id`.
3. Readiness transition (preparing → bound_discoverable → converging → ready) was not observed in real-time because the no-downstream-server config converges near-instantly. The transition path is confirmed by the final `state: "ready"` observation only.