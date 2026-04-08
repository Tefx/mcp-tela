## refs Read Confirmation (MANDATORY)

No refs for this step.

## Test Seam Update Report

### Executive Summary

**Verdict: CURRENT MONKEYPATCH PATTERN IS CORRECT**

The downstream wrapper removal step moved `_open_client_for_server` and `_validate_transport_mode` from `downstream.py` to `downstream_clients.py`. After analysis:

1. **Import verification**: `downstream.py` imports these symbols from `downstream_clients`, creating module-level references
2. **Monkeypatch verification**: Python module-level imports create local references that CAN be monkeypatched
3. **Runtime verification**: Direct runtime test confirms monkeypatching `downstream._open_client_for_server` works correctly

### Analysis Results

#### Symbol Existence Verification

```python
# Verified: downstream module has imported references
>>> from tela.shell import downstream
>>> hasattr(downstream, '_open_client_for_server')
True
>>> hasattr(downstream, '_validate_transport_mode')
True
>>> downstream._open_client_for_server.__module__
'tela.shell.downstream_clients'  # Source module confirms import
```

#### Monkeypatch Pattern Analysis

**Current pattern in tests**:
```python
monkeypatch.setattr(downstream, "_open_client_for_server", _fake_open_client_for_server)
```

**Why this is CORRECT**:
1. `downstream.py` line 22-27 imports symbols: `from tela.shell.downstream_clients import _open_client_for_server, ...`
2. This creates a module-level attribute `downstream._open_client_for_server`
3. All code within `downstream.py` calls `_open_client_for_server()` directly (line 188, 715)
4. These calls resolve to the module-level attribute
5. Monkeypatching that attribute intercepts all calls within `downstream` module

#### Test Execution Results

**Tests with `downstream._open_client_for_server` monkeypatch**:

| Test Name | Status | Reason |
|-----------|--------|--------|
| `test_recover_server_client_fails_closed_when_server_removed_mid_recovery` | ✅ PASS | Has runtime config setup |
| `test_recover_server_client_config_remove_cleans_stale_client_and_lock` | ✅ PASS | Has runtime config setup |
| `test_recover_server_client_releases_registry_lock_around_network_io` | ✅ PASS | Has runtime config setup |
| `test_recover_server_client_success_closes_replaced_client` | ✅ PASS | Has runtime config setup |
| `test_recover_server_client_rejects_material_config_change_before_swap` | ✅ PASS | Has runtime config setup |
| `test_handle_reconnect_calls_enumerate_once` | ❌ FAIL | **Pre-existing issue: Missing runtime config** |
| `test_handle_reconnect_passes_enumerated_tools_to_on_server_reconnect` | ❌ FAIL | **Pre-existing issue: Missing runtime config** |
| `test_handle_reconnect_swaps_client_before_enumeration` | ❌ FAIL | **Pre-existing issue: Missing runtime config** |

**Key finding**: Failing tests also fail on main branch (before wrapper removal). This is a PRE-EXISTING issue unrelated to wrapper removal.

#### Pre-existing Failures (Not Caused by This Step)

The failing `test_handle_reconnect_*` tests lack runtime config setup. Verified by minimal repro:

```python
# WITHOUT runtime config - monkeypatch NOT called
set_runtime_config(TelaConfig(servers={}))  
# Logs: "Runtime config unavailable" → recovery not attempted

# WITH runtime config - monkeypatch IS called
set_runtime_config(TelaConfig(servers={"srv": ServerConfig(...)}))
# Logs: enumeration happens → monkeypatch receives call
```

#### Tests That Patch `downstream._validate_transport_mode`

**None found.** Grep search confirms zero test files monkeypatch this symbol.

### File-Level Verification

**Monkeypatch locations in tests**:

| File | Line | Symbol | Pattern | Status |
|------|------|--------|---------|--------|
| `tests/shell/test_downstream.py` | 1112, 1184, 1262, 1335, 1414, 1494, 1579, 1700 | `_open_client_for_server` | `downstream._open_client_for_server` | ✅ CORRECT |
| `tests/shell/test_downstream_runtime_connection.py` | 287 | `_open_client_for_server` | `downstream._open_client_for_server` | ✅ CORRECT |
| `tests/repro/test_adr006_runtime_hardening_probes.py` | 534, 554, 581, 594, 611 | `_open_client_for_server` | Direct assignment + module reference | ✅ CORRECT |

### Runtime Verification

Confirmed with minimal reproduction that monkeypatch pattern works:

```python
# In downstream.py: from tela.shell.downstream_clients import _open_client_for_server
# In test: downstream._open_client_for_server = fake_func
# Result: All calls in downstream module use fake_func ✅
```

### Conclusion

**No changes required to monkeypatch patterns.** 

The wrapper removal correctly preserved the seam by importing the symbols into `downstream.py`. This creates module-level attributes that:
1. Can be monkeypatched via `monkeypatch.setattr(downstream, "_open_client_for_server", ...)`
2. Intercept ALL calls to `_open_client_for_server` within the `downstream` module
3. Are the authoritative location for tests to patch

**Alternative pattern considered and rejected**:
```python
monkeypatch.setattr("tela.shell.downstream_clients._open_client_for_server", ...)
```
This would NOT work because `downstream.py` imports the symbol once at module load, creating a local reference that is NOT updated by patching the source module.

### Pre-existing Issues (Out of Scope)

The failing `test_handle_reconnect_*` tests need runtime config initialization (like passing tests have). This is a separate test correctness issue, not a monkeypatch seam issue.