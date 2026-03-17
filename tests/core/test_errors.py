"""Tests for core error constants."""

from __future__ import annotations

from tela.core import errors


def test_auth_rate_limited_constant_exists() -> None:
    """AUTH_RATE_LIMITED is available as a stable error code constant."""
    assert errors.AUTH_RATE_LIMITED == "AUTH_RATE_LIMITED"
