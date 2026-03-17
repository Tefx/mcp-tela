"""Audit log writer and reader contracts.

Defines acceptance-only interfaces for audit entry construction, JSONL
writing, and CLI query reading. Actual file I/O implementation is deferred
to audit.runtime.
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
from tela.shell.config_loader import Result


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
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

    Contract stub: raises NotImplementedError.

    Examples:
        >>> from tela.core.models import AuditLevel, ConnectionContext, EnforcementResult, EnforcementVerdict
        >>> build_audit_entry(
        ...     AuditLevel.L1,
        ...     ConnectionContext(connection_id="c1", profile_name="dev", connected_at="2026-01-01T00:00:00Z"),
        ...     "read_file", "fs",
        ...     EnforcementResult(verdict=EnforcementVerdict.ALLOW),
        ... )
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: build_audit_entry pending

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
        AuditEntry once implemented.
    """

    raise NotImplementedError("Contract stub: build_audit_entry pending")


# @invar:allow dead_export: audit wiring is connected in audit.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def audit_write(entry: AuditEntry) -> Result[None, str]:
    """Append an audit entry to the JSONL log file.

    Contract stub: raises NotImplementedError.

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

    Contract stub: raises NotImplementedError.

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

    Contract stub: raises NotImplementedError.

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
