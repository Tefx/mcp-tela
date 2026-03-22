"""HTTP bearer authentication helpers.

Contracts-only module for gateway HTTP auth middleware.
"""

import hmac

from tela.shell.result import Result


def validate_bearer_token(request_token: str, expected_token: str) -> Result[None, str]:
    """Validate bearer token using constant-time comparison.

    Implementations must use ``hmac.compare_digest`` for constant-time token
    comparison.
    """

    if hmac.compare_digest(request_token, expected_token):
        return Result(value=None)
    return Result(error="AUTH_INVALID_TOKEN: bearer token validation failed")


# Internal compatibility symbol used by current tests and call-sites.
_validate_bearer_token = validate_bearer_token
