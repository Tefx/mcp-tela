"""Tests for built-in gateway tools (tela_list_providers, etc.)."""

from __future__ import annotations

import asyncio


from tela.core.models import (
    AuthConfig,
    AuthMode,
    GatewayTransport,
    Posture,
    ProfileConfig,
    ServerConfig,
    TelaConfig,
)
from tela.shell.builtin_tools import (
    BUILTIN_TOOL_NAMES,
    handle_list_providers,
)
from tela.shell.gateway import (
    GatewayStartupConfig,
    gateway_shutdown,
    gateway_start,
)


# --- handle_list_providers unit tests ---


def test_handle_list_providers_returns_provider_info_shape() -> None:
    """handle_list_providers returns list of ProviderInfo dicts for connected servers."""

    tela = TelaConfig(
        servers={
            "fs": ServerConfig(
                name="fs",
                command="cmd",
                default_posture=Posture.READ_ONLY,
            ),
        },
        profiles={
            "dev": ProfileConfig(
                name="dev",
                default=True,
                capabilities={"fs": Posture.READ_ONLY},
            )
        },
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="dev",
    )
    tool_lists = {
        "fs": [
            {"name": "read_file", "inputSchema": {}},
            {"name": "write_file", "inputSchema": {}},
        ]
    }
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    try:
        result = asyncio.run(handle_list_providers())
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "fs"
        assert result[0]["status"] == "connected"
        assert result[0]["tool_count"] == 2
        assert isinstance(result[0]["tool_names"], list)
        assert all(isinstance(n, str) for n in result[0]["tool_names"])
    finally:
        asyncio.run(gateway_shutdown())


def test_handle_list_providers_includes_disconnected_server() -> None:
    """handle_list_providers includes servers that are configured but not connected."""

    # Configure 2 servers but only connect 1 via tool_lists
    tela = TelaConfig(
        servers={
            "fs": ServerConfig(
                name="fs", command="cmd", default_posture=Posture.READ_WRITE
            ),
            "shell": ServerConfig(name="shell", command="cmd"),
        },
        profiles={
            "dev": ProfileConfig(
                name="dev",
                default=True,
                capabilities={"fs": Posture.READ_WRITE},
            )
        },
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="dev",
    )
    # Only "fs" is connected; "shell" is configured but has no tool_lists entry
    tool_lists: dict[str, list[dict]] = {
        "fs": [{"name": "read_file", "inputSchema": {}}],
        # "shell" intentionally omitted to simulate disconnected server
    }
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    try:
        result = asyncio.run(handle_list_providers())

        # Should have 2 entries: one connected, one disconnected
        assert len(result) == 2

        fs_entry = next(r for r in result if r["name"] == "fs")
        assert fs_entry["status"] == "connected"
        assert fs_entry["tool_count"] == 1

        shell_entry = next(r for r in result if r["name"] == "shell")
        assert shell_entry["status"] == "failed"
        assert shell_entry["tool_count"] == 0
    finally:
        asyncio.run(gateway_shutdown())


def test_handle_list_providers_includes_failed_server() -> None:
    """handle_list_providers includes servers that failed during connection."""

    # Configure a server without tool_lists entry to simulate a failed connection
    tela = TelaConfig(
        servers={
            "fs": ServerConfig(name="fs", command="cmd"),
            "bad": ServerConfig(name="bad", command="nonexistent_cmd"),
        },
        profiles={
            "dev": ProfileConfig(
                name="dev",
                default=True,
                capabilities={"fs": Posture.READ_WRITE},
            )
        },
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="dev",
    )
    # Only fs has tool_lists - "bad" will fail to connect
    tool_lists = {
        "fs": [{"name": "read_file", "inputSchema": {}}],
        # "bad" has no tool_lists entry
    }
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    try:
        result = asyncio.run(handle_list_providers())

        # Should have an entry for "bad" with failed status
        bad_entry = next((r for r in result if r["name"] == "bad"), None)
        assert bad_entry is not None
        assert bad_entry["status"] == "failed"
        assert bad_entry["tool_count"] == 0
    finally:
        asyncio.run(gateway_shutdown())


def test_handle_list_providers_filters_by_profile_enforcement() -> None:
    """handle_list_providers only exposes tools permitted by the active profile."""

    tela = TelaConfig(
        servers={
            "fs": ServerConfig(
                name="fs",
                command="cmd",
                default_posture=Posture.READ_ONLY,
            ),
        },
        profiles={
            "dev": ProfileConfig(
                name="dev",
                default=True,
                # Profile blocks all fs tools (posture NONE)
                capabilities={"fs": Posture.NONE},
            )
        },
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="dev",
    )
    tool_lists = {
        "fs": [
            {"name": "read_file", "inputSchema": {}},
            {"name": "write_file", "inputSchema": {}},
        ]
    }
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    try:
        result = asyncio.run(handle_list_providers())

        assert len(result) == 1
        assert result[0]["name"] == "fs"
        # Profile blocks all tools, so tool_count should be 0
        assert result[0]["tool_count"] == 0
        assert result[0]["tool_names"] == []
    finally:
        asyncio.run(gateway_shutdown())


def test_handle_list_providers_raises_on_missing_runtime_config() -> None:
    """handle_list_providers raises RuntimeError when no runtime config is available.

    This tests the failure path where get_runtime_config() returns an error
    (e.g., gateway not started, no config file found).
    The function should raise RuntimeError, not silently return [].
    """
    # Don't start gateway - simulate missing runtime config state
    import pytest

    with pytest.raises(
        RuntimeError, match="handle_list_providers requires a valid runtime config"
    ):
        asyncio.run(handle_list_providers())


def test_builtin_tool_names_set_contains_tela_list_providers() -> None:
    """BUILTIN_TOOL_NAMES includes 'tela_list_providers'."""
    assert "tela_list_providers" in BUILTIN_TOOL_NAMES
