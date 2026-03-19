"""Status CLI command surface.

Provides the ``tela status`` command for displaying gateway runtime status.
"""

from __future__ import annotations

import asyncio

from tela.shell.config_loader import Result
from tela.shell.gateway import gateway_status


def status_command(json_output: bool = False) -> Result[int, str]:
    """Display gateway runtime status.

    Examples:
        >>> status_command().is_ok
        True

    Args:
        json_output: Whether to output JSON.

    Returns:
        Result with process exit code.
    """
    run_result = _run_status_command(json_output=json_output)
    if run_result.is_err:
        return Result(error=run_result.error)
    return Result(value=0)


def _run_status_command(json_output: bool) -> Result[None, str]:
    """Execute status command and print output."""

    try:
        status_result = asyncio.run(gateway_status())
    except Exception as exc:
        return Result(error=str(exc))

    if status_result.is_err:
        return Result(error=status_result.error)

    assert status_result.value is not None
    status = status_result.value

    if json_output:
        print(status.model_dump_json(indent=2))
        return Result(value=None)

    print(f"uptime: {status.uptime_seconds:.1f}s")
    print(
        f"servers: {status.server_count} ({', '.join(status.connected_servers) or 'none'})"
    )
    print(f"connections: {status.active_connections}")
    print(f"profiles: {status.profile_count}")
    print(f"tool_calls: {status.total_tool_calls}")
    return Result(value=None)
