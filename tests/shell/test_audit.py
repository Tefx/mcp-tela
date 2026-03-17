"""Tests for audit entry construction, writer, reader, and model shapes."""

from __future__ import annotations

import asyncio

from tela.core.models import (
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    MetaField,
)
from tela.shell.audit import (
    audit_close,
    audit_query,
    audit_write,
    build_audit_entry,
    clear_audit_entries,
    get_audit_entries,
)


def _conn() -> ConnectionContext:
    return ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )

def _allow() -> EnforcementResult:
    return EnforcementResult(verdict=EnforcementVerdict.ALLOW)

def _deny() -> EnforcementResult:
    return EnforcementResult(
        verdict=EnforcementVerdict.DENY, denied_by="family_admission",
        error_code="AUTHZ_DENY", error_message="Family not admitted",
    )


# --- build_audit_entry: levels ---

def test_build_l1_has_tool_verdict_latency() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "read_file", "fs", _allow(), latency_ms=5.0)
    assert entry.tool_name == "read_file"
    assert entry.verdict == EnforcementVerdict.ALLOW
    assert entry.latency_ms == 5.0

def test_build_l1_omits_param_hash() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "t", "s", _allow(), arguments={"k": "v"})
    assert entry.param_hash is None

def test_build_l1_omits_content() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "t", "s", _allow(), request_content={"k": "v"}, response_content={"ok": True})
    assert entry.request_content is None and entry.response_content is None

def test_build_l2_has_param_hash() -> None:
    entry = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments={"path": "/tmp"})
    assert entry.param_hash is not None and entry.param_hash.startswith("sha256:")

def test_build_l2_omits_content() -> None:
    entry = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments={"k": "v"}, request_content={"k": "v"})
    assert entry.request_content is None

def test_build_l3_has_all_fields() -> None:
    entry = build_audit_entry(AuditLevel.L3, _conn(), "t", "s", _allow(), arguments={"k": "v"}, request_content={"k": "v"}, response_content={"ok": True})
    assert entry.param_hash is not None and entry.request_content is not None and entry.response_content is not None


# --- Meta ---

def test_meta_recorded_at_l1() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "t", "s", _allow(), meta=MetaField(trace_id="tr-1"))
    assert entry.meta is not None and entry.meta.trace_id == "tr-1"


# --- Denied ---

def test_build_denied_carries_metadata() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "exec", "shell", _deny(), latency_ms=0.1)
    assert entry.verdict == EnforcementVerdict.DENY and entry.denied_by == "family_admission"


# --- Param hash ---

def test_param_hash_deterministic() -> None:
    args = {"path": "/tmp", "mode": "r"}
    e1 = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments=args)
    e2 = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments=args)
    assert e1.param_hash == e2.param_hash

def test_param_hash_differs() -> None:
    e1 = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments={"a": 1})
    e2 = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments={"b": 2})
    assert e1.param_hash != e2.param_hash


# --- audit_write / audit_query / audit_close ---

def test_audit_write_stores_entry() -> None:
    clear_audit_entries()
    entry = build_audit_entry(AuditLevel.L1, _conn(), "read_file", "fs", _allow())
    result = asyncio.run(audit_write(entry))
    assert result.is_ok
    assert len(get_audit_entries()) == 1
    assert get_audit_entries()[0].tool_name == "read_file"
    clear_audit_entries()

def test_audit_write_multiple_entries() -> None:
    clear_audit_entries()
    for i in range(3):
        entry = build_audit_entry(AuditLevel.L1, _conn(), f"tool_{i}", "srv", _allow())
        asyncio.run(audit_write(entry))
    assert len(get_audit_entries()) == 3
    clear_audit_entries()

def test_audit_query_returns_entries() -> None:
    clear_audit_entries()
    entry = build_audit_entry(AuditLevel.L1, _conn(), "read_file", "fs", _allow())
    asyncio.run(audit_write(entry))
    result = asyncio.run(audit_query())
    assert result.is_ok and result.value is not None
    assert len(result.value) == 1
    clear_audit_entries()

def test_audit_query_with_limit() -> None:
    clear_audit_entries()
    for i in range(5):
        entry = build_audit_entry(AuditLevel.L1, _conn(), f"tool_{i}", "srv", _allow())
        asyncio.run(audit_write(entry))
    result = asyncio.run(audit_query(limit=2))
    assert result.is_ok and len(result.value) == 2
    clear_audit_entries()

def test_audit_close_succeeds() -> None:
    result = asyncio.run(audit_close())
    assert result.is_ok

def test_clear_audit_entries() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "t", "s", _allow())
    asyncio.run(audit_write(entry))
    clear_audit_entries()
    assert len(get_audit_entries()) == 0


# --- Model tests ---

def test_audit_level_values() -> None:
    assert AuditLevel.L1.value == "L1" and AuditLevel.L2.value == "L2" and AuditLevel.L3.value == "L3"

def test_meta_field_all_optional_fields() -> None:
    meta = MetaField(trace_id="tr-1", event_id="ev-1", idempotency_key="ik-1", instance_id="inst-1", persona_id="persona-1")
    assert meta.idempotency_key == "ik-1"
