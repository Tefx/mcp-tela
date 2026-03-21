"""Tests for lockfile read/write/delete contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tela.core.models import LockfileData
from tela.shell import lockfile


def _sample_lockfile_data(config_path: str = "/tmp/tela.yaml") -> LockfileData:
    return LockfileData(
        pid=os.getpid(),
        host="127.0.0.1",
        port=39123,
        token="token-example",
        started_at="2026-01-01T00:00:00Z",
        config_path=config_path,
        version="0.1.0",
    )


def test_write_lockfile_persists_json_with_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    data = _sample_lockfile_data(config_path=str(tmp_path / "tela.yaml"))
    result = lockfile.write_lockfile(data)

    assert result.is_ok
    assert path.exists()
    assert path.parent.stat().st_mode & 0o777 == lockfile.LOCKFILE_DIRECTORY_MODE
    assert path.stat().st_mode & 0o777 == lockfile.LOCKFILE_FILE_MODE

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == data.model_dump()

    # temp files should not leak after successful write
    assert not any(
        item.is_file() and str(item).endswith(lockfile.LOCKFILE_TMP_SUFFIX)
        for item in path.parent.iterdir()
    )


def test_read_lockfile_returns_stored_data_when_pid_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    data = _sample_lockfile_data()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data.model_dump_json(), encoding="utf-8")

    result = lockfile.read_lockfile()
    assert result.is_ok
    assert result.value == data


def test_read_lockfile_detects_stale_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    stale_pid_data = LockfileData(
        pid=2**31,
        host="127.0.0.1",
        port=39124,
        token="token-stale",
        started_at="2026-01-01T00:00:00Z",
        config_path=str(tmp_path / "tela.yaml"),
        version="0.1.0",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stale_pid_data.model_dump_json(), encoding="utf-8")

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None


def test_read_lockfile_rejects_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None


def test_delete_lockfile_removes_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    result = lockfile.delete_lockfile()

    assert result.is_ok
    assert not path.exists()


def test_delete_lockfile_succeeds_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    result = lockfile.delete_lockfile()

    assert result.is_ok
    assert not path.exists()
