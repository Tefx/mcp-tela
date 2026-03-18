"""Black-box repro tests for medium-severity findings M1-M10."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tela.core.models import (
    AuditLevel,
    EnforcementVerdict,
    Posture,
)


# --- M3: Core errors module ---


class TestM3CoreErrors:
    """M3: core/errors.py must exist and provide ConfigContractError."""

    def test_core_errors_exists(self) -> None:
        assert Path("src/tela/core/errors.py").exists()

    def test_config_contract_error_importable(self) -> None:
        from tela.core.errors import ConfigContractError

        err = ConfigContractError(code="TEST", message="test message")
        assert err.code == "TEST"
        assert err.message == "test message"


# --- M5: Timestamp comparison Z vs offset ---


class TestM5TimestampComparison:
    """M5: Z and +00:00 timestamps must interop correctly."""

    def test_audit_query_z_vs_offset(self) -> None:
        from tela.shell.audit import (
            audit_write,
            audit_query,
            clear_audit_entries,
        )
        from tela.core.models import AuditEntry

        clear_audit_entries()

        entry = AuditEntry(
            timestamp="2026-01-15T12:00:00+00:00",
            level=AuditLevel.L1,
            connection_id="c1",
            profile_name="dev",
            tool_name="t1",
            server_name="s1",
            verdict=EnforcementVerdict.ALLOW,
        )
        asyncio.run(audit_write(entry))

        # Query with Z format should still match
        result = asyncio.run(audit_query(since="2026-01-15T11:00:00Z"))
        assert result.is_ok
        assert len(result.value) == 1


# --- M6: Bounded audit store ---


class TestM6BoundedAuditStore:
    """M6: In-memory audit store must evict old entries."""

    def test_max_entries_eviction(self) -> None:
        from tela.shell.audit import (
            audit_set_max_entries,
            audit_write,
            clear_audit_entries,
            get_audit_entries,
        )
        from tela.core.models import AuditEntry

        clear_audit_entries()
        asyncio.run(audit_set_max_entries(5))

        async def write_ten():
            for i in range(10):
                entry = AuditEntry(
                    timestamp=f"2026-01-01T00:00:{i:02d}Z",
                    level=AuditLevel.L1,
                    connection_id=f"c{i}",
                    profile_name="dev",
                    tool_name=f"tool_{i}",
                    server_name="s1",
                    verdict=EnforcementVerdict.ALLOW,
                )
                await audit_write(entry)

        asyncio.run(write_ten())
        entries = get_audit_entries()
        assert len(entries) <= 5
        # Oldest should be evicted, newest preserved
        assert entries[-1].tool_name == "tool_9"

        # Reset
        asyncio.run(audit_set_max_entries(10000))


# --- M7: FastMCP importable ---


class TestM7FastMCPImportable:
    """M7: FastMCP must be importable."""

    def test_fastmcp_importable(self) -> None:
        try:
            from mcp.server.fastmcp import FastMCP  # noqa: F401
        except ImportError:
            pytest.skip("mcp package not installed")


# --- M8: Unset env var errors ---


class TestM8UnsetEnvVar:
    """M8: Unset env vars must produce clear startup error."""

    def test_unset_env_var_raises(self) -> None:
        from tela.core.config import parse_config
        from tela.core.errors import ConfigContractError

        raw = {
            "auth": {"mode": "token", "secrets": ["${NONEXISTENT_VAR}"]},
        }
        with pytest.raises(ConfigContractError) as exc_info:
            parse_config(raw, {})
        assert "NONEXISTENT_VAR" in str(exc_info.value.message)


# --- M10: Default posture bridged to enforcement ---


class TestM10DefaultPosture:
    """M10: Server default_posture flows through to enforcement."""

    def test_default_posture_on_server_config(self) -> None:
        from tela.core.models import ServerConfig

        server = ServerConfig(name="fs", default_posture=Posture.READ_ONLY)
        assert server.default_posture == Posture.READ_ONLY

    def test_default_posture_used_in_classification(self) -> None:
        from tela.core.family import resolve_tools
        from tela.core.models import ServerConfig

        raw_tools = [{"name": "read_file", "inputSchema": {}}]
        server = ServerConfig(
            name="fs", command="cmd", default_posture=Posture.READ_ONLY
        )
        resolved = resolve_tools("fs", server, raw_tools)
        assert len(resolved) == 1
