"""Tests for shared connection cleanup lifecycle authority."""

from __future__ import annotations

from tela.core.models import ConnectionContext, TelaConfig
from tela.shell.connection_lifecycle import cleanup_connection_by_id
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    capture_session,
    clear_runtime_connections,
    get_captured_session,
    set_runtime_config,
    set_runtime_running,
)


class _StubSession:
    async def send_tool_list_changed(self) -> None:
        return None


def test_cleanup_connection_by_id_is_idempotent_by_connection_id() -> None:
    """Repeated cleanup on the same connection_id is safe and idempotent."""

    set_runtime_config(TelaConfig())
    set_runtime_running(True)
    clear_runtime_connections()

    connection_id = "cleanup-idem-1"
    add_runtime_connection(
        ConnectionContext(
            connection_id=connection_id,
            profile_name="default",
            connected_at="2026-01-01T00:00:00Z",
        )
    )
    capture_session(connection_id, _StubSession())

    try:
        first = cleanup_connection_by_id(connection_id)
        second = cleanup_connection_by_id(connection_id)

        assert first.is_ok
        assert first.value is not None
        assert first.value.connection_id == connection_id
        assert first.value.removed_runtime_connection is True

        assert second.is_ok
        assert second.value is not None
        assert second.value.connection_id == connection_id
        assert second.value.removed_runtime_connection is False

        assert get_captured_session(connection_id).is_err
    finally:
        clear_runtime_connections()
        set_runtime_running(False)
        set_runtime_config(None)
