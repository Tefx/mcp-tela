"""Status CLI command surface.

Provides the ``tela status`` command for displaying gateway runtime status.

Human and JSON diagnostics are expected to serialize the shared fact model
declared in ``tela.commands.remote_state`` rather than inventing parallel
wording or state derivations.
"""

from __future__ import annotations

from tela.shell.config_loader import Result
from tela.commands.remote_state import query_remote_state


def status_command(json_output: bool = False) -> Result[int, str]:
    """Display gateway runtime status.

    Examples:
        >>> callable(status_command)
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


# @shell_complexity: Lifecycle event handlers with inherently branching behavior — routes/priorities/status modes are mutually exclusive by design.
def _run_status_command(json_output: bool) -> Result[None, str]:
    """Execute status command and print output."""

    remote_state_result = query_remote_state()
    if remote_state_result.is_err:
        return Result(error=remote_state_result.error)
    assert remote_state_result.value is not None
    status = remote_state_result.value.status

    if json_output:
        print(status.model_dump_json(indent=2))
        return Result(value=None)

    state = getattr(status, "state", None)
    if isinstance(state, str) and state:
        print(f"state: {state}")

    degraded_reason = getattr(status, "degraded_reason", None)
    if isinstance(degraded_reason, str) and degraded_reason:
        print(f"degraded_reason: {degraded_reason}")

    print(f"uptime: {status.uptime_seconds:.1f}s")
    print(
        f"servers: {status.server_count} ({', '.join(status.connected_servers) or 'none'})"
    )
    print(f"connections: {status.active_connections}")
    print(f"profiles: {status.profile_count}")
    print(f"tool_calls: {status.total_tool_calls}")
    return Result(value=None)
