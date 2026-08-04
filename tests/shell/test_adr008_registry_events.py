"""ADR-008 shell persistence tests for registry and runtime events."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import os
import stat
from pathlib import Path

from tela.core.classification import (
    AttachmentDisplayState,
    AttachmentRegistry,
    ClientAttachment,
    Recoverability,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeState,
)
from tela.shell.adr008_registry_events import (
    ATTACHMENT_REGISTRY_LOCK_FILENAME,
    ATTACHMENT_TERMINAL_LIMIT,
    DIAGNOSTIC_FILE_MODE,
    TELA_DIRECTORY_MODE,
    append_runtime_event,
    attachment_registry_path,
    read_attachment_registry,
    read_runtime_events,
    runtime_events_path,
    upsert_attachment_registry,
    write_attachment_registry,
)


def _attachment(client_id: str) -> ClientAttachment:
    return ClientAttachment(
        client_id=client_id,
        client_kind="cli",
        display_state=AttachmentDisplayState.HEALTHY,
        runtime_state=RuntimeState.ACTIVE,
        recoverability=Recoverability.RECOVERABLE,
        connected_at="2026-01-01T00:00:00Z",
        last_heartbeat="2026-01-01T00:01:00Z",
    )


def _upsert_in_process(home: str, client_id: str) -> tuple[bool, str | None]:
    os.environ["HOME"] = home
    result = upsert_attachment_registry(_attachment(client_id))
    return result.is_ok, result.error


def _event(details: dict[str, object] | None = None) -> RuntimeEvent:
    return RuntimeEvent(
        kind=RuntimeEventKind.HEARTBEAT,
        client_id="c1",
        client_kind="cli",
        timestamp="2026-01-01T00:00:00Z",
        details=details or {},
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _registry_path() -> Path:
    result = attachment_registry_path()
    assert result.value is not None
    return result.value


def _events_path() -> Path:
    result = runtime_events_path()
    assert result.value is not None
    return result.value


def test_missing_files_return_empty_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    registry = read_attachment_registry()
    events = read_runtime_events()

    assert registry.is_ok
    assert registry.value == AttachmentRegistry(attachments=[])
    assert not (tmp_path / ".tela").exists()
    assert events.is_ok
    assert events.value is not None
    assert events.value.events == []
    assert events.value.malformed_line_count == 0


def test_legacy_registry_read_does_not_create_sidecar_or_change_bytes(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        AttachmentRegistry(attachments=[_attachment("legacy")]).model_dump_json(),
        encoding="utf-8",
    )
    before = path.read_bytes()

    result = read_attachment_registry()

    assert result.is_ok
    assert path.read_bytes() == before
    assert not (path.parent / ATTACHMENT_REGISTRY_LOCK_FILENAME).exists()


def test_write_read_and_upsert_registry_sets_modes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    first = _attachment("c1")
    second = _attachment("c2")

    write_result = write_attachment_registry(AttachmentRegistry(attachments=[first]))
    upsert_result = upsert_attachment_registry(second)
    read_result = read_attachment_registry()

    assert write_result.is_ok
    assert upsert_result.is_ok
    assert read_result.is_ok
    assert read_result.value is not None
    assert [item.client_id for item in read_result.value.attachments] == ["c1", "c2"]
    assert _mode(_registry_path().parent) == TELA_DIRECTORY_MODE
    assert _mode(_registry_path()) == DIAGNOSTIC_FILE_MODE


def test_upsert_compacts_terminal_history_and_preserves_active_records(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    now = datetime.now(UTC)
    active = [
        _attachment(f"active-{index}").model_copy(
            update={
                "connected_at": (now - timedelta(hours=1)).isoformat(),
                "last_heartbeat": (now - timedelta(hours=1)).isoformat(),
            }
        )
        for index in range(100)
    ]
    recent = [
        _attachment(f"recent-{index:03d}").model_copy(
            update={
                "display_state": AttachmentDisplayState.EXITED,
                "runtime_state": RuntimeState.EXITED,
                "recoverability": Recoverability.NOT_RECOVERABLE,
                "connected_at": (now - timedelta(days=1, seconds=index)).isoformat(),
                "last_heartbeat": (now - timedelta(days=1, seconds=index)).isoformat(),
            }
        )
        for index in range(600)
    ]
    expired = [
        _attachment(f"expired-{index:03d}").model_copy(
            update={
                "display_state": AttachmentDisplayState.EXITED,
                "runtime_state": RuntimeState.EXITED,
                "recoverability": Recoverability.NOT_RECOVERABLE,
                "connected_at": (now - timedelta(days=30)).isoformat(),
                "last_heartbeat": (now - timedelta(days=30)).isoformat(),
            }
        )
        for index in range(600)
    ]
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        AttachmentRegistry(attachments=active + recent + expired).model_dump_json(),
        encoding="utf-8",
    )

    result = upsert_attachment_registry(_attachment("new-active"))

    assert result.is_ok and result.value is not None
    assert len(result.value.attachments) == 100 + ATTACHMENT_TERMINAL_LIMIT + 1
    ids = {item.client_id for item in result.value.attachments}
    assert {item.client_id for item in active} <= ids
    assert "new-active" in ids
    assert not any(client_id.startswith("expired-") for client_id in ids)


def test_concurrent_process_upserts_use_stable_lock_and_preserve_ids(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client_ids = [f"process-{index:02d}" for index in range(24)]

    with ProcessPoolExecutor(max_workers=8) as executor:
        outcomes = list(
            executor.map(
                _upsert_in_process,
                [str(tmp_path)] * len(client_ids),
                client_ids,
            )
        )

    assert all(ok for ok, _error in outcomes), outcomes
    raw = _registry_path().read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    result = AttachmentRegistry.model_validate(parsed)
    assert sorted(item.client_id for item in result.attachments) == client_ids
    assert len({item.client_id for item in result.attachments}) == len(client_ids)
    lock_path = _registry_path().parent / ATTACHMENT_REGISTRY_LOCK_FILENAME
    assert _mode(_registry_path().parent) == TELA_DIRECTORY_MODE
    assert _mode(_registry_path()) == DIAGNOSTIC_FILE_MODE
    assert _mode(lock_path) == DIAGNOSTIC_FILE_MODE


def test_nonterminal_overflow_is_preserved_and_reported(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    records = [_attachment(f"active-overflow-{index}") for index in range(501)]

    caplog.set_level("WARNING")
    result = write_attachment_registry(AttachmentRegistry(attachments=records))

    assert result.is_ok
    read_result = read_attachment_registry()
    assert read_result.is_ok and read_result.value is not None
    assert len(read_result.value.attachments) == 501
    assert "attachment_registry_nonterminal_overflow" in caplog.text
    assert "nonterminal_count" in caplog.text


def test_malformed_upsert_fails_closed_without_replacing_bytes(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _registry_path().parent.mkdir(parents=True)
    original = b"{not-json"
    _registry_path().write_bytes(original)

    result = upsert_attachment_registry(_attachment("new"))

    assert result.is_err
    assert result.error is not None
    assert result.error.startswith("ATTACHMENT_REGISTRY_PARSE_ERROR")
    assert _registry_path().read_bytes() == original


def test_empty_existing_registry_fails_closed_without_replacing_bytes(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _registry_path().parent.mkdir(parents=True)
    _registry_path().write_bytes(b"")

    result = upsert_attachment_registry(_attachment("new"))

    assert result.is_err
    assert result.error is not None
    assert result.error.startswith("ATTACHMENT_REGISTRY_PARSE_ERROR")
    assert _registry_path().read_bytes() == b""


def test_malformed_registry_returns_parse_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _registry_path().parent.mkdir(parents=True)
    _registry_path().write_text("{not-json", encoding="utf-8")

    result = read_attachment_registry()

    assert result.is_err
    assert result.error is not None
    assert result.error.startswith("ATTACHMENT_REGISTRY_PARSE_ERROR")


def test_append_and_read_runtime_events_counts_malformed_lines(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    append_result = append_runtime_event(_event())
    with _events_path().open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
        handle.write("\n")
    read_result = read_runtime_events()

    assert append_result.is_ok
    assert read_result.is_ok
    assert read_result.value is not None
    assert len(read_result.value.events) == 1
    assert read_result.value.malformed_line_count == 1
    assert _mode(_events_path().parent) == TELA_DIRECTORY_MODE
    assert _mode(_events_path()) == DIAGNOSTIC_FILE_MODE


def test_append_runtime_event_rejects_oversized_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    oversized = _event({"payload": "x" * (17 * 1024)})

    result = append_runtime_event(oversized)

    assert result.is_err
    assert result.error == "RUNTIME_EVENT_TOO_LARGE: event exceeds 16 KiB"
    assert not _events_path().exists()
