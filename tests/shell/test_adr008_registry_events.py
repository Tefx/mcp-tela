"""ADR-008 shell persistence tests for registry and runtime events."""

from __future__ import annotations

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
    assert events.is_ok
    assert events.value is not None
    assert events.value.events == []
    assert events.value.malformed_line_count == 0


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
