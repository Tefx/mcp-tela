"""Stop command surface for graceful gateway termination."""

from __future__ import annotations

import os
import signal
import time
import subprocess

from tela.shell.config_loader import Result
from tela.shell.lockfile import delete_lockfile, read_lockfile


STOP_WAIT_TIMEOUT_SECONDS = 5.0
STOP_POLL_INTERVAL_SECONDS = 0.05


def stop_command() -> Result[int, str]:
    """Request graceful shutdown of the running ``tela serve`` process."""

    run_result = _run_stop_command()
    if run_result.is_err:
        return Result(error=run_result.error)
    return Result(value=0)


# @shell_complexity: stop path branches on lockfile discovery, signal outcomes, and bounded exit wait.
def _run_stop_command() -> Result[None, str]:
    """Resolve the running server from lockfile, send ``SIGTERM``, and wait for exit."""

    lockfile_result = read_lockfile()
    if lockfile_result.is_err:
        detail = lockfile_result.error or "lockfile unavailable"
        return Result(
            error=(
                "NO_RUNNING_SERVER: no running tela server found via "
                f"~/.tela/gateway.lock ({detail})"
            )
        )
    assert lockfile_result.value is not None
    lockfile = lockfile_result.value

    try:
        os.kill(lockfile.pid, signal.SIGTERM)
    except ProcessLookupError:
        cleanup_result = _cleanup_lockfile_after_exit()
        if cleanup_result.is_err:
            return cleanup_result
        print(f"stop requested: tela server pid {lockfile.pid} already exited")
        return Result(value=None)
    except PermissionError:
        return Result(
            error=f"STOP_PERMISSION_DENIED: cannot signal tela server pid {lockfile.pid}"
        )
    except OSError as exc:
        return Result(error=f"STOP_SIGNAL_ERROR: {exc}")

    wait_result = _wait_for_process_exit(lockfile.pid)
    if wait_result.is_err:
        return wait_result
    cleanup_result = _cleanup_lockfile_after_exit()
    if cleanup_result.is_err:
        return cleanup_result
    print(f"stop confirmed: tela server pid {lockfile.pid} exited and lockfile cleaned")
    return Result(value=None)


# @shell_complexity: exit wait branches on live, exited, permission-limited, and timeout outcomes.
def _wait_for_process_exit(pid: int) -> Result[None, str]:
    """Wait boundedly for a process to exit after ``SIGTERM``."""

    deadline = time.monotonic() + STOP_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _is_zombie_process(pid):
            return Result(value=None)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return Result(value=None)
        except PermissionError:
            time.sleep(STOP_POLL_INTERVAL_SECONDS)
            continue
        except OSError as exc:
            return Result(error=f"STOP_WAIT_ERROR: {exc}")

        time.sleep(STOP_POLL_INTERVAL_SECONDS)

    return Result(
        error=(
            f"STOP_TIMEOUT: tela server pid {pid} did not exit within "
            f"{STOP_WAIT_TIMEOUT_SECONDS:.1f}s"
        )
    )


# @invar:allow shell_result: best-effort subprocess inspection returns bool, not a failable boundary.
def _is_zombie_process(pid: int) -> bool:
    """Return whether ``pid`` is a zombie process according to ``ps``."""

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


def _cleanup_lockfile_after_exit() -> Result[None, str]:
    """Remove lockfile after process exit confirmation."""

    delete_lockfile_result = delete_lockfile()
    if delete_lockfile_result.is_err:
        return Result(error=delete_lockfile_result.error)
    return Result(value=None)
