"""Shell-level lockfile and bearer-token contracts.

Contracts in this module are intentionally declarative only.
Implementation is added in the corresponding runtime step.
"""

from __future__ import annotations

from pathlib import Path

from tela.core.contracts import post, pre
from tela.core.models import LockfileData
from tela.shell.result import Result

LOCKFILE_PATH = Path.home() / ".tela" / "gateway.lock"
LOCKFILE_TMP_SUFFIX = ".tmp"
LOCKFILE_DIRECTORY_MODE = 0o700
LOCKFILE_FILE_MODE = 0o600


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

    Examples:
        >>> from tela.core.models import LockfileData
        >>> write_lockfile(LockfileData(pid=1, host="127.0.0.1", port=1234, token="t", started_at="2026-01-01T00:00:00Z", config_path="/tmp/tela.yaml", version="0.1.0"))  # doctest: +ELLIPSIS
        Traceback (most recent call last):
        ...
        NotImplementedError: ...

    Returns:
        ``Result[None, str]``
    """

    raise NotImplementedError("write_lockfile implementation is in a follow-up step")


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
def read_lockfile() -> Result[LockfileData, str]:
    """Read and parse ``~/.tela/gateway.lock``.

    Additional contract checks:
    - Parsed data must satisfy ``LockfileData`` exactly.
    - Stale entries must be detected via PID liveness checks.

    Staleness rule:
    - lockfile data is considered stale when ``pid`` is not alive.

    Examples:
        >>> read_lockfile()  # doctest: +ELLIPSIS
        Traceback (most recent call last):
        ...
        NotImplementedError: ...

    Returns:
        ``Result[LockfileData, str]``
    """

    raise NotImplementedError("read_lockfile implementation is in a follow-up step")


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

    Examples:
        >>> delete_lockfile()  # doctest: +ELLIPSIS
        Traceback (most recent call last):
        ...
        NotImplementedError: ...

    Returns:
        ``Result[None, str]``
    """

    raise NotImplementedError("delete_lockfile implementation is in a follow-up step")


@pre(lambda: True)
# @invar:allow shell_result: generate_bearer_token is a pure token generator and does not return Result.
@post(lambda result: isinstance(result, str) and len(result) >= 43)
def generate_bearer_token() -> str:
    """Generate bearer token for gateway startup.

    MUST use ``secrets.token_urlsafe(32)`` and return the generated token string.
    The token is printed to stderr by the startup flow and stored in the lockfile
    ``token`` field.

    Notes:
        ``secrets.token_urlsafe(32)`` typically yields at least 43 printable chars.

    Examples:
        >>> generate_bearer_token()  # doctest: +ELLIPSIS
        Traceback (most recent call last):
        ...
        NotImplementedError: ...

    Returns:
        Generated bearer token.
    """

    raise NotImplementedError(
        "generate_bearer_token implementation is in a follow-up step"
    )
