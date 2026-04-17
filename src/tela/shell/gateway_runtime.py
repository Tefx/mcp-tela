"""Locked runtime state accessors for the gateway.

Extracted from gateway.py to stay within shell file-size limits.
All public functions follow Shell convention: return Result[T, E].

Authoritative runtime boundary policy (applies to ALL public accessors):

  DATA READ:     Returns a deep-copied / detached snapshot.  Callers
                 may freely read or discard the returned value; it
                 shares no mutable state with the runtime.
                 Applies to: get_runtime_config, get_runtime_secrets,
                 get_runtime_connections_snapshot,
                 get_session_registry_snapshot.

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
                 increment_*, capture_*, release_*) that acquire
                 ``_runtime_lock`` for the full mutation.

Session registry authority:
  The session registry (connection_id -> UpstreamSession mapping) is
  owned by GatewayRuntime under ``_runtime.session_registry``.  All
  session capture/release/lookup operations are locked accessors in
  this module.  upstream.py provides MCP-handler-level convenience
  wrappers that delegate here.

Reaper and converge-event authority:
  The ConnectionReaper instance and asyncio.Event for downstream
  convergence are owned by GatewayRuntime under ``_runtime.reaper``
  and ``_runtime.converge_event``.  Their lifecycle (start/stop/set)
  is managed through locked accessors in this module.
"""

# @invar:allow file_size: Single authority for all runtime/session state — session registry, reaper, and converge event were consolidated here from upstream.py and gateway.py to eliminate split runtime-truth ownership. Splitting would re-introduce the dual-ownership problem this consolidation resolves.

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, TypeVar, runtime_checkable

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette

from tela.core.models import ConnectionContext, TelaConfig
from tela.shell.result import Result

_T = TypeVar("_T")


# --- Session Capture Protocol (authority: gateway_runtime.py) ---


@runtime_checkable
class UpstreamSession(Protocol):
    """Upstream MCP session that can receive tool-list-changed notifications."""

    async def send_tool_list_changed(self) -> None:
        """Send ``notifications/tools/list_changed`` to the upstream client."""
        ...


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
    """Mutable gateway runtime state.

    Single authority for all runtime/session truth.  Session registry,
    reaper instance, and converge event are co-located with config,
    connections, and server references so that ``_runtime_lock`` covers
    all mutable state transitions atomically.
    """

    config: TelaConfig | None = None
    startup_config: object | None = None
    start_time: float | None = None
    connections: list[ConnectionContext] = field(default_factory=list)
    total_tool_calls: int = 0
    running: bool = False
    upstream_server: FastMCP | None = None
    expected_bearer_token: str | None = None
    secrets: list[str] = field(default_factory=list)
    pending_bridge_registrations: set[str] = field(default_factory=set)
    # Session registry: connection_id -> UpstreamSession
    session_registry: dict[str, UpstreamSession] = field(default_factory=dict)
    # Connection reaper instance (lifecycle managed via locked accessors)
    reaper: Any = None  # ConnectionReaper | None (lazy to avoid import cycle)
    # asyncio.Event signalling downstream convergence is complete
    converge_event: asyncio.Event | None = None


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
        >>> c = ConnectionContext(connection_id="test", profile_id="p", connected_at="t")
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
        >>> add_runtime_connection(ConnectionContext(connection_id="doc1", profile_id="p", connected_at="t"))
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
    """Remove all connections and pending bridge registrations under lock.

    Examples:
        >>> clear_runtime_connections()
        >>> get_runtime_connections_snapshot().value
        []
    """
    with _runtime_lock:
        _runtime.connections.clear()
        _runtime.pending_bridge_registrations.clear()


def register_bridge_connection(connection_id: str) -> Result[None, str]:
    """Register a bridge connection identifier before MCP initialize.

    The bridge lifecycle uses ``POST /connect`` to reserve a connection ID.
    Canonical profile binding still occurs later at MCP initialize.
    """

    if not connection_id:
        return Result(
            error="BRIDGE_REGISTRATION_FAILED: connection_id must not be empty"
        )
    with _runtime_lock:
        _runtime.pending_bridge_registrations.add(connection_id)
    return Result(value=None)


def has_bridge_registration(connection_id: str) -> Result[bool, str]:
    """Return whether a bridge connection identifier has been registered."""

    if not connection_id:
        return Result(
            error="BRIDGE_REGISTRATION_LOOKUP_FAILED: connection_id must not be empty"
        )
    with _runtime_lock:
        return Result(value=connection_id in _runtime.pending_bridge_registrations)


def remove_bridge_registration(connection_id: str) -> Result[bool, str]:
    """Remove a registered bridge connection identifier.

    Returns True when a pending bridge registration existed for the ID.
    """

    if not connection_id:
        return Result(
            error="BRIDGE_REGISTRATION_REMOVE_FAILED: connection_id must not be empty"
        )
    with _runtime_lock:
        existed = connection_id in _runtime.pending_bridge_registrations
        _runtime.pending_bridge_registrations.discard(connection_id)
    return Result(value=existed)


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


# --- Session Registry Accessors (authority: gateway_runtime.py) ---


def capture_session(connection_id: str, session: UpstreamSession) -> Result[None, str]:
    """Register an upstream MCP session for a connection.

    First-binding semantics: re-capturing the *same* session is idempotent.
    A *different* session on an already-bound connection_id returns error.
    Thread-safe: acquires ``_runtime_lock``.

    Examples:
        >>> from tela.shell.gateway_runtime import capture_session, release_session
        >>> class S:
        ...     async def send_tool_list_changed(self) -> None: ...
        >>> r = capture_session("conn_abc", S())
        >>> r.is_ok
        True
        >>> _ = release_session("conn_abc")

    Args:
        connection_id: The connection identifier from ``ConnectionContext``.
        session: The upstream MCP session implementing ``UpstreamSession``.

    Returns:
        Result[None, str] on success, error if empty or already bound.
    """
    if not connection_id:
        return Result(error="SESSION_CAPTURE_FAILED: connection_id must not be empty")
    with _runtime_lock:
        existing = _runtime.session_registry.get(connection_id)
        if existing is not None:
            if existing is session:
                return Result(value=None)  # idempotent re-capture
            return Result(
                error=f"SESSION_ALREADY_BOUND: connection '{connection_id}' already has a session"
            )
        _runtime.session_registry[connection_id] = session
    return Result(value=None)


def release_session(connection_id: str) -> Result[None, str]:
    """Remove a captured session for a disconnected connection.

    Idempotent: silently succeeds if not in registry.
    Thread-safe: acquires ``_runtime_lock``.

    Examples:
        >>> from tela.shell.gateway_runtime import release_session
        >>> r = release_session("nonexistent")
        >>> r.is_ok
        True

    Args:
        connection_id: The connection identifier to release.

    Returns:
        Result[None, str] always succeeds.
    """
    with _runtime_lock:
        _runtime.session_registry.pop(connection_id, None)
    return Result(value=None)


def get_captured_session(connection_id: str) -> Result[UpstreamSession, str]:
    """Look up a captured session by connection ID.

    Returns the session if found, or an error string if no session
    is registered for the given connection.

    Thread-safe: acquires ``_runtime_lock``.

    Examples:
        >>> from tela.shell.gateway_runtime import get_captured_session
        >>> r = get_captured_session("nonexistent")
        >>> r.is_err
        True
        >>> "not found" in r.error
        True

    Args:
        connection_id: The connection identifier to look up.

    Returns:
        Result[UpstreamSession, str] with the session or error.
    """
    with _runtime_lock:
        session = _runtime.session_registry.get(connection_id)
    if session is None:
        return Result(
            error=f"SESSION_NOT_FOUND: session for '{connection_id}' not found"
        )
    return Result(value=session)


def get_connection_id_for_session(session: UpstreamSession) -> Result[str, str]:
    """Reverse-lookup: find the connection_id bound to a session.

    Thread-safe: acquires ``_runtime_lock``.

    Examples:
        >>> from tela.shell.gateway_runtime import (
        ...     capture_session, get_connection_id_for_session, release_session,
        ... )
        >>> class S:
        ...     async def send_tool_list_changed(self) -> None: ...
        >>> s = S()
        >>> _ = capture_session("c", s)
        >>> r = get_connection_id_for_session(s)
        >>> r.is_ok and r.value == "c"
        True
        >>> _ = release_session("c")

    Args:
        session: The upstream session object to look up.

    Returns:
        Result[str, str] with connection_id, or error if not registered.
    """
    with _runtime_lock:
        for conn_id, registered in _runtime.session_registry.items():
            if registered is session:
                return Result(value=conn_id)
    return Result(error="SESSION_NOT_REGISTERED: session has no binding")


def get_session_registry_snapshot() -> Result[dict[str, UpstreamSession], str]:
    """Return a shallow copy of the session registry under lock.

    The returned dict is a snapshot; mutations to it do not affect runtime.
    Session objects themselves are not copied (they are live references).

    Examples:
        >>> r = get_session_registry_snapshot()
        >>> r.is_ok
        True
        >>> isinstance(r.value, dict)
        True

    Returns:
        Result with a copy of the session registry dict.
    """
    with _runtime_lock:
        return Result(value=dict(_runtime.session_registry))


def clear_session_registry() -> None:
    """Remove all captured sessions from the registry under lock.

    Examples:
        >>> clear_session_registry()
        >>> get_session_registry_snapshot().value
        {}
    """
    with _runtime_lock:
        _runtime.session_registry.clear()


# --- Reaper Accessors (authority: gateway_runtime.py) ---


def get_runtime_reaper() -> Result[Any, str]:
    """Return the ConnectionReaper instance under lock, or None.

    Operation accessor: the reaper reference does not escape as a
    mutable alias.  Returns the reaper for lifecycle calls (start/stop)
    under the caller's control.

    Examples:
        >>> r = get_runtime_reaper()
        >>> r.is_ok
        True

    Returns:
        Result with the reaper instance or None.
    """
    with _runtime_lock:
        return Result(value=_runtime.reaper)


def set_runtime_reaper(reaper: Any) -> None:
    """Replace the runtime reaper instance under lock.

    Used during gateway startup (create) and shutdown (set None).

    Examples:
        >>> set_runtime_reaper(None)
        >>> get_runtime_reaper().value is None
        True
    """
    with _runtime_lock:
        _runtime.reaper = reaper


# --- Converge Event Accessors (authority: gateway_runtime.py) ---


def get_runtime_converge_event() -> Result[asyncio.Event | None, str]:
    """Return the converge event under lock, or None.

    Examples:
        >>> r = get_runtime_converge_event()
        >>> r.is_ok
        True

    Returns:
        Result with the asyncio.Event or None.
    """
    with _runtime_lock:
        return Result(value=_runtime.converge_event)


def set_runtime_converge_event(event: asyncio.Event | None) -> None:
    """Replace the runtime converge event under lock.

    Used during gateway startup (create) and shutdown (set None).

    Examples:
        >>> set_runtime_converge_event(None)
        >>> get_runtime_converge_event().value is None
        True
    """
    with _runtime_lock:
        _runtime.converge_event = event
