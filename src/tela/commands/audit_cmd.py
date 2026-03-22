"""Audit CLI command surface.

Provides the ``tela audit`` command for querying audit log entries.
"""

from __future__ import annotations

from datetime import datetime

from tela.core.models import AuditEntry
from tela.commands.remote_state import query_remote_state
from tela.shell.config_loader import Result


def audit_command(
    since: str | None = None,
    limit: int = 100,
    json_output: bool = False,
) -> Result[int, str]:
    """Query and display audit log entries.

    Examples:
        >>> callable(audit_command)
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

    remote_state_result = query_remote_state()
    if remote_state_result.is_err:
        return Result(error=remote_state_result.error)
    assert remote_state_result.value is not None

    entries_result = _filter_entries(
        entries=remote_state_result.value.audit_entries,
        since=since,
        limit=limit,
    )
    if entries_result.is_err:
        return Result(error=entries_result.error)
    assert entries_result.value is not None
    entries = entries_result.value

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


def _filter_entries(
    *, entries: list[AuditEntry], since: str | None, limit: int
) -> Result[list[AuditEntry], str]:
    """Apply ``since`` and ``limit`` filtering to audit entries."""

    filtered = entries
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return Result(error=f"AUDIT_QUERY_ERROR: invalid timestamp format: {since}")

        filtered = [
            entry
            for entry in entries
            if datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
            >= since_dt
        ]

    return Result(value=filtered[-limit:])
