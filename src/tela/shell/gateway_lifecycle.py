"""Lifecycle authority surface for status/readiness facts.

This module is the single authority for lifecycle/discovery/readiness facts
consumed by gateway status surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass

from tela.shell.config_loader import Result
from tela.shell.downstream import get_all_tools
from tela.shell.gateway_runtime import (
    RuntimeStatusSnapshot,
    get_runtime_status_snapshot,
)


@dataclass(frozen=True)
class LifecycleStatusFacts:
    """Authoritative lifecycle and readiness facts for status consumers."""

    snapshot: RuntimeStatusSnapshot
    server_count: int
    profile_count: int
    connected_servers: tuple[str, ...]
    active_connections: int
    total_tool_calls: int
    state: str
    degraded_reason: str | None


def get_lifecycle_status_facts() -> Result[LifecycleStatusFacts, str]:
    """Return authoritative status facts from runtime snapshot + downstream state.

    Returns:
        Result with lifecycle/readiness facts shared by status consumers.
    """

    snapshot_result = get_runtime_status_snapshot()
    if snapshot_result.is_err:
        return Result(error=snapshot_result.error)
    assert snapshot_result.value is not None
    snapshot = snapshot_result.value

    all_tools_result = get_all_tools()
    if all_tools_result.is_err:
        return Result(error=all_tools_result.error)
    assert all_tools_result.value is not None
    connected_servers = tuple(all_tools_result.value.keys())

    server_count = len(snapshot.config.servers) if snapshot.config else 0
    profile_count = len(snapshot.config.profiles) if snapshot.config else 0
    active_connections = len(snapshot.connections)
    total_tool_calls = snapshot.total_tool_calls

    if connected_servers:
        if len(connected_servers) < server_count:
            state = "degraded"
            degraded_reason = "downstream_not_fully_converged"
        else:
            state = "ready"
            degraded_reason = None
    elif server_count > 0:
        state = "warming"
        degraded_reason = None
    else:
        state = "ready"
        degraded_reason = None

    return Result(
        value=LifecycleStatusFacts(
            snapshot=snapshot,
            server_count=server_count,
            profile_count=profile_count,
            connected_servers=connected_servers,
            active_connections=active_connections,
            total_tool_calls=total_tool_calls,
            state=state,
            degraded_reason=degraded_reason,
        )
    )
