"""Tests for ``tela connect`` command discovery and lifecycle wiring."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tela.cli import main
from tela.commands import connect_cmd
from tela.core.models import LockfileData
from tela.shell.config_loader import Result


def test_connect_subcommand_exists() -> None:
    """CLI must expose ``tela connect`` command parser."""

    with pytest.raises(SystemExit) as exc_info:
        main(["connect", "--help"])
    assert exc_info.value.code == 0


def test_connect_token_override_priority_cli_env_lockfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token precedence must be ``--token`` > ``TELA_BEARER_TOKEN`` > lockfile."""

    monkeypatch.delenv("TELA_BEARER_TOKEN", raising=False)
    from_lockfile = connect_cmd._resolve_connect_token(
        cli_token=None,
        lockfile_token="lock-token",
    )
    assert from_lockfile.is_ok
    assert from_lockfile.value == "lock-token"

    monkeypatch.setenv("TELA_BEARER_TOKEN", "env-token")
    from_env = connect_cmd._resolve_connect_token(
        cli_token=None,
        lockfile_token="lock-token",
    )
    assert from_env.is_ok
    assert from_env.value == "env-token"

    from_cli = connect_cmd._resolve_connect_token(
        cli_token="cli-token",
        lockfile_token="lock-token",
    )
    assert from_cli.is_ok
    assert from_cli.value == "cli-token"


def test_connect_server_path_requires_token_or_env() -> None:
    """Explicit ``--server`` mode must reject missing CLI/env token."""

    result = connect_cmd._resolve_connect_token(
        cli_token=None,
        lockfile_token=None,
    )
    assert result.is_err
    assert result.error is not None
    assert "MISSING_TOKEN" in result.error


def test_connect_server_path_uses_env_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--server`` mode must skip lockfile and use env token."""

    monkeypatch.setenv("TELA_BEARER_TOKEN", "env-token")
    calls: list[tuple[str, int, str]] = []

    def _fake_run_bridge(
        *, host: str, port: int, bearer_token: str
    ) -> Result[None, str]:
        calls.append((host, port, bearer_token))
        return Result(value=None)

    monkeypatch.setattr(connect_cmd, "_run_bridge", _fake_run_bridge)

    result = connect_cmd.connect_command(
        config_path="tela.yaml",
        default_profile=None,
        server="127.0.0.1:8123",
        token=None,
    )
    assert result.is_ok
    assert calls == [("127.0.0.1", 8123, "env-token")]


def test_discovery_autostart_handles_race_lockfile_appearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery must recover when auto-start races another connector."""

    lockfile = LockfileData(
        pid=1234,
        host="127.0.0.1",
        port=9000,
        token="lock-token",
        started_at="2026-03-22T10:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )

    monkeypatch.setattr(
        connect_cmd,
        "read_lockfile",
        lambda: Result(error="LOCKFILE_READ_ERROR: lockfile does not exist"),
    )

    waits: list[float] = []
    wait_outcomes = [
        Result[LockfileData, str](error="LOCKFILE_WAIT_TIMEOUT: timed out"),
        Result[LockfileData, str](value=lockfile),
    ]

    def _fake_wait_for_live_lockfile(
        timeout_seconds: float,
    ) -> Result[LockfileData, str]:
        waits.append(timeout_seconds)
        return wait_outcomes.pop(0)

    autostarts = 0

    def _fake_autostart_serve(
        *,
        config_path: str,
        default_profile: str | None,
    ) -> Result[None, str]:
        nonlocal autostarts
        _ = config_path
        _ = default_profile
        autostarts += 1
        return Result(error="AUTOSTART_FAILED: address in use")

    monkeypatch.setattr(
        connect_cmd,
        "_wait_for_live_lockfile",
        _fake_wait_for_live_lockfile,
    )
    monkeypatch.setattr(connect_cmd, "_autostart_serve", _fake_autostart_serve)

    result = connect_cmd._discover_or_autostart(
        config_path="tela.yaml",
        default_profile=None,
    )
    assert result.is_ok
    assert result.value == lockfile
    assert autostarts == 1
    assert waits == [0.3, connect_cmd.LOCKFILE_WAIT_TIMEOUT_SECONDS]


def test_connect_discovery_uses_published_lockfile_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connect discovery must use the lockfile's published bound port."""

    lockfile = LockfileData(
        pid=1234,
        host="127.0.0.1",
        port=49152,
        token="lock-token",
        started_at="2026-03-22T10:00:00Z",
        config_path="/tmp/tela.yaml",
        version="0.1.0",
    )

    calls: list[tuple[str, int, str]] = []

    def _fake_run_bridge(
        *, host: str, port: int, bearer_token: str
    ) -> Result[None, str]:
        calls.append((host, port, bearer_token))
        return Result(value=None)

    monkeypatch.delenv("TELA_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(connect_cmd, "read_lockfile", lambda: Result(value=lockfile))
    monkeypatch.setattr(connect_cmd, "_run_bridge", _fake_run_bridge)

    result = connect_cmd.connect_command(
        config_path="tela.yaml",
        default_profile=None,
        server=None,
        token=None,
    )

    assert result.is_ok
    assert calls == [("127.0.0.1", 49152, "lock-token")]


def test_bridge_lifecycle_posts_connect_and_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge lifecycle must call connect, forward, then disconnect."""

    endpoints: list[str] = []

    def _fake_post_json(
        *, url: str, bearer_token: str, payload: dict[str, str]
    ) -> Result[None, str]:
        _ = bearer_token
        _ = payload
        endpoints.append(url)
        return Result(value=None)

    def _fake_forward_stdio_http(
        *,
        mcp_url: str,
        bearer_token: str,
        should_stop: Callable[[], bool],
        stdin_buffer,
        stdout_buffer,
    ) -> Result[None, str]:
        _ = mcp_url
        _ = bearer_token
        _ = should_stop
        _ = stdin_buffer
        _ = stdout_buffer
        return Result(value=None)

    monkeypatch.setattr(connect_cmd, "_post_json", _fake_post_json)
    monkeypatch.setattr(connect_cmd, "_forward_stdio_http", _fake_forward_stdio_http)

    result = connect_cmd._run_bridge(
        host="127.0.0.1",
        port=8123,
        bearer_token="token",
    )
    assert result.is_ok
    assert endpoints == [
        "http://127.0.0.1:8123/connect",
        "http://127.0.0.1:8123/disconnect",
    ]
