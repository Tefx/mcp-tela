"""Shell-level lockfile and bearer-token contracts.

Contracts in this module are intentionally declarative only.
Implementation is added in the corresponding runtime step.
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import subprocess

from tela.core.contracts import post, pre
from tela.core.models import LockfileData
from tela.shell.result import Result

LOCKFILE_PATH = Path.home() / ".tela" / "gateway.lock"
LOCKFILE_TMP_SUFFIX = ".tmp"
LOCKFILE_DIRECTORY_MODE = 0o700
LOCKFILE_FILE_MODE = 0o600


__all__ = [
    "LOCKFILE_PATH",
    "LOCKFILE_TMP_SUFFIX",
    "LOCKFILE_DIRECTORY_MODE",
    "LOCKFILE_FILE_MODE",
    "is_stale",
    "write_lockfile",
    "read_lockfile",
    "delete_lockfile",
    "delete_lockfile_if_stale",
    "generate_bearer_token",
]


class _IsStale:
    """Predicate used by lockfile stale checks."""

    def __call__(self, lockfile: LockfileData) -> bool:
        """Return True when lockfile's PID does not resolve to a live process."""

        if _is_zombie_process(lockfile.pid):
            return True

        try:
            os.kill(lockfile.pid, 0)
        except OverflowError:
            return True
        except ProcessLookupError:
            return True
        except PermissionError:
            # Process exists but we lack permission to signal it.
            return False
        except OSError:
            return True

        return False


is_stale = _IsStale()


# @invar:allow shell_result: best-effort subprocess inspection returns bool, not a failable boundary.
def _is_zombie_process(pid: int) -> bool:
    """Return whether ``pid`` is a zombie process according to ``ps``.

    Best-effort only: any inspection failure is treated as non-zombie so callers
    fall back to standard liveness checks.
    """

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False

    if result.returncode != 0:
        return False

    return "Z" in result.stdout.strip().upper()


@pre(lambda data: isinstance(data, LockfileData))
@post(
    lambda result: (
        isinstance(result, Result)
        and (
            (result.value is None and result.error is None) or result.error is not None
        )
    )
)
def write_lockfile(data: LockfileData) -> Result[None, str]:
    """Persist ``LockfileData`` to ``~/.tela/gateway.lock``.

    Contract requirements:
    - Write data atomically via temp-file write + ``os.rename``.
    - Temp file should use ``LOCKFILE_TMP_SUFFIX``.
    - Parent directory created with mode ``0o700``.
    - Lockfile file created with mode ``0o600``.

    Returns:
        ``Result[None, str]``
    """

    temp_path = LOCKFILE_PATH.with_name(f"{LOCKFILE_PATH.name}{LOCKFILE_TMP_SUFFIX}")
    try:
        LOCKFILE_PATH.parent.mkdir(
            parents=True, exist_ok=True, mode=LOCKFILE_DIRECTORY_MODE
        )
        LOCKFILE_PATH.parent.chmod(LOCKFILE_DIRECTORY_MODE)

        payload = data.model_dump_json()
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.chmod(LOCKFILE_FILE_MODE)
        temp_path.replace(LOCKFILE_PATH)
        return Result()
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return Result(error=f"LOCKFILE_WRITE_ERROR: {exc}")


@pre(lambda: True)
@post(
    lambda result: (
        isinstance(result, Result)
        and (
            (result.value is None and result.error is not None)
            or (isinstance(result.value, LockfileData) and result.error is None)
        )
    )
)
# @shell_complexity: lockfile I/O branches on file-read, JSON-parse, and stale-PID detection.
def read_lockfile() -> Result[LockfileData, str]:
    """Read and parse ``~/.tela/gateway.lock``.

    Additional contract checks:
    - Extra fields in the lockfile are **accepted** (per Pydantic's default
      ``extra="ignore"`` behavior) and will not cause parse errors.
    - Only the 7 required fields defined in ``LockfileData`` are guaranteed
      to be present; extra fields are silently ignored.
    - Stale entries must be detected via PID liveness checks.

    Staleness rule:
    - lockfile data is considered stale when ``pid`` is not alive.

    Returns:
        ``Result[LockfileData, str]``
    """

    try:
        raw = LOCKFILE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Result(error="LOCKFILE_READ_ERROR: lockfile does not exist")
    except OSError as exc:
        return Result(error=f"LOCKFILE_READ_ERROR: {exc}")

    try:
        data = LockfileData.model_validate_json(raw)
    except Exception as exc:
        return Result(error=f"LOCKFILE_PARSE_ERROR: {exc}")

    if is_stale(data):
        return Result(
            error=(
                f"LOCKFILE_STALE: no live process found for pid {data.pid}; "
                "lockfile should be considered stale"
            )
        )

    return Result(value=data)


# @shell_orchestration: stale cleanup must re-read current on-disk state to avoid deleting a fresh lockfile published by a newly started live server.
def delete_lockfile_if_stale() -> Result[bool, str]:
    """Delete the lockfile only when the current on-disk entry is still stale.

    This is the compare-and-delete form used by discovery/autostart wait loops.
    It prevents a stale-read caller from deleting a newer live lockfile that was
    published after the stale read but before cleanup executed.
    """

    try:
        raw = LOCKFILE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Result(value=False)
    except OSError as exc:
        return Result(error=f"LOCKFILE_READ_ERROR: {exc}")

    try:
        data = LockfileData.model_validate_json(raw)
    except Exception as exc:
        return Result(error=f"LOCKFILE_PARSE_ERROR: {exc}")

    if not is_stale(data):
        return Result(value=False)

    delete_result = delete_lockfile()
    if delete_result.is_err:
        return Result(error=delete_result.error)
    return Result(value=True)


@pre(lambda: True)
@post(
    lambda result: (
        isinstance(result, Result)
        and (
            (result.value is None and result.error is None) or result.error is not None
        )
    )
)
def delete_lockfile() -> Result[None, str]:
    """Delete ``~/.tela/gateway.lock`` during shutdown.

    Stale lockfiles should be removed safely as a cleanup step.

    Returns:
        ``Result[None, str]``
    """

    try:
        LOCKFILE_PATH.unlink()
        return Result()
    except FileNotFoundError:
        return Result()
    except OSError as exc:
        return Result(error=f"LOCKFILE_DELETE_ERROR: {exc}")


@pre(lambda: True)
@post(
    lambda result: (
        isinstance(result, Result)
        and result.error is None
        and isinstance(result.value, str)
        and len(result.value) >= 43
    )
)
def generate_bearer_token() -> Result[str, str]:
    """Generate bearer token for gateway startup.

    MUST use ``secrets.token_urlsafe(32)`` and return the generated token string.
    The token is printed to stderr by the startup flow and stored in the lockfile
    ``token`` field.

    Notes:
        ``secrets.token_urlsafe(32)`` typically yields at least 43 printable chars.

    Examples:
        >>> result = generate_bearer_token()
        >>> result.is_ok
        True
        >>> token = result.value
        >>> token is not None
        True

    Returns:
        Result containing generated bearer token.
    """

    return Result(value=secrets.token_urlsafe(32))
