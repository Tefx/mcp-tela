"""Tests for ``tela serve`` command wiring and lifecycle behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tela.cli import main
from tela.commands import serve_cmd
from tela.core.models import AuthConfig, AuthMode, TelaConfig
from tela.shell.result import Result
from starlette.applications import Starlette

from tela.shell.gateway_runtime import (
    clear_runtime_connections,
    is_runtime_running,
    set_runtime_running,
    set_upstream_server,
)
from tela.shell import serve_runtime
from tela.shell.serve_runtime import HttpServerHandle


def test_serve_subcommand_exists() -> None:
    """CLI must expose ``tela serve`` command parser."""

    with pytest.raises(SystemExit) as exc_info:
        main(["serve", "--help"])
    assert exc_info.value.code == 0


def test_token_override_priority_cli_over_env_over_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token precedence must be ``--token`` > ``TELA_BEARER_TOKEN`` > generated."""

    monkeypatch.delenv("TELA_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_cmd,
        "generate_bearer_token",
        lambda: Result(value="generated-token"),
    )
    generated = serve_cmd._resolve_bearer_token(None)
    assert generated.is_ok
    assert generated.value == "generated-token"

    monkeypatch.setenv("TELA_BEARER_TOKEN", "env-token")
    env_selected = serve_cmd._resolve_bearer_token(None)
    assert env_selected.is_ok
    assert env_selected.value == "env-token"
    cli_selected = serve_cmd._resolve_bearer_token("cli-token")
    assert cli_selected.is_ok
    assert cli_selected.value == "cli-token"


def test_serve_lockfile_written_then_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Serve flow writes lockfile at startup and deletes it on shutdown."""

    writes: list[object] = []
    deletes: list[bool] = []

    def _fake_load_config(path: Path | None = None, default_profile: str | None = None):
        _ = path
        _ = default_profile
        return Result(
            value=TelaConfig(
                auth=AuthConfig(mode=AuthMode.OPEN),
                resolved_default_profile="dev",
            )
        )

    async def _fake_gateway_prepare_startup(*args, **kwargs) -> Result[None, str]:
        _ = args
        _ = kwargs
        set_upstream_server(object())  # type: ignore[arg-type]  # test fake: not a real FastMCP
        set_runtime_running(True)
        return Result(value=None)

    async def _fake_gateway_converge_startup(*args, **kwargs) -> Result[None, str]:
        _ = args
        _ = kwargs
        return Result(value=None)

    async def _fake_launch_streamable_http_server(
        *, upstream_app: object, upstream_log_level: str, host: str, requested_port: int
    ) -> Result[HttpServerHandle, str]:
        _ = upstream_app
        _ = upstream_log_level
        _ = host
        task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0.01))
        return Result(
            value=HttpServerHandle(
                task=task,
                bound_port=requested_port,
                request_shutdown=lambda: None,
            )
        )

    async def _fake_gateway_shutdown() -> Result[None, str]:
        set_upstream_server(None)
        set_runtime_running(False)
        from tela.shell.gateway_runtime import clear_runtime_connections

        clear_runtime_connections()
        return Result(value=None)

    def _fake_write_lockfile(data):
        writes.append(data)
        return Result(value=None)

    def _fake_delete_lockfile():
        deletes.append(True)
        return Result(value=None)

    async def _fake_watch_config_changes(
        *,
        config_path: Path,
        default_profile: str | None,
        reaper_sweep_interval: float | None,
        reaper_native_ttl: float | None,
        reaper_bridge_ttl: float | None,
        stop_event: asyncio.Event,
    ) -> None:
        _ = config_path
        _ = default_profile
        _ = reaper_sweep_interval
        _ = reaper_native_ttl
        _ = reaper_bridge_ttl
        await stop_event.wait()

    async def _fake_idle_shutdown_watch(
        *,
        idle_timeout_seconds: int,
        stop_event: asyncio.Event,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        _ = idle_timeout_seconds
        _ = stop_event
        _ = poll_interval_seconds
        return

    # Monkeypatch the operation accessors that serve_cmd now uses
    # instead of get_upstream_server — must return Result to match signatures
    monkeypatch.setattr(
        serve_cmd, "is_upstream_server_initialized", lambda: Result(value=True)
    )
    monkeypatch.setattr(
        serve_cmd,
        "get_upstream_http_app",
        lambda: Result(value=Starlette()),
    )
    monkeypatch.setattr(
        serve_cmd, "get_upstream_log_level", lambda: Result(value="info")
    )
    monkeypatch.setattr(serve_cmd, "load_config", _fake_load_config)
    monkeypatch.setattr(
        serve_cmd,
        "gateway_prepare_startup",
        _fake_gateway_prepare_startup,
    )
    monkeypatch.setattr(
        serve_cmd,
        "gateway_converge_startup",
        _fake_gateway_converge_startup,
    )
    monkeypatch.setattr(serve_cmd, "gateway_shutdown", _fake_gateway_shutdown)
    monkeypatch.setattr(
        serve_cmd,
        "launch_streamable_http_server",
        _fake_launch_streamable_http_server,
    )
    monkeypatch.setattr(serve_cmd, "write_lockfile", _fake_write_lockfile)
    monkeypatch.setattr(serve_cmd, "delete_lockfile", _fake_delete_lockfile)
    monkeypatch.setattr(serve_cmd, "watch_config_changes", _fake_watch_config_changes)
    monkeypatch.setattr(serve_cmd, "idle_shutdown_watch", _fake_idle_shutdown_watch)
    monkeypatch.setattr(serve_cmd, "package_version", lambda: Result(value="0.1.0"))

    result = serve_cmd.serve_command(
        config_path=str(tmp_path / "tela.yaml"),
        port=8123,
        host="127.0.0.1",
        default_profile="dev",
        idle_timeout=0,
        token="cli-token",
    )

    assert result.is_ok
    assert len(writes) == 1
    lock_data = writes[0]
    assert getattr(lock_data, "host") == "127.0.0.1"
    assert getattr(lock_data, "port") == 8123
    assert getattr(lock_data, "token") == "cli-token"
    assert len(deletes) == 1


def test_serve_port_zero_writes_actual_bound_port_to_lockfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Port ``0`` must publish the actual OS-selected port in lockfile."""

    writes: list[object] = []
    published_port = 49152

    def _fake_load_config(path: Path | None = None, default_profile: str | None = None):
        _ = path
        _ = default_profile
        return Result(
            value=TelaConfig(
                auth=AuthConfig(mode=AuthMode.OPEN),
                resolved_default_profile="dev",
            )
        )

    async def _fake_gateway_prepare_startup(*args, **kwargs) -> Result[None, str]:
        _ = args
        _ = kwargs
        set_upstream_server(object())  # type: ignore[arg-type]  # test fake: not a real FastMCP
        set_runtime_running(True)
        return Result(value=None)

    async def _fake_gateway_converge_startup(*args, **kwargs) -> Result[None, str]:
        _ = args
        _ = kwargs
        return Result(value=None)

    async def _fake_gateway_shutdown() -> Result[None, str]:
        set_upstream_server(None)
        set_runtime_running(False)
        from tela.shell.gateway_runtime import clear_runtime_connections

        clear_runtime_connections()
        return Result(value=None)

    async def _fake_launch_streamable_http_server(
        *, upstream_app: object, upstream_log_level: str, host: str, requested_port: int
    ) -> Result[HttpServerHandle, str]:
        _ = upstream_app
        _ = upstream_log_level
        _ = host
        assert requested_port == 0
        task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0.01))
        return Result(
            value=HttpServerHandle(
                task=task,
                bound_port=published_port,
                request_shutdown=lambda: None,
            )
        )

    def _fake_write_lockfile(data):
        writes.append(data)
        return Result(value=None)

    async def _fake_watch_config_changes(
        *,
        config_path: Path,
        default_profile: str | None,
        reaper_sweep_interval: float | None,
        reaper_native_ttl: float | None,
        reaper_bridge_ttl: float | None,
        stop_event: asyncio.Event,
    ) -> None:
        _ = config_path
        _ = default_profile
        _ = reaper_sweep_interval
        _ = reaper_native_ttl
        _ = reaper_bridge_ttl
        await stop_event.wait()

    async def _fake_idle_shutdown_watch(
        *,
        idle_timeout_seconds: int,
        stop_event: asyncio.Event,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        _ = idle_timeout_seconds
        _ = stop_event
        _ = poll_interval_seconds
        return

    # Monkeypatch the operation accessors that serve_cmd now uses
    # — must return Result to match signatures
    monkeypatch.setattr(
        serve_cmd, "is_upstream_server_initialized", lambda: Result(value=True)
    )
    monkeypatch.setattr(
        serve_cmd,
        "get_upstream_http_app",
        lambda: Result(value=Starlette()),
    )
    monkeypatch.setattr(
        serve_cmd, "get_upstream_log_level", lambda: Result(value="info")
    )
    monkeypatch.setattr(serve_cmd, "load_config", _fake_load_config)
    monkeypatch.setattr(
        serve_cmd,
        "gateway_prepare_startup",
        _fake_gateway_prepare_startup,
    )
    monkeypatch.setattr(
        serve_cmd,
        "gateway_converge_startup",
        _fake_gateway_converge_startup,
    )
    monkeypatch.setattr(serve_cmd, "gateway_shutdown", _fake_gateway_shutdown)
    monkeypatch.setattr(
        serve_cmd,
        "launch_streamable_http_server",
        _fake_launch_streamable_http_server,
    )
    monkeypatch.setattr(serve_cmd, "write_lockfile", _fake_write_lockfile)
    monkeypatch.setattr(serve_cmd, "delete_lockfile", lambda: Result(value=None))
    monkeypatch.setattr(serve_cmd, "watch_config_changes", _fake_watch_config_changes)
    monkeypatch.setattr(serve_cmd, "idle_shutdown_watch", _fake_idle_shutdown_watch)
    monkeypatch.setattr(serve_cmd, "package_version", lambda: Result(value="0.1.0"))

    result = serve_cmd.serve_command(
        config_path=str(tmp_path / "tela.yaml"),
        port=0,
        host="127.0.0.1",
        default_profile="dev",
        idle_timeout=0,
        token="cli-token",
    )

    assert result.is_ok
    assert len(writes) == 1
    assert getattr(writes[0], "port") == published_port
    assert getattr(writes[0], "port") > 0


def test_serve_command_reaper_cli_overrides_win_over_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_reaper: list[tuple[float, float, float]] = []

    def _fake_load_config(path: Path | None = None, default_profile: str | None = None):
        _ = path
        _ = default_profile
        return Result(
            value=TelaConfig(
                auth=AuthConfig(mode=AuthMode.OPEN),
                resolved_default_profile="dev",
            )
        )

    async def _fake_run_serve_gateway(**kwargs) -> Result[None, str]:
        tela_config = kwargs["tela_config"]
        captured_reaper.append(
            (
                tela_config.reaper.sweep_interval_seconds,
                tela_config.reaper.native_idle_ttl_seconds,
                tela_config.reaper.bridge_idle_ttl_seconds,
            )
        )
        return Result(value=None)

    monkeypatch.setattr(serve_cmd, "load_config", _fake_load_config)
    monkeypatch.setattr(serve_cmd, "_run_serve_gateway", _fake_run_serve_gateway)
    monkeypatch.setattr(
        serve_cmd, "_resolve_bearer_token", lambda token: Result(value=token or "tok")
    )

    result = serve_cmd.serve_command(
        config_path="tela.yaml",
        idle_timeout=0,
        reaper_sweep_interval=60.0,
        reaper_native_ttl=0.0,
        reaper_bridge_ttl=1800.0,
        token="tok",
    )

    assert result.is_ok
    assert captured_reaper == [(60.0, 0.0, 1800.0)]


def test_idle_shutdown_sets_stop_event_when_connections_stay_idle() -> None:
    """Idle watcher must request shutdown when no active connections exist."""

    from tela.shell.serve_runtime import idle_shutdown_watch

    async def _scenario() -> bool:
        stop_event = asyncio.Event()
        clear_runtime_connections()
        await idle_shutdown_watch(
            idle_timeout_seconds=1,
            stop_event=stop_event,
            poll_interval_seconds=0.01,
        )
        return stop_event.is_set()

    assert asyncio.run(_scenario()) is True


def test_post_bind_convergence_failure_rolls_back_discovery_and_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Convergence failure after lockfile publish removes discovery and tears down HTTP."""

    observed: list[str] = []
    discovery = {"published": False}

    def _fake_load_config(path: Path | None = None, default_profile: str | None = None):
        _ = path
        _ = default_profile
        return Result(
            value=TelaConfig(
                auth=AuthConfig(mode=AuthMode.OPEN),
                resolved_default_profile="dev",
            )
        )

    async def _fake_gateway_prepare_startup(*args, **kwargs) -> Result[None, str]:
        _ = args
        _ = kwargs
        set_runtime_running(True)
        observed.append("prepare")
        return Result(value=None)

    async def _fake_launch_streamable_http_server(
        *, upstream_app: object, upstream_log_level: str, host: str, requested_port: int
    ) -> Result[HttpServerHandle, str]:
        _ = upstream_app
        _ = upstream_log_level
        _ = host
        _ = requested_port
        observed.append("bind")
        task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0.01))
        return Result(
            value=HttpServerHandle(
                task=task,
                bound_port=8123,
                request_shutdown=lambda: None,
            )
        )

    def _fake_write_lockfile(data: object) -> Result[None, str]:
        _ = data
        observed.append("publish_lockfile")
        discovery["published"] = True
        return Result(value=None)

    async def _fake_gateway_converge_startup(*args, **kwargs) -> Result[None, str]:
        _ = args
        _ = kwargs
        observed.append("convergence_failed")
        return Result(error="CONVERGENCE_FAILED: injected")

    def _fake_delete_lockfile() -> Result[None, str]:
        observed.append("remove_lockfile")
        discovery["published"] = False
        return Result(value=None)

    async def _fake_stop_http_server(server: HttpServerHandle) -> None:
        _ = server
        observed.append("teardown_http")

    async def _fake_gateway_shutdown() -> Result[None, str]:
        observed.append("shutdown_runtime")
        set_runtime_running(False)
        return Result(value=None)

    monkeypatch.setattr(serve_cmd, "load_config", _fake_load_config)
    monkeypatch.setattr(
        serve_cmd,
        "gateway_prepare_startup",
        _fake_gateway_prepare_startup,
    )
    monkeypatch.setattr(
        serve_cmd,
        "launch_streamable_http_server",
        _fake_launch_streamable_http_server,
    )
    monkeypatch.setattr(serve_cmd, "write_lockfile", _fake_write_lockfile)
    monkeypatch.setattr(
        serve_cmd,
        "gateway_converge_startup",
        _fake_gateway_converge_startup,
    )
    # Patch delete_lockfile on both serve_cmd and serve_runtime:
    # rollback_after_post_bind_convergence_failure calls delete_lockfile
    # from its own (serve_runtime) module-level import.
    monkeypatch.setattr(serve_cmd, "delete_lockfile", _fake_delete_lockfile)
    monkeypatch.setattr(serve_runtime, "delete_lockfile", _fake_delete_lockfile)
    # Patch stop_http_server on both serve_cmd and serve_runtime:
    # rollback_after_post_bind_convergence_failure calls stop_http_server
    # from its own (serve_runtime) module-level import.
    monkeypatch.setattr(serve_cmd, "stop_http_server", _fake_stop_http_server)
    monkeypatch.setattr(serve_runtime, "stop_http_server", _fake_stop_http_server)
    # Patch gateway_shutdown on both serve_cmd and serve_runtime:
    monkeypatch.setattr(serve_cmd, "gateway_shutdown", _fake_gateway_shutdown)
    monkeypatch.setattr(serve_runtime, "gateway_shutdown", _fake_gateway_shutdown)
    monkeypatch.setattr(
        serve_cmd,
        "is_upstream_server_initialized",
        lambda: Result(value=True),
    )
    monkeypatch.setattr(
        serve_cmd, "get_upstream_http_app", lambda: Result(value=Starlette())
    )
    monkeypatch.setattr(
        serve_cmd, "get_upstream_log_level", lambda: Result(value="info")
    )
    monkeypatch.setattr(serve_cmd, "package_version", lambda: Result(value="0.1.0"))

    result = serve_cmd.serve_command(
        config_path=str(tmp_path / "tela.yaml"),
        port=8123,
        host="127.0.0.1",
        default_profile="dev",
        idle_timeout=0,
        token="cli-token",
    )

    assert result.is_err
    assert result.error == "CONVERGENCE_FAILED: injected"
    assert observed == [
        "prepare",
        "bind",
        "publish_lockfile",
        "convergence_failed",
        "remove_lockfile",
        "teardown_http",
        "shutdown_runtime",
    ]
    assert discovery["published"] is False
    runtime_state_result = is_runtime_running()
    assert runtime_state_result.is_ok
    assert runtime_state_result.value is False
