"""HTTP bearer authentication helpers.

Contracts-only module for gateway HTTP auth middleware.
"""

import hmac


# @invar:allow shell_orchestration: middleware boundary requires bool return for auth gate contract.
# @invar:allow shell_result: explicit bool contract required by middleware signature.
def _validate_bearer_token(request_token: str, expected_token: str) -> bool:
    """Validate bearer token using constant-time comparison.

    Implementations must use ``hmac.compare_digest`` for constant-time token
    comparison.
    """

    return hmac.compare_digest(request_token, expected_token)


# Public contract symbol referenced by middleware wiring.
validate_bearer_token = _validate_bearer_token
