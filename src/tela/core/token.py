"""Token validation logic for HMAC-SHA256 capability tokens.

Pure logic -- receives current time and secrets as parameters.
Does NOT read secrets from environment or access system clock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Mapping

from tela.core.contracts import pre, post
from tela.core.models import (
    CapabilityToken,
    EnforcementResult,
    EnforcementVerdict,
    TokenInitBinding,
)


@pre(lambda token_fields: isinstance(token_fields, Mapping))
@post(
    lambda result: (
        isinstance(result, dict)
        and "signature" not in result
        and result.get("token_version") == "0.1.0"
        and isinstance(result.get("persona_ref"), str)
        and isinstance(result.get("instance_id"), str)
    )
)
def canonicalize_token_fields(token_fields: Mapping[str, object]) -> dict[str, object]:
    """Return the canonical capability-token payload used for signing.

    The returned payload matches runtime validation semantics: it excludes the
    detached ``signature`` field and preserves all required schema fields,
    including ``token_version``.

    Examples:
        >>> canonicalize_token_fields({"token_id": "tok_1", "profile_id": "dev", "persona_ref": "persona.dev", "instance_id": "inst-1", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z", "token_version": "0.1.0", "signature": "abc"})["token_version"]
        '0.1.0'

    Args:
        token_fields: Candidate capability-token payload.

    Returns:
        Canonical payload for HMAC signing and verification.

    Raises:
        ValueError: If a required signing field is missing or ``None``.
    """
    canonical = {
        key: value
        for key, value in token_fields.items()
        if key != "signature" and value is not None
    }
    required_fields = (
        "token_id",
        "profile_id",
        "persona_ref",
        "instance_id",
        "issued_at",
        "expires_at",
        "token_version",
    )
    missing = [field for field in required_fields if field not in canonical]
    if missing:
        raise ValueError(
            "Capability token payload missing required signing fields: "
            + ", ".join(missing)
        )
    return canonical


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
        >>> sig = compute_signature({"token_id": "tok_1", "profile_id": "dev", "persona_ref": "persona.dev", "instance_id": "inst-1", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z", "token_version": "0.1.0"}, "secret")
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
        >>> fields = {"token_id": "tok_1", "profile_id": "dev", "persona_ref": "persona.dev", "instance_id": "inst-1", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z", "token_version": "0.1.0"}
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
        >>> fields = {"token_id": "tok_1", "profile_id": "dev", "persona_ref": "persona.dev", "instance_id": "inst-1", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z", "token_version": "0.1.0"}
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
    token_fields = canonicalize_token_fields(token.model_dump())

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
        >>> fields = {"token_id": "tok_1", "profile_id": "dev", "persona_ref": "persona.dev", "instance_id": "inst-1", "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-12-31T23:59:59Z", "token_version": "0.1.0"}
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
    lambda profile, secret, token_id="tok_auto", expires_at="2099-12-31T23:59:59Z", issued_at="2026-01-01T00:00:00Z", persona_ref="persona.default", instance_id="instance.default", token_version="0.1.0": (
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
        and isinstance(persona_ref, str)
        and len(persona_ref) > 0
        and isinstance(instance_id, str)
        and len(instance_id) > 0
        and token_version == "0.1.0"
    )
)
@post(lambda result: isinstance(result, CapabilityToken))
def create_token(
    profile: str,
    secret: str,
    token_id: str = "tok_auto",
    expires_at: str = "2099-12-31T23:59:59Z",
    issued_at: str = "2026-01-01T00:00:00Z",
    persona_ref: str = "persona.default",
    instance_id: str = "instance.default",
    token_version: str = "0.1.0",
) -> CapabilityToken:
    """Create a signed capability token (for testing).

    Examples:
        >>> tok = create_token("dev", "secret1")
        >>> tok.profile_id
        'dev'
        >>> tok.persona_ref
        'persona.default'
        >>> r = validate_token(tok, ["secret1"], "2026-06-01T00:00:00Z")
        >>> r.verdict
        <EnforcementVerdict.ALLOW: 'allow'>

    Args:
        profile: Profile identifier bound by the capability token.
        secret: HMAC secret key.
        token_id: Token identifier.
        expires_at: Expiration time (ISO-8601).
        issued_at: Issue time (ISO-8601).
        persona_ref: Persona reference bound into the token.
        instance_id: Agent instance identifier bound into the token.
        token_version: Canonical token schema version.

    Returns:
        Signed CapabilityToken.
    """
    fields: dict[str, str] = {
        "token_id": token_id,
        "profile_id": profile,
        "persona_ref": persona_ref,
        "instance_id": instance_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "token_version": token_version,
    }
    sig = compute_signature(canonicalize_token_fields(fields), secret)
    return CapabilityToken(
        token_id=token_id,
        profile_id=profile,
        persona_ref=persona_ref,
        instance_id=instance_id,
        issued_at=issued_at,
        expires_at=expires_at,
        token_version=fields["token_version"],
        signature=sig,
    )
