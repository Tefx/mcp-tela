"""Black-box repro tests for blocker findings B1-B4."""

from __future__ import annotations

import asyncio

import pytest

from tela.core.models import (
    AuditConfig,
    AuditLevel,
    ResolvedTool,
)


# --- B1: Wire all CLI subcommands ---


class TestB1WireCliSubcommands:
    """B1: All 5 CLI subcommands must be registered and callable."""

    def test_all_subcommands_registered(self) -> None:
        from tela.cli import main

        # main with no args returns 1 and prints help (not crash)
        assert main([]) == 1

    def test_start_subcommand_exists(self) -> None:
        from tela.cli import main

        # --help exits with SystemExit(0)
        with pytest.raises(SystemExit) as exc_info:
            main(["start", "--help"])
        assert exc_info.value.code == 0

    def test_status_subcommand_exists(self) -> None:
        from tela.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["status", "--help"])
        assert exc_info.value.code == 0

    def test_profiles_subcommand_exists(self) -> None:
        from tela.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["profiles", "--help"])
        assert exc_info.value.code == 0

    def test_connections_subcommand_exists(self) -> None:
        from tela.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["connections", "--help"])
        assert exc_info.value.code == 0

    def test_audit_subcommand_exists(self) -> None:
        from tela.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["audit", "--help"])
        assert exc_info.value.code == 0

    def test_status_command_callable(self) -> None:
        """tela status returns int exit code without crash."""
        from tela.commands.status_cmd import status_command

        result = status_command(json_output=False)
        assert isinstance(result, int)

    def test_connections_command_callable(self) -> None:
        from tela.commands.connections_cmd import connections_command

        result = connections_command(json_output=False)
        assert isinstance(result, int)

    def test_audit_command_callable(self) -> None:
        from tela.commands.audit_cmd import audit_command

        result = audit_command(json_output=False)
        assert isinstance(result, int)


# --- B2: Wire AuditConfig into audit writer ---


class TestB2AuditConfigWiring:
    """B2: AuditConfig.level and .output must be consumed."""

    def test_audit_init_sets_path(self, tmp_path) -> None:
        from tela.shell.audit import audit_init
        import tela.shell.audit as audit_mod

        path = str(tmp_path / "audit.jsonl")
        config = AuditConfig(level=AuditLevel.L1, output=path)
        result = asyncio.run(audit_init(config))
        assert result.is_ok
        assert audit_mod._audit_log_path is not None

    def test_audit_init_expands_tilde(self, tmp_path, monkeypatch) -> None:
        from tela.shell.audit import audit_init
        import tela.shell.audit as audit_mod

        monkeypatch.setenv("HOME", str(tmp_path))
        config = AuditConfig(level=AuditLevel.L2, output="~/audit.jsonl")
        result = asyncio.run(audit_init(config))
        assert result.is_ok
        assert audit_mod._audit_log_path is not None
        assert "~" not in str(audit_mod._audit_log_path)

    def test_audit_write_persists_to_disk(self, tmp_path) -> None:
        from tela.shell.audit import audit_init, audit_write, audit_close
        from tela.core.models import (
            AuditEntry,
            AuditLevel,
            EnforcementVerdict,
        )

        path = tmp_path / "audit.jsonl"
        asyncio.run(audit_init(AuditConfig(level=AuditLevel.L1, output=str(path))))

        entry = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            level=AuditLevel.L1,
            connection_id="c1",
            profile_name="dev",
            tool_name="t1",
            server_name="s1",
            verdict=EnforcementVerdict.ALLOW,
        )
        asyncio.run(audit_write(entry))

        assert path.exists()
        content = path.read_text()
        assert "t1" in content

        asyncio.run(audit_close())

    def test_audit_level_filters_param_hash(self) -> None:
        from tela.shell.audit import build_audit_entry
        from tela.core.models import (
            AuditLevel,
            ConnectionContext,
            EnforcementResult,
            EnforcementVerdict,
        )

        conn = ConnectionContext(
            connection_id="c1",
            profile_name="dev",
            connected_at="2026-01-01T00:00:00Z",
        )
        allow = EnforcementResult(verdict=EnforcementVerdict.ALLOW)

        l1 = build_audit_entry(
            AuditLevel.L1, conn, "t1", "s1", allow, arguments={"key": "val"}
        )
        assert l1.param_hash is None

        l2 = build_audit_entry(
            AuditLevel.L2, conn, "t1", "s1", allow, arguments={"key": "val"}
        )
        assert l2.param_hash is not None
        assert l2.param_hash.startswith("sha256:")


# --- B3: Fix parse_config name injection ---


class TestB3NameInjection:
    """B3: YAML dict keys must become server/profile names."""

    def test_config_without_explicit_name(self) -> None:
        from tela.core.config import parse_config

        raw = {
            "servers": {
                "fs": {"command": "node", "args": ["server.js"]},
            },
            "profiles": {
                "dev": {"default": True},
            },
            "auth": {"mode": "open"},
        }
        config = parse_config(raw, {})
        assert config.servers["fs"].name == "fs"
        assert config.profiles["dev"].name == "dev"

    def test_config_with_explicit_name_backward_compat(self) -> None:
        from tela.core.config import parse_config

        raw = {
            "servers": {
                "fs": {"name": "fs", "command": "node"},
            },
            "profiles": {
                "dev": {"name": "dev", "default": True},
            },
            "auth": {"mode": "open"},
        }
        config = parse_config(raw, {})
        assert config.servers["fs"].name == "fs"
        assert config.profiles["dev"].name == "dev"


# --- B4: Fix hot reload rollback isolation ---


class TestB4RollbackIsolation:
    """B4: Rejected reload must not corrupt other servers' tool registrations."""

    def test_snapshot_and_restore(self) -> None:
        from tela.shell.downstream import DownstreamRegistry

        reg = DownstreamRegistry()
        tool_fs = ResolvedTool(
            name="read_file", server_name="fs", family="filesystem"
        )
        tool_custom = ResolvedTool(
            name="custom_tool", server_name="custom", family="custom"
        )
        reg.register("fs", [tool_fs])
        reg.register("custom", [tool_custom])

        snap = reg.snapshot()

        # Corrupt registry
        reg.register("custom", [
            ResolvedTool(name="read_file", server_name="custom", family="custom"),
        ])
        # read_file now owned by custom
        assert reg.get_tool_server("read_file") == "custom"

        # Restore
        reg.restore(snap)
        assert reg.get_tool_server("read_file") == "fs"
        assert reg.get_tool_server("custom_tool") == "custom"

    def test_rollback_preserves_other_servers(self) -> None:
        """Multi-server scenario: rejected reload of 'custom' preserves 'fs'."""
        from tela.shell.downstream import DownstreamRegistry

        reg = DownstreamRegistry()
        tool_fs = ResolvedTool(
            name="read_file", server_name="fs", family="filesystem"
        )
        tool_write = ResolvedTool(
            name="write_file", server_name="fs", family="filesystem"
        )
        tool_custom = ResolvedTool(
            name="custom_tool", server_name="custom", family="custom"
        )

        reg.register("fs", [tool_fs, tool_write])
        reg.register("custom", [tool_custom])

        snap = reg.snapshot()

        # Simulate rejected reload of custom that corrupts fs mapping
        reg.register("custom", [
            ResolvedTool(name="read_file", server_name="custom", family="custom"),
            ResolvedTool(name="new_tool", server_name="custom", family="custom"),
        ])

        # After corruption, fs tools are lost
        assert reg.get_tool_server("read_file") == "custom"

        # Rollback restores everything
        reg.restore(snap)
        assert reg.get_tool_server("read_file") == "fs"
        assert reg.get_tool_server("write_file") == "fs"
        assert reg.get_tool_server("custom_tool") == "custom"
        assert reg.get_tool_server("new_tool") is None
