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
