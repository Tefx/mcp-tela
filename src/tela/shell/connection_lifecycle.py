"""Shared cleanup authority for connection teardown paths.

This module centralizes per-connection runtime/session cleanup so disconnect and
shutdown callers apply the same cleanup semantics keyed by ``connection_id``.
"""

from __future__ import annotations

from dataclasses import dataclass

from tela.shell.result import Result
from tela.shell.gateway_runtime import release_session, remove_runtime_connection


@dataclass(frozen=True)
class ConnectionCleanupOutcome:
    """Result payload from one cleanup invocation for a connection_id.

    Attributes:
        connection_id: Connection identifier that cleanup was requested for.
        removed_runtime_connection: True when at least one runtime connection
            entry was removed for this ``connection_id``; False when already
            absent (idempotent repeat invocation).
    """

    connection_id: str
    removed_runtime_connection: bool


def cleanup_connection_by_id(
    connection_id: str,
) -> Result[ConnectionCleanupOutcome, str]:
    """Apply shared cleanup semantics for one connection identifier.

    Cleanup is idempotent by ``connection_id``:
    - repeated calls for the same ID are safe
    - session release is best-effort/idempotent
    - runtime removal reports whether the ID existed on this invocation

    Args:
        connection_id: Target connection identifier.

    Returns:
        Result carrying a ``ConnectionCleanupOutcome``.
    """

    removed_result = remove_runtime_connection(connection_id)
    if removed_result.is_err:
        return Result(error=removed_result.error)

    release_result = release_session(connection_id)
    if release_result.is_err:
        return Result(error=release_result.error)

    return Result(
        value=ConnectionCleanupOutcome(
            connection_id=connection_id,
            removed_runtime_connection=bool(removed_result.value),
        )
    )
