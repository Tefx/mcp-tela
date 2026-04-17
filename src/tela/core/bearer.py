"""Pure bearer-token parsing helpers."""

from __future__ import annotations

from tela.core.contracts import post, pre


@pre(lambda value: isinstance(value, str))
@post(lambda result: result is None or isinstance(result, str))
def extract_bearer_from_header_value(value: str) -> str | None:
    """Extract a bearer token from a decoded Authorization header value.

    Examples:
        >>> extract_bearer_from_header_value("Bearer secret")
        'secret'
        >>> extract_bearer_from_header_value("Basic secret") is None
        True
    """

    if not value.startswith("Bearer "):
        return None
    token = value[len("Bearer ") :].strip()
    if not token:
        return None
    return token
