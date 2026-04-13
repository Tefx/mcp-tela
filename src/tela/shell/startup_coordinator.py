"""Startup coordination for lockfile discovery and autostart leadership.

This module owns race-sensitive startup arbitration for ``tela connect``:

- config ownership matching for lockfile discovery
- single-leader autostart lock per resolved config path
- follower wait/attach behavior while leader is starting

The coordinator only sequences discovery/autostart entry flow. It does not own
readiness, downstream convergence, or notification/audit policy.
"""

from __future__ import annotations

import fcntl
import hashlib
from pathlib import Path
import time
from typing import IO, Callable

from tela.core.models import LockfileData
from tela.shell.result import Result
from tela.shell.lockfile import delete_lockfile


STARTUP_LOCK_DIR = Path.home() / ".tela"
STARTUP_LOCK_PREFIX = "startup."
STARTUP_LOCK_SUFFIX = ".lock"
FOLLOWER_WAIT_POLL_SECONDS = 0.1
RACE_WAIT_SECONDS = 0.3
START_RACE_RETRIES = 3


ReadLockfile = Callable[[], Result[LockfileData, str]]
WaitForLiveLockfile = Callable[[float, int | None], Result[LockfileData, str]]
AutostartServe = Callable[[str, str | None], Result[int, str]]


def _normalize_config_path(raw_path: str) -> Result[str, str]:
    """Return canonical absolute config path for ownership checks."""

    try:
        return Result(value=str(Path(raw_path).expanduser().resolve(strict=False)))
    except OSError as exc:
        return Result(error=f"CONFIG_PATH_RESOLVE_ERROR: {exc}")


def _requested_config_owner_path(raw_path: str) -> Result[str | None, str]:
    """Return canonical requested path when ownership can be asserted."""

    path = Path(raw_path).expanduser()
    if not path.is_absolute() and not path.exists():
        return Result(value=None)
    try:
        return Result(value=str(path.resolve(strict=False)))
    except OSError as exc:
        return Result(error=f"CONFIG_PATH_RESOLVE_ERROR: {exc}")


def _lockfile_owned_by_config(
    lockfile: LockfileData, requested_config_path: str
) -> Result[bool, str]:
    """Return whether lockfile ownership matches requested config path."""

    requested_owner_result = _requested_config_owner_path(requested_config_path)
    if requested_owner_result.is_err:
        return Result(error=requested_owner_result.error)
    requested_owner_path = requested_owner_result.value
    if requested_owner_path is None:
        return Result(value=True)

    lockfile_owner_result = _normalize_config_path(lockfile.config_path)
    if lockfile_owner_result.is_err:
        return Result(error=lockfile_owner_result.error)
    assert lockfile_owner_result.value is not None
    return Result(value=lockfile_owner_result.value == requested_owner_path)


def _startup_lock_path(requested_config_path: str) -> Result[Path, str]:
    """Build per-config startup lock path in ``~/.tela``."""

    normalized_result = _normalize_config_path(requested_config_path)
    if normalized_result.is_err:
        return Result(error=normalized_result.error)
    assert normalized_result.value is not None
    normalized = normalized_result.value
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return Result(
        value=STARTUP_LOCK_DIR / f"{STARTUP_LOCK_PREFIX}{digest}{STARTUP_LOCK_SUFFIX}"
    )


# @shell_complexity: Lifecycle event handlers with inherently branching behavior — routes/priorities/status modes are mutually exclusive by design.
def _try_acquire_startup_lock(
    requested_config_path: str,
) -> Result[IO[str] | None, str]:
    """Try to acquire non-blocking startup leadership lock for config."""

    lock_path_result = _startup_lock_path(requested_config_path)
    if lock_path_result.is_err:
        return Result(error=lock_path_result.error)
    assert lock_path_result.value is not None
    lock_path = lock_path_result.value
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        return Result(error=f"STARTUP_LOCK_ERROR: {exc}")

    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return Result(value=lock_handle)
    except BlockingIOError:
        lock_handle.close()
        return Result(value=None)
    except OSError as exc:
        lock_handle.close()
        return Result(error=f"STARTUP_LOCK_ERROR: {exc}")


# @shell_orchestration: releases OS file lock handle acquired for startup leadership.
def _release_startup_lock(lock_handle: IO[str] | None) -> None:
    """Release startup lock handle best-effort."""

    if lock_handle is None:
        return
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        lock_handle.close()


# @shell_complexity: polling loop handles stale cleanup, ownership filter, and optional PID filter.
def _wait_for_owned_live_lockfile(
    *,
    timeout_seconds: float,
    requested_config_path: str,
    read_lockfile: ReadLockfile,
    expected_pid: int | None = None,
) -> Result[LockfileData, str]:
    """Wait for a live lockfile owned by the requested config path.

    If ``expected_pid`` is provided, only that process id is accepted.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        lockfile_result = read_lockfile()
        if lockfile_result.is_ok:
            assert lockfile_result.value is not None
            lockfile = lockfile_result.value
            if expected_pid is not None and lockfile.pid != expected_pid:
                time.sleep(FOLLOWER_WAIT_POLL_SECONDS)
                continue
            owned_result = _lockfile_owned_by_config(lockfile, requested_config_path)
            if owned_result.is_err:
                return Result(error=owned_result.error)
            assert owned_result.value is not None
            if not owned_result.value:
                time.sleep(FOLLOWER_WAIT_POLL_SECONDS)
                continue
            return Result(value=lockfile)

        if lockfile_result.error is not None and lockfile_result.error.startswith(
            "LOCKFILE_STALE"
        ):
            _ = delete_lockfile()

        time.sleep(FOLLOWER_WAIT_POLL_SECONDS)

    return Result(error="LOCKFILE_WAIT_TIMEOUT: timed out waiting for gateway.lock")


# @shell_complexity: startup arbitration branches leader/follower lock ownership and autostart sequencing.
def discover_or_autostart(
    *,
    config_path: str,
    default_profile: str | None,
    read_lockfile: ReadLockfile,
    wait_for_live_lockfile: WaitForLiveLockfile,
    autostart_serve: AutostartServe,
    lockfile_wait_timeout_seconds: float,
) -> Result[LockfileData, str]:
    """Discover running lockfile owner or coordinate one autostart leader.

    This coordinator guarantees that followers do not spawn a second leader while
    a leader lock is held for the same config.
    """

    immediate_result = read_lockfile()
    if immediate_result.is_ok:
        assert immediate_result.value is not None
        immediate_owned_result = _lockfile_owned_by_config(
            immediate_result.value,
            config_path,
        )
        if immediate_owned_result.is_err:
            return Result(error=immediate_owned_result.error)
        assert immediate_owned_result.value is not None
        if immediate_owned_result.value:
            return Result(value=immediate_result.value)

    for _attempt in range(START_RACE_RETRIES):
        lock_result = _try_acquire_startup_lock(config_path)
        if lock_result.is_err:
            return Result(error=lock_result.error)
        lock_handle = lock_result.value

        if lock_handle is not None:
            try:
                race_wait_result = wait_for_live_lockfile(RACE_WAIT_SECONDS, None)
                if race_wait_result.is_ok:
                    assert race_wait_result.value is not None
                    race_owned_result = _lockfile_owned_by_config(
                        race_wait_result.value,
                        config_path,
                    )
                    if race_owned_result.is_err:
                        return Result(error=race_owned_result.error)
                    assert race_owned_result.value is not None
                    if race_owned_result.value:
                        return Result(value=race_wait_result.value)

                start_result = autostart_serve(config_path, default_profile)
                if start_result.is_err:
                    continue
                assert start_result.value is not None
                spawned_pid = start_result.value

                wait_result = wait_for_live_lockfile(
                    lockfile_wait_timeout_seconds,
                    spawned_pid,
                )
                if wait_result.is_ok:
                    assert wait_result.value is not None
                    wait_owned_result = _lockfile_owned_by_config(
                        wait_result.value,
                        config_path,
                    )
                    if wait_owned_result.is_err:
                        return Result(error=wait_owned_result.error)
                    assert wait_owned_result.value is not None
                    if wait_owned_result.value:
                        return Result(value=wait_result.value)
                continue
            finally:
                _release_startup_lock(lock_handle)

        follower_wait_result = _wait_for_owned_live_lockfile(
            timeout_seconds=lockfile_wait_timeout_seconds,
            requested_config_path=config_path,
            read_lockfile=read_lockfile,
        )
        if follower_wait_result.is_ok:
            assert follower_wait_result.value is not None
            return Result(value=follower_wait_result.value)

    return Result(
        error=(
            "DISCOVERY_FAILED: could not discover or auto-start tela serve via lockfile"
        )
    )
