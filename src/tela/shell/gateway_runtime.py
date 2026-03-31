"""Locked runtime state accessors for the gateway.

Extracted from gateway.py to stay within shell file-size limits.
All public functions follow Shell convention: return Result[T, E].

Authoritative runtime boundary policy (applies to ALL public accessors):

  DATA READ:     Returns a deep-copied / detached snapshot.  Callers
                 may freely read or discard the returned value; it
                 shares no mutable state with the runtime.
                 Applies to: get_runtime_config, get_runtime_secrets,
                 get_runtime_connections_snapshot.

  SNAPSHOT:      Frozen dataclass with deep-copied Pydantic models and
                 containers.  No shallow alias survives the boundary.
                 Applies to: get_runtime_status_snapshot.

  OPERATION:     For non-copyable runtime-owned services (e.g. FastMCP),
                 the accessor acquires the lock, performs the needed
                 operation on the live service, and returns only the
                 operation result — never the service reference itself.
                 Applies to: get_upstream_http_app, get_upstream_log_level,
                 is_upstream_server_initialized.

  WRITE:         Locked mutators (set_*, add_*, remove_*, clear_*,
                 increment_*) that acquire ``_runtime_lock`` for the
                 full mutation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Literal, TypeVar

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette

from tela.core.models import ConnectionContext, TelaConfig
from tela.shell.result import Result

_T = TypeVar("_T")

RuntimeTruthPlane = Literal[
    "discovery",
    "lifecycle_readiness",
    "downstream_convergence",
]


@dataclass(frozen=True)
class RuntimeTruthContract:
    """Declarative source-of-truth contract for one runtime plane.

    These contracts are intentionally descriptive only. They define which
    artifact a consumer may trust for a given concern and, equally important,
    which adjacent concerns MUST NOT be inferred from that artifact.
    """

    plane: RuntimeTruthPlane
    authoritative_artifact: str
    authoritative_fields: tuple[str, ...]
    not_authoritative_for: tuple[RuntimeTruthPlane, ...]
    consumer_rule: str


LOCKFILE_DISCOVERY_CONTRACT = RuntimeTruthContract(
    plane="discovery",
    authoritative_artifact="~/.tela/gateway.lock",
    authoritative_fields=(
        "pid",
        "host",
        "port",
        "token",
        "config_path",
        "started_at",
        "version",
    ),
    not_authoritative_for=("lifecycle_readiness", "downstream_convergence"),
    consumer_rule=(
        "Use lockfile data only to discover process identity, bind target, auth bootstrap, "
        "and startup config ownership. Do not infer readiness or downstream sync from lockfile presence."
    ),
)


STATUS_SNAPSHOT_CONTRACT = RuntimeTruthContract(
    plane="lifecycle_readiness",
    authoritative_artifact="RuntimeStatusSnapshot / GET /status",
    authoritative_fields=(
        "running",
        "start_time",
        "connections",
        "total_tool_calls",
        "config",
    ),
    not_authoritative_for=("discovery",),
    consumer_rule=(
        "Use runtime status snapshots to answer gateway lifecycle/readiness questions. "
        "Do not replace status checks with lockfile existence checks."
    ),
)


RUNTIME_TRUTH_CONTRACTS: tuple[RuntimeTruthContract, ...] = (
    LOCKFILE_DISCOVERY_CONTRACT,
    STATUS_SNAPSHOT_CONTRACT,
)


RUNTIME_TRUTH_BEHAVIORAL_NOTES: tuple[str, ...] = (
    "Discovery succeeds when an endpoint can be located; readiness remains a separate runtime question.",
    "A live process advertised by the lockfile may still be unready or unconverged downstream.",
    "Connect/serve consumers must gate lifecycle decisions on runtime status, not discovery artifacts.",
)


@dataclass
class GatewayRuntime:
    """Mutable gateway runtime state."""

    config: TelaConfig | None = None
    startup_config: object | None = None
    start_time: float | None = None
    connections: list[ConnectionContext] = field(default_factory=list)
    total_tool_calls: int = 0
    running: bool = False
    upstream_server: FastMCP | None = None
    expected_bearer_token: str | None = None
    secrets: list[str] = field(default_factory=list)


_runtime = GatewayRuntime()
_runtime_lock = threading.RLock()


# --- Locked runtime accessors ------------------------------------------


def get_runtime_config() -> Result[TelaConfig | None, str]:
    """Return a deep copy of the current runtime config under lock.

    The returned ``TelaConfig`` is a deep-copied Pydantic model captured
    while ``_runtime_lock`` is held.  Callers may read or mutate the
    returned object freely; changes do **not** propagate back to runtime
    state.

    Examples:
        >>> r = get_runtime_config()
        >>> r.is_ok
        True

    Returns:
        Result with deep-copied TelaConfig or None.
    """
    with _runtime_lock:
        if _runtime.config is None:
            return Result(value=None)
        return Result(value=_runtime.config.model_copy(deep=True))


def set_runtime_config(config: TelaConfig | None) -> None:
    """Replace the runtime config under lock.

    Examples:
        >>> from tela.core.models import TelaConfig
        >>> set_runtime_config(TelaConfig())
        >>> get_runtime_config().value is not None
        True
        >>> set_runtime_config(None)
    """
    with _runtime_lock:
        _runtime.config = config


def is_runtime_running() -> Result[bool, str]:
    """Return whether the gateway runtime is running, under lock.

    Examples:
        >>> r = is_runtime_running()
        >>> r.is_ok
        True
        >>> isinstance(r.value, bool)
        True

    Returns:
        Result with boolean running state.
    """
    with _runtime_lock:
        return Result(value=_runtime.running)


def get_runtime_connections_snapshot() -> Result[list[ConnectionContext], str]:
    """Return a deep-copied snapshot of the active connections list under lock.

    The returned list and its ``ConnectionContext`` members are fully
    detached from runtime state.  Mutations to the returned objects do
    not affect the runtime connections list.

    Examples:
        >>> r = get_runtime_connections_snapshot()
        >>> r.is_ok
        True
        >>> r.value
        []

    Returns:
        Result with deep-copied list of ConnectionContext.
    """
    with _runtime_lock:
        return Result(value=[c.model_copy(deep=True) for c in _runtime.connections])


def add_runtime_connection(ctx: ConnectionContext) -> None:
    """Append a connection to the runtime connections list under lock.

    Examples:
        >>> c = ConnectionContext(connection_id="test", profile_name="p", connected_at="t")
        >>> add_runtime_connection(c)
        >>> get_runtime_connections_snapshot().value  # doctest: +ELLIPSIS
        [ConnectionContext(...)]
        >>> remove_runtime_connection("test").value
        True
    """
    with _runtime_lock:
        _runtime.connections.append(ctx)


def remove_runtime_connection(connection_id: str) -> Result[bool, str]:
    """Remove a connection by ID under lock.  Returns True if removed.

    Examples:
        >>> r = remove_runtime_connection("nonexistent")
        >>> r.is_ok
        True
        >>> r.value
        False

    Returns:
        Result with True if a connection was removed, False otherwise.
    """
    with _runtime_lock:
        original = len(_runtime.connections)
        _runtime.connections[:] = [
            c for c in _runtime.connections if c.connection_id != connection_id
        ]
        return Result(value=len(_runtime.connections) != original)


def touch_connection_activity(connection_id: str, timestamp: str) -> Result[bool, str]:
    """Update last_activity for a connection. Returns True if connection found.

    Thread-safe: acquires _runtime_lock.

    Args:
        connection_id: The connection identifier to update.
        timestamp: ISO-8601 timestamp to record as the last activity time.

    Returns:
        Result with True if the connection was found and updated,
        False if no connection with the given ID exists.

    Examples:
        >>> r = touch_connection_activity("nonexistent", "2026-01-01T00:00:00Z")
        >>> r.is_ok
        True
        >>> r.value
        False

        >>> from tela.core.models import ConnectionContext
        >>> add_runtime_connection(ConnectionContext(connection_id="doc1", profile_name="p", connected_at="t"))
        >>> r = touch_connection_activity("doc1", "2026-03-31T12:00:00Z")
        >>> r.value
        True
        >>> snap = get_runtime_connections_snapshot()
        >>> [c for c in snap.value if c.connection_id == "doc1"][0].last_activity
        '2026-03-31T12:00:00Z'
        >>> remove_runtime_connection("doc1").value
        True
    """
    with _runtime_lock:
        for conn in _runtime.connections:
            if conn.connection_id == connection_id:
                conn.last_activity = timestamp
                return Result(value=True)
        return Result(value=False)


def set_runtime_running(running: bool) -> None:
    """Set the runtime running flag under lock.

    Examples:
        >>> set_runtime_running(True)
        >>> is_runtime_running().value
        True
        >>> set_runtime_running(False)
    """
    with _runtime_lock:
        _runtime.running = running


def clear_runtime_connections() -> None:
    """Remove all connections from the runtime under lock.

    Examples:
        >>> clear_runtime_connections()
        >>> get_runtime_connections_snapshot().value
        []
    """
    with _runtime_lock:
        _runtime.connections.clear()


def increment_tool_calls() -> None:
    """Atomically increment the tool-call counter under lock.

    Examples:
        >>> increment_tool_calls()
    """
    with _runtime_lock:
        _runtime.total_tool_calls += 1


def get_runtime_secrets() -> Result[list[str], str]:
    """Return a copy of runtime auth secrets under lock.

    Examples:
        >>> r = get_runtime_secrets()
        >>> r.is_ok
        True
        >>> isinstance(r.value, list)
        True

    Returns:
        Result with a copy of the secrets list.
    """
    with _runtime_lock:
        return Result(value=list(_runtime.secrets))


def set_runtime_secrets(secrets: list[str]) -> None:
    """Replace the runtime auth secrets list under lock.

    Examples:
        >>> set_runtime_secrets(["s1", "s2"])
        >>> get_runtime_secrets().value
        ['s1', 's2']
        >>> set_runtime_secrets([])
    """
    with _runtime_lock:
        _runtime.secrets = list(secrets)


def set_runtime_total_tool_calls(count: int) -> None:
    """Set the runtime tool-call counter under lock.

    Examples:
        >>> set_runtime_total_tool_calls(0)
    """
    with _runtime_lock:
        _runtime.total_tool_calls = count


def with_upstream_server(fn: Callable[[FastMCP], _T]) -> Result[_T, str]:
    """Execute *fn* with the live upstream server under lock, return the result.

    The server reference does **not** escape: *fn* receives it for a
    single synchronous call inside ``_runtime_lock``, and only the return
    value of *fn* is propagated to the caller.  This is the OPERATION
    pattern applied to arbitrary test introspection (handler lookup,
    attribute reads, etc.).

    Intended for **test code only**.  Production callers should use the
    typed operation accessors (``get_upstream_http_app``,
    ``get_upstream_log_level``, ``is_upstream_server_initialized``).

    Args:
        fn: Synchronous callable receiving the ``FastMCP`` instance.

    Returns:
        Result containing *fn*'s return value, or an error string if the
        upstream server is not initialized.

    Examples:
        >>> r = with_upstream_server(lambda s: type(s).__name__)
        >>> r.is_ok or r.is_err
        True
    """
    with _runtime_lock:
        if _runtime.upstream_server is None:
            return Result(
                error="UPSTREAM_NOT_INITIALIZED: upstream MCP server not initialized"
            )
        return Result(value=fn(_runtime.upstream_server))


def is_upstream_server_initialized() -> Result[bool, str]:
    """Return whether the upstream FastMCP server has been created, under lock.

    This is the boundary-safe replacement for the removed
    ``get_upstream_server() is not None`` pattern.

    Examples:
        >>> r = is_upstream_server_initialized()
        >>> r.is_ok
        True
        >>> isinstance(r.value, bool)
        True

    Returns:
        Result with boolean server-initialized state.
    """
    with _runtime_lock:
        return Result(value=_runtime.upstream_server is not None)


def get_upstream_http_app() -> Result[Starlette, str]:
    """Return the Streamable HTTP ASGI app from the upstream server, under lock.

    Acquires ``_runtime_lock``, invokes ``streamable_http_app()`` on the
    live FastMCP server, and returns the resulting Starlette app.  The
    caller receives a fully constructed ASGI application without ever
    holding a reference to the runtime-owned ``FastMCP`` instance.

    Returns:
        Result containing the Starlette ASGI app, or an error string if
        the upstream server is not initialized.

    Examples:
        >>> r = get_upstream_http_app()
        >>> r.is_ok or r.is_err
        True
    """
    with _runtime_lock:
        if _runtime.upstream_server is None:
            return Result(
                error="UPSTREAM_NOT_INITIALIZED: upstream MCP server not initialized"
            )
        return Result(value=_runtime.upstream_server.streamable_http_app())


def get_upstream_log_level() -> Result[str, str]:
    """Return the upstream server's log level setting, under lock.

    Falls back to ``"info"`` if the server or its settings are unavailable.

    Examples:
        >>> r = get_upstream_log_level()
        >>> r.is_ok
        True
        >>> isinstance(r.value, str)
        True

    Returns:
        Result with log level string.
    """
    with _runtime_lock:
        if _runtime.upstream_server is None:
            return Result(value="info")
        return Result(
            value=str(
                getattr(
                    getattr(_runtime.upstream_server, "settings", None),
                    "log_level",
                    "info",
                )
            )
        )


def get_upstream_server() -> None:
    """Removed in loop 4 — use operation accessors instead.

    This function previously returned the live ``FastMCP`` reference,
    leaking a mutable runtime alias across the boundary.  It was replaced
    by ``is_upstream_server_initialized``, ``get_upstream_http_app``, and
    ``get_upstream_log_level``.

    Raises:
        RuntimeError: Always, to surface stale call sites.

    Examples:
        >>> import pytest
        >>> with pytest.raises(RuntimeError, match="removed in loop 4"):
        ...     get_upstream_server()
    """
    raise RuntimeError(
        "get_upstream_server removed in loop 4: use operation accessors "
        "(is_upstream_server_initialized, get_upstream_http_app, get_upstream_log_level)"
    )


def set_upstream_server(server: FastMCP | None) -> None:
    """Replace the upstream FastMCP server reference under lock.

    This is the locked write accessor for ``_runtime.upstream_server``.
    Used during gateway startup (internal) and test fixture setup.

    Examples:
        >>> set_upstream_server(None)
        >>> is_upstream_server_initialized().value
        False
    """
    with _runtime_lock:
        _runtime.upstream_server = server


@dataclass(frozen=True)
class RuntimeStatusSnapshot:
    """Frozen snapshot of runtime fields needed for status queries.

    All Pydantic model members (``config``, ``connections``) are
    deep-copied at construction time so no mutable alias back into
    runtime-owned objects survives the snapshot boundary.
    """

    config: TelaConfig | None
    running: bool
    start_time: float | None
    total_tool_calls: int
    connections: tuple[ConnectionContext, ...]


def get_runtime_status_snapshot() -> Result[RuntimeStatusSnapshot, str]:
    """Return a frozen snapshot of runtime status fields under lock.

    Used by HTTP status handler to capture all fields atomically.
    Config and connection members are deep-copied; the snapshot is
    fully detached from runtime state.

    Examples:
        >>> r = get_runtime_status_snapshot()
        >>> r.is_ok
        True
        >>> isinstance(r.value.running, bool)
        True

    Returns:
        Result with frozen RuntimeStatusSnapshot.
    """
    with _runtime_lock:
        return Result(
            value=RuntimeStatusSnapshot(
                config=(
                    _runtime.config.model_copy(deep=True)
                    if _runtime.config is not None
                    else None
                ),
                running=_runtime.running,
                start_time=_runtime.start_time,
                total_tool_calls=_runtime.total_tool_calls,
                connections=tuple(
                    c.model_copy(deep=True) for c in _runtime.connections
                ),
            )
        )


def get_expected_bearer_token() -> Result[str | None, str]:
    """Return the current expected bearer token under runtime lock.

    Thread-safe accessor intended as the ``get_expected_token`` callable
    for ``BearerAuthMiddleware`` (via ``.value`` unwrap).

    Examples:
        >>> r = get_expected_bearer_token()
        >>> r.is_ok
        True

    Returns:
        Result with expected bearer token string or None.
    """
    with _runtime_lock:
        return Result(value=_runtime.expected_bearer_token)
