"""Tests for ``tela stop`` command wiring and local shutdown signaling."""

from __future__ import annotations

import signal

import pytest

from tela.cli import main
from tela.commands.stop_cmd import stop_command
from tela.core.models import LockfileData
from tela.shell.result import Result


def test_stop_subcommand_exists() -> None:
    """CLI must expose ``tela stop`` command parser."""

    with pytest.raises(SystemExit) as exc_info:
        main(["stop", "--help"])
    assert exc_info.value.code == 0


def test_stop_command_requires_running_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop must report a clear error when no live lockfile-backed server exists."""

    monkeypatch.setattr(
        "tela.commands.stop_cmd.read_lockfile",
        lambda: Result(error="LOCKFILE_READ_ERROR: lockfile does not exist"),
    )

    result = stop_command()
    assert result.is_err
    assert result.error is not None
    assert "NO_RUNNING_SERVER" in result.error


def test_cli_stop_routes_to_stop_command_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI ``tela stop`` must dispatch to stop handler and return exit code."""

    monkeypatch.setattr("tela.cli.stop_command", lambda: Result(value=0))

    assert main(["stop"]) == 0


def test_cli_stop_error_does_not_fallback_to_top_level_help(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI stop errors must report error text, not top-level argparse help."""

    monkeypatch.setattr(
        "tela.cli.stop_command",
        lambda: Result(
            error="STOP_PERMISSION_DENIED: cannot signal tela server pid 42"
        ),
    )

    exit_code = main(["stop"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error: STOP_PERMISSION_DENIED" in captured.err
    assert "usage: tela" not in captured.out


def test_cli_stop_no_running_server_has_stable_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No-running-server path must use a stable non-zero exit and clear message."""

    monkeypatch.setattr(
        "tela.cli.stop_command",
        lambda: Result(error="NO_RUNNING_SERVER: no running tela server found"),
    )

    exit_code = main(["stop"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error: NO_RUNNING_SERVER" in captured.err
    assert "usage: tela" not in captured.out


def test_stop_command_sends_sigterm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stop must send SIGTERM, wait for exit, and clean lockfile."""

    observed: list[tuple[int, int]] = []
    delete_calls: list[bool] = []
    probe_results = iter([None, ProcessLookupError()])

    monkeypatch.setattr(
        "tela.commands.stop_cmd.read_lockfile",
        lambda: Result(
            value=LockfileData(
                pid=43210,
                host="127.0.0.1",
                port=8080,
                token="token",
                started_at="2026-04-05T00:00:00Z",
                config_path="/tmp/tela.yaml",
                version="0.1.0",
            )
        ),
    )
    monkeypatch.setattr(
        "tela.commands.stop_cmd.os.kill",
        lambda pid, sig: (
            observed.append((pid, sig)),
            None if sig == signal.SIGTERM else _raise_if_needed(next(probe_results)),
        ),
    )
    monkeypatch.setattr(
        "tela.commands.stop_cmd.delete_lockfile",
        lambda: delete_calls.append(True) or Result(value=None),
    )
    monkeypatch.setattr("tela.commands.stop_cmd.time.sleep", lambda _seconds: None)

    result = stop_command()

    assert result.is_ok
    assert observed == [(43210, signal.SIGTERM), (43210, 0), (43210, 0)]
    assert delete_calls == [True]
    assert (
        "stop confirmed: tela server pid 43210 exited and lockfile cleaned"
        in capsys.readouterr().out
    )


def test_stop_command_times_out_if_process_does_not_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop must fail if the process stays alive past the bounded wait."""

    monkeypatch.setattr(
        "tela.commands.stop_cmd.read_lockfile",
        lambda: Result(
            value=LockfileData(
                pid=43210,
                host="127.0.0.1",
                port=8080,
                token="token",
                started_at="2026-04-05T00:00:00Z",
                config_path="/tmp/tela.yaml",
                version="0.1.0",
            )
        ),
    )
    monkeypatch.setattr("tela.commands.stop_cmd.STOP_WAIT_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr("tela.commands.stop_cmd.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("tela.commands.stop_cmd.os.kill", lambda _pid, _sig: None)

    result = stop_command()

    assert result.is_err
    assert result.error is not None
    assert "STOP_TIMEOUT" in result.error


def test_stop_command_treats_zombie_as_exited(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Zombie process state must count as exited for stop cleanup."""

    delete_calls: list[bool] = []

    monkeypatch.setattr(
        "tela.commands.stop_cmd.read_lockfile",
        lambda: Result(
            value=LockfileData(
                pid=43210,
                host="127.0.0.1",
                port=8080,
                token="token",
                started_at="2026-04-05T00:00:00Z",
                config_path="/tmp/tela.yaml",
                version="0.1.0",
            )
        ),
    )
    monkeypatch.setattr("tela.commands.stop_cmd.os.kill", lambda _pid, _sig: None)
    monkeypatch.setattr("tela.commands.stop_cmd._is_zombie_process", lambda _pid: True)
    monkeypatch.setattr(
        "tela.commands.stop_cmd.delete_lockfile",
        lambda: delete_calls.append(True) or Result(value=None),
    )

    result = stop_command()

    assert result.is_ok
    assert delete_calls == [True]
    assert (
        "stop confirmed: tela server pid 43210 exited and lockfile cleaned"
        in capsys.readouterr().out
    )


def _raise_if_needed(result: object) -> None:
    if isinstance(result, BaseException):
        raise result
