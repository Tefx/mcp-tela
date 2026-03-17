"""Audit log writer and reader.

Implements audit entry construction with level filtering, in-memory
storage with JSONL serialization support, and query/read functionality.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from tela.core.models import (
    AuditConfig,
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
    """Compute SHA-256 hash of tool arguments for L2+ audit entries."""
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
        >>> entry.param_hash is None
        True

    Args:
        level: Audit level for field filtering.
        connection: Connection context.
        tool_name: Tool name.
        server_name: Server name.
        result: Enforcement result.
        latency_ms: Latency in ms.
        arguments: Tool arguments (for param_hash at L2+).
        request_content: Request content (for L3).
        response_content: Response content (for L3).
        meta: Held _meta.

    Returns:
        AuditEntry with fields populated per audit level.
    """
    param_hash: str | None = None
    if level in (AuditLevel.L2, AuditLevel.L3) and arguments is not None:
        param_hash = _compute_param_hash(arguments)

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


# --- In-memory audit store ---

_audit_entries: list[AuditEntry] = []
_audit_log_path: Path | None = None
_audit_level: AuditLevel = AuditLevel.L2


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
def audit_init(config: "AuditConfig") -> Result[None, str]:
    """Initialize audit subsystem from AuditConfig.

    Sets the module-level audit log path and level. Creates parent
    directory if needed. Backward-compatible: if never called, behavior
    is unchanged (in-memory only, default L2 level).

    Examples:
        >>> from tela.core.models import AuditConfig, AuditLevel
        >>> r = audit_init(AuditConfig(level=AuditLevel.L1, output="/tmp/tela-test-audit.jsonl"))
        >>> r.is_ok
        True

    Args:
        config: AuditConfig with level and output path.

    Returns:
        Result[None, str] on success, or error string on failure.
    """
    global _audit_log_path, _audit_level

    _audit_level = config.level

    expanded = Path(config.output).expanduser()
    if not expanded.is_absolute():
        expanded = expanded.resolve()

    try:
        expanded.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return Result(error=f"AUDIT_INIT_ERROR: cannot create directory: {e}")

    _audit_log_path = expanded
    return Result(value=None)


# @invar:allow dead_export: audit accessor used by tests and integration.
# @invar:allow shell_result: returns list, not a failable I/O boundary.
def get_audit_entries() -> list[AuditEntry]:
    """Return all stored audit entries (for testing)."""
    return list(_audit_entries)


# @invar:allow dead_export: audit accessor used by tests and integration.
# @invar:allow shell_result: returns None, state reset not failable I/O.
def clear_audit_entries() -> None:
    """Clear all stored audit entries."""
    _audit_entries.clear()


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
async def audit_write(entry: AuditEntry) -> Result[None, str]:
    """Append an audit entry to the in-memory store and optional JSONL file.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import AuditEntry, AuditLevel, EnforcementVerdict
        >>> clear_audit_entries()
        >>> entry = AuditEntry(
        ...     timestamp="2026-01-01T00:00:00Z", level=AuditLevel.L1,
        ...     connection_id="c1", profile_name="dev",
        ...     tool_name="read_file", server_name="fs",
        ...     verdict=EnforcementVerdict.ALLOW,
        ... )
        >>> r = asyncio.run(audit_write(entry))
        >>> r.is_ok
        True
        >>> len(get_audit_entries())
        1

    Args:
        entry: Audit entry to write.

    Returns:
        Result[None, str] on success.
    """
    _audit_entries.append(entry)

    if _audit_log_path is not None:
        try:
            with open(_audit_log_path, "a") as f:
                f.write(entry.model_dump_json() + "\n")
        except OSError as e:
            return Result(error=f"AUDIT_WRITE_ERROR: {e}")

    return Result(value=None)


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
async def audit_close() -> Result[None, str]:
    """Flush and close the audit log.

    Resets the audit log path to None (disabling disk persistence).

    Examples:
        >>> import asyncio
        >>> r = asyncio.run(audit_close())
        >>> r.is_ok
        True

    Returns:
        Result[None, str] always succeeds.
    """
    global _audit_log_path
    _audit_log_path = None
    return Result(value=None)


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
async def audit_query(
    since: str | None = None,
    limit: int = 100,
) -> Result[list[AuditEntry], str]:
    """Query audit log entries.

    Filters by timestamp (if since provided) and limits results.

    Examples:
        >>> import asyncio
        >>> r = asyncio.run(audit_query(limit=10))
        >>> r.is_ok
        True
        >>> r.value is not None
        True

    Args:
        since: ISO-8601 timestamp filter (entries after this time).
        limit: Maximum entries to return.

    Returns:
        Result[list[AuditEntry], str] with matching entries.
    """
    entries = _audit_entries
    if since is not None:
        entries = [e for e in entries if e.timestamp >= since]
    return Result(value=entries[-limit:])
