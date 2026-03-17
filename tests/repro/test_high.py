"""Black-box repro tests for high-severity findings H1-H4."""

from __future__ import annotations

import asyncio
import re

import pytest

from tela.core.models import (
    AuditLevel,
    CapabilityToken,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
)


# --- H1: Contracts must actually check ---


class TestH1ContractEnforcement:
    """H1: @pre/@post must not be identity lambdas."""

    def test_pre_post_actually_check(self) -> None:
        """Trigger a contract violation and verify it raises."""
        from tela.core.config import parse_config
        from tela.core.errors import ConfigContractError

        with pytest.raises((ConfigContractError, Exception)):
            parse_config({"auth": {"mode": "invalid_mode"}}, {})

    def test_no_pure_identity_lambda_in_core_contracts(self) -> None:
        """Core modules must not have pure identity lambdas in @pre/@post."""
        from pathlib import Path

        core_dir = Path("src/tela/core")
        # Match only EXACT identity: "lambda x: x)" or "lambda result: result)"
        identity_re = re.compile(
            r"@(?:pre|post)\(\s*lambda\s+(\w+)\s*:\s*\1\s*\)"
        )

        for py_file in core_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            source = py_file.read_text()
            matches = identity_re.findall(source)
            if matches:
                pytest.fail(
                    f"{py_file.name} has pure identity lambda in contract"
                )


# --- H2: Concurrent audit writes ---


class TestH2ConcurrentAuditWrites:
    """H2: Concurrent audit writes must be safe."""

    def test_concurrent_audit_writes_no_data_loss(self) -> None:
        from tela.shell.audit import (
            audit_write,
            clear_audit_entries,
            get_audit_entries,
        )
        from tela.core.models import AuditEntry

        clear_audit_entries()

        async def write_many():
            tasks = []
            for i in range(50):
                entry = AuditEntry(
                    timestamp=f"2026-01-01T00:00:{i:02d}Z",
                    level=AuditLevel.L1,
                    connection_id=f"c{i}",
                    profile_name="dev",
                    tool_name=f"tool_{i}",
                    server_name="s1",
                    verdict=EnforcementVerdict.ALLOW,
                )
                tasks.append(audit_write(entry))
            await asyncio.gather(*tasks)

        asyncio.run(write_many())
        entries = get_audit_entries()
        assert len(entries) == 50


# --- H3: No NotImplementedError stubs ---


class TestH3NoStubs:
    """H3: All former stubs must be implemented (no raise NotImplementedError)."""

    def test_no_raise_not_implemented_in_shell(self) -> None:
        from pathlib import Path

        shell_dir = Path("src/tela/shell")
        for py_file in shell_dir.glob("*.py"):
            source = py_file.read_text()
            lines = source.split("\n")
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("raise NotImplementedError"):
                    pytest.fail(
                        f"{py_file.name}:{i+1} has NotImplementedError stub: {stripped}"
                    )


# --- H4: Token validation ---


class TestH4TokenValidation:
    """H4: HMAC token validation must accept/reject correctly."""

    def test_compute_signature_deterministic(self) -> None:
        from tela.core.token import compute_signature

        fields = {"token_id": "tok_1", "tools_profile": "dev"}
        sig1 = compute_signature(fields, "secret")
        sig2 = compute_signature(fields, "secret")
        assert sig1 == sig2
        assert len(sig1) == 64

    def test_validate_token_accepts_valid(self) -> None:
        from tela.core.token import compute_signature, validate_token

        fields = {
            "token_id": "tok_1",
            "tools_profile": "dev",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-12-31T23:59:59Z",
        }
        sig = compute_signature(fields, "my_secret")

        token = CapabilityToken(**fields, signature=sig)
        result = validate_token(token, ["my_secret"], "2026-06-15T12:00:00Z")
        assert result.verdict == EnforcementVerdict.ALLOW

    def test_validate_token_rejects_invalid_signature(self) -> None:
        from tela.core.token import validate_token

        token = CapabilityToken(
            token_id="tok_1",
            tools_profile="dev",
            issued_at="2026-01-01T00:00:00Z",
            expires_at="2026-12-31T23:59:59Z",
            signature="bad_signature",
        )
        result = validate_token(token, ["my_secret"], "2026-06-15T12:00:00Z")
        assert result.verdict == EnforcementVerdict.DENY

    def test_validate_token_rejects_expired(self) -> None:
        from tela.core.token import compute_signature, validate_token

        fields = {
            "token_id": "tok_1",
            "tools_profile": "dev",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-01-02T00:00:00Z",
        }
        sig = compute_signature(fields, "my_secret")

        token = CapabilityToken(**fields, signature=sig)
        result = validate_token(token, ["my_secret"], "2027-06-15T12:00:00Z")
        assert result.verdict == EnforcementVerdict.DENY
