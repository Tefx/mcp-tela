"""Tests for bearer token generation contract."""

from __future__ import annotations

import re

from tela.shell.lockfile import generate_bearer_token


def test_generate_bearer_token_is_urlsafe_and_long_enough() -> None:
    result = generate_bearer_token()
    assert result.is_ok
    assert result.value is not None
    token = result.value

    assert isinstance(token, str)
    assert len(token) >= 43
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", token) is not None


def test_generate_bearer_token_is_not_constant_across_calls() -> None:
    tokens: set[str] = set()
    for _ in range(16):
        result = generate_bearer_token()
        assert result.is_ok
        assert result.value is not None
        tokens.add(result.value)
    assert len(tokens) == 16
