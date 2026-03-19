"""Connections CLI command surface.

Provides the ``tela connections`` command for listing active upstream connections.
"""

from __future__ import annotations

import asyncio
import json
import sys

from tela.shell.gateway import gateway_connections


# @invar:allow dead_export: CLI entrypoint is wired by the command framework.
# @invar:allow shell_result: CLI handler returns int exit code per POSIX convention.
def connections_command(json_output: bool = False) -> int:
    """List active upstream connections.

    Examples:
        >>> connections_command()
        No active connections.
        0

    Args:
        json_output: Whether to output JSON.

    Returns:
        Process exit code.
    """
    try:
        conns_result = asyncio.run(gateway_connections())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if conns_result.is_err:
        print(f"error: {conns_result.error}", file=sys.stderr)
        return 1

    assert conns_result.value is not None
    conns = conns_result.value

    if json_output:
        out = [c.model_dump() for c in conns]
        print(json.dumps(out, indent=2))
    else:
        if not conns:
            print("No active connections.")
        else:
            for c in conns:
                print(
                    f"  {c.connection_id} profile={c.profile_name} since={c.connected_at}"
                )

    return 0
