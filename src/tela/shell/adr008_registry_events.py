"""ADR-008 shell persistence for attachment diagnostics.

This module owns the filesystem boundary for ADR-008 diagnostic state:
``~/.tela/client-attachments.json`` and ``~/.tela/runtime-events.jsonl``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import IO, Iterator

from pydantic import ValidationError

from tela.core.classification import (
    AttachmentRegistry,
    ClientAttachment,
    RuntimeEvent,
    RuntimeState,
    compact_attachment_registry,
)
from tela.shell.result import Result

ATTACHMENT_REGISTRY_FILENAME = "client-attachments.json"
ATTACHMENT_REGISTRY_LOCK_FILENAME = "client-attachments.json.lock"
RUNTIME_EVENTS_FILENAME = "runtime-events.jsonl"
TELA_DIRECTORY_MODE = 0o700
DIAGNOSTIC_FILE_MODE = 0o600
MAX_RUNTIME_EVENT_BYTES = 16 * 1024
ATTACHMENT_TERMINAL_TTL = timedelta(days=14)
ATTACHMENT_TERMINAL_LIMIT = 500

logger = logging.getLogger(__name__)


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


# @shell_complexity: stable sidecar locking must distinguish missing-before-lock, removed-during-lock, read failure, and parse failure without mutating the registry.
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
    if not path.exists():
        return Result(value=AttachmentRegistry(attachments=[]))
    try:
        # Do not create the sidecar from a read-only path. Mutation paths create
        # it before the registry can exist; a legacy registry without the
        # sidecar is read directly and remains byte-for-byte unchanged.
        lock_path = path.parent / ATTACHMENT_REGISTRY_LOCK_FILENAME
        if not lock_path.exists():
            return _read_attachment_registry_path(path)
        with _attachment_registry_lock(path, fcntl.LOCK_SH):
            return _read_attachment_registry_path(path)
    except OSError as exc:
        return Result(error=f"ATTACHMENT_REGISTRY_READ_ERROR: {exc}")


def _read_attachment_registry_path(path: Path) -> Result[AttachmentRegistry, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Result(value=AttachmentRegistry(attachments=[]))
    return _decode_attachment_registry(raw)


def write_attachment_registry(registry: AttachmentRegistry) -> Result[None, str]:
    """Atomically persist the ADR-008 attachment registry.

    Terminal history is compacted with the default retention policy, then the
    registry file is written through a same-directory temporary file and
    atomically replaced under a stable sidecar lock. The ``~/.tela`` directory
    mode is forced to ``0o700`` and registry/lock file modes to ``0o600``.

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
    as empty before the upsert. Terminal history compaction runs inside the same
    stable sidecar-locked mutation critical section before atomic replacement.

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
        with _attachment_registry_lock(path, fcntl.LOCK_EX):
            registry_missing = False
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                registry_missing = True
                raw = ""
            registry: AttachmentRegistry
            if registry_missing:
                registry = AttachmentRegistry(attachments=[])
            else:
                decoded = _decode_attachment_registry(raw)
                if decoded.is_err or decoded.value is None:
                    return Result(
                        error=decoded.error
                        or "ATTACHMENT_REGISTRY_PARSE_ERROR: empty decode"
                    )
                registry = decoded.value

            next_attachments = [
                item
                for item in registry.attachments
                if item.client_id != attachment.client_id
            ]
            next_attachments.append(attachment)
            next_registry = compact_attachment_registry(
                AttachmentRegistry(attachments=next_attachments),
                datetime.now(UTC),
                ATTACHMENT_TERMINAL_TTL,
                ATTACHMENT_TERMINAL_LIMIT,
            )
            _report_attachment_registry_overflow(next_registry)
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

    payload_result = _runtime_event_payload(event)
    if payload_result.value is None:
        return Result(error=payload_result.error or "RUNTIME_EVENT_SERIALIZE_ERROR")
    payload = payload_result.value

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
        with _attachment_registry_lock(path, fcntl.LOCK_EX):
            compacted = compact_attachment_registry(
                registry,
                datetime.now(UTC),
                ATTACHMENT_TERMINAL_TTL,
                ATTACHMENT_TERMINAL_LIMIT,
            )
            _report_attachment_registry_overflow(compacted)
            return _replace_attachment_registry_under_lock(compacted, path)
    except OSError as exc:
        return Result(error=f"ATTACHMENT_REGISTRY_WRITE_ERROR: {exc}")


def _report_attachment_registry_overflow(registry: AttachmentRegistry) -> None:
    """Warn when preserved nonterminal state alone exceeds the history limit."""

    nonterminal_count = sum(
        attachment.runtime_state != RuntimeState.EXITED
        for attachment in registry.attachments
    )
    if nonterminal_count <= ATTACHMENT_TERMINAL_LIMIT:
        return
    logger.warning(
        "%s",
        {
            "event": "attachment_registry_nonterminal_overflow",
            "nonterminal_count": nonterminal_count,
            "terminal_limit": ATTACHMENT_TERMINAL_LIMIT,
        },
    )


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
    return Result(
        value=json.dumps(
            event.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _runtime_event_payload(event: RuntimeEvent) -> Result[str, str]:
    payload_result = _runtime_event_jsonl(event)
    if payload_result.value is None:
        return Result(error=payload_result.error or "RUNTIME_EVENT_SERIALIZE_ERROR")
    payload = payload_result.value
    if len(payload.encode("utf-8")) > MAX_RUNTIME_EVENT_BYTES:
        return Result(error="RUNTIME_EVENT_TOO_LARGE: event exceeds 16 KiB")
    return Result(value=payload)


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


# @shell_orchestration: context manager delegates stable sidecar file locking and keeps atomic registry replacements serialized across processes.
@contextmanager
def _attachment_registry_lock(
    registry_path: Path,
    lock_operation: int,
) -> Iterator[IO[str]]:
    """Lock a stable sidecar inode across atomic registry replacements."""

    lock_path = registry_path.parent / ATTACHMENT_REGISTRY_LOCK_FILENAME
    with _locked_file(lock_path, "a+", lock_operation) as handle:
        yield handle


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
    "ATTACHMENT_REGISTRY_LOCK_FILENAME",
    "ATTACHMENT_TERMINAL_LIMIT",
    "ATTACHMENT_TERMINAL_TTL",
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
