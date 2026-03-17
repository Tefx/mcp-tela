"""Tests for audit entry construction, writer/reader stubs, and model shapes."""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import (
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    MetaField,
)
from tela.shell.audit import audit_close, audit_query, audit_write, build_audit_entry


def _conn() -> ConnectionContext:
    return ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )


def _allow() -> EnforcementResult:
    return EnforcementResult(verdict=EnforcementVerdict.ALLOW)


def _deny() -> EnforcementResult:
    return EnforcementResult(
        verdict=EnforcementVerdict.DENY,
        denied_by="family_admission",
        error_code="AUTHZ_DENY",
        error_message="Family not admitted",
    )


# --- build_audit_entry: L1 ---

def test_build_l1_has_tool_verdict_latency() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "read_file", "fs", _allow(), latency_ms=5.0)
    assert entry.tool_name == "read_file"
    assert entry.verdict == EnforcementVerdict.ALLOW
    assert entry.latency_ms == 5.0

def test_build_l1_omits_param_hash() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "t", "s", _allow(), arguments={"k": "v"})
    assert entry.param_hash is None

def test_build_l1_omits_content() -> None:
    entry = build_audit_entry(
        AuditLevel.L1, _conn(), "t", "s", _allow(),
        request_content={"k": "v"}, response_content={"ok": True},
    )
    assert entry.request_content is None
    assert entry.response_content is None


# --- build_audit_entry: L2 ---

def test_build_l2_has_param_hash() -> None:
    entry = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments={"path": "/tmp"})
    assert entry.param_hash is not None
    assert entry.param_hash.startswith("sha256:")

def test_build_l2_omits_content() -> None:
    entry = build_audit_entry(
        AuditLevel.L2, _conn(), "t", "s", _allow(),
        arguments={"k": "v"}, request_content={"k": "v"}, response_content={"ok": True},
    )
    assert entry.request_content is None
    assert entry.response_content is None

def test_build_l2_no_args_no_hash() -> None:
    entry = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow())
    assert entry.param_hash is None


# --- build_audit_entry: L3 ---

def test_build_l3_has_all_fields() -> None:
    entry = build_audit_entry(
        AuditLevel.L3, _conn(), "t", "s", _allow(),
        arguments={"k": "v"}, request_content={"k": "v"}, response_content={"ok": True},
    )
    assert entry.param_hash is not None
    assert entry.request_content == {"k": "v"}
    assert entry.response_content == {"ok": True}


# --- Meta always recorded ---

def test_meta_recorded_at_l1() -> None:
    meta = MetaField(trace_id="tr-1")
    entry = build_audit_entry(AuditLevel.L1, _conn(), "t", "s", _allow(), meta=meta)
    assert entry.meta is not None
    assert entry.meta.trace_id == "tr-1"

def test_meta_recorded_at_l3() -> None:
    meta = MetaField(trace_id="tr-1", event_id="ev-1")
    entry = build_audit_entry(AuditLevel.L3, _conn(), "t", "s", _allow(), meta=meta)
    assert entry.meta is not None


# --- Denied call semantics ---

def test_build_denied_carries_denial_metadata() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "exec", "shell", _deny(), latency_ms=0.1)
    assert entry.verdict == EnforcementVerdict.DENY
    assert entry.denied_by == "family_admission"
    assert entry.error_code == "AUTHZ_DENY"

def test_build_denied_no_response_content() -> None:
    entry = build_audit_entry(AuditLevel.L3, _conn(), "exec", "shell", _deny())
    assert entry.response_content is None


# --- Timestamp ---

def test_build_entry_has_timestamp() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "t", "s", _allow())
    assert entry.timestamp is not None
    assert "T" in entry.timestamp  # ISO-8601 format


# --- Connection fields ---

def test_build_entry_carries_connection_fields() -> None:
    entry = build_audit_entry(AuditLevel.L1, _conn(), "t", "s", _allow())
    assert entry.connection_id == "c1"
    assert entry.profile_name == "dev"


# --- Param hash determinism ---

def test_param_hash_deterministic() -> None:
    args = {"path": "/tmp", "mode": "r"}
    e1 = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments=args)
    e2 = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments=args)
    assert e1.param_hash == e2.param_hash

def test_param_hash_differs_for_different_args() -> None:
    e1 = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments={"a": 1})
    e2 = build_audit_entry(AuditLevel.L2, _conn(), "t", "s", _allow(), arguments={"b": 2})
    assert e1.param_hash != e2.param_hash


# --- Writer/reader stubs ---

def test_audit_write_is_contract_stub() -> None:
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z", level=AuditLevel.L1,
        connection_id="c1", profile_name="dev",
        tool_name="t", server_name="s", verdict=EnforcementVerdict.ALLOW,
    )
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(audit_write(entry))

def test_audit_close_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(audit_close())

def test_audit_query_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(audit_query())

def test_audit_query_accepts_filter_params() -> None:
    with pytest.raises(NotImplementedError):
        asyncio.run(audit_query(since="2026-01-01T00:00:00Z", limit=50))


# --- AuditEntry model tests ---

def test_audit_entry_with_meta() -> None:
    meta = MetaField(trace_id="tr-1", event_id="ev-1")
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z", level=AuditLevel.L1,
        connection_id="c1", profile_name="dev",
        tool_name="t", server_name="s", verdict=EnforcementVerdict.ALLOW,
        meta=meta,
    )
    assert entry.meta is not None

def test_meta_field_all_optional_fields() -> None:
    meta = MetaField(
        trace_id="tr-1", event_id="ev-1", idempotency_key="ik-1",
        instance_id="inst-1", persona_id="persona-1",
    )
    assert meta.idempotency_key == "ik-1"

def test_audit_level_values() -> None:
    assert AuditLevel.L1.value == "L1"
    assert AuditLevel.L2.value == "L2"
    assert AuditLevel.L3.value == "L3"
