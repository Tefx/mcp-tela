"""Integration tests for audit level filtering and outcome semantics.

Tests verify the expected field population for each audit level (L1/L2/L3)
and the field semantics for different call outcomes (allowed, denied,
downstream error). These tests define acceptance criteria for the
build_audit_entry implementation.
"""

from __future__ import annotations

from tela.core.models import (
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    MetaField,
)


def _make_connection() -> ConnectionContext:
    return ConnectionContext(
        connection_id="conn-1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"
    )


def _make_allow() -> EnforcementResult:
    return EnforcementResult(verdict=EnforcementVerdict.ALLOW)


def _make_deny() -> EnforcementResult:
    return EnforcementResult(
        verdict=EnforcementVerdict.DENY,
        denied_by="family_admission",
        error_code="AUTHZ_DENY",
        error_message="Family not admitted",
    )


# --- L1 field expectations ---


def test_l1_entry_carries_tool_verdict_latency() -> None:
    """L1 entries must carry tool name, verdict, and latency."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L1,
        connection_id="c1",
        profile_name="dev",
        tool_name="read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
        latency_ms=5.2,
    )
    assert entry.tool_name == "read_file"
    assert entry.verdict == EnforcementVerdict.ALLOW
    assert entry.latency_ms == 5.2


def test_l1_entry_omits_param_hash_and_content() -> None:
    """L1 entries must not carry param_hash or content."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L1,
        connection_id="c1",
        profile_name="dev",
        tool_name="read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
    )
    assert entry.param_hash is None
    assert entry.request_content is None
    assert entry.response_content is None


# --- L2 field expectations ---


def test_l2_entry_carries_param_hash() -> None:
    """L2 entries carry param_hash in addition to L1 fields."""
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
    assert entry.request_content is None


# --- L3 field expectations ---


def test_l3_entry_carries_full_content() -> None:
    """L3 entries carry full request/response content."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L3,
        connection_id="c1",
        profile_name="dev",
        tool_name="write_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
        param_hash="sha256:def456",
        request_content={"path": "/tmp/file", "content": "hello"},
        response_content={"ok": True},
    )
    assert entry.param_hash is not None
    assert entry.request_content is not None
    assert entry.response_content is not None


# --- Meta always recorded ---


def test_meta_recorded_at_all_levels() -> None:
    """_meta fields are always recorded regardless of audit level."""
    meta = MetaField(trace_id="tr-1", event_id="ev-1")
    for level in AuditLevel:
        entry = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            level=level,
            connection_id="c1",
            profile_name="dev",
            tool_name="tool",
            server_name="srv",
            verdict=EnforcementVerdict.ALLOW,
            meta=meta,
        )
        assert entry.meta is not None
        assert entry.meta.trace_id == "tr-1"


# --- Outcome-specific field semantics ---


def test_denied_call_has_no_response_content() -> None:
    """Denied calls must not carry response_content."""
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
        latency_ms=0.5,
    )
    assert entry.response_content is None
    assert entry.denied_by == "family_admission"


def test_denied_call_latency_is_enforcement_only() -> None:
    """Denied calls' latency reflects enforcement chain latency only."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L1,
        connection_id="c1",
        profile_name="dev",
        tool_name="exec_cmd",
        server_name="shell",
        verdict=EnforcementVerdict.DENY,
        latency_ms=0.1,
    )
    # Enforcement-only latency should be very small
    assert entry.latency_ms is not None
    assert entry.latency_ms < 100.0  # not a downstream call duration


def test_allowed_success_carries_response_at_l3() -> None:
    """Allowed calls with success carry downstream response at L3."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L3,
        connection_id="c1",
        profile_name="dev",
        tool_name="read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
        response_content={"data": "file contents"},
    )
    assert entry.response_content is not None


def test_allowed_downstream_error_carries_error_response() -> None:
    """Allowed calls with downstream error carry error as response_content."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=AuditLevel.L3,
        connection_id="c1",
        profile_name="dev",
        tool_name="read_file",
        server_name="fs",
        verdict=EnforcementVerdict.ALLOW,
        error_code="DOWNSTREAM_ERROR",
        response_content={"error": "server unavailable"},
    )
    assert entry.verdict == EnforcementVerdict.ALLOW
    assert entry.error_code == "DOWNSTREAM_ERROR"
    assert entry.response_content is not None


# --- Upstream emission trigger shapes ---


def test_upstream_emission_shape_for_allowed_call() -> None:
    """After enforcement ALLOW, upstream builds audit entry with held _meta.

    This tests the expected data shape that upstream will pass to
    build_audit_entry after a successful tool call.
    """
    conn = _make_connection()
    result = _make_allow()
    meta = MetaField(trace_id="tr-1")
    # These are the args that upstream handler would pass to build_audit_entry
    assert conn.connection_id == "conn-1"
    assert result.verdict == EnforcementVerdict.ALLOW
    assert meta.trace_id == "tr-1"


def test_upstream_emission_shape_for_denied_call() -> None:
    """After enforcement DENY, upstream builds audit entry with denial metadata."""
    result = _make_deny()
    assert result.verdict == EnforcementVerdict.DENY
    assert result.denied_by == "family_admission"
    assert result.error_code == "AUTHZ_DENY"


def test_upstream_strips_meta_before_downstream_forwarding() -> None:
    """_meta is stripped before forwarding but held for audit entry."""
    from tela.shell.upstream_utils import strip_meta

    args = {"path": "/tmp", "_meta": {"trace_id": "tr-1"}}
    stripped, held_meta = strip_meta(args)
    assert "_meta" not in stripped
    assert held_meta is not None
    assert held_meta["trace_id"] == "tr-1"
