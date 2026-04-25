"""ADR-008 shell persistence for attachment diagnostics.

This module owns the filesystem boundary for ADR-008 diagnostic state:
``~/.tela/client-attachments.json`` and ``~/.tela/runtime-events.jsonl``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import IO, Iterator

from pydantic import ValidationError

from tela.core.classification import AttachmentRegistry, ClientAttachment, RuntimeEvent
from tela.shell.result import Result

ATTACHMENT_REGISTRY_FILENAME = "client-attachments.json"
RUNTIME_EVENTS_FILENAME = "runtime-events.jsonl"
TELA_DIRECTORY_MODE = 0o700
DIAGNOSTIC_FILE_MODE = 0o600
MAX_RUNTIME_EVENT_BYTES = 16 * 1024


@dataclass(frozen=True)
class RuntimeEventsRead:
    """Runtime events decoded from JSONL with malformed-line accounting.

    Attributes:
        events: Valid runtime events decoded from the JSONL file.
        malformed_line_count: Count of non-empty malformed JSONL lines skipped.
    """

    events: list[RuntimeEvent]
    malformed_line_count: int

    @property
    def malformed_lines(self) -> int:
        """Return ``malformed_line_count`` under a concise compatibility name.

        Returns:
            Number of malformed non-empty lines observed while reading.
        """

        return self.malformed_line_count


def attachment_registry_path() -> Result[Path, str]:
    """Return the ADR-008 attachment registry path for the current home.

    Returns:
        Result containing ``~/.tela/client-attachments.json`` resolved from the
        process home.
    """

    return Result(value=Path.home() / ".tela" / ATTACHMENT_REGISTRY_FILENAME)


def runtime_events_path() -> Result[Path, str]:
    """Return the ADR-008 runtime events path for the current home.

    Returns:
        Result containing ``~/.tela/runtime-events.jsonl`` resolved from the
        process home.
    """

    return Result(value=Path.home() / ".tela" / RUNTIME_EVENTS_FILENAME)


def read_attachment_registry() -> Result[AttachmentRegistry, str]:
    """Read the ADR-008 attachment registry.

    Missing files are treated as an empty registry. Malformed JSON or schema
    failures are reported as ``ATTACHMENT_REGISTRY_PARSE_ERROR``.

    Returns:
        Result containing the decoded attachment registry or an error string.

    Raises:
        No exceptions are intentionally raised; filesystem and parse failures
        are represented in the returned ``Result``.
    """

    path_result = attachment_registry_path()
    if path_result.value is None:
        return Result(error=path_result.error or "ATTACHMENT_REGISTRY_PATH_ERROR")
    path = path_result.value
    try:
        with _open_existing_locked(path, fcntl.LOCK_SH) as handle:
            if handle is None:
                return Result(value=AttachmentRegistry(attachments=[]))
            raw = handle.read()
    except OSError as exc:
        return Result(error=f"ATTACHMENT_REGISTRY_READ_ERROR: {exc}")

    return _decode_attachment_registry(raw)


def write_attachment_registry(registry: AttachmentRegistry) -> Result[None, str]:
    """Atomically persist the ADR-008 attachment registry.

    The registry file is written through a same-directory temporary file and
    atomically replaced. The ``~/.tela`` directory mode is forced to ``0o700``
    and the registry file mode is forced to ``0o600``.

    Args:
        registry: Attachment registry to persist.

    Returns:
        Empty success Result or an error string.

    Raises:
        No exceptions are intentionally raised; filesystem failures are
        represented in the returned ``Result``.
    """

    return _write_attachment_registry_locked(registry)


# @shell_complexity: upsert must combine locked read, malformed-registry handling, replacement, and write-error propagation.
def upsert_attachment_registry(
    attachment: ClientAttachment,
) -> Result[AttachmentRegistry, str]:
    """Insert or replace one attachment in the ADR-008 registry.

    Attachments are matched by ``client_id``. Missing registry files are treated
    as empty before the upsert.

    Args:
        attachment: Attachment record to insert or replace.

    Returns:
        Result containing the persisted registry or an error string.

    Raises:
        No exceptions are intentionally raised; filesystem and parse failures
        are represented in the returned ``Result``.
    """

    path_result = attachment_registry_path()
    if path_result.value is None:
        return Result(error=path_result.error or "ATTACHMENT_REGISTRY_PATH_ERROR")
    path = path_result.value
    try:
        _ensure_tela_directory(path.parent)
        with _locked_file(path, "a+", fcntl.LOCK_EX) as handle:
            handle.seek(0)
            raw = handle.read()
            if raw:
                decoded = _decode_attachment_registry(raw)
                if decoded.is_err:
                    return Result(error=decoded.error)
                registry = decoded.value
                if registry is None:
                    return Result(error="ATTACHMENT_REGISTRY_PARSE_ERROR: empty decode")
            else:
                registry = AttachmentRegistry(attachments=[])

            next_attachments = [
                item
                for item in registry.attachments
                if item.client_id != attachment.client_id
            ]
            next_attachments.append(attachment)
            next_registry = AttachmentRegistry(attachments=next_attachments)
            write_result = _replace_attachment_registry_under_lock(next_registry, path)
            if write_result.is_err:
                return Result(error=write_result.error)
            return Result(value=next_registry)
    except OSError as exc:
        return Result(error=f"ATTACHMENT_REGISTRY_WRITE_ERROR: {exc}")


def upsert_attachment(attachment: ClientAttachment) -> Result[AttachmentRegistry, str]:
    """Insert or replace one attachment in the registry.

    Args:
        attachment: Attachment record to insert or replace.

    Returns:
        Result containing the persisted registry or an error string.
    """

    return upsert_attachment_registry(attachment)


def upsert_client_attachment(attachment: ClientAttachment) -> Result[AttachmentRegistry, str]:
    """Insert or replace one client attachment in the registry.

    Args:
        attachment: Attachment record to insert or replace.

    Returns:
        Result containing the persisted registry or an error string.
    """

    return upsert_attachment_registry(attachment)


def append_runtime_event(event: RuntimeEvent) -> Result[None, str]:
    """Append one ADR-008 runtime event as a JSONL line.

    Events whose serialized JSONL line exceeds 16 KiB are rejected before any
    filesystem write occurs. Successful appends flush and fsync the file.

    Args:
        event: Runtime event to append.

    Returns:
        Empty success Result or an error string.

    Raises:
        No exceptions are intentionally raised; validation and filesystem
        failures are represented in the returned ``Result``.
    """

    payload_result = _runtime_event_jsonl(event)
    if payload_result.value is None:
        return Result(error=payload_result.error or "RUNTIME_EVENT_SERIALIZE_ERROR")
    payload = payload_result.value
    if len(payload.encode("utf-8")) > MAX_RUNTIME_EVENT_BYTES:
        return Result(error="RUNTIME_EVENT_TOO_LARGE: event exceeds 16 KiB")

    path_result = runtime_events_path()
    if path_result.value is None:
        return Result(error=path_result.error or "RUNTIME_EVENT_PATH_ERROR")
    path = path_result.value
    try:
        _ensure_tela_directory(path.parent)
        with _locked_file(path, "a+", fcntl.LOCK_EX) as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(path, DIAGNOSTIC_FILE_MODE)
        return Result()
    except OSError as exc:
        return Result(error=f"RUNTIME_EVENT_WRITE_ERROR: {exc}")


# @shell_orchestration: diagnostic connect callers intentionally ignore append failure results.
def append_runtime_event_best_effort(event: RuntimeEvent) -> None:
    """Best-effort runtime event append for diagnostic-only call sites.

    Args:
        event: Runtime event to append if persistence is available.

    Returns:
        None. Write failures are intentionally ignored because ADR-008
        diagnostics must not fail connect callers.
    """

    append_runtime_event(event)


# @shell_complexity: JSONL diagnostics must skip empty lines, count malformed lines, and preserve valid events.
def read_runtime_events() -> Result[RuntimeEventsRead, str]:
    """Read ADR-008 runtime events from JSONL.

    Missing files are treated as an empty event stream. Malformed non-empty
    lines are skipped and counted.

    Returns:
        Result containing events and malformed-line count, or an error string.

    Raises:
        No exceptions are intentionally raised; filesystem failures are
        represented in the returned ``Result``.
    """

    path_result = runtime_events_path()
    if path_result.value is None:
        return Result(error=path_result.error or "RUNTIME_EVENTS_PATH_ERROR")
    path = path_result.value
    try:
        with _open_existing_locked(path, fcntl.LOCK_SH) as handle:
            if handle is None:
                return Result(value=RuntimeEventsRead(events=[], malformed_line_count=0))
            lines = handle.readlines()
    except OSError as exc:
        return Result(error=f"RUNTIME_EVENTS_READ_ERROR: {exc}")

    events: list[RuntimeEvent] = []
    malformed_line_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(RuntimeEvent.model_validate_json(stripped))
        except (ValueError, ValidationError):
            malformed_line_count += 1

    return Result(
        value=RuntimeEventsRead(
            events=events,
            malformed_line_count=malformed_line_count,
        )
    )


def _decode_attachment_registry(raw: str) -> Result[AttachmentRegistry, str]:
    try:
        return Result(value=AttachmentRegistry.model_validate_json(raw))
    except (ValueError, ValidationError) as exc:
        return Result(error=f"ATTACHMENT_REGISTRY_PARSE_ERROR: {exc}")


def _write_attachment_registry_locked(registry: AttachmentRegistry) -> Result[None, str]:
    path_result = attachment_registry_path()
    if path_result.value is None:
        return Result(error=path_result.error or "ATTACHMENT_REGISTRY_PATH_ERROR")
    path = path_result.value
    try:
        _ensure_tela_directory(path.parent)
        with _locked_file(path, "a+", fcntl.LOCK_EX):
            return _replace_attachment_registry_under_lock(registry, path)
    except OSError as exc:
        return Result(error=f"ATTACHMENT_REGISTRY_WRITE_ERROR: {exc}")


def _replace_attachment_registry_under_lock(
    registry: AttachmentRegistry,
    path: Path,
) -> Result[None, str]:
    payload = registry.model_dump_json()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_handle:
            temp_path = Path(temp_handle.name)
            temp_handle.write(payload)
            temp_handle.flush()
            os.fsync(temp_handle.fileno())
        os.chmod(temp_path, DIAGNOSTIC_FILE_MODE)
        os.replace(temp_path, path)
        os.chmod(path, DIAGNOSTIC_FILE_MODE)
        return Result()
    except OSError as exc:
        if temp_path is not None:
            _remove_temp_file(temp_path)
        return Result(error=f"ATTACHMENT_REGISTRY_WRITE_ERROR: {exc}")


def _runtime_event_jsonl(event: RuntimeEvent) -> Result[str, str]:
    return Result(value=json.dumps(
        event.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n")


def _ensure_tela_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=TELA_DIRECTORY_MODE)
    os.chmod(path, TELA_DIRECTORY_MODE)


@contextmanager
def _open_existing_locked(
    path: Path,
    lock_operation: int,
) -> Iterator[IO[str] | None]:
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        yield None
        return

    with handle:
        fcntl.flock(handle.fileno(), lock_operation)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked_file(path: Path, mode: str, lock_operation: int) -> Iterator[IO[str]]:
    with path.open(mode, encoding="utf-8") as handle:
        os.chmod(path, DIAGNOSTIC_FILE_MODE)
        fcntl.flock(handle.fileno(), lock_operation)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remove_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(f"failed to remove temporary registry file {path}: {exc}") from exc


__all__ = [
    "ATTACHMENT_REGISTRY_FILENAME",
    "DIAGNOSTIC_FILE_MODE",
    "MAX_RUNTIME_EVENT_BYTES",
    "RUNTIME_EVENTS_FILENAME",
    "RuntimeEventsRead",
    "TELA_DIRECTORY_MODE",
    "append_runtime_event",
    "append_runtime_event_best_effort",
    "attachment_registry_path",
    "read_attachment_registry",
    "read_runtime_events",
    "runtime_events_path",
    "upsert_attachment",
    "upsert_attachment_registry",
    "upsert_client_attachment",
    "write_attachment_registry",
]
