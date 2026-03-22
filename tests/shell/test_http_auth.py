"""Tests for bearer token middleware validation semantics."""

from __future__ import annotations

import ast
import inspect

from tela.shell import http_auth


def _simulate_http_auth_gate(
    path: str, auth_header: str | None, expected_token: str
) -> bool:
    if path == "/health":
        return True

    if auth_header is None:
        return False

    bearer_prefix = "Bearer "
    if not auth_header.startswith(bearer_prefix):
        return False

    presented_token = auth_header[len(bearer_prefix) :]
    return http_auth.validate_bearer_token(presented_token, expected_token).is_ok


def test_validate_bearer_token_accepts_valid_token() -> None:
    result = http_auth.validate_bearer_token("correct-token", "correct-token")
    assert result.is_ok
    assert result.error is None


def test_validate_bearer_token_rejects_invalid_token() -> None:
    result = http_auth.validate_bearer_token("wrong-token", "correct-token")
    assert result.is_err
    assert result.error == "AUTH_INVALID_TOKEN: bearer token validation failed"


def test_http_auth_gate_rejects_missing_authorization_header() -> None:
    assert (
        _simulate_http_auth_gate("/status", None, expected_token="correct-token")
        is False
    )


def test_http_auth_gate_rejects_malformed_bearer_prefix() -> None:
    assert (
        _simulate_http_auth_gate(
            "/status",
            "Token correct-token",
            expected_token="correct-token",
        )
        is False
    )


def test_http_auth_gate_bypasses_health_path() -> None:
    assert (
        _simulate_http_auth_gate("/health", None, expected_token="correct-token")
        is True
    )


def test_validate_bearer_token_uses_constant_time_compare_digest() -> None:
    source = inspect.getsource(http_auth._validate_bearer_token)
    tree = ast.parse(source)

    compare_digest_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hmac"
        and node.func.attr == "compare_digest"
    ]
    assert len(compare_digest_calls) >= 1

    eq_comparisons = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, ast.Eq) for op in node.ops)
    ]
    assert not eq_comparisons
