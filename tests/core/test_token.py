"""Tests for core/token.py HMAC token validation."""

from __future__ import annotations

import pytest

from tela.core.models import CapabilityToken, EnforcementVerdict
from tela.core.token import (
    compute_signature,
    create_token,
    is_expired,
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
    assert tok.profile_name == "production"
    assert tok.token_id == "tok_auto"
