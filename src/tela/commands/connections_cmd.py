"""Connections CLI command surface.

Provides the ``tela connections`` command for listing active upstream connections.
"""

from __future__ import annotations

import asyncio
import json

from tela.shell.config_loader import Result
from tela.shell.gateway import gateway_connections


def connections_command(json_output: bool = False) -> Result[int, str]:
    """List active upstream connections.

    Examples:
        >>> connections_command().is_ok
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

    try:
        conns_result = asyncio.run(gateway_connections())
    except Exception as exc:
        return Result(error=str(exc))

    if conns_result.is_err:
        return Result(error=conns_result.error)

    assert conns_result.value is not None
    conns = conns_result.value
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
