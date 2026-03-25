"""Audit log writer and reader.

Implements audit entry construction with level filtering, in-memory
storage with JSONL serialization support, and query/read functionality.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import asyncio
from collections import deque
from pathlib import Path

from tela.core.models import (
    AuditConfig,
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    MetaField,
)
from tela.shell.config_loader import Result


def _compute_param_hash(arguments: dict) -> Result[str, str]:
    """Compute SHA-256 hash of tool arguments for L2+ audit entries."""
    try:
        serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        return Result(error=f"AUDIT_ENTRY_ERROR: cannot serialize arguments: {exc}")
    digest = hashlib.sha256(serialized.encode()).hexdigest()[:16]
    return Result(value=f"sha256:{digest}")


def _now_iso() -> Result[str, str]:
    """Return current UTC timestamp in ISO-8601 format."""
    return Result(value=datetime.now(timezone.utc).isoformat())


# @shell_complexity: entry builder conditionally populates fields by audit level contract.
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
) -> Result[AuditEntry, str]:
    """Build an AuditEntry respecting audit level filtering.

    L1: tool name, verdict, latency
    L2: L1 + parameter hash
    L3: L2 + full request/response content
    _meta fields are always recorded regardless of level.

    Examples:
        >>> from tela.core.models import AuditLevel, ConnectionContext, EnforcementResult, EnforcementVerdict
        >>> conn = ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z")
        >>> allow = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        >>> entry_result = build_audit_entry(AuditLevel.L1, conn, "read_file", "fs", allow, latency_ms=5.0)
        >>> entry_result.is_ok
        True
        >>> entry_result.value.tool_name
        'read_file'
        >>> entry_result.value.param_hash is None
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
        Result[AuditEntry, str] with fields populated per audit level.
    """
    param_hash: str | None = None
    if level in (AuditLevel.L2, AuditLevel.L3) and arguments is not None:
        hash_result = _compute_param_hash(arguments)
        if hash_result.is_err:
            return Result(error=hash_result.error)
        assert hash_result.value is not None
        param_hash = hash_result.value

    now_result = _now_iso()
    if now_result.is_err:
        return Result(error=now_result.error)
    assert now_result.value is not None

    entry_request: dict | None = None
    entry_response: dict | None = None
    if level == AuditLevel.L3:
        entry_request = request_content
        entry_response = response_content

    return Result(
        value=AuditEntry(
            timestamp=now_result.value,
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
    )


# --- In-memory audit store ---

_audit_entries: deque[AuditEntry] = deque(maxlen=10000)
_AUDIT_MAX_ENTRIES: int = 10000
_audit_lock = asyncio.Lock()
_audit_log_path: Path | None = None


async def audit_init(config: "AuditConfig") -> Result[None, str]:
    """Initialize audit subsystem from AuditConfig.

    Sets the module-level audit log path and level. Creates parent
    directory if needed. Backward-compatible: if never called, behavior
    is unchanged (in-memory only, default L2 level).

    Examples:
        >>> import asyncio
        >>> from tela.core.models import AuditConfig, AuditLevel
        >>> r = asyncio.run(audit_init(AuditConfig(level=AuditLevel.L1, output="/tmp/tela-test-audit.jsonl")))
        >>> r.is_ok
        True

    Args:
        config: AuditConfig with level and output path.

    Returns:
        Result[None, str] on success, or error string on failure.
    """
    global _audit_log_path

    async with _audit_lock:
        expanded = Path(config.output).expanduser()
        if not expanded.is_absolute():
            expanded = expanded.resolve()

        try:
            expanded.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return Result(error=f"AUDIT_INIT_ERROR: cannot create directory: {e}")

        _audit_log_path = expanded
    return Result(value=None)


# @shell_orchestration: mutates module-level audit deque under async lock for runtime config.
async def _audit_set_max_entries(max_entries: int) -> None:
    """Set maximum in-memory audit entries (FIFO eviction).

    Examples:
        >>> import asyncio
        >>> asyncio.run(_audit_set_max_entries(100))
    """
    global _audit_entries, _AUDIT_MAX_ENTRIES
    async with _audit_lock:
        _AUDIT_MAX_ENTRIES = max_entries
        old_entries = list(_audit_entries)
        _audit_entries = deque(old_entries[-max_entries:], maxlen=max_entries)


def _get_audit_entries() -> Result[list[AuditEntry], str]:
    """Return all stored audit entries (for testing)."""
    return Result(value=list(_audit_entries))


def _clear_audit_entries() -> None:
    """Clear all stored audit entries."""
    _audit_entries.clear()


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
        >>> entries_result = get_audit_entries()
        >>> entries_result.is_ok
        True
        >>> len(entries_result.value)
        1

    Args:
        entry: Audit entry to write.

    Returns:
        Result[None, str] on success.
    """
    async with _audit_lock:
        _audit_entries.append(entry)

        if _audit_log_path is not None:
            try:
                with open(_audit_log_path, "a") as f:
                    f.write(entry.model_dump_json() + "\n")
            except OSError as e:
                return Result(error=f"AUDIT_WRITE_ERROR: {e}")

    return Result(value=None)


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
    async with _audit_lock:
        _audit_log_path = None
    return Result(value=None)


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
    async with _audit_lock:
        entries = list(_audit_entries)
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            entries = [
                e
                for e in entries
                if datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
                >= since_dt
            ]
        except (ValueError, TypeError):
            return Result(error=f"AUDIT_QUERY_ERROR: invalid timestamp format: {since}")
    return Result(value=entries[-limit:])


# Backward-compatible test hooks.
audit_set_max_entries = _audit_set_max_entries
get_audit_entries = _get_audit_entries
clear_audit_entries = _clear_audit_entries
