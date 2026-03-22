"""CLI runtime wiring tests for config-reload watcher path."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tela.commands.serve_cmd import _watch_config_changes
from tela.shell.config_loader import Result


def test_watch_config_changes_routes_reload_to_gateway_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Config watcher must route mtime changes to gateway reload callback."""

    config_path = tmp_path / "tela.yaml"
    config_path.write_text(
        "profiles:\n  dev:\n    name: dev\n    default: true\nauth:\n  mode: open\n",
        encoding="utf-8",
    )

    calls: list[tuple[Path, str | None]] = []

    async def _fake_gateway_reload_config_from_disk(
        config_path: Path,
        default_profile: str | None,
    ) -> Result[None, str]:
        calls.append((config_path, default_profile))
        return Result(value=None)

    monkeypatch.setattr(
        "tela.commands.serve_cmd.gateway_reload_config_from_disk",
        _fake_gateway_reload_config_from_disk,
    )
    monkeypatch.setattr("tela.commands.serve_cmd.CONFIG_WATCH_POLL_SECONDS", 0.01)

    async def _scenario() -> None:
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            _watch_config_changes(
                config_path=config_path,
                default_profile="dev",
                stop_event=stop_event,
            )
        )

        try:
            await asyncio.sleep(0.03)
            config_path.write_text(
                "profiles:\n"
                "  dev:\n"
                "    name: dev\n"
                "    default: true\n"
                "auth:\n"
                "  mode: open\n"
                "servers:\n"
                "  fs:\n"
                "    command: cmd\n",
                encoding="utf-8",
            )

            for _ in range(20):
                if calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            stop_event.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_scenario())

    assert calls == [(config_path, "dev")]
