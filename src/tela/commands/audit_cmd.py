"""Audit CLI command surface.

Provides the ``tela audit`` command for querying audit log entries.
"""

from __future__ import annotations

import asyncio

from tela.shell.audit import audit_query


# @invar:allow dead_export: CLI entrypoint is wired by the command framework.
# @invar:allow shell_result: CLI handler returns int exit code per POSIX convention.
# @shell_complexity: command must handle query errors and two output formats.
def audit_command(
    since: str | None = None,
    limit: int = 100,
    json_output: bool = False,
) -> int:
    """Query and display audit log entries.

    Examples:
        >>> from tela.shell.audit import clear_audit_entries
        >>> clear_audit_entries()
        >>> audit_command(limit=0)
        0

    Args:
        since: ISO-8601 timestamp or relative duration filter.
        limit: Maximum entries to return.
        json_output: Whether to output JSON.

    Returns:
        Process exit code.
    """
    result = asyncio.run(audit_query(since=since, limit=limit))

    if result.is_err:
        import sys

        print(f"error: {result.error}", file=sys.stderr)
        return 1

    assert result.value is not None
    entries = result.value

    if json_output:

        for entry in entries:
            print(entry.model_dump_json())
    else:
        for entry in entries:
            verdict = entry.verdict.value.upper()
            print(
                f"[{entry.timestamp}] {verdict} {entry.tool_name} ({entry.server_name}) profile={entry.profile_name}"
            )

    return 0
