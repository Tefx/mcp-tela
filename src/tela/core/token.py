"""Token validation logic for HMAC-SHA256 capability tokens.

Pure logic -- receives current time and secrets as parameters.
Does NOT read secrets from environment or access system clock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from tela.core.contracts import pre, post
from tela.core.models import (
    CapabilityToken,
    EnforcementResult,
    EnforcementVerdict,
    TokenInitBinding,
)


@pre(
    lambda token_fields, secret: (
        isinstance(token_fields, dict) and isinstance(secret, str) and len(secret) > 0
    )
)
@post(lambda result: isinstance(result, str) and len(result) > 0)
def compute_signature(token_fields: dict, secret: str) -> str:
    """Compute HMAC-SHA256 signature over token fields.

    Input: all token fields except 'signature', serialized as JSON
    with keys in alphabetical order, no whitespace.

    Examples:
        >>> sig = compute_signature({"token_id": "tok_1", "profile_id": "dev"}, "secret")
        >>> isinstance(sig, str) and len(sig) == 64
        True

    Args:
        token_fields: Token fields (excluding 'signature').
        secret: HMAC secret key.

    Returns:
        Hex-encoded HMAC-SHA256 signature.
    """
    canonical = json.dumps(token_fields, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@pre(
    lambda expires_at, now_iso: isinstance(expires_at, str) and isinstance(now_iso, str)
)
@post(lambda result: isinstance(result, bool))
def is_expired(expires_at: str, now_iso: str) -> bool:
    """Check if a token has expired.

    Examples:
        >>> fields = {"token_id": "tok_1", "profile_id": "dev", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z"}
        >>> sig = compute_signature(fields, "secret")
        >>> isinstance(sig, str) and len(sig) == 64
        True
        >>> is_expired("2026-02-28T11:00:00Z", "2026-02-28T10:30:00Z")
        False

    Args:
        expires_at: Token expiration time (ISO-8601).
        now_iso: Current time (ISO-8601).

    Returns:
        True if the token has expired.
    """
    expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    return now_dt >= expires_dt


@pre(
    lambda token, secrets, now_iso: (
        isinstance(token, CapabilityToken)
        and isinstance(secrets, list)
        and len(secrets) > 0
        and isinstance(now_iso, str)
        and len(now_iso) > 0
    )
)
@post(lambda result: isinstance(result, EnforcementResult))
def validate_token(
    token: CapabilityToken,
    secrets: list[str],
    now_iso: str,
) -> EnforcementResult:
    """Validate a capability token against secrets and current time.

    Tries each secret (dual-key rotation). Checks HMAC signature, then expiry.

    Examples:
        >>> fields = {"token_id": "tok_1", "profile_id": "dev", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z"}
        >>> sig = compute_signature(fields, "secret1")
        >>> tok = CapabilityToken(**fields, signature=sig)
        >>> r = validate_token(tok, ["secret1"], "2026-06-01T00:00:00Z")
        >>> r.verdict
        <EnforcementVerdict.ALLOW: 'allow'>

    Args:
        token: Capability token to validate.
        secrets: List of HMAC secrets (primary + rotation keys).
        now_iso: Current time in ISO-8601 format.

    Returns:
        EnforcementResult with verdict ALLOW or DENY.
    """
    # Build token fields dict for signature verification (exclude 'signature')
    token_fields = token.model_dump(exclude={"signature"})
    # Remove None fields to match canonical form
    token_fields = {k: v for k, v in token_fields.items() if v is not None}

    # Try each secret (dual-key rotation)
    signature_valid = False
    for secret in secrets:
        expected = compute_signature(token_fields, secret)
        if hmac.compare_digest(expected, token.signature):
            signature_valid = True
            break

    if not signature_valid:
        return EnforcementResult(
            verdict=EnforcementVerdict.DENY,
            denied_by="token_validation",
            error_code="TOKEN_INVALID",
            error_message="Token signature verification failed",
        )

    # Check expiry
    if is_expired(token.expires_at, now_iso):
        return EnforcementResult(
            verdict=EnforcementVerdict.DENY,
            denied_by="token_validation",
            error_code="TOKEN_EXPIRED",
            error_message=f"Token expired at {token.expires_at}",
        )

    return EnforcementResult(verdict=EnforcementVerdict.ALLOW)


@pre(
    lambda token, secrets, now_iso: (
        isinstance(token, CapabilityToken)
        and len(token.token_id) > 0
        and len(token.profile_id) > 0
        and len(token.signature) > 0
        and isinstance(secrets, list)
        and len(secrets) > 0
        and isinstance(now_iso, str)
        and len(now_iso) > 0
    )
)
@post(lambda result: isinstance(result, TokenInitBinding))
def resolve_token_init_binding(
    token: CapabilityToken,
    secrets: list[str],
    now_iso: str,
) -> TokenInitBinding:
    """Resolve token-mode initialization binding.

    Validates the capability token and binds the connection to the token's
    profile name. Shell must reject initialization if the returned binding
    has `token_result.verdict == DENY`.

    Examples:
        >>> fields = {"token_id": "tok_1", "profile_id": "dev", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z"}
        >>> sig = compute_signature(fields, "secret1")
        >>> tok = CapabilityToken(**fields, signature=sig)
        >>> binding = resolve_token_init_binding(tok, ["secret1"], "2026-06-01T00:00:00Z")
        >>> binding.token_result.verdict
        <EnforcementVerdict.ALLOW: 'allow'>
        >>> binding.profile_id
        'dev'
        >>> bad_tok = CapabilityToken(**fields, signature="invalid_sig")
        >>> binding = resolve_token_init_binding(bad_tok, ["secret1"], "2026-06-01T00:00:00Z")
        >>> binding.token_result.verdict
        <EnforcementVerdict.DENY: 'deny'>

    Args:
        token: Capability token from upstream client.
        secrets: List of HMAC secrets (primary + rotation keys).
        now_iso: Current time in ISO-8601 format.

    Returns:
        TokenInitBinding with validation result and profile binding.
    """
    token_result = validate_token(token, secrets, now_iso)
    return TokenInitBinding(token_result=token_result, profile_id=token.profile_id)


@pre(
    lambda profile, secret, token_id="tok_auto", expires_at="2099-12-31T23:59:59Z", issued_at="2026-01-01T00:00:00Z": (
        isinstance(profile, str)
        and len(profile) > 0
        and isinstance(secret, str)
        and len(secret) > 0
        and isinstance(token_id, str)
        and len(token_id) > 0
        and isinstance(expires_at, str)
        and len(expires_at) > 0
        and isinstance(issued_at, str)
        and len(issued_at) > 0
    )
)
@post(lambda result: isinstance(result, CapabilityToken))
def create_token(
    profile: str,
    secret: str,
    token_id: str = "tok_auto",
    expires_at: str = "2099-12-31T23:59:59Z",
    issued_at: str = "2026-01-01T00:00:00Z",
) -> CapabilityToken:
    """Create a signed capability token (for testing).

    Examples:
        >>> tok = create_token("dev", "secret1")
        >>> tok.profile_id
        'dev'
        >>> r = validate_token(tok, ["secret1"], "2026-06-01T00:00:00Z")
        >>> r.verdict
        <EnforcementVerdict.ALLOW: 'allow'>

    Args:
        profile: Profile identifier bound by the capability token.
        secret: HMAC secret key.
        token_id: Token identifier.
        expires_at: Expiration time (ISO-8601).
        issued_at: Issue time (ISO-8601).

    Returns:
        Signed CapabilityToken.
    """
    fields: dict[str, str] = {
        "token_id": token_id,
        "profile_id": profile,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    sig = compute_signature(fields, secret)
    return CapabilityToken(
        token_id=token_id,
        profile_id=profile,
        issued_at=issued_at,
        expires_at=expires_at,
        signature=sig,
    )
