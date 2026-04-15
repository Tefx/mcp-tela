"""Expected-red tests for hard_cut.impl_core_vocab step.

Verifies:
- ProfileConfig no longer accepts `tools` alias (hard cut)
- CapabilityToken uses canonical `profile_id` (not `profile_name`)
- CapabilityToken rejects legacy `profile_name` field fail-closed
- parse_config rejects `tools:` key in profile YAML data
- normalize_profile_config_aliases module is deleted
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tela.core.models import (
    CapabilityToken,
    Posture,
    ProfileConfig,
)
from tela.core.token import compute_signature, create_token, validate_token


# ==============================================================================
# (1) ProfileConfig rejects `tools=` keyword argument (hard cut)
# ==============================================================================


class TestProfileConfigRejectsToolsAlias:
    """ProfileConfig must NOT accept `tools=` as a keyword argument."""

    def test_tools_kwarg_rejected(self) -> None:
        """ProfileConfig(tools={...}) must raise because `tools` alias is removed."""
        with pytest.raises((TypeError, ValidationError)):
            ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})  # type: ignore[call-arg]

    def test_tools_property_removed(self) -> None:
        """ProfileConfig must NOT have a `.tools` property."""
        p = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
        with pytest.raises(AttributeError):
            _ = p.tools  # type: ignore[attr-defined]

    def test_capabilities_is_canonical(self) -> None:
        """ProfileConfig(capabilities={...}) must work as the canonical field."""
        p = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
        assert p.capabilities["fs"] == Posture.READ_WRITE

    def test_capabilities_default_empty(self) -> None:
        """ProfileConfig with no capabilities must default to empty dict."""
        p = ProfileConfig(name="dev")
        assert p.capabilities == {}


# ==============================================================================
# (2) CapabilityToken uses `profile_id` (not `profile_name`)
# ==============================================================================


class TestCapabilityTokenCanonicalProfileId:
    """CapabilityToken must use `profile_id` as canonical identity field."""

    def test_token_has_profile_id_field(self) -> None:
        """CapabilityToken must expose `profile_id` field."""
        tok = CapabilityToken(
            token_id="tok_1",
            profile_id="dev",
            issued_at="2026-01-01T00:00:00Z",
            expires_at="2026-12-31T23:59:59Z",
            signature="abc",
        )
        assert tok.profile_id == "dev"

    def test_token_requires_profile_id(self) -> None:
        """CapabilityToken must require `profile_id` (not optional)."""
        with pytest.raises(ValidationError):
            CapabilityToken(  # type: ignore[call-arg]
                token_id="tok_1",
                issued_at="2026-01-01T00:00:00Z",
                expires_at="2026-12-31T23:59:59Z",
                signature="abc",
            )

    def test_token_rejects_profile_name_alias_fail_closed(self) -> None:
        """CapabilityToken must NOT accept `profile_name` as an alias for `profile_id`.

        Fail-closed: legacy `profile_name` field must not silently map to `profile_id`.
        """
        with pytest.raises(ValidationError):
            CapabilityToken(  # type: ignore[call-arg]
                token_id="tok_1",
                profile_name="dev",
                issued_at="2026-01-01T00:00:00Z",
                expires_at="2026-12-31T23:59:59Z",
                signature="abc",
            )


# ==============================================================================
# (3) Token functions use `profile_id` canonical field
# ==============================================================================


class TestTokenFunctionsUseProfileId:
    """Core token functions must use `profile_id` throughout."""

    def test_create_token_uses_profile_id(self) -> None:
        """create_token must bind the profile parameter to `profile_id`."""
        tok = create_token("dev", "secret1")
        assert tok.profile_id == "dev"

    def test_validate_token_with_profile_id(self) -> None:
        """validate_token must work with `profile_id`-bound tokens."""
        tok = create_token("dev", "secret1")
        result = validate_token(tok, ["secret1"], "2026-06-01T00:00:00Z")
        from tela.core.models import EnforcementVerdict

        assert result.verdict == EnforcementVerdict.ALLOW

    def test_compute_signature_includes_profile_id(self) -> None:
        """compute_signature must include `profile_id` in the canonical field set."""
        fields = {
            "token_id": "tok_1",
            "profile_id": "dev",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-12-31T23:59:59Z",
        }
        sig = compute_signature(fields, "secret1")
        assert isinstance(sig, str) and len(sig) == 64


# ==============================================================================
# (4) parse_config rejects `tools:` key in profile YAML data
# ==============================================================================


class TestParseConfigRejectsToolsKey:
    """parse_config must NOT accept `tools:` in profile data (hard cut)."""

    def test_parse_config_rejects_tools_key_in_profile(self) -> None:
        """Profile data with `tools:` key must be rejected (not aliased)."""
        from tela.core.config import parse_config
        from tela.core.errors import ConfigContractError

        raw_config = {
            "profiles": {
                "dev": {
                    "name": "dev",
                    "tools": {"filesystem": "read_only"},
                }
            },
            "auth": {"mode": "open"},
        }
        with pytest.raises((ConfigContractError, ValidationError)):
            parse_config(raw_config, {})

    def test_parse_config_accepts_capabilities_key(self) -> None:
        """Profile data with `capabilities:` key must still work."""
        from tela.core.config import parse_config

        raw_config = {
            "profiles": {
                "dev": {
                    "name": "dev",
                    "capabilities": {"filesystem": "read_write"},
                }
            },
            "auth": {"mode": "open"},
        }
        config = parse_config(raw_config, {})
        assert config.profiles["dev"].capabilities["filesystem"] == Posture.READ_WRITE


# ==============================================================================
# (5) normalize_profile_config_aliases module deleted
# ==============================================================================


class TestProfileAliasesModuleDeleted:
    """The profile_aliases module must no longer exist."""

    def test_import_profile_aliases_raises(self) -> None:
        """Importing tela.core.profile_aliases must raise ImportError."""
        with pytest.raises(ImportError):
            import tela.core.profile_aliases  # type: ignore[import]  # noqa: F401
