"""Pure transient-error classification helpers for HTTP retry logic."""

from __future__ import annotations

from tela.core.contracts import post, pre
from tela.shell.transient_types import (
    TRANSIENT_CONNECTION_EXCEPTIONS,
    TRANSIENT_ERRNOS,
)


@pre(lambda exc: hasattr(exc, "reason"))
@post(lambda result: isinstance(result, bool))
def is_transient_url_error(exc: Exception) -> bool:
    """Return whether a URLError is a transient connection failure.

    Examples:
        >>> class DummyError(Exception):
        ...     def __init__(self, reason):
        ...         self.reason = reason
        >>> is_transient_url_error(DummyError("Connection refused"))
        True
        >>> is_transient_url_error(DummyError("Name or service not known"))
        False
    """

    reason = getattr(exc, "reason", None)
    if isinstance(reason, OSError):
        if isinstance(reason, TRANSIENT_CONNECTION_EXCEPTIONS):
            return True
        return reason.errno in TRANSIENT_ERRNOS
    if isinstance(reason, str):
        normalized_reason = reason.lower()
        transient_reason_markers = (
            "connection refused",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "timed out",
            "temporarily unavailable",
        )
        return any(marker in normalized_reason for marker in transient_reason_markers)
    return False
