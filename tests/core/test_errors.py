"""Tests for core error constants."""

from __future__ import annotations

from tela.core import errors


def test_auth_rate_limited_constant_exists() -> None:
    """AUTH_RATE_LIMITED is available as a stable error code constant."""
    assert errors.AUTH_RATE_LIMITED == "AUTH_RATE_LIMITED"


def test_error_to_http_status_auth_invalid_token() -> None:
    """AUTH_INVALID_TOKEN errors map to HTTP 401."""
    assert (
        errors.error_to_http_status(
            "AUTH_INVALID_TOKEN: bearer token validation failed"
        )
        == 401
    )


def test_error_to_http_status_connection_not_found() -> None:
    """CONNECTION_NOT_FOUND errors map to HTTP 404."""
    assert errors.error_to_http_status("CONNECTION_NOT_FOUND: id=abc123") == 404


def test_error_to_http_status_gateway_not_started() -> None:
    """GATEWAY_NOT_STARTED errors map to HTTP 503."""
    assert errors.error_to_http_status("GATEWAY_NOT_STARTED: gateway not ready") == 503


def test_error_to_http_status_admission_rejected_warming() -> None:
    """ADMISSION_REJECTED_WARMING errors map to HTTP 503."""
    assert (
        errors.error_to_http_status("ADMISSION_REJECTED_WARMING: too many warming")
        == 503
    )


def test_error_to_http_status_unknown_error() -> None:
    """Unknown errors map to HTTP 400 (default)."""
    assert errors.error_to_http_status("UNKNOWN_ERROR: something went wrong") == 400


def test_error_to_http_status_empty_error() -> None:
    """Empty error string maps to HTTP 400 (default)."""
    assert errors.error_to_http_status("") == 400


def test_error_to_http_status_mapping_table_complete() -> None:
    """All error prefixes in mapping table have classification helpers."""
    # Verify that every error prefix in ERROR_TO_HTTP_STATUS has a matching is_*_error helper
    assert errors.AUTH_INVALID_TOKEN in errors.ERROR_TO_HTTP_STATUS
    assert errors.CONNECTION_NOT_FOUND in errors.ERROR_TO_HTTP_STATUS
    assert errors.GATEWAY_NOT_STARTED in errors.ERROR_TO_HTTP_STATUS
    assert errors.ADMISSION_REJECTED_WARMING in errors.ERROR_TO_HTTP_STATUS
    # Verify expected HTTP status codes
    assert errors.ERROR_TO_HTTP_STATUS[errors.AUTH_INVALID_TOKEN] == 401
    assert errors.ERROR_TO_HTTP_STATUS[errors.CONNECTION_NOT_FOUND] == 404
    assert errors.ERROR_TO_HTTP_STATUS[errors.GATEWAY_NOT_STARTED] == 503
    assert errors.ERROR_TO_HTTP_STATUS[errors.ADMISSION_REJECTED_WARMING] == 503
