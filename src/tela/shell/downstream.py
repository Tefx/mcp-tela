"""Downstream server management and runtime coordination boundaries."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from mcp import types as mcp_types
from mcp.client.session import MessageHandlerFnT
from mcp.shared.session import RequestResponder
from typing import Any, Literal, Protocol, TypeAlias

from tela.core.conflict import detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import ResolvedTool, ServerConfig, TelaError
from tela.shell.downstream_clients import (
    _ClientHandle,
    _enumerate_tools,
    _open_client_for_server as _transport_open_client_for_server,
    _validate_transport_mode as _transport_validate_transport_mode,
)
from tela.shell.downstream_registry import DownstreamRegistry
from tela.shell.config_loader import Result
from tela.shell.gateway_runtime import get_runtime_config

# Module-level registry instance
_registry = DownstreamRegistry()
_registry_lock = asyncio.Lock()


_clients: dict[str, _ClientHandle] = {}
_server_instructions: dict[str, str] = {}
_server_config_hints: dict[str, ServerConfig] = {}
_attempted_servers: set[str] = set()
_successful_servers: set[str] = set()
_recovery_locks: dict[str, asyncio.Lock] = {}


_RECOVERY_TIMEOUT_SECONDS = 15.0
_RECOVERY_STAGE_NOT_ATTEMPTED = "not_attempted"
_RECOVERY_STAGE_RECONNECT_STARTED = "reconnect_started"
_RECOVERY_STAGE_RECONNECT_SUCCEEDED = "reconnect_succeeded"
_RECOVERY_STAGE_CONVERGENCE_REJECTED = "convergence_rejected"
_RECOVERY_STAGE_RETRY_FAILED = "retry_failed"
_RECOVERY_STAGE_RECOVERY_TIMEOUT = "recovery_timeout"
_RECOVERY_STAGE_CLASSIFIER_UNKNOWN = "classifier_unknown"
_ELIGIBLE_RUNTIME_ERRORS: tuple[str, ...] = (
    "Client is not connected. Use the 'async with client:' context manager first.",
    "Server session was closed unexpectedly",
)


# --- Downstream convergence contracts (from lockfile_status_contract) ---

DownstreamSyncTruth = Literal["registry", "reconnect_payload", "live_reenumeration"]


@dataclass(frozen=True)
class DownstreamConvergenceContract:
    """Declarative contract for downstream synchronization truth.

    This module owns downstream convergence state: connected sessions, resolved
    tool registry contents, and reconnect/reload update application.
    """

    authoritative_sources: tuple[DownstreamSyncTruth, ...]
    not_authoritative_sources: tuple[str, ...]
    consumer_rule: str


DOWNSTREAM_CONVERGENCE_CONTRACT = DownstreamConvergenceContract(
    authoritative_sources=("registry", "reconnect_payload", "live_reenumeration"),
    not_authoritative_sources=("~/.tela/gateway.lock",),
    consumer_rule=(
        "Treat downstream registry state and accepted reconnect/reload payloads as sync truth. "
        "Do not infer downstream readiness or tool convergence from lockfile discovery alone."
    ),
)


DOWNSTREAM_CONVERGENCE_BEHAVIORAL_NOTES: tuple[str, ...] = (
    "Downstream convergence is established by successful connect_all, reload acceptance, or reconnect payload application.",
    "Lockfile discovery proves endpoint discoverability only; it does not prove downstream sync.",
    "Resolved-tool routing contracts must preserve server_name + raw_name + exposed_name together on the key path.",
)


# --- Startup convergence contracts (from startup_convergence_contract) ---

EventEntryKind: TypeAlias = Literal[
    "reconnect",
    "reload",
    "watcher",
    "manual_reenumeration",
]
EnumerationFreshness: TypeAlias = Literal[
    "reuse_fresh_raw_tools",
    "requires_new_enumeration",
]


class EventEntryAdapter(Protocol):
    """Adapter boundary that translates runtime events into convergence input."""

    async def collect_raw_tools(
        self,
        server_name: str,
        server_config: ServerConfig,
        *,
        entry_kind: EventEntryKind,
    ) -> Result[list[dict], str]: ...


STARTUP_BEHAVIORAL_NOTES: tuple[str, ...] = (
    "connect_all owns multi-server startup coordination and remains outside the single-server convergence kernel in this refactor.",
    "Reconnect entry adapters must reuse fresh raw_tools already obtained during reconnect before calling the convergence kernel.",
    "Reload, watcher, and manual re-enumeration entry adapters must obtain a new enumeration before calling the convergence kernel.",
    "Entry adapters own trigger detection and transport recovery; they do not own resolve/register/conflict/rollback semantics.",
)


@dataclass(frozen=True)
class _ConnectedServerData:
    """Temporary successful downstream startup result before registry publish."""

    server_name: str
    raw_tools: list[dict]
    client_handle: _ClientHandle | None = None
    instructions: str | None = None


def _validate_transport_mode(
    server_name: str,
    server_config: ServerConfig,
) -> Result[None, str]:
    """Validate server transport mode and return explicit error on mismatch."""
    return _transport_validate_transport_mode(server_name, server_config)


async def _open_client_for_server(
    server_name: str,
    server_config: ServerConfig,
    message_handler: MessageHandlerFnT | None = None,
) -> Result[_ClientHandle, str]:
    """Open a connected client handle from a server config transport."""
    return await _transport_open_client_for_server(
        server_name,
        server_config,
        message_handler=message_handler,
    )


# @shell_orchestration: swaps client handle under lock and closes prior session via aclose().
async def _swap_client_handle(server_name: str, new_handle: _ClientHandle) -> None:
    """Replace one client handle and close any prior handle best-effort."""

    async with _registry_lock:
        old_handle = _clients.get(server_name)
        _clients[server_name] = new_handle

    if old_handle is not None:
        try:
            await old_handle.stack.aclose()
        except Exception:
            return


async def _enumerate_client_tools(
    server_name: str,
    handle: _ClientHandle,
) -> Result[list[dict], str]:
    """Enumerate tools for one connected client handle."""

    tools_result = await _enumerate_tools(handle.session)
    if tools_result.is_err:
        return Result(
            error=(
                "DOWNSTREAM_UNAVAILABLE: "
                f"re-enumeration failed for server '{server_name}': {tools_result.error}"
            )
        )
    assert tools_result.value is not None
    return Result(value=tools_result.value)


# @shell_orchestration: temporary handle cleanup closes transport stacks before registry publish.
async def _close_client_handles(handles: list[_ClientHandle]) -> None:
    """Close temporary client handles best-effort before registry publish."""

    for handle in handles:
        try:
            await handle.stack.aclose()
        except Exception:
            continue


async def _connect_server(
    server_name: str,
    server_config: ServerConfig,
) -> Result[_ConnectedServerData, str]:
    """Open one downstream client and enumerate its tools."""

    open_result = await _open_client_for_server(
        server_name,
        server_config,
        message_handler=_build_downstream_message_handler(server_name, server_config),
    )
    if open_result.is_err:
        return Result(error=open_result.error)
    assert open_result.value is not None
    client_handle = open_result.value

    tools_result = await _enumerate_tools(client_handle.session)
    if tools_result.is_err:
        try:
            await client_handle.stack.aclose()
        except Exception:
            pass
        return Result(
            error=(
                "DOWNSTREAM_CONNECT_FAILED: "
                f"server '{server_name}' connection/enumeration failed: {tools_result.error}"
            )
        )
    assert tools_result.value is not None
    return Result(
        value=_ConnectedServerData(
            server_name=server_name,
            raw_tools=tools_result.value,
            client_handle=client_handle,
            instructions=client_handle.instructions,
        )
    )


async def _handle_tools_list_changed(
    server_name: str,
    server_config: ServerConfig,
) -> None:
    """Re-enumerate server tools after downstream list-changed notification.

    Contract role: event-entry adapter.
    Enumeration policy: requires_new_enumeration.

    # NOTE: tools/list_changed semantics are evaluated against the exposed tool
    # set after resolution/registration, not against a raw downstream inventory
    # snapshot.
    """

    async with _registry_lock:
        client = _clients.get(server_name)

    if client is None:
        return

    raw_tools_result = await _enumerate_client_tools(server_name, client)
    if raw_tools_result.is_err:
        logging.warning(
            "Failed downstream tool re-enumeration for %s: %s",
            server_name,
            raw_tools_result.error,
        )
        return
    assert raw_tools_result.value is not None
    raw_tools = raw_tools_result.value

    from tela.shell.reload import on_tools_changed

    result = await on_tools_changed(server_name, server_config, raw_tools)
    if result.is_err:
        logging.warning(
            "Rejected downstream tool-list update for %s: %s",
            server_name,
            result.error,
        )


async def _handle_reconnect(
    server_name: str,
    server_config: ServerConfig,
) -> None:
    """Attempt downstream reconnect and route updated tools into reload flow.

    Contract role: event-entry adapter.
    Enumeration policy: reuse_fresh_raw_tools once reconnect enumeration succeeds.
    """

    recovery_started = time.monotonic()
    _emit_recovery_diagnostic(
        event="downstream_recovery_started",
        level="INFO",
        server_name=server_name,
        tool_name=None,
        elapsed_ms=0.0,
        recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
        underlying_error=None,
    )

    recovery_result = await _recover_server_client(
        server_name,
        deadline_monotonic=time.monotonic() + _RECOVERY_TIMEOUT_SECONDS,
    )
    if recovery_result.is_err:
        assert recovery_result.error is not None
        details = recovery_result.error.details or {}
        stage = str(details.get("recovery_stage", _RECOVERY_STAGE_RETRY_FAILED))
        event = (
            "downstream_recovery_rejected"
            if stage == _RECOVERY_STAGE_CONVERGENCE_REJECTED
            else "downstream_recovery_exhausted"
        )
        _emit_recovery_diagnostic(
            event=event,
            level="WARNING",
            server_name=server_name,
            tool_name=None,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=stage,
            underlying_error=str(
                details.get("underlying_error") or recovery_result.error.message
            ),
        )
        logging.warning(
            "Downstream reconnect rejected for %s: %s",
            server_name,
            recovery_result.error.message,
        )
        return

    _emit_recovery_diagnostic(
        event="downstream_recovery_succeeded",
        level="INFO",
        server_name=server_name,
        tool_name=None,
        elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
        recovery_stage=_RECOVERY_STAGE_RECONNECT_SUCCEEDED,
        underlying_error=None,
    )


# @shell_orchestration: builds closure that dispatches reconnect and tool-list-changed I/O.
def _build_downstream_message_handler(
    server_name: str,
    server_config: ServerConfig,
):
    """Build per-server message handler for downstream notifications/events."""

    async def _message_handler(
        message: (
            RequestResponder[mcp_types.ServerRequest, mcp_types.ClientResult]
            | mcp_types.ServerNotification
            | Exception
        ),
    ) -> None:
        _server_config_hints[server_name] = server_config
        if isinstance(message, Exception):
            await _handle_reconnect(server_name, server_config)
            return

        if isinstance(message, mcp_types.ServerNotification):
            if isinstance(message.root, mcp_types.ToolListChangedNotification):
                await _handle_tools_list_changed(server_name, server_config)

    return _message_handler


# @shell_orchestration: iterates client handles and closes each session via aclose().
async def _close_all_clients_locked() -> None:
    """Close all connected downstream sessions/processes best-effort."""

    handles = list(_clients.values())
    _clients.clear()
    for handle in handles:
        try:
            await handle.stack.aclose()
        except Exception:
            continue


# @invar:allow shell_result: returns registry object directly, not a failable I/O boundary.
def get_registry() -> DownstreamRegistry:
    """Return the module-level downstream registry."""
    return _registry


# @shell_complexity: startup path coordinates transport connection, enumeration, and conflict rollback.
async def connect_all(
    servers: dict[str, ServerConfig],
    tool_lists: dict[str, list[dict]] | None = None,
) -> Result[None, str]:
    """Connect all servers, register resolved tools, and fail on conflicts."""

    async with _registry_lock:
        await _close_all_clients_locked()
        _registry.clear()
        _server_instructions.clear()
        _server_config_hints.clear()

        all_resolved: dict[str, list[ResolvedTool]] = {}
        connected: dict[str, _ConnectedServerData] = {}

        for server_name, server_config in servers.items():
            validation_result = _validate_transport_mode(server_name, server_config)
            if validation_result.is_err:
                await _close_all_clients_locked()
                _registry.clear()
                return Result(error=validation_result.error)

        _attempted_servers.clear()
        _successful_servers.clear()

        if tool_lists is not None:
            for server_name in servers:
                _attempted_servers.add(server_name)
                if server_name in tool_lists:
                    _successful_servers.add(server_name)
                connected[server_name] = _ConnectedServerData(
                    server_name=server_name,
                    raw_tools=tool_lists.get(server_name, []),
                )
        else:
            startup_results = await asyncio.gather(
                *[
                    _connect_server(server_name, server_config)
                    for server_name, server_config in servers.items()
                ]
            )
            temporary_handles: list[_ClientHandle] = []
            server_names_list = list(servers.keys())
            for idx, startup_result in enumerate(startup_results):
                if startup_result.is_err:
                    _attempted_servers.add(server_names_list[idx])
                    await _close_client_handles(temporary_handles)
                    _registry.clear()
                    return Result(error=startup_result.error)
                assert startup_result.value is not None
                startup = startup_result.value
                _attempted_servers.add(startup.server_name)
                connected[startup.server_name] = startup
                if startup.client_handle is not None:
                    temporary_handles.append(startup.client_handle)

        for server_name, server_config in servers.items():
            startup = connected[server_name]
            _server_config_hints[server_name] = server_config
            if startup.client_handle is not None:
                _clients[server_name] = startup.client_handle
            if startup.instructions:
                _server_instructions[server_name] = startup.instructions
            resolved = resolve_tools(server_name, server_config, startup.raw_tools)
            all_resolved[server_name] = resolved
            _registry.register(server_name, resolved)

        if tool_lists is None:
            for server_name in servers:
                _successful_servers.add(server_name)

        conflicts = detect_conflicts(all_resolved)
        if conflicts:
            await _close_all_clients_locked()
            _registry.clear()
            conflict_desc = "; ".join(
                f"{c.tool_name} in [{', '.join(c.servers)}]" for c in conflicts
            )
            return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

    return Result(value=None)


async def disconnect_all() -> Result[None, str]:
    """Disconnect all servers and clear registry and connection tracking."""

    async with _registry_lock:
        await _close_all_clients_locked()
        _registry.clear()
        _server_instructions.clear()
        _server_config_hints.clear()
        _attempted_servers.clear()
        _successful_servers.clear()
        _recovery_locks.clear()
    return Result(value=None)


def _get_exception_text(exc: Exception) -> str:
    """Return normalized exception text for diagnostics."""

    return f"{type(exc).__name__}: {exc}"


def _is_recovery_eligible_exception(exc: Exception) -> bool:
    """Classify transport failures that are safe for one automatic retry."""

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return False
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return False
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        return any(msg == expected for expected in _ELIGIBLE_RUNTIME_ERRORS)
    return False


def _emit_recovery_diagnostic(
    *,
    event: str,
    level: Literal["INFO", "WARNING"],
    server_name: str,
    recovery_stage: str,
    elapsed_ms: float,
    tool_name: str | None,
    underlying_error: str | None,
) -> None:
    """Emit ADR-006 structured recovery diagnostics via logger."""

    entry: dict[str, Any] = {
        "event": event,
        "level": level,
        "server_name": server_name,
        "tool_name": tool_name,
        "elapsed_ms": elapsed_ms,
        "recovery_stage": recovery_stage,
        "underlying_error": underlying_error,
        "request_id": None,
    }
    if level == "INFO":
        logging.info("%s", entry)
        return
    logging.warning("%s", entry)


async def _prune_recovery_lock_if_unused(server_name: str) -> None:
    """Remove per-server lock entry when no active client remains."""

    async with _registry_lock:
        existing_lock = _recovery_locks.get(server_name)
        if existing_lock is None:
            return
        if existing_lock.locked():
            return
        if server_name in _clients:
            return
        _recovery_locks.pop(server_name, None)


def _build_recovery_error(
    server_name: str,
    *,
    recovery_attempted: bool,
    recovery_eligible: bool,
    recovery_stage: str,
    underlying_error: str,
    config_missing: bool | None = None,
) -> TelaError:
    """Build ADR-006 error envelope for recovery outcomes."""

    details: dict[str, Any] = {
        "server_name": server_name,
        "recovery_attempted": recovery_attempted,
        "recovery_stage": recovery_stage,
        "recovery_eligible": recovery_eligible,
        "underlying_error": underlying_error,
    }
    if config_missing is not None:
        details["config_missing"] = config_missing
    return TelaError(
        code="DOWNSTREAM_UNAVAILABLE",
        message=f"Downstream server '{server_name}' is not connected",
        details=details,
    )


async def _acquire_recovery_lock(
    server_name: str,
    *,
    deadline_monotonic: float,
) -> Result[tuple[asyncio.Lock, bool], TelaError]:
    """Acquire per-server recovery lock within shared timeout budget."""

    async with _registry_lock:
        lock = _recovery_locks.get(server_name)
        if lock is None:
            lock = asyncio.Lock()
            _recovery_locks[server_name] = lock
        wait_contended = lock.locked()

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                underlying_error="Recovery lock wait timed out",
                config_missing=False,
            )
        )
    try:
        await asyncio.wait_for(lock.acquire(), timeout=remaining)
    except asyncio.TimeoutError:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                underlying_error="Recovery lock wait timed out",
                config_missing=False,
            )
        )
    return Result(value=(lock, wait_contended))


def _get_runtime_server_config(server_name: str) -> Result[ServerConfig, TelaError]:
    """Resolve server config from runtime authority only.

    ADR-006 reload-wins rule: once runtime config no longer contains a server,
    recovery MUST fail closed and must not revive stale hint/cache state.
    """

    runtime_result = get_runtime_config()
    if runtime_result.is_err:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                underlying_error=runtime_result.error or "Runtime config read failed",
                config_missing=True,
            )
        )
    runtime_config = runtime_result.value
    if runtime_config is not None:
        server_config = runtime_config.servers.get(server_name)
        if server_config is not None:
            return Result(value=server_config)

    missing_reason = (
        "Runtime config unavailable"
        if runtime_config is None
        else f"Server '{server_name}' missing from runtime config"
    )
    return Result(
        error=_build_recovery_error(
            server_name,
            recovery_attempted=True,
            recovery_eligible=True,
            recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
            underlying_error=missing_reason,
            config_missing=True,
        )
    )


async def _close_handle_best_effort(handle: _ClientHandle) -> None:
    """Close one temporary handle without surfacing cleanup failures."""

    try:
        await handle.stack.aclose()
    except Exception:
        return


async def _drop_client_for_server(server_name: str) -> None:
    """Remove one cached client handle and close it best-effort."""

    async with _registry_lock:
        stale_handle = _clients.pop(server_name, None)
        _server_instructions.pop(server_name, None)

    if stale_handle is not None:
        await _close_handle_best_effort(stale_handle)


async def _recover_server_client(
    server_name: str,
    *,
    deadline_monotonic: float,
) -> Result[None, TelaError]:
    """Recover one downstream client through reload convergence authority."""

    lock_result = await _acquire_recovery_lock(
        server_name,
        deadline_monotonic=deadline_monotonic,
    )
    if lock_result.is_err:
        return Result(error=lock_result.error)
    assert lock_result.value is not None
    lock, wait_contended = lock_result.value

    new_handle: _ClientHandle | None = None
    old_handle: _ClientHandle | None = None
    previous_tools: list[ResolvedTool] = []
    should_prune_lock = False
    try:
        config_result = _get_runtime_server_config(server_name)
        if config_result.is_err:
            if (
                config_result.error is not None
                and (config_result.error.details or {}).get("config_missing") is True
            ):
                await _drop_client_for_server(server_name)
                should_prune_lock = True
            return Result(error=config_result.error)
        assert config_result.value is not None
        server_config = config_result.value

        if wait_contended:
            async with _registry_lock:
                current_handle = _clients.get(server_name)
                current_registry = _registry.get_all_tools().get(server_name)
            if current_handle is not None and current_registry is not None:
                return Result(value=None)

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted before reconnect",
                    config_missing=False,
                )
            )
        try:
            open_result = await asyncio.wait_for(
                _open_client_for_server(
                    server_name,
                    server_config,
                    message_handler=_build_downstream_message_handler(
                        server_name,
                        server_config,
                    ),
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted while reconnecting",
                    config_missing=False,
                )
            )
        if open_result.is_err:
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                    underlying_error=open_result.error
                    or "Reconnect transport open failed",
                    config_missing=False,
                )
            )
        assert open_result.value is not None
        new_handle = open_result.value

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted before enumeration",
                    config_missing=False,
                )
            )
        try:
            raw_tools_result = await asyncio.wait_for(
                _enumerate_client_tools(server_name, new_handle),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted while enumerating tools",
                    config_missing=False,
                )
            )
        if raw_tools_result.is_err:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                    underlying_error=raw_tools_result.error
                    or "Tool enumeration failed during recovery",
                    config_missing=False,
                )
            )
        assert raw_tools_result.value is not None

        post_enum_config = _get_runtime_server_config(server_name)
        if post_enum_config.is_err:
            await _close_handle_best_effort(new_handle)
            if (
                post_enum_config.error is not None
                and (post_enum_config.error.details or {}).get("config_missing") is True
            ):
                await _drop_client_for_server(server_name)
                should_prune_lock = True
            return Result(error=post_enum_config.error)
        assert post_enum_config.value is not None
        latest_server_config = post_enum_config.value
        if latest_server_config != server_config:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                    underlying_error="Server config changed during recovery",
                    config_missing=False,
                )
            )

        async with _registry_lock:
            previous_tools = list(_registry.get_all_tools().get(server_name, []))

        from tela.shell.reload import on_server_reconnect

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted before convergence",
                    config_missing=False,
                )
            )
        try:
            convergence_result = await asyncio.wait_for(
                on_server_reconnect(
                    server_name,
                    latest_server_config,
                    raw_tools_result.value,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted during convergence",
                    config_missing=False,
                )
            )

        if convergence_result.is_err:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_CONVERGENCE_REJECTED,
                    underlying_error=convergence_result.error
                    or "Convergence rejected recovered tools",
                    config_missing=False,
                )
            )

        pre_swap_config = _get_runtime_server_config(server_name)
        if pre_swap_config.is_err:
            await _close_handle_best_effort(new_handle)
            async with _registry_lock:
                if previous_tools:
                    _registry.register(server_name, previous_tools)
                else:
                    _registry.unregister(server_name)
            if (
                pre_swap_config.error is not None
                and (pre_swap_config.error.details or {}).get("config_missing") is True
            ):
                await _drop_client_for_server(server_name)
                should_prune_lock = True
            return Result(error=pre_swap_config.error)
        assert pre_swap_config.value is not None
        if pre_swap_config.value != latest_server_config:
            await _close_handle_best_effort(new_handle)
            async with _registry_lock:
                if previous_tools:
                    _registry.register(server_name, previous_tools)
                else:
                    _registry.unregister(server_name)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                    underlying_error="Server config changed before recovered client swap",
                    config_missing=False,
                )
            )

        async with _registry_lock:
            old_handle = _clients.get(server_name)
            _clients[server_name] = new_handle
            if new_handle.instructions:
                _server_instructions[server_name] = new_handle.instructions

        new_handle = None
        if old_handle is not None:
            await _close_handle_best_effort(old_handle)
        return Result(value=None)
    finally:
        lock.release()
        if should_prune_lock:
            await _prune_recovery_lock_if_unused(server_name)


async def call_tool(
    server_name: str,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]:
    """Call one downstream tool on a connected server session."""

    async with _registry_lock:
        client = _clients.get(server_name)

    initial_exc: Exception | None = None
    recovery_eligible = False
    deadline_monotonic = time.monotonic() + _RECOVERY_TIMEOUT_SECONDS

    if client is None:
        recovery_eligible = True
    else:
        try:
            downstream_result = await client.session.call_tool(
                tool_name,
                arguments=arguments,
            )
        except Exception as exc:
            initial_exc = exc
            recovery_eligible = _is_recovery_eligible_exception(exc)
            if not recovery_eligible:
                if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    pass
                elif isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                    pass
                else:
                    _emit_recovery_diagnostic(
                        event="downstream_recovery_classifier_unknown",
                        level="WARNING",
                        server_name=server_name,
                        tool_name=tool_name,
                        elapsed_ms=0.0,
                        recovery_stage=_RECOVERY_STAGE_CLASSIFIER_UNKNOWN,
                        underlying_error=_get_exception_text(exc),
                    )
        else:
            payload = downstream_result.model_dump(by_alias=True, exclude_none=True)
            if downstream_result.isError:
                return Result(
                    error=TelaError(
                        code="DOWNSTREAM_ERROR",
                        message=(
                            f"Downstream server '{server_name}' returned tool error for '{tool_name}'"
                        ),
                        details={
                            "server_name": server_name,
                            "tool_name": tool_name,
                            "downstream": payload,
                        },
                    )
                )
            return Result(value=payload)

    if not recovery_eligible:
        underlying = (
            _get_exception_text(initial_exc)
            if initial_exc is not None
            else f"Server '{server_name}' has no connected client"
        )
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=False,
                recovery_eligible=False,
                recovery_stage=_RECOVERY_STAGE_NOT_ATTEMPTED,
                underlying_error=underlying,
            )
        )

    recovery_started = time.monotonic()
    _emit_recovery_diagnostic(
        event="downstream_recovery_started",
        level="INFO",
        server_name=server_name,
        tool_name=tool_name,
        elapsed_ms=0.0,
        recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
        underlying_error=(
            _get_exception_text(initial_exc) if initial_exc is not None else None
        ),
    )
    recovery_result = await _recover_server_client(
        server_name,
        deadline_monotonic=deadline_monotonic,
    )
    if recovery_result.is_err:
        assert recovery_result.error is not None
        details = recovery_result.error.details or {}
        stage = str(details.get("recovery_stage", _RECOVERY_STAGE_RETRY_FAILED))
        failure_event = (
            "downstream_recovery_rejected"
            if stage == _RECOVERY_STAGE_CONVERGENCE_REJECTED
            else "downstream_recovery_exhausted"
        )
        _emit_recovery_diagnostic(
            event=failure_event,
            level="WARNING",
            server_name=server_name,
            tool_name=tool_name,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=stage,
            underlying_error=str(
                details.get("underlying_error") or recovery_result.error.message
            ),
        )
        return Result(error=recovery_result.error)

    async with _registry_lock:
        refreshed_client = _clients.get(server_name)

    if refreshed_client is None:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
                underlying_error="Recovered client handle missing after convergence",
            )
        )

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                underlying_error="Recovery timeout exhausted before retry",
                config_missing=False,
            )
        )

    try:
        retry_result = await asyncio.wait_for(
            refreshed_client.session.call_tool(tool_name, arguments=arguments),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        _emit_recovery_diagnostic(
            event="downstream_recovery_exhausted",
            level="WARNING",
            server_name=server_name,
            tool_name=tool_name,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
            underlying_error="Recovery timeout exhausted during retry",
        )
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                underlying_error="Recovery timeout exhausted during retry",
                config_missing=False,
            )
        )
    except Exception as exc:
        _emit_recovery_diagnostic(
            event="downstream_recovery_exhausted",
            level="WARNING",
            server_name=server_name,
            tool_name=tool_name,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
            underlying_error=_get_exception_text(exc),
        )
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
                underlying_error=_get_exception_text(exc),
                config_missing=False,
            )
        )

    retry_payload = retry_result.model_dump(by_alias=True, exclude_none=True)
    if retry_result.isError:
        _emit_recovery_diagnostic(
            event="downstream_recovery_exhausted",
            level="WARNING",
            server_name=server_name,
            tool_name=tool_name,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
            underlying_error="Retry returned downstream tool error",
        )
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
                underlying_error="Retry returned downstream tool error",
                config_missing=False,
            )
        )
    _emit_recovery_diagnostic(
        event="downstream_recovery_succeeded",
        level="INFO",
        server_name=server_name,
        tool_name=tool_name,
        elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
        recovery_stage=_RECOVERY_STAGE_RECONNECT_SUCCEEDED,
        underlying_error=None,
    )
    return Result(value=retry_payload)


def get_server_instructions() -> Result[dict[str, str], str]:
    """Return MCP initialize instructions keyed by server name."""

    return Result(value=dict(_server_instructions))


async def get_connected_server_names() -> Result[set[str], str]:
    """Return names of servers that currently have active client handles."""
    async with _registry_lock:
        return Result(value=set(_clients.keys()))


def get_attempted_servers() -> Result[set[str], str]:
    """Return server names included in the most recent connect attempt."""
    return Result(value=set(_attempted_servers))


def get_successful_servers() -> Result[set[str], str]:
    """Return server names that connected successfully."""
    return Result(value=set(_successful_servers))


def get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]:
    """Return resolved tools grouped by server name."""

    return Result(value=_registry.get_all_tools())


def get_tool_server(tool_name: str) -> Result[str | None, str]:
    """Return the owning server name for an exposed tool, if present."""

    return Result(value=_registry.get_tool_server(tool_name))


# @shell_complexity: re-enumeration validates live runtime and server ownership before registry update.
async def re_enumerate(
    server_name: str,
) -> Result[list[ResolvedTool], str]:
    """Re-enumerate and re-register tools for a single connected server."""

    async with _registry_lock:
        client = _clients.get(server_name)
        if client is None:
            return Result(
                error=(
                    f"DOWNSTREAM_UNAVAILABLE: downstream server '{server_name}' is not connected"
                )
            )

        config = get_runtime_config().value
        if config is None:
            return Result(
                error="DOWNSTREAM_UNAVAILABLE: gateway runtime config is not loaded"
            )

        server_config = config.servers.get(server_name)
        if server_config is None:
            return Result(
                error=(
                    f"DOWNSTREAM_UNAVAILABLE: server '{server_name}' not found in runtime config"
                )
            )

        tools_result = await _enumerate_tools(client.session)
        if tools_result.is_err:
            return Result(
                error=(
                    "DOWNSTREAM_UNAVAILABLE: "
                    f"re-enumeration failed for server '{server_name}': {tools_result.error}"
                )
            )

        assert tools_result.value is not None
        resolved = resolve_tools(server_name, server_config, tools_result.value)
        _registry.register(server_name, resolved)
        return Result(value=resolved)
