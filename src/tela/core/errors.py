"""Error code definitions and error model for Core zone.

Consolidates error types per DESIGN.md specification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


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
