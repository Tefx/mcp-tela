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
    GatewayTransport,
    ProfileConfig,
    TelaConfig,
)
from tela.core.token import compute_signature
from tela.shell.gateway_runtime import (
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

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"production": ProfileConfig(name="production")},
        )
    )
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

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={
                "staging": ProfileConfig(name="staging"),
                "production": ProfileConfig(name="production", default=True),
            },
        )
    )
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
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[new_secret, old_secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
    set_runtime_secrets(
        [new_secret, old_secret]
    )  # Token mode requires secrets in runtime
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
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret"]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
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
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=["secret"]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
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

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
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

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
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
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
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

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={
                "production": ProfileConfig(name="production"),
                "staging": ProfileConfig(name="staging"),
            },
        )
    )
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

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
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

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
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

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
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


# --- Recovery-critical runtime state for token-mode reconnect ---


def test_handle_initialize_token_mode_records_init_mode() -> None:
    """Token-mode handle_initialize must set init_mode=AUTH_TOKEN on ConnectionContext.

    Without init_mode, recovery cannot distinguish TOKEN from OPEN mode
    during reconnect, preventing correct revalidation path selection.
    """
    secret = "recovery-init-mode-key"
    fields = _make_valid_token_fields(profile="dev")
    signed_token = _sign_token(fields, secret)

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
    set_runtime_secrets([secret])
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        assert result.value is not None
        assert result.value.init_mode == AuthMode.TOKEN

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_preserves_client_info_snapshot() -> None:
    """Token-mode handle_initialize must preserve client_info snapshot on ConnectionContext.

    The snapshot carries the original capability-token fields
    (token_id, profile_name, issued_at, expires_at, signature)
    required for revalidation on reconnect.
    Without the snapshot, recovery cannot reconstruct a CapabilityToken
    from an empty initialize.
    """
    secret = "recovery-snapshot-key"
    fields = _make_valid_token_fields(profile="production", token_id="tok-recovery-1")
    signed_token = _sign_token(fields, secret)

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"production": ProfileConfig(name="production")},
        )
    )
    set_runtime_secrets([secret])
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        ctx = result.value
        assert ctx is not None
        assert ctx.client_info_snapshot is not None
        # All required token fields must be in snapshot
        assert ctx.client_info_snapshot["token_id"] == "tok-recovery-1"
        assert ctx.client_info_snapshot["profile_name"] == "production"
        assert "issued_at" in ctx.client_info_snapshot
        assert "expires_at" in ctx.client_info_snapshot
        assert "signature" in ctx.client_info_snapshot

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_snapshot_enables_capability_token_reconstruction() -> (
    None
):
    """client_info_snapshot must contain enough data to reconstruct CapabilityToken.

    This is the core recovery contract: the snapshot must carry all fields
    required by the CapabilityToken constructor so that a recovery path can
    re-validate the token without requiring the client to re-present it.

    Without this, a connection drop during idle period makes reconnect
    impossible because the authority state is lost.
    """
    secret = "recovery-reconstruct-key"
    fields = _make_valid_token_fields(
        profile="staging",
        token_id="tok-recon-1",
    )
    signed_token = _sign_token(fields, secret)

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"staging": ProfileConfig(name="staging")},
        )
    )
    set_runtime_secrets([secret])
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        ctx = result.value
        assert ctx is not None
        assert ctx.init_mode == AuthMode.TOKEN
        assert ctx.client_info_snapshot is not None

        # Prove snapshot enables CapabilityToken reconstruction
        for field in (
            "token_id",
            "profile_name",
            "issued_at",
            "expires_at",
            "signature",
        ):
            assert field in ctx.client_info_snapshot, (
                f"Recovery-critical field {field!r} missing from client_info_snapshot"
            )

        # The snapshot values must match the original token fields
        assert ctx.client_info_snapshot["token_id"] == "tok-recon-1"
        assert ctx.client_info_snapshot["profile_name"] == "staging"

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_token_mode_bridge_connection_id_is_none() -> None:
    """Token-mode non-bridge initialize must have bridge_connection_id=None."""
    secret = "recovery-no-bridge-key"
    fields = _make_valid_token_fields(profile="dev")
    signed_token = _sign_token(fields, secret)

    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={"dev": ProfileConfig(name="dev")},
        )
    )
    set_runtime_secrets([secret])
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize(signed_token)
        assert result.is_ok
        assert result.value is not None
        assert result.value.bridge_connection_id is None

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_handle_initialize_open_mode_records_init_mode() -> None:
    """Open-mode handle_initialize must set init_mode=AUTH_OPEN on ConnectionContext.

    Proves that init_mode recording is not limited to token mode.
    """
    set_runtime_config(
        TelaConfig(
            auth=AuthConfig(mode=AuthMode.OPEN),
            resolved_default_profile="dev",
            profiles={"dev": ProfileConfig(name="dev", default=True)},
        )
    )
    set_runtime_secrets([])
    clear_runtime_connections()

    async def _run() -> None:
        result = await handle_initialize({"client": "desktop"})
        assert result.is_ok
        assert result.value is not None
        assert result.value.init_mode == AuthMode.OPEN
        assert result.value.client_info_snapshot is not None
        assert result.value.client_info_snapshot["client"] == "desktop"

    try:
        asyncio.run(_run())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


# --- Gateway fail-closed recovery tests (idle_recovery.gateway_fail_closed) ---


def test_token_mode_gateway_fails_closed_on_lost_session() -> None:
    """Token-mode gateway must fail closed when session is lost.

    When a token-mode connection exists but its MCP session is unavailable,
    the gateway must raise RECONNECT_REQUIRED rather than silently creating
    a new empty connection via handle_initialize({}).

    This tests the idle recovery path: session truth must not diverge from
    connection truth. An empty-initialize recovery would create a connection
    without the token authority that the original connection had.
    """
    from tela.shell.gateway import (
        GatewayStartupConfig,
        gateway_start,
        gateway_shutdown,
    )

    secret = "fail-closed-secret"
    fields = _make_valid_token_fields(profile="production")
    signed_token = _sign_token(fields, secret)

    async def _scenario() -> None:
        tela = TelaConfig(
            auth=AuthConfig(mode=AuthMode.TOKEN, secrets=[secret]),
            profiles={
                "production": ProfileConfig(name="production"),
            },
            resolved_default_profile="production",
        )
        config = GatewayStartupConfig(
            transport=GatewayTransport.STDIO,
            port=None,
            auth_mode=AuthMode.TOKEN,
            default_profile=None,
        )

        start_result = await gateway_start(config, tela_config=tela)
        assert start_result.is_ok
        try:
            # handle_initialize with a valid token must succeed
            result = await handle_initialize(signed_token)
            assert result.is_ok
            assert result.value is not None
            assert result.value.init_mode == AuthMode.TOKEN
        finally:
            await gateway_shutdown()

    try:
        asyncio.run(_scenario())
    finally:
        set_runtime_config(None)
        set_runtime_secrets([])


def test_token_mode_handle_initialize_not_in_wire_handlers() -> None:
    """_wire_upstream_handlers must not import or call handle_initialize.

    When the idle recovery path is triggered, handle_initialize({})
    would create a connection without token authority — the worst possible
    outcome for token mode. This test verifies that the gateway module
    has removed the empty-initialize recovery path entirely by checking
    that handle_initialize is not in the function's import list or call sites.
    """
    import ast
    import inspect
    from tela.shell import gateway

    source = inspect.getsource(gateway._wire_upstream_handlers)
    tree = ast.parse(source)

    # Check that handle_initialize is not imported in the function body
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "handle_initialize", (
                    "_wire_upstream_handlers must not import handle_initialize – "
                    "the empty-initialize recovery path has been replaced with fail-closed"
                )

    # Check that no function call references handle_initialize (excluding docstrings)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "handle_initialize":
                raise AssertionError(
                    "_wire_upstream_handlers must not call handle_initialize – "
                    "the empty-initialize recovery path has been replaced with fail-closed"
                )
            if isinstance(func, ast.Attribute) and func.attr == "handle_initialize":
                raise AssertionError(
                    "_wire_upstream_handlers must not call handle_initialize – "
                    "the empty-initialize recovery path has been replaced with fail-closed"
                )
