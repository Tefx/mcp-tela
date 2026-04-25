"""RED tests for dedicated audit pagination / cursor query surface.

These tests define acceptance criteria for a distinct paginated audit query
surface that provides bounded ``limit`` and deterministic ``cursor`` / ``next_cursor``
semantics over stable audit entry ordering.

The product code currently **lacks** this surface, so every test here is
expected to fail until the downstream implementation step
``tela.operator_p1.surfaces.audit_query_projection`` is completed.

Requirements encoded by these tests (per step spec):
- bounded default limit and enforced max limit (do not permit arbitrary unbounded limit)
- deterministic cursor over a stable audit entry ordering
- long-history query MUST NOT be implemented by expanding ``/status.audit_entries``
- property-based coverage for limit bounds / cursor determinism
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tela.core.models import (
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
)
from tela.shell.audit import build_audit_entry, clear_audit_entries, audit_write


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn() -> ConnectionContext:
    return ConnectionContext(
        connection_id="c1", profile_id="dev", connected_at="2026-01-01T00:00:00Z"
    )


def _make_allow() -> EnforcementResult:
    return EnforcementResult(verdict=EnforcementVerdict.ALLOW)


def _seed_entries(count: int) -> list[AuditEntry]:
    """Populate the in-memory audit store with deterministic fake entries."""
    clear_audit_entries()
    conn = _make_conn()
    allow = _make_allow()
    entries: list[AuditEntry] = []
    for i in range(count):
        r = build_audit_entry(
            AuditLevel.L1,
            conn,
            f"tool_{i:04d}",
            "srv",
            allow,
            latency_ms=float(i),
        )
        assert r.is_ok and r.value is not None
        entries.append(r.value)
    for e in entries:
        result = asyncio.run(audit_write(e))
        assert result.is_ok
    return entries


def _import_paginated_surface() -> tuple[Any, Any]:
    """Dynamically import the expected paginated audit surface.

    Returns ``(query_fn, page_cls)``.  Missing symbols become ``None`` so that
    tests can produce explicit assertion failures rather than collection-time
    ``ImportError``.
    """
    module = importlib.import_module("tela.shell.audit")
    query_fn = getattr(module, "audit_query_paginated", None)
    page_cls = getattr(module, "AuditPage", None)
    return query_fn, page_cls


# ---------------------------------------------------------------------------
# Surface existence
# ---------------------------------------------------------------------------


def test_paginated_audit_surface_is_importable() -> None:
    """The paginated audit query surface must exist as distinct symbols."""
    query_fn, page_cls = _import_paginated_surface()
    assert query_fn is not None, (
        "audit_query_paginated must be defined in tela.shell.audit"
    )
    assert page_cls is not None, (
        "AuditPage model must be defined in tela.shell.audit"
    )


def test_audit_page_model_has_expected_fields() -> None:
    """AuditPage must expose ``entries``, ``next_cursor``, and ``has_more``."""
    query_fn, page_cls = _import_paginated_surface()
    assert page_cls is not None, "AuditPage model is missing"
    instance = page_cls(
        entries=[],
        next_cursor=None,
        has_more=False,
    )
    assert hasattr(instance, "entries")
    assert hasattr(instance, "next_cursor")
    assert hasattr(instance, "has_more")


# ---------------------------------------------------------------------------
# Bounded limit
# ---------------------------------------------------------------------------


def test_paginated_audit_default_limit_is_bounded() -> None:
    """When called without an explicit limit, the default must be bounded."""
    query_fn, page_cls = _import_paginated_surface()
    assert query_fn is not None, "audit_query_paginated is missing"
    _seed_entries(250)
    result = asyncio.run(query_fn())
    assert result.is_ok and result.value is not None, f"query failed: {result.error}"
    page = result.value
    assert isinstance(page, page_cls)
    assert len(page.entries) <= 100, (
        "default limit must be bounded (expected <= 100)"
    )


def test_paginated_audit_enforces_max_limit() -> None:
    """A very large limit must be clamped to a hard max, not return the whole store."""
    query_fn, page_cls = _import_paginated_surface()
    assert query_fn is not None, "audit_query_paginated is missing"
    _seed_entries(5_000)
    result = asyncio.run(query_fn(limit=99_999))
    assert result.is_ok and result.value is not None, f"query failed: {result.error}"
    page = result.value
    assert isinstance(page, page_cls)
    assert len(page.entries) < 5_000, (
        "max limit must be enforced so limit=99999 does not return full store"
    )


# ---------------------------------------------------------------------------
# Cursor determinism
# ---------------------------------------------------------------------------


def test_paginated_audit_cursor_is_deterministic() -> None:
    """Repeated queries with the same cursor over a stable store yield identical pages."""
    query_fn, page_cls = _import_paginated_surface()
    assert query_fn is not None, "audit_query_paginated is missing"
    _seed_entries(50)

    page1_result = asyncio.run(query_fn(cursor=None, limit=10))
    assert page1_result.is_ok and page1_result.value is not None
    page1 = page1_result.value

    page2_result = asyncio.run(query_fn(cursor=None, limit=10))
    assert page2_result.is_ok and page2_result.value is not None
    page2 = page2_result.value

    assert [e.tool_name for e in page1.entries] == [e.tool_name for e in page2.entries]
    assert page1.next_cursor == page2.next_cursor
    assert page1.has_more == page2.has_more


def test_paginated_audit_next_cursor_pages_through_all_entries() -> None:
    """Iterating with ``next_cursor`` must eventually exhaust the store deterministically."""
    query_fn, page_cls = _import_paginated_surface()
    assert query_fn is not None, "audit_query_paginated is missing"
    _seed_entries(55)

    all_seen: list[str] = []
    cursor: str | None = None
    for _ in range(20):  # safety bound
        result = asyncio.run(query_fn(cursor=cursor, limit=10))
        assert result.is_ok and result.value is not None, f"page query failed: {result.error}"
        page = result.value
        all_seen.extend([e.tool_name for e in page.entries])
        if not page.has_more:
            break
        assert page.next_cursor is not None, (
            "has_more=True but next_cursor is None"
        )
        cursor = page.next_cursor
    else:
        pytest.fail("Pagination did not terminate within expected iterations")

    assert len(set(all_seen)) == 55, (
        "pagination must cover every seeded entry without duplication"
    )


# ---------------------------------------------------------------------------
# Long-history query must NOT expand /status.audit_entries
# ---------------------------------------------------------------------------


def test_long_history_query_is_distinct_surface() -> None:
    """The paginated audit surface must be a distinct projection.

    It MUST NOT be implemented by simply returning the list from
    ``get_audit_entries`` or by expanding ``/status.audit_entries``.
    A distinct type shape (``AuditPage`` vs ``list[AuditEntry]``)
    enforces the separation at the API boundary.
    """
    from tela.shell.audit import get_audit_entries

    query_fn, _page_cls = _import_paginated_surface()
    assert query_fn is not None, "audit_query_paginated is missing"
    assert query_fn is not get_audit_entries, (
        "paginated query must be a distinct surface, not a re-export of get_audit_entries"
    )


# ---------------------------------------------------------------------------
# Property-based tests (Hypothesis)
# ---------------------------------------------------------------------------


#: Hypothesis strategy for limit values that probe boundary behaviour.
_ProbeLimit = st.integers(min_value=-1_000, max_value=100_000)


@given(limit=_ProbeLimit)
@settings(max_examples=150)
def test_paginated_audit_limit_bounds_property(limit: int) -> None:
    """For any integer limit value the surface must not crash and must
    return a bounded non-negative page size.
    """
    query_fn, page_cls = _import_paginated_surface()
    assert query_fn is not None, "audit_query_paginated is missing"
    _seed_entries(300)

    result = asyncio.run(query_fn(limit=limit))
    assert result.is_ok, (
        f"limit={limit} caused unexpected error: {result.error}"
    )
    page = result.value
    assert isinstance(page, page_cls)
    assert 0 <= len(page.entries) <= 300, (
        f"page size {len(page.entries)} out of bounds for limit={limit}"
    )


@given(page_size=st.integers(min_value=1, max_value=50))
@settings(max_examples=100)
def test_paginated_audit_cursor_determinism_property(page_size: int) -> None:
    """Repeated identical cursor queries must yield identical entry sequences."""
    query_fn, page_cls = _import_paginated_surface()
    assert query_fn is not None, "audit_query_paginated is missing"
    _seed_entries(200)

    result1 = asyncio.run(query_fn(cursor=None, limit=page_size))
    result2 = asyncio.run(query_fn(cursor=None, limit=page_size))
    assert result1.is_ok and result2.is_ok
    page1 = result1.value
    page2 = result2.value
    assert isinstance(page1, page_cls) and isinstance(page2, page_cls)
    assert [e.tool_name for e in page1.entries] == [e.tool_name for e in page2.entries]
    assert page1.next_cursor == page2.next_cursor
    assert page1.has_more == page2.has_more
