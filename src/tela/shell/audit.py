"""Audit log writer and reader.

Implements audit entry construction with level filtering, JSONL writing,
and CLI query reading. Level semantics:
- L1: tool name, verdict, latency
- L2: L1 + parameter hash
- L3: L2 + full request/response content
- _meta fields are always recorded regardless of level
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from tela.core.models import (
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    MetaField,
)
from tela.shell.config_loader import Result


# @invar:allow shell_result: returns hash string, pure data shaping not I/O.
def _compute_param_hash(arguments: dict) -> str:
    """Compute SHA-256 hash of tool arguments for L2+ audit entries.

    Examples:
        >>> _compute_param_hash({"path": "/tmp"})
        'sha256:...'
    """
    serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode()).hexdigest()[:16]
    return f"sha256:{digest}"


# @invar:allow shell_result: returns timestamp string, minimal I/O.
def _now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
# @invar:allow shell_result: returns AuditEntry per DESIGN.md spec, data shaping not I/O.
def build_audit_entry(
    level: AuditLevel,
    connection: ConnectionContext,
    tool_name: str,
    server_name: str,
    result: EnforcementResult,
    latency_ms: float | None = None,
    arguments: dict | None = None,
    request_content: dict | None = None,
    response_content: dict | None = None,
    meta: MetaField | None = None,
) -> AuditEntry:
    """Build an AuditEntry respecting audit level filtering.

    L1: tool name, verdict, latency
    L2: L1 + parameter hash
    L3: L2 + full request/response content

    _meta fields are always recorded regardless of level.

    Examples:
        >>> from tela.core.models import AuditLevel, ConnectionContext, EnforcementResult, EnforcementVerdict
        >>> conn = ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z")
        >>> allow = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        >>> entry = build_audit_entry(AuditLevel.L1, conn, "read_file", "fs", allow, latency_ms=5.0)
        >>> entry.tool_name
        'read_file'
        >>> entry.verdict
        <EnforcementVerdict.ALLOW: 'allow'>
        >>> entry.param_hash is None
        True

    Args:
        level: Audit level for field filtering.
        connection: Connection context for the tool call.
        tool_name: Name of the tool called.
        server_name: Name of the downstream server.
        result: Enforcement chain result.
        latency_ms: Call latency in milliseconds.
        arguments: Tool arguments (for param_hash at L2+).
        request_content: Full request content (for L3).
        response_content: Full response content (for L3).
        meta: Held _meta from the tool call arguments.

    Returns:
        AuditEntry with fields populated per audit level.
    """

    # L2+: compute param_hash from arguments
    param_hash: str | None = None
    if level in (AuditLevel.L2, AuditLevel.L3) and arguments is not None:
        param_hash = _compute_param_hash(arguments)

    # L3: include full content
    entry_request: dict | None = None
    entry_response: dict | None = None
    if level == AuditLevel.L3:
        entry_request = request_content
        entry_response = response_content

    return AuditEntry(
        timestamp=_now_iso(),
        level=level,
        connection_id=connection.connection_id,
        profile_name=connection.profile_name,
        tool_name=tool_name,
        server_name=server_name,
        verdict=result.verdict,
        denied_by=result.denied_by,
        error_code=result.error_code,
        latency_ms=latency_ms,
        param_hash=param_hash,
        request_content=entry_request,
        response_content=entry_response,
        meta=meta,
    )


# Module-level state for JSONL writer
_audit_log_path: Path | None = None
_audit_log_file = None


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def audit_write(entry: AuditEntry) -> Result[None, str]:
    """Append an audit entry to the JSONL log file.

    Contract stub: actual file I/O deferred to integration step.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import AuditEntry, AuditLevel, EnforcementVerdict
        >>> entry = AuditEntry(
        ...     timestamp="2026-01-01T00:00:00Z", level=AuditLevel.L1,
        ...     connection_id="c1", profile_name="dev",
        ...     tool_name="read_file", server_name="fs",
        ...     verdict=EnforcementVerdict.ALLOW,
        ... )
        >>> asyncio.run(audit_write(entry))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: audit_write pending

    Args:
        entry: Audit entry to write.

    Returns:
        Result[None, str] once implemented.
    """

    raise NotImplementedError("Contract stub: audit_write pending")


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
async def audit_close() -> Result[None, str]:
    """Flush and close the audit log file.

    Contract stub: actual file I/O deferred to integration step.

    Examples:
        >>> import asyncio
        >>> asyncio.run(audit_close())
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: audit_close pending

    Returns:
        Result[None, str] once implemented.
    """

    raise NotImplementedError("Contract stub: audit_close pending")


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def audit_query(
    since: str | None = None,
    limit: int = 100,
) -> Result[list[AuditEntry], str]:
    """Query audit log entries for CLI display.

    Contract stub: actual file I/O deferred to integration step.

    Examples:
        >>> import asyncio
        >>> asyncio.run(audit_query())
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: audit_query pending

    Args:
        since: ISO-8601 timestamp or relative duration filter.
        limit: Maximum entries to return.

    Returns:
        Result[list[AuditEntry], str] once implemented.
    """

    raise NotImplementedError("Contract stub: audit_query pending")
