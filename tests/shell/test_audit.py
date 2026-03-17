"""Contract tests for audit log writer and reader stubs."""

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


def _make_connection() -> ConnectionContext:
    return ConnectionContext(
        connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )


def _make_result() -> EnforcementResult:
    return EnforcementResult(verdict=EnforcementVerdict.ALLOW)


# --- build_audit_entry ---

def test_build_audit_entry_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        build_audit_entry(
            AuditLevel.L1, _make_connection(), "read_file", "fs", _make_result()
        )


def test_build_audit_entry_accepts_all_params() -> None:
    """build_audit_entry signature accepts all optional params."""
    with pytest.raises(NotImplementedError):
        build_audit_entry(
            level=AuditLevel.L3,
            connection=_make_connection(),
            tool_name="write_file",
            server_name="fs",
            result=_make_result(),
            latency_ms=42.5,
            arguments={"path": "/tmp"},
            request_content={"path": "/tmp"},
            response_content={"ok": True},
            meta=MetaField(trace_id="tr-1", event_id="ev-1"),
        )


# --- audit_write ---

def test_audit_write_is_contract_stub() -> None:
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L1,
        connection_id="c1",
        profile_name="dev",
        tool_name="read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
    )
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(audit_write(entry))


# --- audit_close ---

def test_audit_close_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(audit_close())


# --- audit_query ---

def test_audit_query_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError, match="Contract stub"):
        asyncio.run(audit_query())


def test_audit_query_accepts_filter_params() -> None:
    with pytest.raises(NotImplementedError):
        asyncio.run(audit_query(since="2026-01-01T00:00:00Z", limit=50))


# --- AuditEntry model tests ---

def test_audit_entry_l1_fields() -> None:
    """L1 entry carries tool name, verdict, and latency."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L1,
        connection_id="c1",
        profile_name="dev",
        tool_name="read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
        latency_ms=10.5,
    )
    assert entry.tool_name == "read_file"
    assert entry.verdict == EnforcementVerdict.ALLOW
    assert entry.latency_ms == 10.5
    assert entry.param_hash is None
    assert entry.request_content is None


def test_audit_entry_l2_fields() -> None:
    """L2 entry carries param_hash in addition to L1 fields."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L2,
        connection_id="c1",
        profile_name="dev",
        tool_name="read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
        param_hash="sha256:abc123",
    )
    assert entry.param_hash == "sha256:abc123"


def test_audit_entry_l3_fields() -> None:
    """L3 entry carries full request/response content."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L3,
        connection_id="c1",
        profile_name="dev",
        tool_name="write_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
        request_content={"path": "/tmp/file"},
        response_content={"ok": True},
    )
    assert entry.request_content == {"path": "/tmp/file"}
    assert entry.response_content == {"ok": True}


def test_audit_entry_with_meta() -> None:
    """Audit entry carries _meta regardless of level."""
    meta = MetaField(trace_id="tr-1", event_id="ev-1")
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L1,
        connection_id="c1",
        profile_name="dev",
        tool_name="read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
        meta=meta,
    )
    assert entry.meta is not None
    assert entry.meta.trace_id == "tr-1"


def test_audit_entry_denied_with_error() -> None:
    """Denied entry carries denial metadata."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L1,
        connection_id="c1",
        profile_name="dev",
        tool_name="exec_cmd",
        server_name="shell",
        verdict=EnforcementVerdict.DENY,
        denied_by="family_admission",
        error_code="AUTHZ_DENY",
    )
    assert entry.verdict == EnforcementVerdict.DENY
    assert entry.denied_by == "family_admission"
    assert entry.error_code == "AUTHZ_DENY"


# --- MetaField model tests ---

def test_meta_field_required_trace_id() -> None:
    meta = MetaField(trace_id="tr-abc")
    assert meta.trace_id == "tr-abc"
    assert meta.event_id is None


def test_meta_field_all_optional_fields() -> None:
    meta = MetaField(
        trace_id="tr-1",
        event_id="ev-1",
        idempotency_key="ik-1",
        instance_id="inst-1",
        persona_id="persona-1",
    )
    assert meta.idempotency_key == "ik-1"
    assert meta.instance_id == "inst-1"
    assert meta.persona_id == "persona-1"


# --- AuditLevel semantics tests ---

def test_audit_level_values() -> None:
    """AuditLevel enum values match spec."""
    assert AuditLevel.L1.value == "L1"
    assert AuditLevel.L2.value == "L2"
    assert AuditLevel.L3.value == "L3"
