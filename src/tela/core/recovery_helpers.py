"""Pure helpers for downstream recovery classification and error shaping."""

from __future__ import annotations

import asyncio

from tela.core.contracts import post, pre
from tela.core.errors import DOWNSTREAM_UNAVAILABLE
from tela.core.models import TelaError

_ELIGIBLE_RUNTIME_ERRORS: tuple[str, ...] = (
    "Client is not connected. Use the 'async with client:' context manager first.",
    "Server session was closed unexpectedly",
)


@pre(lambda exc: isinstance(exc, Exception))
@post(lambda result: isinstance(result, str) and len(result) > 0)
def get_exception_text(exc: Exception) -> str:
    """Return normalized exception text for recovery diagnostics.

    Examples:
        >>> get_exception_text(RuntimeError("boom"))
        'RuntimeError: boom'
    """

    return f"{type(exc).__name__}: {exc}"


@pre(lambda exc: isinstance(exc, Exception))
@post(lambda result: isinstance(result, bool))
def is_recovery_eligible_exception(exc: Exception) -> bool:
    """Classify transport failures that are safe for one automatic retry.

    Examples:
        >>> is_recovery_eligible_exception(RuntimeError("Client is not connected. Use the 'async with client:' context manager first."))
        True
        >>> is_recovery_eligible_exception(BrokenPipeError("boom"))
        False
    """

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return False
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return False
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        return any(msg == expected for expected in _ELIGIBLE_RUNTIME_ERRORS)
    return False


@post(lambda result: isinstance(result, TelaError))
def build_recovery_error(
    server_name: str,
    *,
    recovery_attempted: bool,
    recovery_eligible: bool,
    recovery_stage: str,
    underlying_error: str,
    config_missing: bool | None = None,
) -> TelaError:
    """Build the canonical TelaError envelope for recovery outcomes.

    Examples:
        >>> err = build_recovery_error("srv", recovery_attempted=True, recovery_eligible=True, recovery_stage="reconnect_started", underlying_error="boom")
        >>> err.code
        'DOWNSTREAM_UNAVAILABLE'
    """

    details: dict[str, object] = {
        "server_name": server_name,
        "recovery_attempted": recovery_attempted,
        "recovery_stage": recovery_stage,
        "recovery_eligible": recovery_eligible,
        "underlying_error": underlying_error,
    }
    if config_missing is not None:
        details["config_missing"] = config_missing
    return TelaError(
        code=DOWNSTREAM_UNAVAILABLE,
        message=f"Downstream server '{server_name}' is not connected",
        details=details,
    )
