"""Shared HTTP request → retry → backoff → Result skeleton.

Provides ``retry_http_request`` which centralizes the urllib request
construction and retry/backoff semantics used across tela's connect
bridge HTTP call sites. Response parsing, session handling, and payload
interpretation remain in the caller.

Architecture
============

This module owns **only** the request/retry/backoff/result skeleton:

- Building the ``urllib_request.Request`` from caller-supplied parameters
- Executing ``urlopen`` with configurable timeout
- Retrying on 503 status codes (opt-in via ``retry_on_503``)
- Retrying on transient connection errors (opt-in via ``retry_on_transient``)
- Linear backoff between retries (``backoff_seconds * attempt``)
- Returning ``Result[HTTPResponse, str]`` on success or exhaustion

What this module does **not** own:

- Response body/header parsing (caller responsibility)
- Bearer header construction (caller passes ``headers`` dict)
- Session/lifecycle management (caller responsibility)
- Error message semantics beyond the generic ``HTTP_{code}`` /
  ``HTTP_CONNECT_ERROR`` prefixes
"""

from __future__ import annotations

import http.client
import time
from urllib import error as urllib_error
from urllib import request as urllib_request

from tela.shell.config_loader import Result
from tela.shell.transient_types import (
    TRANSIENT_CONNECTION_EXCEPTIONS,
    TRANSIENT_ERRNOS,
)


# @invar:allow shell_result: pure boolean classifier returning bool, not a failable I/O boundary.
def _is_transient_url_error(exc: urllib_error.URLError) -> bool:
    """Classify whether a URLError is a transient connection failure.

    Transient failures (connection refused, reset, broken pipe) can occur
    when the gateway HTTP server is still starting up or temporarily
    unreachable. Non-transient failures (DNS, SSL, etc.) should not be
    retried.

    This is the canonical classification for the retry skeleton. It
    mirrors the same logic previously defined inline in
    ``connect_cmd._is_transient_url_error`` but returns a plain ``bool``
    rather than ``Result[bool, str]`` — the ``Result`` wrapper added
    no semantic value since classification never fails.

    Args:
        exc: The URLError to classify.

    Returns:
        ``True`` if the underlying error is a transient connection
        failure, ``False`` otherwise.
    """
    reason = exc.reason
    if isinstance(reason, OSError):
        # Prefer type-based classification: Python's builtin subclasses
        # (ConnectionRefusedError, ConnectionResetError, etc.) may carry
        # errno=None when constructed with only a message string — which is
        # the common pattern in both production urllib and test fixtures.
        if isinstance(reason, TRANSIENT_CONNECTION_EXCEPTIONS):
            return True

        # Fallback: errno check for generic OSError instances raised by the
        # OS with a numeric errno but no dedicated exception subclass.
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


# @shell_complexity: retry/backoff loop branches on HTTPError code, transient
# classification, and attempt exhaustion — inherent to the retry skeleton.
def retry_http_request(
    *,
    url: str,
    method: str = "POST",
    headers: dict[str, str],
    data: bytes | None = None,
    max_retries: int = 3,
    timeout_seconds: float = 10.0,
    backoff_seconds: float = 0.5,
    retry_on_503: bool = True,
    retry_on_transient: bool = True,
) -> Result[http.client.HTTPResponse, str]:
    """Execute an HTTP request with retry and backoff on transient failures.

    Performs up to ``max_retries + 1`` attempts. On success, returns the
    open ``http.client.HTTPResponse`` — the caller **must** close it
    (preferably via ``with result.value:`` or ``result.value.close()``).

    Retry conditions:

    - **503**: Retried when ``retry_on_503=True`` and attempts remain.
    - **Transient URLError**: Retried when ``retry_on_transient=True``,
      the error is classified as transient by ``_is_transient_url_error``,
      and attempts remain.

    Backoff is linear: ``backoff_seconds * (attempt + 1)``. Attempt
    numbering starts at 0.

    ``_post_json_once`` (single attempt, no retry) maps to
    ``max_retries=0, retry_on_503=False, retry_on_transient=False``.
    ``_post_json`` (transient retry with backoff) maps to
    ``max_retries=3, retry_on_503=True, retry_on_transient=True``.

    Args:
        url: Full URL to request.
        method: HTTP method (default ``"POST"``).
        headers: Complete request headers — caller constructs including
            ``Authorization``. This preserves Bearer header semantics at
            the edge.
        data: Request body bytes, or ``None`` for bodyless requests.
        max_retries: Maximum retry attempts (0 = single attempt, no retry).
        timeout_seconds: Per-attempt socket timeout.
        backoff_seconds: Base backoff interval (multiplied by attempt number).
        retry_on_503: Whether to retry on HTTP 503 responses.
        retry_on_transient: Whether to retry on transient URLErrors.

    Returns:
        ``Result`` with the open ``HTTPResponse`` on success, or an error
        string (``HTTP_{code}: {url}`` or ``HTTP_CONNECT_ERROR: {reason}``)
        on failure.
    """
    last_error: str = ""
    for attempt in range(max_retries + 1):
        request = urllib_request.Request(
            url,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            response = urllib_request.urlopen(request, timeout=timeout_seconds)
            return Result(value=response)
        except urllib_error.HTTPError as exc:
            if exc.code == 503 and retry_on_503 and attempt < max_retries:
                time.sleep(backoff_seconds * (attempt + 1))
                continue
            return Result(error=f"HTTP_{exc.code}: {url}")
        except urllib_error.URLError as exc:
            last_error = f"HTTP_CONNECT_ERROR: {exc.reason}"
            if (
                not retry_on_transient
                or not _is_transient_url_error(exc)
                or attempt == max_retries
            ):
                return Result(error=last_error)
            time.sleep(backoff_seconds * (attempt + 1))

    return Result(error=last_error)
