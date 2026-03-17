"""Error code definitions and error model for Core zone.

Consolidates error types per DESIGN.md specification.
"""

from __future__ import annotations

from dataclasses import dataclass


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
PROFILE_NOT_FOUND: str = "PROFILE_NOT_FOUND"
TOKEN_INVALID: str = "TOKEN_INVALID"
TOKEN_EXPIRED: str = "TOKEN_EXPIRED"
TOOL_CONFLICT: str = "TOOL_CONFLICT"
TOOL_UNCLASSIFIED: str = "TOOL_UNCLASSIFIED"
CONFIG_PARSE_ERROR: str = "CONFIG_PARSE_ERROR"
CONFIG_ENV_UNSET: str = "CONFIG_ENV_UNSET"
CONFIG_FILE_MISSING: str = "CONFIG_FILE_MISSING"
