"""Tests for shared lifecycle authority consumed by status surfaces."""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import (
    ConnectionContext,
    ProfileConfig,
    ResolvedTool,
    ServerConfig,
    TelaConfig,
)
from tela.shell.result import Result
from tela.shell.downstream import DownstreamStartupSnapshot, ProviderStartupFailure
from tela.shell.gateway import gateway_status
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,
    set_runtime_config,
    set_runtime_running,
    set_runtime_total_tool_calls,
)
from tela.shell.http_routes import handle_status


@pytest.fixture
def status_authority_snapshot(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure one runtime snapshot consumed by both status surfaces."""

    config = TelaConfig(
        servers={
            "fs": ServerConfig(name="fs", command="cmd"),
            "shell": ServerConfig(name="shell", command="cmd"),
        },
        profiles={"dev": ProfileConfig(name="dev")},
    )
    set_runtime_config(config)
    set_runtime_running(True)
    clear_runtime_connections()
    set_runtime_total_tool_calls(7)
    add_runtime_connection(
        ConnectionContext(
            connection_id="bridge-shared",
            profile_id="dev",
            connected_at="2026-03-29T12:00:00Z",
        )
    )

    def _mock_get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]:
        return Result(
            value={
                "fs": [
                    ResolvedTool(
                        name="read_file",
                        server_name="fs",
                        family="fs",
                        schema_={},
                    )
                ]
            }
        )

    def _mock_startup_snapshot() -> Result[DownstreamStartupSnapshot, str]:
        return Result(
            value=DownstreamStartupSnapshot(
                attempted_servers=("fs", "shell"),
                successful_servers=("fs",),
                failed_servers={},
                in_progress_servers=(),
                complete=True,
                degraded_reason=None,
            )
        )

    import tela.shell.gateway_lifecycle as gateway_lifecycle

    monkeypatch.setattr(gateway_lifecycle, "get_all_tools", _mock_get_all_tools)
    monkeypatch.setattr(
        gateway_lifecycle, "get_downstream_startup_snapshot", _mock_startup_snapshot
    )

    try:
        yield "status_authority_snapshot"
    finally:
        clear_runtime_connections()
        set_runtime_running(False)
        set_runtime_config(None)


def test_gateway_and_http_status_share_lifecycle_authority(
    status_authority_snapshot: str,
) -> None:
    """gateway_status and handle_status report identical lifecycle facts."""

    gateway_result = asyncio.run(gateway_status())
    http_result = handle_status("valid", "valid")

    assert status_authority_snapshot == "status_authority_snapshot"
    assert gateway_result.is_ok
    assert gateway_result.value is not None
    assert http_result.is_ok
    assert http_result.value is not None

    gateway_payload = gateway_result.value
    http_payload = http_result.value

    assert gateway_payload.server_count == http_payload.server_count == 2
    assert gateway_payload.connected_servers == http_payload.connected_servers == ["fs"]
    assert gateway_payload.active_connections == http_payload.active_connections == 1
    assert gateway_payload.profile_count == http_payload.profile_count == 1
    assert gateway_payload.total_tool_calls == http_payload.total_tool_calls == 7
    assert gateway_payload.state == http_payload.state == "degraded"
    assert (
        gateway_payload.degraded_reason
        == http_payload.degraded_reason
        == "downstream_not_fully_converged"
    )

    assert set(http_payload.model_dump().keys()) == {
        "uptime_seconds",
        "server_count",
        "connected_servers",
        "active_connections",
        "profile_count",
        "total_tool_calls",
        "state",
        "discovery_source",
        "config_path",
        "requested_config_path",
        "config_mismatch",
        "degraded_reason",
        "connections",
        "audit_entries",
    }


def test_status_reports_degraded_provider_timeout_without_connected_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed failed convergence is degraded, never indefinite warming."""

    config = TelaConfig(servers={"slow": ServerConfig(name="slow", command="cmd")})
    set_runtime_config(config)
    set_runtime_running(True)

    def _mock_get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]:
        return Result(value={})

    def _mock_startup_snapshot() -> Result[DownstreamStartupSnapshot, str]:
        failure = ProviderStartupFailure(
            server_name="slow",
            phase="tools_list",
            reason="timeout",
            timeout=True,
        )
        return Result(
            value=DownstreamStartupSnapshot(
                attempted_servers=("slow",),
                successful_servers=(),
                failed_servers={"slow": failure},
                in_progress_servers=(),
                complete=True,
                degraded_reason="provider_tools_list_timeout:slow",
            )
        )

    import tela.shell.gateway_lifecycle as gateway_lifecycle

    monkeypatch.setattr(gateway_lifecycle, "get_all_tools", _mock_get_all_tools)
    monkeypatch.setattr(
        gateway_lifecycle, "get_downstream_startup_snapshot", _mock_startup_snapshot
    )

    try:
        result = asyncio.run(gateway_status())
    finally:
        set_runtime_running(False)
        set_runtime_config(None)

    assert result.is_ok
    assert result.value is not None
    assert result.value.state == "degraded"
    assert result.value.degraded_reason == "provider_tools_list_timeout:slow"
