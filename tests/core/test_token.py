"""Tests for core/token.py HMAC token validation."""

from __future__ import annotations


import pytest
from pydantic import ValidationError

from tela.core.models import EnforcementVerdict, TokenInitBinding
from tela.core.token import (
    compute_signature,
    create_token,
    is_expired,
    resolve_token_init_binding,
    validate_token,
)


def test_compute_signature_is_deterministic() -> None:
    fields = {
        "token_id": "tok_1",
        "profile_id": "dev",
        "persona_ref": "persona.dev",
        "instance_id": "inst-1",
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-12-31T23:59:59Z",
        "token_version": "0.1.0",
    }
    sig1 = compute_signature(fields, "secret")
    sig2 = compute_signature(fields, "secret")
    assert sig1 == sig2


def test_compute_signature_differs_for_different_secrets() -> None:
    fields = {
        "token_id": "tok_1",
        "profile_id": "dev",
        "persona_ref": "persona.dev",
        "instance_id": "inst-1",
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-12-31T23:59:59Z",
        "token_version": "0.1.0",
    }
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


def test_validate_token_preserves_token_version_in_signature_round_trip() -> None:
    from tela.core.models import CapabilityToken

    fields = {
        "token_id": "tok_1",
        "profile_id": "dev",
        "persona_ref": "persona.dev",
        "instance_id": "inst-1",
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-12-31T23:59:59Z",
        "token_version": "0.1.0",
    }
    signature = compute_signature(fields, "secret1")

    token = CapabilityToken.model_validate(fields | {"signature": signature})

    assert (
        token.model_dump(exclude={"signature"}, exclude_none=True)["token_version"]
        == "0.1.0"
    )
    result = validate_token(token, ["secret1"], "2026-06-01T00:00:00Z")
    assert result.verdict == EnforcementVerdict.ALLOW


def test_validate_token_rejects_signature_missing_token_version() -> None:
    from tela.core.models import CapabilityToken

    token_fields = {
        "token_id": "tok_1",
        "profile_id": "dev",
        "persona_ref": "persona.dev",
        "instance_id": "inst-1",
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-12-31T23:59:59Z",
        "token_version": "0.1.0",
    }
    signature_without_token_version = compute_signature(
        {key: value for key, value in token_fields.items() if key != "token_version"},
        "secret1",
    )

    token = CapabilityToken.model_validate(
        token_fields | {"signature": signature_without_token_version}
    )

    result = validate_token(token, ["secret1"], "2026-06-01T00:00:00Z")
    assert result.verdict == EnforcementVerdict.DENY
    assert result.error_code == "TOKEN_INVALID"


def test_capability_token_rejects_unknown_extra_field() -> None:
    from tela.core.models import CapabilityToken

    with pytest.raises(ValidationError) as exc_info:
        CapabilityToken.model_validate(
            {
                "token_id": "tok_1",
                "profile_id": "dev",
                "persona_ref": "persona.dev",
                "instance_id": "inst-1",
                "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-12-31T23:59:59Z",
                "token_version": "0.1.0",
                "signature": "abc",
                "unexpected_field": "nope",
            }
        )

    assert "unexpected_field" in str(exc_info.value)


def test_capability_token_rejects_missing_persona_ref_and_instance_id() -> None:
    from tela.core.models import CapabilityToken

    with pytest.raises(ValidationError) as exc_info:
        CapabilityToken.model_validate(
            {
                "token_id": "tok_1",
                "profile_id": "dev",
                "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-12-31T23:59:59Z",
                "token_version": "0.1.0",
                "signature": "abc",
            }
        )

    message = str(exc_info.value)
    assert "persona_ref" in message
    assert "instance_id" in message


def test_create_token_profile() -> None:
    tok = create_token("production", "secret")
    assert tok.profile_id == "production"
    assert tok.token_id == "tok_auto"
    assert tok.token_version == "0.1.0"


# --- resolve_token_init_binding tests ---


def test_resolve_token_init_binding_valid_token_binds_to_profile() -> None:
    """Valid token must bind the connection to the token's profile_id."""
    tok = create_token("dev", "secret1")
    binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
    assert binding.token_result.verdict == EnforcementVerdict.ALLOW
    assert binding.profile_id == "dev"


def test_resolve_token_init_binding_expired_token_rejected() -> None:
    """Expired token must result in DENY verdict in the binding."""
    tok = create_token("dev", "secret1", expires_at="2026-01-01T00:00:00Z")
    binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
    assert binding.token_result.verdict == EnforcementVerdict.DENY
    assert binding.token_result.error_code == "TOKEN_EXPIRED"
    # Profile id is still preserved in the binding (for error context)
    assert binding.profile_id == "dev"


def test_resolve_token_init_binding_invalid_signature_rejected() -> None:
    """Token with invalid signature must result in DENY verdict."""
    tok = create_token("dev", "secret1")
    binding = resolve_token_init_binding(tok, ["wrong_secret"], "2026-06-01T00:00:00Z")
    assert binding.token_result.verdict == EnforcementVerdict.DENY
    assert binding.token_result.error_code == "TOKEN_INVALID"
    # Profile id is preserved in binding for error context even on DENY
    assert binding.profile_id == "dev"


def test_resolve_token_init_binding_preserves_profile_id() -> None:
    """Profile id from token is always carried in the binding."""
    tok = create_token("production", "secret1")
    binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
    assert binding.profile_id == "production"
    assert isinstance(binding, TokenInitBinding)


def test_resolve_token_init_binding_dual_key_rotation() -> None:
    """Valid token with rotated secrets must succeed."""
    tok = create_token("staging", "old_secret")
    binding = resolve_token_init_binding(
        tok, ["new_secret", "old_secret"], "2026-06-01T00:00:00Z"
    )
    assert binding.token_result.verdict == EnforcementVerdict.ALLOW
    assert binding.profile_id == "staging"


def test_resolve_token_init_binding_returns_binding_type() -> None:
    """Result must be a TokenInitBinding dataclass with correct fields."""
    from tela.core.models import EnforcementResult

    tok = create_token("dev", "secret1")
    binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
    # Verify it's a TokenInitBinding
    assert isinstance(binding, TokenInitBinding)
    assert isinstance(binding.token_result, EnforcementResult)
    assert isinstance(binding.profile_id, str)
