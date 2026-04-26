"""Runtime contract tests for remaining shared-surface hard-cut rules."""

from __future__ import annotations

import asyncio
import logging

import pytest

from tela.core.models import (
    AuthConfig,
    AuthMode,
    ConnectionContext,
    Posture,
    ProfileConfig,
    ResolvedTool,
    ServerConfig,
    TelaConfig,
)
from tela.core.token import compute_signature
from tela.shell.builtin_tools import (
    handle_profiles_list as handle_builtin_profiles_list,
    handle_list_providers,
)
from tela.shell.downstream_registry import DownstreamRegistry
from tela.shell.gateway_runtime import (
    clear_runtime_connections,
    set_runtime_config,
    set_runtime_secrets,
)
from tela.shell.result import Result
from tela.shell.upstream import (
    handle_initialize,
    handle_profiles_list as handle_upstream_profiles_list,
    handle_tools_list,
)


_LEGACY_PROFILE_KEY = "profile" + "_name"
_LEGACY_TOOLS_PROFILE_KEY = "tools" + "_profile"


def _make_token_fields(
    *,
    profile_id: str = "dev",
    token_id: str = "tok_test",
    persona_ref: str = "persona.default",
    instance_id: str = "instance.default",
    issued_at: str = "2026-01-01T00:00:00Z",
    expires_at: str = "2099-12-31T23:59:59Z",
) -> dict[str, str]:
    return {
        "token_id": token_id,
        "profile_id": profile_id,
        "persona_ref": persona_ref,
        "instance_id": instance_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "token_version": "0.1.0",
    }


def _make_client_info(
    *,
    secret: str = "test-secret",
    token_overrides: dict[str, object] | None = None,
    **hints: object,
) -> dict[str, object]:
    token_fields: dict[str, object] = dict(_make_token_fields())
    if token_overrides is not None:
        token_fields.update(token_overrides)
    signature = compute_signature(token_fields, secret)
    return {
        **hints,
        "capability_token": {
            **token_fields,
            "signature": signature,
        },
    }


def _configure_token_mode(secret: str = "test-secret") -> None:
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
    set_runtime_secrets([secret])
    clear_runtime_connections()


def _reset_runtime() -> None:
    set_runtime_config(None)
    set_runtime_secrets([])


def test_handle_initialize_accepts_non_reserved_top_level_hints() -> None:
    """Non-reserved top-level client_info hints are accepted and ignored."""
    _configure_token_mode()
    client_info = _make_client_info(
        client="desktop", profile="ignore-me", x_trace="abc123"
    )

    async def _run() -> None:
        result = await handle_initialize(client_info)
        assert result.is_ok
        assert result.value is not None
        assert result.value.profile_id == "dev"
        assert result.value.client_info_snapshot is not None
        assert result.value.client_info_snapshot["client"] == "desktop"
        assert result.value.client_info_snapshot["x_trace"] == "abc123"

    try:
        asyncio.run(_run())
    finally:
        _reset_runtime()


@pytest.mark.parametrize(
    "reserved_key",
    [
        "token_id",
        "profile_id",
        "signature",
        _LEGACY_PROFILE_KEY,
        _LEGACY_TOOLS_PROFILE_KEY,
    ],
)
def test_handle_initialize_rejects_reserved_top_level_token_semantics(
    reserved_key: str,
) -> None:
    """Top-level token semantics and alias keys are fail-closed."""
    _configure_token_mode()
    client_info = {
        **_make_client_info(),
        reserved_key: "forbidden",
    }

    async def _run() -> None:
        result = await handle_initialize(client_info)
        assert result.is_err
        assert result.error is not None
        assert "INITIALIZE_REJECTED" in result.error
        assert reserved_key in result.error

    try:
        asyncio.run(_run())
    finally:
        _reset_runtime()


@pytest.mark.parametrize("reserved_key", ["tela_profile_id", "opifex_profile_id"])
def test_handle_initialize_rejects_reserved_vendor_top_level_keys(
    reserved_key: str,
) -> None:
    """tela/opifex-owned top-level client_info keys are rejected."""
    _configure_token_mode()
    client_info = {
        **_make_client_info(),
        reserved_key: "forbidden",
    }

    async def _run() -> None:
        result = await handle_initialize(client_info)
        assert result.is_err
        assert result.error is not None
        assert "INITIALIZE_REJECTED" in result.error
        assert reserved_key in result.error

    try:
        asyncio.run(_run())
    finally:
        _reset_runtime()


@pytest.mark.parametrize(
    "alias_field", [_LEGACY_PROFILE_KEY, _LEGACY_TOOLS_PROFILE_KEY]
)
def test_handle_initialize_rejects_alias_fields_inside_capability_token(
    alias_field: str,
) -> None:
    """Capability-token alias fields are rejected inside the nested token payload."""
    _configure_token_mode()
    client_info = _make_client_info(token_overrides={alias_field: "legacy-profile"})

    async def _run() -> None:
        result = await handle_initialize(client_info)
        assert result.is_err
        assert result.error is not None
        assert "INITIALIZE_REJECTED" in result.error
        assert alias_field in result.error

    try:
        asyncio.run(_run())
    finally:
        _reset_runtime()


def test_handle_initialize_rejects_extra_capability_token_fields() -> None:
    """Canonical capability_token payload is fail-closed on extra fields."""
    _configure_token_mode()
    client_info = _make_client_info(token_overrides={"unexpected": "boom"})

    async def _run() -> None:
        result = await handle_initialize(client_info)
        assert result.is_err
        assert result.error is not None
        assert "INITIALIZE_REJECTED" in result.error
        assert "extra_key" in result.error
        assert "rejected_keys=unexpected" in result.error
        assert "unexpected" in result.error

    try:
        asyncio.run(_run())
    finally:
        _reset_runtime()


def test_handle_initialize_alias_rejection_is_auditable_for_top_level_legacy_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Top-level alias rejection must have a stable code and audit log."""
    _configure_token_mode()
    caplog.set_level(logging.WARNING, logger="tela.shell.upstream")

    async def _run() -> None:
        result = await handle_initialize(
            {**_make_client_info(), _LEGACY_PROFILE_KEY: "legacy"}
        )
        assert result.is_err
        assert result.error is not None
        assert "alias_field_present" in result.error

    try:
        asyncio.run(_run())
        assert "INITIALIZE_AUDIT" in caplog.text
        assert "alias_field_present" in caplog.text
        assert "location=client_info" in caplog.text
        assert f"field={_LEGACY_PROFILE_KEY}" in caplog.text
    finally:
        _reset_runtime()


def test_handle_initialize_alias_rejection_is_auditable_for_nested_legacy_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nested token alias rejection must have a stable code and audit log."""
    _configure_token_mode()
    caplog.set_level(logging.WARNING, logger="tela.shell.upstream")

    async def _run() -> None:
        result = await handle_initialize(
            _make_client_info(token_overrides={_LEGACY_TOOLS_PROFILE_KEY: "legacy"})
        )
        assert result.is_err
        assert result.error is not None
        assert "alias_field_present" in result.error

    try:
        asyncio.run(_run())
        assert "INITIALIZE_AUDIT" in caplog.text
        assert "alias_field_present" in caplog.text
        assert "location=capability_token" in caplog.text
        assert f"field={_LEGACY_TOOLS_PROFILE_KEY}" in caplog.text
    finally:
        _reset_runtime()


def test_handle_initialize_requires_capability_token_object() -> None:
    """Token mode requires the nested capability_token object."""
    _configure_token_mode()

    async def _run() -> None:
        result = await handle_initialize({"client": "desktop"})
        assert result.is_err
        assert result.error is not None
        assert "INITIALIZE_REJECTED" in result.error
        assert "capability_token" in result.error

    try:
        asyncio.run(_run())
    finally:
        _reset_runtime()


@pytest.mark.parametrize("tool_name", ["read.file", "ReadFile"])
def test_handle_tools_list_rejects_non_snake_case_tool_names(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    """Shared MCP tools/list surface rejects dotted and camelCase names."""
    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name=tool_name,
                server_name="fs",
                family="filesystem",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
            )
        ],
    )
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
                    capabilities={"filesystem": Posture.READ_ONLY},
                )
            },
        )
    )
    connection = ConnectionContext(
        connection_id="c1",
        profile_id="dev",
        connected_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        "tela.shell.upstream.get_all_tools",
        lambda: Result(value=registry.get_all_tools()),
    )

    async def _run() -> None:
        result = await handle_tools_list(connection)
        assert result.is_err
        assert result.error is not None
        assert (
            "INVALID_TOOL_NAME" in result.error or "invalid_tool_name" in result.error
        )
        assert tool_name in result.error

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)


def test_handle_list_providers_rejects_non_snake_case_tool_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared tela_list_providers payload rejects non-snake-case tool names."""
    registry = DownstreamRegistry()
    registry.register(
        "fs",
        [
            ResolvedTool(
                name="read.file",
                server_name="fs",
                family="filesystem",
                posture=Posture.READ_ONLY,
                schema_={"type": "object"},
            )
        ],
    )
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            servers={"fs": ServerConfig(name="fs", command="cmd")},
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
                    capabilities={"filesystem": Posture.READ_ONLY},
                )
            },
        )
    )
    monkeypatch.setattr(
        "tela.shell.builtin_tools.get_all_tools",
        lambda: Result(value=registry.get_all_tools()),
    )
    monkeypatch.setattr(
        "tela.shell.builtin_tools.get_successful_servers",
        lambda: Result(value={"fs"}),
    )
    monkeypatch.setattr(
        "tela.shell.builtin_tools.get_attempted_servers",
        lambda: Result(value={"fs"}),
    )

    try:
        connection = ConnectionContext(
            connection_id="c1",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.OPEN,
        )
        with pytest.raises(RuntimeError, match="INVALID_TOOL_NAME|invalid_tool_name"):
            asyncio.run(handle_list_providers(connection))
    finally:
        set_runtime_config(None)


def test_handle_list_providers_emits_provider_name_only() -> None:
    """Supporting provider payload must use provider_name, not name."""

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            servers={"fs": ServerConfig(name="fs", command="cmd")},
            profiles={
                "dev": ProfileConfig(
                    name="dev",
                    default=True,
                    capabilities={"filesystem": Posture.READ_ONLY},
                )
            },
        )
    )

    try:
        connection = ConnectionContext(
            connection_id="c_provider_shape",
            profile_id="dev",
            connected_at="2026-01-01T00:00:00Z",
            init_mode=AuthMode.OPEN,
        )
        result = asyncio.run(handle_list_providers(connection))
        assert result[0]["provider_name"] == "fs"
        assert "name" not in result[0]
    finally:
        set_runtime_config(None)


def test_builtin_profiles_list_rejects_multiple_default_profiles_fail_closed() -> None:
    """Shared profile-list tool must reject more than one default profile."""
    set_runtime_config(
        TelaConfig(
            profiles={
                "dev": ProfileConfig(name="dev", default=True),
                "reviewer": ProfileConfig(name="reviewer", default=True),
            }
        )
    )
    try:
        with pytest.raises(RuntimeError, match="invalid_default_profile_state"):
            handle_builtin_profiles_list()
    finally:
        set_runtime_config(None)


def test_upstream_profiles_list_rejects_multiple_default_profiles_fail_closed() -> None:
    """Result-wrapped profile listing must also fail closed on multi-default."""
    set_runtime_config(
        TelaConfig(
            profiles={
                "dev": ProfileConfig(name="dev", default=True),
                "reviewer": ProfileConfig(name="reviewer", default=True),
            }
        )
    )
    try:
        result = handle_upstream_profiles_list()
        assert result.is_err
        assert result.error is not None
        assert "invalid_default_profile_state" in result.error
    finally:
        set_runtime_config(None)
