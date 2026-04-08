"""Error code definitions and error model for Core zone.

Consolidates error types per DESIGN.md specification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tela.core.contracts import pre


@dataclass(frozen=True)
class ConfigContractError(Exception):
    """Contract-level configuration rejection.

    Attributes:
        code: Stable contract error code.
        message: Human-readable reason for rejection.

    Examples:
        >>> err = ConfigContractError(code="CONFIG_PARSE_ERROR", message="bad config")
        >>> err.code
        'CONFIG_PARSE_ERROR'
    """

    code: str
    message: str


# Stable error code constants per DESIGN.md
AUTHZ_DENY: str = "AUTHZ_DENY"
AUTH_RATE_LIMITED: str = "AUTH_RATE_LIMITED"
PROFILE_NOT_FOUND: str = "PROFILE_NOT_FOUND"
TOKEN_INVALID: str = "TOKEN_INVALID"
TOKEN_EXPIRED: str = "TOKEN_EXPIRED"
TOOL_CONFLICT: str = "TOOL_CONFLICT"
TOOL_UNCLASSIFIED: str = "TOOL_UNCLASSIFIED"
CONFIG_PARSE_ERROR: str = "CONFIG_PARSE_ERROR"
CONFIG_ENV_UNSET: str = "CONFIG_ENV_UNSET"
CONFIG_FILE_MISSING: str = "CONFIG_FILE_MISSING"

# Shared shell error codes (canonical source: core/errors.py)
# All downstream/Gateway/http-layer error codes are defined here to avoid
# scattering string literals across modules.
# Use Literal types to preserve type-checked API contracts.

# Auth
AUTH_INVALID_TOKEN: Literal["AUTH_INVALID_TOKEN"] = "AUTH_INVALID_TOKEN"

# Gateway lifecycle
GATEWAY_NOT_STARTED: Literal["GATEWAY_NOT_STARTED"] = "GATEWAY_NOT_STARTED"

# Connection lifecycle
CONNECTION_NOT_FOUND: Literal["CONNECTION_NOT_FOUND"] = "CONNECTION_NOT_FOUND"

# Admission / MCP lifecycle
ADMISSION_REJECTED_WARMING: Literal["ADMISSION_REJECTED_WARMING"] = (
    "ADMISSION_REJECTED_WARMING"
)

# Downstream runtime
DOWNSTREAM_UNAVAILABLE: Literal["DOWNSTREAM_UNAVAILABLE"] = "DOWNSTREAM_UNAVAILABLE"
DOWNSTREAM_CONNECT_FAILED: Literal["DOWNSTREAM_CONNECT_FAILED"] = (
    "DOWNSTREAM_CONNECT_FAILED"
)
DOWNSTREAM_ERROR: Literal["DOWNSTREAM_ERROR"] = "DOWNSTREAM_ERROR"


# --------------------------------------------------------------------
# Shared prefix / classification helpers
# Canonical home: core/errors.py
# Used by shell/core consumers to check error-code classification without
# duplicating string-literal prefix patterns.
# --------------------------------------------------------------------


# --------------------------------------------------------------------
# HTTP status code mapping for shell/gateway error responses
# Data-driven classification using shared error-prefix semantics.
# --------------------------------------------------------------------

# Type alias for error-prefix keys (Literal types from constants above)
ErrorPrefix = Literal[
    "AUTH_INVALID_TOKEN",
    "CONNECTION_NOT_FOUND",
    "GATEWAY_NOT_STARTED",
    "ADMISSION_REJECTED_WARMING",
]

# Canonical mapping from error prefix to HTTP status code
# Used by shell/gateway._as_error_response to preserve externally visible status outcomes.
ERROR_TO_HTTP_STATUS: dict[ErrorPrefix, int] = {
    AUTH_INVALID_TOKEN: 401,
    CONNECTION_NOT_FOUND: 404,
    GATEWAY_NOT_STARTED: 503,
    ADMISSION_REJECTED_WARMING: 503,
}

# Default HTTP status for unclassified errors
DEFAULT_HTTP_ERROR_STATUS: int = 400


@pre(lambda error: isinstance(error, str))
def is_auth_error(error: str) -> bool:
    """Return True when an error message has AUTH_INVALID_TOKEN prefix.

    Examples:
        >>> is_auth_error("AUTH_INVALID_TOKEN: bearer token validation failed")
        True
        >>> is_auth_error("AUTHZ_DENY: permission denied")
        False
    """
    return error.startswith(AUTH_INVALID_TOKEN)


@pre(lambda error: isinstance(error, str))
def is_gateway_not_started_error(error: str) -> bool:
    """Return True when an error message has GATEWAY_NOT_STARTED prefix.

    Examples:
        >>> is_gateway_not_started_error("GATEWAY_NOT_STARTED: gateway not ready")
        True
        >>> is_gateway_not_started_error("AUTH_INVALID_TOKEN: bearer token validation failed")
        False
    """
    return error.startswith(GATEWAY_NOT_STARTED)


@pre(lambda error: isinstance(error, str))
def is_connection_not_found_error(error: str) -> bool:
    """Return True when an error message has CONNECTION_NOT_FOUND prefix.

    Examples:
        >>> is_connection_not_found_error("CONNECTION_NOT_FOUND: id=abc")
        True
        >>> is_connection_not_found_error("AUTH_INVALID_TOKEN: bearer token validation failed")
        False
    """
    return error.startswith(CONNECTION_NOT_FOUND)


@pre(lambda error: isinstance(error, str))
def is_admission_rejected_warming_error(error: str) -> bool:
    """Return True when an error message has ADMISSION_REJECTED_WARMING prefix.

    Examples:
        >>> is_admission_rejected_warming_error("ADMISSION_REJECTED_WARMING: too many warming")
        True
        >>> is_admission_rejected_warming_error("AUTH_INVALID_TOKEN: bearer token validation failed")
        False
    """
    return error.startswith(ADMISSION_REJECTED_WARMING)


@pre(lambda error: isinstance(error, str))
def error_to_http_status(error: str) -> int:
    """Return HTTP status code for an error message using shared classification.

    Preserves the same HTTP status outcomes as the previous ad-hoc mapping
    in shell/gateway._as_error_response:
        - AUTH_INVALID_TOKEN → 401
        - CONNECTION_NOT_FOUND → 404
        - GATEWAY_NOT_STARTED → 503
        - ADMISSION_REJECTED_WARMING → 503
        - <unclassified> → 400 (default)

    Examples:
        >>> error_to_http_status("AUTH_INVALID_TOKEN: bearer token validation failed")
        401
        >>> error_to_http_status("CONNECTION_NOT_FOUND: id=abc")
        404
        >>> error_to_http_status("GATEWAY_NOT_STARTED: gateway not ready")
        503
        >>> error_to_http_status("ADMISSION_REJECTED_WARMING: too many warming")
        503
        >>> error_to_http_status("UNKNOWN_ERROR: something went wrong")
        400
    """
    # Use classification helpers for data-driven mapping
    # Order matches original gateway._as_error_response for exact equivalence
    if is_auth_error(error):
        return ERROR_TO_HTTP_STATUS[AUTH_INVALID_TOKEN]
    if is_connection_not_found_error(error):
        return ERROR_TO_HTTP_STATUS[CONNECTION_NOT_FOUND]
    if is_gateway_not_started_error(error):
        return ERROR_TO_HTTP_STATUS[GATEWAY_NOT_STARTED]
    if is_admission_rejected_warming_error(error):
        return ERROR_TO_HTTP_STATUS[ADMISSION_REJECTED_WARMING]
    return DEFAULT_HTTP_ERROR_STATUS
