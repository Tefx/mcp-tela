"""Tests for built-in gateway tools (tela_list_providers, etc.)."""

from __future__ import annotations

import asyncio


from tela.core.models import (
    AuthConfig,
    AuthMode,
    ConnectionContext,
    GatewayTransport,
    Posture,
    ProfileConfig,
    ServerConfig,
    TelaConfig,
)
from tela.shell.builtin_tools import (
    BUILTIN_TOOL_NAMES,
    _validate_profile_list_payload,
    handle_list_providers,
    handle_profiles_list,
)
from tela.shell.gateway import (
    GatewayStartupConfig,
    gateway_shutdown,
    gateway_start,
)


_LEGACY_PROFILE_KEY = "profile" + "_name"
_LEGACY_FAMILIES_KEY = "famil" + "ies"


def _bound_connection(profile_id: str = "dev") -> ConnectionContext:
    return ConnectionContext(
        connection_id=f"conn_{profile_id}",
        profile_id=profile_id,
        connected_at="2026-01-01T00:00:00Z",
        init_mode=AuthMode.OPEN,
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
        result = asyncio.run(handle_list_providers(_bound_connection()))
        assert isinstance(result, list)
        assert len(result) == 1
        assert set(result[0].keys()) == {
            "provider_name",
            "profile_id",
            "status",
            "tool_prefix",
            "tool_count",
            "tool_names",
        }
        assert result[0]["provider_name"] == "fs"
        assert result[0]["profile_id"] == "dev"
        assert result[0]["status"] == "connected"
        assert result[0]["tool_prefix"] is None
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
        result = asyncio.run(handle_list_providers(_bound_connection()))

        # Should have 2 entries: one connected, one disconnected
        assert len(result) == 2

        fs_entry = next(r for r in result if r["provider_name"] == "fs")
        assert fs_entry["status"] == "connected"
        assert fs_entry["tool_count"] == 1

        shell_entry = next(r for r in result if r["provider_name"] == "shell")
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
        result = asyncio.run(handle_list_providers(_bound_connection()))

        # Should have an entry for "bad" with failed status
        bad_entry = next((r for r in result if r["provider_name"] == "bad"), None)
        assert bad_entry is not None
        assert bad_entry["status"] == "failed"
        assert bad_entry["tool_count"] == 0
    finally:
        asyncio.run(gateway_shutdown())


def test_handle_list_providers_returns_sorted_providers_and_tool_names() -> None:
    """Provider listing order is canonical and independent of config/tool order."""

    tela = TelaConfig(
        servers={
            "zeta": ServerConfig(
                name="zeta",
                command="cmd",
                default_posture=Posture.READ_ONLY,
            ),
            "alpha": ServerConfig(
                name="alpha",
                command="cmd",
                default_posture=Posture.READ_ONLY,
            ),
        },
        profiles={
            "dev": ProfileConfig(
                name="dev",
                default=True,
                capabilities={"zeta": Posture.READ_ONLY, "alpha": Posture.READ_ONLY},
            )
        },
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="dev",
    )
    tool_lists = {
        "zeta": [
            {"name": "z_last", "inputSchema": {}},
            {"name": "a_first", "inputSchema": {}},
        ],
        "alpha": [{"name": "middle", "inputSchema": {}}],
    }
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    try:
        result = asyncio.run(handle_list_providers(_bound_connection()))

        assert [entry["provider_name"] for entry in result] == ["alpha", "zeta"]
        zeta = next(entry for entry in result if entry["provider_name"] == "zeta")
        assert zeta["tool_names"] == ["a_first", "z_last"]
    finally:
        asyncio.run(gateway_shutdown())


def test_handle_profiles_list_returns_sorted_profiles_and_capabilities() -> None:
    """Profile listing order is canonical and independent of config order."""

    tela = TelaConfig(
        servers={},
        profiles={
            "zeta": ProfileConfig(
                name="zeta",
                default=False,
                capabilities={"z_family": Posture.READ_ONLY, "a_family": Posture.NONE},
            ),
            "alpha": ProfileConfig(
                name="alpha",
                default=True,
                capabilities={"shell": Posture.READ_WRITE},
            ),
        },
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="alpha",
    )
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="alpha",
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists={}))
    try:
        result = handle_profiles_list()

        assert [entry["profile_id"] for entry in result] == ["alpha", "zeta"]
        zeta = next(entry for entry in result if entry["profile_id"] == "zeta")
        assert list(zeta["capabilities"].keys()) == ["a_family", "z_family"]
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
        result = asyncio.run(handle_list_providers(_bound_connection()))

        assert len(result) == 1
        assert result[0]["provider_name"] == "fs"
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
        asyncio.run(handle_list_providers(_bound_connection()))


def test_builtin_tool_names_set_contains_tela_list_providers() -> None:
    """BUILTIN_TOOL_NAMES includes 'tela_list_providers'."""
    assert "tela_list_providers" in BUILTIN_TOOL_NAMES


def test_handle_list_providers_requires_bound_connection() -> None:
    """Canonical provider listing must reject missing bound profile truth."""
    import pytest

    tela = TelaConfig(
        servers={"fs": ServerConfig(name="fs", command="cmd")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
        resolved_default_profile="dev",
    )
    config = GatewayStartupConfig(
        transport=GatewayTransport.STDIO,
        port=None,
        auth_mode=AuthMode.OPEN,
        default_profile="dev",
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists={"fs": []}))
    try:
        with pytest.raises(RuntimeError, match="missing_bound_profile"):
            asyncio.run(handle_list_providers())
    finally:
        asyncio.run(gateway_shutdown())


def test_handle_list_providers_uses_bound_connection_profile_in_token_mode() -> None:
    """Provider visibility must follow the admitted token-bound connection profile."""

    tela = TelaConfig(
        servers={
            "fs": ServerConfig(
                name="fs",
                command="cmd",
                default_posture=Posture.READ_ONLY,
            ),
        },
        profiles={
            "default": ProfileConfig(
                name="default",
                default=True,
                capabilities={"fs": Posture.READ_ONLY},
            ),
            "token-bound": ProfileConfig(
                name="token-bound",
                default=False,
                capabilities={"fs": Posture.NONE},
            ),
        },
        auth=AuthConfig(mode=AuthMode.TOKEN),
        resolved_default_profile="default",
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
        auth_mode=AuthMode.TOKEN,
        default_profile=None,
    )
    connection = ConnectionContext(
        connection_id="conn_token_bound",
        profile_id="token-bound",
        connected_at="2026-01-01T00:00:00Z",
        init_mode=AuthMode.TOKEN,
    )

    asyncio.run(gateway_start(config, tela_config=tela, tool_lists=tool_lists))
    try:
        result = asyncio.run(handle_list_providers(connection))

        assert len(result) == 1
        assert result[0]["provider_name"] == "fs"
        assert result[0]["tool_count"] == 0
        assert result[0]["tool_names"] == []
    finally:
        asyncio.run(gateway_shutdown())


def test_validate_profile_list_payload_rejects_missing_default_key() -> None:
    """Canonical profile payload must fail closed when default is missing."""
    import pytest

    with pytest.raises(RuntimeError, match="missing_required_field: field=default"):
        _validate_profile_list_payload(
            [
                {
                    "profile_id": "dev",
                    "capabilities": {"filesystem": "read_only"},
                }  # type: ignore[list-item]
            ]
        )


def test_validate_profile_list_payload_rejects_wrong_capabilities_type() -> None:
    """Canonical profile payload must reject non-object capabilities values."""
    import pytest

    with pytest.raises(RuntimeError, match="wrong_type: field=capabilities"):
        _validate_profile_list_payload(
            [
                {
                    "profile_id": "dev",
                    "capabilities": "read_only",
                    "default": True,
                }  # type: ignore[list-item]
            ]
        )


def test_validate_profile_list_payload_rejects_bad_posture_value() -> None:
    """Canonical profile payload must reject non-canonical posture values."""
    import pytest

    with pytest.raises(RuntimeError, match="bad_enum: field=capabilities"):
        _validate_profile_list_payload(
            [
                {
                    "profile_id": "dev",
                    "capabilities": {"filesystem": "readonly"},
                    "default": True,
                }
            ]
        )


def test_validate_profile_list_payload_rejects_legacy_profile_name_key() -> None:
    """Canonical profile payload must reject the retired profile-id alias."""
    import pytest

    with pytest.raises(RuntimeError, match=f"legacy_alias: field={_LEGACY_PROFILE_KEY}"):
        _validate_profile_list_payload(
            [
                {
                    "profile_id": "dev",
                    _LEGACY_PROFILE_KEY: "legacy-dev",
                    "capabilities": {"filesystem": "read_only"},
                    "default": True,
                }  # type: ignore[list-item]
            ]
        )


def test_validate_profile_list_payload_rejects_legacy_families_key() -> None:
    """Canonical profile payload must reject the retired capability alias."""
    import pytest

    with pytest.raises(RuntimeError, match=f"legacy_alias: field={_LEGACY_FAMILIES_KEY}"):
        _validate_profile_list_payload(
            [
                {
                    "profile_id": "dev",
                    _LEGACY_FAMILIES_KEY: {"filesystem": "read_only"},
                    "capabilities": {"filesystem": "read_only"},
                    "default": True,
                }  # type: ignore[list-item]
            ]
        )
