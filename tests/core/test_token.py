"""Tests for core/token.py HMAC token validation."""

from __future__ import annotations


from tela.core.models import EnforcementVerdict, TokenInitBinding
from tela.core.token import (
    compute_signature,
    create_token,
    is_expired,
    resolve_token_init_binding,
    validate_token,
)


def test_compute_signature_is_deterministic() -> None:
    fields = {"token_id": "tok_1", "profile_name": "dev"}
    sig1 = compute_signature(fields, "secret")
    sig2 = compute_signature(fields, "secret")
    assert sig1 == sig2


def test_compute_signature_differs_for_different_secrets() -> None:
    fields = {"token_id": "tok_1", "profile_name": "dev"}
    sig1 = compute_signature(fields, "secret1")
    sig2 = compute_signature(fields, "secret2")
    assert sig1 != sig2


def test_is_expired_true() -> None:
    assert is_expired("2026-02-28T10:00:00Z", "2026-02-28T10:30:00Z") is True


def test_is_expired_false() -> None:
    assert is_expired("2026-02-28T11:00:00Z", "2026-02-28T10:30:00Z") is False


def test_is_expired_exact_boundary() -> None:
    assert is_expired("2026-02-28T10:00:00Z", "2026-02-28T10:00:00Z") is True


def test_validate_token_valid() -> None:
    tok = create_token("dev", "secret1")
    result = validate_token(tok, ["secret1"], "2026-06-01T00:00:00Z")
    assert result.verdict == EnforcementVerdict.ALLOW


def test_validate_token_expired() -> None:
    tok = create_token("dev", "secret1", expires_at="2026-01-01T00:00:00Z")
    result = validate_token(tok, ["secret1"], "2026-06-01T00:00:00Z")
    assert result.verdict == EnforcementVerdict.DENY
    assert result.error_code == "TOKEN_EXPIRED"


def test_validate_token_invalid_signature() -> None:
    tok = create_token("dev", "secret1")
    result = validate_token(tok, ["wrong_secret"], "2026-06-01T00:00:00Z")
    assert result.verdict == EnforcementVerdict.DENY
    assert result.error_code == "TOKEN_INVALID"


def test_validate_token_dual_key_rotation() -> None:
    tok = create_token("dev", "old_secret")
    # Validate with new primary + old secondary
    result = validate_token(tok, ["new_secret", "old_secret"], "2026-06-01T00:00:00Z")
    assert result.verdict == EnforcementVerdict.ALLOW


def test_create_token_profile() -> None:
    tok = create_token("production", "secret")
    assert tok.profile_id == "production"
    assert tok.token_id == "tok_auto"


# --- resolve_token_init_binding tests ---


def test_resolve_token_init_binding_valid_token_binds_to_profile() -> None:
    """Valid token must bind the connection to the token's profile_name."""
    tok = create_token("dev", "secret1")
    binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
    assert binding.token_result.verdict == EnforcementVerdict.ALLOW
    assert binding.profile_name == "dev"


def test_resolve_token_init_binding_expired_token_rejected() -> None:
    """Expired token must result in DENY verdict in the binding."""
    tok = create_token("dev", "secret1", expires_at="2026-01-01T00:00:00Z")
    binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
    assert binding.token_result.verdict == EnforcementVerdict.DENY
    assert binding.token_result.error_code == "TOKEN_EXPIRED"
    # Profile name is still preserved in the binding (for error context)
    assert binding.profile_name == "dev"


def test_resolve_token_init_binding_invalid_signature_rejected() -> None:
    """Token with invalid signature must result in DENY verdict."""
    tok = create_token("dev", "secret1")
    binding = resolve_token_init_binding(tok, ["wrong_secret"], "2026-06-01T00:00:00Z")
    assert binding.token_result.verdict == EnforcementVerdict.DENY
    assert binding.token_result.error_code == "TOKEN_INVALID"
    # Profile name is preserved in binding for error context even on DENY
    assert binding.profile_name == "dev"


def test_resolve_token_init_binding_preserves_profile_name() -> None:
    """Profile name from token is always carried in the binding."""
    tok = create_token("production", "secret1")
    binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
    assert binding.profile_name == "production"
    assert isinstance(binding, TokenInitBinding)


def test_resolve_token_init_binding_dual_key_rotation() -> None:
    """Valid token with rotated secrets must succeed."""
    tok = create_token("staging", "old_secret")
    binding = resolve_token_init_binding(
        tok, ["new_secret", "old_secret"], "2026-06-01T00:00:00Z"
    )
    assert binding.token_result.verdict == EnforcementVerdict.ALLOW
    assert binding.profile_name == "staging"


def test_resolve_token_init_binding_returns_binding_type() -> None:
    """Result must be a TokenInitBinding dataclass with correct fields."""
    from tela.core.models import EnforcementResult

    tok = create_token("dev", "secret1")
    binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
    # Verify it's a TokenInitBinding
    assert isinstance(binding, TokenInitBinding)
    assert isinstance(binding.token_result, EnforcementResult)
    assert isinstance(binding.profile_name, str)
