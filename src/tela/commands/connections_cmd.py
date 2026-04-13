"""Connections CLI command surface.

Provides the ``tela connections`` command for listing active upstream connections.
"""

from __future__ import annotations
import json

from tela.shell.result import Result
from tela.commands.remote_state import query_remote_state


def connections_command(json_output: bool = False) -> Result[int, str]:
    """List active upstream connections.

    Examples:
        >>> callable(connections_command)
        True

    Args:
        json_output: Whether to output JSON.

    Returns:
        Result with process exit code.
    """
    result = _run_connections_command(json_output=json_output)
    if result.is_err:
        return Result(error=result.error)
    return Result(value=0)


# @shell_complexity: command handles transport errors and two output shapes.
def _run_connections_command(json_output: bool) -> Result[None, str]:
    """Execute connections command and print output."""

    remote_state_result = query_remote_state()
    if remote_state_result.is_err:
        return Result(error=remote_state_result.error)
    assert remote_state_result.value is not None
    conns = remote_state_result.value.connections
    if json_output:
        print(json.dumps([c.model_dump() for c in conns], indent=2))
        return Result(value=None)

    if not conns:
        print("No active connections.")
        return Result(value=None)

    for connection in conns:
        print(
            f"  {connection.connection_id} "
            f"profile={connection.profile_name} "
            f"since={connection.connected_at}"
        )
    return Result(value=None)
