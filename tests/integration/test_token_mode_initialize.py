"""Integration tests for token-mode handle_initialize proving validate_token wiring.

These tests verify that handle_initialize correctly wires validate_token for
token-mode initialization, specifically testing:
- Token field extraction from client_info
- HMAC signature verification through validate_token
- Expiry checking through validate_token
- Profile binding from token
- Connection context creation with token-derived profile

This is the dedicated token-mode counterpart to test_open_mode.py.
"""

from __future__ import annotations

import asyncio

from tela.core.models import (
    AuthConfig,
    AuthMode,
    ConnectionContext,
    ProfileConfig,
    TelaConfig,
)
from tela.core.token import compute_signature
from tela.shell.gateway import (
    clear_runtime_connections,
    get_runtime_connections_snapshot,
    set_runtime_config,
    set_runtime_secrets,
)
from tela.shell.upstream import handle_initialize


def _make_valid_token_fields(
    profile: str = "dev",
    token_id: str = "tok-1",
    issued_at: str = "2026-01-01T00:00:00Z",
    expires_at: str = "2099-12-31T23:59:59Z",
) -> dict:
    """Make token fields dict with computed signature."""
    fields = {
        "token_id": token_id,
        "profile_name": profile,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    return fields


def _sign_token(fields: dict, secret: str) -> dict:
    """Add signature to token fields."""
    sig = compute_signature(fields, secret)
    return {**fields, "signature": sig}


# --- Token-mode handle_initialize success cases ---


def test_handle_initialize_token_mode_valid_token() -> None:
    """Token mode with valid token must bind connection to token's profile.

    This proves validate_token wiring: signature verification succeeds
    and the returned binding carries the profile from the token.
    """
    secret = "test-secret"
    fields = _make_valid_token_fields(profile="production")
    signed_token = _sign_token(fields, secret)

    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
        profiles={"production": ProfileConfig(name="production")},
    ))
    set_runtime_secrets([secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        assert result.value is not None
        assert isinstance(result.value, ConnectionContext)
        # Profile must come from the token, not from config default
        assert result.value.profile_name == "production"
        assert result.value.connection_id.startswith("conn_")

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_binds_profile_from_token() -> None:
    """Token mode must extract profile_name from token, ignoring config defaults.

    Proves that the profile binding flows through validate_token ->
    resolve_token_init_binding -> handle_initialize.
    """
    secret = "my-key"
    fields = _make_valid_token_fields(profile="staging")
    signed_token = _sign_token(fields, secret)

    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
        profiles={
            "staging": ProfileConfig(name="staging"),
            "production": ProfileConfig(name="production", default=True),
        },
    ))
    set_runtime_secrets([secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        assert result.value is not None
        # Profile must be from token, NOT the config's default profile
        assert result.value.profile_name == "staging"

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_dual_key_rotation() -> None:
    """Token mode with rotated secrets must validate with secondary key.

    Proves validate_token tries all secrets (dual-key rotation).
    """
    old_secret = "old-key"
    new_secret = "new-key"
    fields = _make_valid_token_fields(profile="dev")
    # Token signed with old secret
    signed_token = _sign_token(fields, old_secret)

    # Gateway has new primary, old secondary
    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[new_secret, old_secret]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets([new_secret, old_secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        assert result.value is not None
        assert result.value.profile_name == "dev"

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


# --- Token-mode handle_initialize rejection cases ---


def test_handle_initialize_token_mode_missing_token_fields() -> None:
    """Token mode must reject when required token fields are missing."""
    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret"]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets(["secret"])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        # Missing: token_id, profile_name, issued_at, expires_at, signature
        result = await handle_initialize({"client": "desktop"})
        assert result.is_err
        assert "INITIALIZE_REJECTED" in (result.error or "")
        assert "token_id" in (result.error or "").lower()

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_missing_signature() -> None:
    """Token mode must reject when signature is missing."""
    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret"]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets(["secret"])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        # Has all fields except signature
        token_info = {
            "token_id": "tok-1",
            "profile_name": "dev",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-12-31T23:59:59Z",
        }
        result = await handle_initialize(token_info)
        assert result.is_err
        assert "INITIALIZE_REJECTED" in (result.error or "")

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_invalid_signature() -> None:
    """Token mode must reject when signature verification fails.

    Proves validate_token returns DENY with TOKEN_INVALID error.
    """
    secret = "correct-secret"
    fields = _make_valid_token_fields(profile="dev")
    # Sign with WRONG secret
    signed_token = _sign_token(fields, "wrong-secret")

    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets([secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_err
        assert "INITIALIZE_REJECTED" in (result.error or "")
        assert "TOKEN_INVALID" in (result.error or "")

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_expired_token() -> None:
    """Token mode must reject expired tokens.

    Proves validate_token checks expiry and returns DENY with TOKEN_EXPIRED.
    """
    secret = "secret"
    fields = _make_valid_token_fields(
        profile="dev",
        expires_at="2026-01-01T00:00:00Z",  # Expired
    )
    signed_token = _sign_token(fields, secret)

    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets([secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        # Current time is after expiry
        result = await handle_initialize(signed_token)
        assert result.is_err
        assert "INITIALIZE_REJECTED" in (result.error or "")
        assert "TOKEN_EXPIRED" in (result.error or "")

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_no_secrets_configured() -> None:
    """Token mode must reject when no secrets are configured."""
    fields = _make_valid_token_fields(profile="dev")
    signed_token = _sign_token(fields, "any-secret")

    # No secrets means token validation cannot proceed
    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets([])  # No secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_err
        assert "INITIALIZE_REJECTED" in (result.error or "")
        assert "secrets" in (result.error or "").lower()

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


# --- Token-mode vs Open-mode boundary ---


def test_handle_initialize_token_mode_ignores_profile_hints_in_metadata() -> None:
    """Token mode must derive profile from token, not client metadata.

    Even if client_info contains profile hints, they must be ignored;
    the profile comes from the token's profile_name field.
    """
    secret = "key"
    fields = _make_valid_token_fields(profile="production")
    signed_token = _sign_token(fields, secret)

    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
        profiles={
            "production": ProfileConfig(name="production"),
            "staging": ProfileConfig(name="staging"),
        },
    ))
    set_runtime_secrets([secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        # Add a profile hint in metadata that should be IGNORED
        token_with_hint = {**signed_token, "profile": "should-be-ignored"}
        result = await handle_initialize(token_with_hint)
        assert result.is_ok
        assert result.value is not None
        # Profile must be from token, not from metadata
        assert result.value.profile_name == "production"

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_preserves_optional_token_fields() -> None:
    """Token mode must handle optional token fields (persona_ref, etc.).

    prove token construction handles both minimal and full token shapes.
    """
    secret = "optional-key"
    fields = {
        "token_id": "tok-opt",
        "profile_name": "dev",
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2099-12-31T23:59:59Z",
        "persona_ref": "user-123",
        "instance_id": "inst-456",
        "max_depth": 5,
    }
    sig = compute_signature({k: v for k, v in fields.items() if v is not None}, secret)
    signed_token = {**fields, "signature": sig}

    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets([secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        assert result.value is not None
        assert result.value.profile_name == "dev"

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


# --- Integration with connection registration ---


def test_handle_initialize_token_mode_registers_connection() -> None:
    """Token mode must register connection in runtime after successful init."""
    secret = "conn-secret"
    fields = _make_valid_token_fields(profile="dev")
    signed_token = _sign_token(fields, secret)

    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets([secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        initial_count = len(get_runtime_connections_snapshot().value)
        result = await handle_initialize(signed_token)
        assert result.is_ok
        # Connection must be registered
        conns = get_runtime_connections_snapshot().value
        assert len(conns) == initial_count + 1
        # Last connection must match returned context
        assert conns[-1] == result.value

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_connection_id_format() -> None:
    """Token mode connection IDs must follow conn_ prefix pattern."""
    secret = "id-format-key"
    fields = _make_valid_token_fields(profile="dev")
    signed_token = _sign_token(fields, secret)

    set_runtime_config(TelaConfig(
        auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
        profiles={"dev": ProfileConfig(name="dev")},
    ))
    set_runtime_secrets([secret])  # Token mode requires secrets in runtime
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        assert result.value is not None
        # Connection ID format: conn_<hex8>
        assert result.value.connection_id.startswith("conn_")
        # 8 hex chars after conn_
        suffix = result.value.connection_id[5:]
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])
