"""Audit CLI command surface.

Provides the ``tela audit`` command for querying audit log entries.
"""

from __future__ import annotations

import asyncio

from tela.shell.config_loader import Result
from tela.shell.audit import audit_query


def audit_command(
    since: str | None = None,
    limit: int = 100,
    json_output: bool = False,
) -> Result[int, str]:
    """Query and display audit log entries.

    Examples:
        >>> from tela.shell.audit import clear_audit_entries
        >>> clear_audit_entries()
        >>> audit_command(limit=0).is_ok
        True

    Args:
        since: ISO-8601 timestamp or relative duration filter.
        limit: Maximum entries to return.
        json_output: Whether to output JSON.

    Returns:
        Result with process exit code.
    """
    run_result = _run_audit_command(since=since, limit=limit, json_output=json_output)
    if run_result.is_err:
        return Result(error=run_result.error)
    return Result(value=0)


# @shell_complexity: command handles query errors and dual output formatting.
def _run_audit_command(
    since: str | None,
    limit: int,
    json_output: bool,
) -> Result[None, str]:
    """Execute audit query and render entries."""

    result = asyncio.run(audit_query(since=since, limit=limit))

    if result.is_err:
        return Result(error=result.error)

    assert result.value is not None
    entries = result.value

    if json_output:
        for entry in entries:
            print(entry.model_dump_json())
        return Result(value=None)

    for entry in entries:
        verdict = entry.verdict.value.upper()
        print(
            f"[{entry.timestamp}] {verdict} {entry.tool_name} ({entry.server_name}) profile={entry.profile_name}"
        )
    return Result(value=None)
