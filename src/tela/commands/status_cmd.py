"""Status CLI command surface.

Provides the ``tela status`` command for displaying gateway runtime status.
"""

from __future__ import annotations

import asyncio
import sys

from tela.shell.gateway import gateway_status


# @invar:allow dead_export: CLI entrypoint is wired by the command framework.
# @invar:allow shell_result: CLI handler returns int exit code per POSIX convention.
def status_command(json_output: bool = False) -> int:
    """Display gateway runtime status.

    Examples:
        >>> status_command()
        uptime: 0.0s
        servers: 0 (none)
        connections: 0
        profiles: 0
        tool_calls: 0
        0

    Args:
        json_output: Whether to output JSON.

    Returns:
        Process exit code.
    """
    try:
        status = asyncio.run(gateway_status())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if json_output:
        print(status.model_dump_json(indent=2))
    else:
        print(f"uptime: {status.uptime_seconds:.1f}s")
        print(f"servers: {status.server_count} ({', '.join(status.connected_servers) or 'none'})")
        print(f"connections: {status.active_connections}")
        print(f"profiles: {status.profile_count}")
        print(f"tool_calls: {status.total_tool_calls}")

    return 0
