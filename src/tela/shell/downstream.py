"""Downstream server management and runtime coordination boundaries.

This module owns the downstream public authority surface: connect/disconnect
lifecycle, registry state, and query APIs. Recovery and call-path logic is
extracted to ``tela.shell._downstream_recovery``; this module re-exports
``call_tool`` and internal hooks for monkeypatching compatibility.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from mcp import types as mcp_types
from mcp.shared.session import RequestResponder
from typing import Literal

from tela.core.errors import DOWNSTREAM_CONNECT_FAILED, DOWNSTREAM_UNAVAILABLE
from tela.core.conflict import detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import ResolvedTool, ServerConfig
from tela.shell.downstream_clients import (
    _ClientHandle,
    _enumerate_tools,
    _open_client_for_server,
    _validate_transport_mode,
)
from tela.shell.downstream_registry import DownstreamRegistry
from tela.shell.result import Result
from tela.shell.gateway_runtime import get_runtime_config
from tela.core.classification import RuntimeEvent, RuntimeEventKind
from tela.shell.adr008_registry_events import append_runtime_event_best_effort

# Recovery constants and functions live in _downstream_recovery.
# Re-exported here for monkeypatching compatibility and public surface stability.
from tela.shell._downstream_recovery import (  # noqa: F401
    _RECOVERY_STAGE_CLASSIFIER_UNKNOWN,
    _RECOVERY_STAGE_CONVERGENCE_REJECTED,
    _RECOVERY_STAGE_NOT_ATTEMPTED,
    _RECOVERY_STAGE_RECONNECT_STARTED,
    _RECOVERY_STAGE_RECONNECT_SUCCEEDED,
    _RECOVERY_STAGE_RECOVERY_TIMEOUT,
    _RECOVERY_STAGE_RETRY_FAILED,
    _RECOVERY_TIMEOUT_SECONDS,
    _acquire_recovery_lock,
    _build_recovery_error,
    _drop_client_for_server,
    _emit_recovery_diagnostic,
    _get_exception_text,
    _get_runtime_server_config,
    _is_recovery_eligible_exception,
    _prune_recovery_lock_if_unused,
    _recover_server_client,
    call_tool,
)

# Module-level registry instance
_registry = DownstreamRegistry()
_registry_lock = asyncio.Lock()


_clients: dict[str, _ClientHandle] = {}
_server_instructions: dict[str, str] = {}
_server_config_hints: dict[str, ServerConfig] = {}
_attempted_servers: set[str] = set()
_successful_servers: set[str] = set()
_failed_servers: dict[str, ProviderStartupFailure] = {}
_in_progress_servers: set[str] = set()
_startup_complete: bool = True
_recovery_locks: dict[str, asyncio.Lock] = {}


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


PROVIDER_INITIALIZE_TIMEOUT_SECONDS = 30.0
PROVIDER_TOOLS_LIST_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProviderStartupFailure:
    """Diagnostic for one downstream provider startup failure."""

    server_name: str
    phase: str
    reason: str
    timeout: bool = False
    elapsed_ms: float | None = None


@dataclass(frozen=True)
class DownstreamStartupSnapshot:
    """Detached snapshot of the current/last downstream startup convergence."""

    attempted_servers: tuple[str, ...]
    successful_servers: tuple[str, ...]
    failed_servers: dict[str, ProviderStartupFailure]
    in_progress_servers: tuple[str, ...]
    complete: bool
    degraded_reason: str | None


@dataclass(frozen=True)
class _ConnectedServerData:
    """Temporary successful downstream startup result before registry publish."""

    server_name: str
    raw_tools: list[dict]
    client_handle: _ClientHandle | None = None
    instructions: str | None = None


# @invar:allow shell_result: pure timestamp formatting for best-effort diagnostic events
# @shell_orchestration: timestamp creation belongs at runtime diagnostic boundary
def _utc_timestamp() -> str:
    """Return a UTC timestamp for downstream diagnostic events."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# @shell_orchestration: wraps runtime-event model construction and append side effect
def _provider_event(
    kind: RuntimeEventKind,
    *,
    server_name: str,
    phase: str,
    details: dict[str, object] | None = None,
) -> None:
    """Append one best-effort downstream provider startup event."""

    payload: dict[str, object] = {"provider_name": server_name, "phase": phase}
    if details:
        payload.update(details)
    append_runtime_event_best_effort(
        RuntimeEvent(
            kind=kind,
            client_id=f"provider:{server_name}",
            client_kind="downstream_provider",
            timestamp=_utc_timestamp(),
            details=payload,
        )
    )


# @shell_orchestration: status diagnostic token formatting stays local to startup state
def _failure_reason(failure: ProviderStartupFailure) -> Result[str, str]:
    """Return a compact status degraded_reason token for one provider failure."""

    suffix = "timeout" if failure.timeout else "failed"
    return Result(value=f"provider_{failure.phase}_{suffix}:{failure.server_name}")


# @shell_orchestration: aggregates shell-owned provider failure diagnostics for /status
def _degraded_reason_from_failures(
    failures: dict[str, ProviderStartupFailure],
) -> Result[str | None, str]:
    """Return stable semicolon-separated provider diagnostics for /status."""

    if not failures:
        return Result(value=None)
    reasons: list[str] = []
    for name in sorted(failures.keys()):
        reason_result = _failure_reason(failures[name])
        if reason_result.is_err or reason_result.value is None:
            return Result(error=reason_result.error or "PROVIDER_FAILURE_REASON_ERROR")
        reasons.append(reason_result.value)
    return Result(value=";".join(reasons))


# @shell_orchestration: mutates module-owned startup convergence diagnostics
def _mark_startup_begin(server_names: set[str]) -> None:
    """Reset startup tracking for a new convergence attempt."""

    global _startup_complete
    _attempted_servers.clear()
    _successful_servers.clear()
    _failed_servers.clear()
    _in_progress_servers.clear()
    _attempted_servers.update(server_names)
    _in_progress_servers.update(server_names)
    _startup_complete = not server_names


# @shell_orchestration: mutates module-owned startup convergence diagnostics
def _mark_startup_finished(
    *,
    successful: set[str],
    failures: dict[str, ProviderStartupFailure],
) -> None:
    """Publish final startup tracking after one convergence attempt settles."""

    global _startup_complete
    _successful_servers.clear()
    _successful_servers.update(successful)
    _failed_servers.clear()
    _failed_servers.update(failures)
    _in_progress_servers.clear()
    _startup_complete = True


def get_downstream_startup_snapshot() -> Result[DownstreamStartupSnapshot, str]:
    """Return detached downstream startup/convergence diagnostics."""

    failures = dict(_failed_servers)
    degraded_reason_result = _degraded_reason_from_failures(failures)
    if degraded_reason_result.is_err:
        return Result(error=degraded_reason_result.error)
    return Result(
        value=DownstreamStartupSnapshot(
            attempted_servers=tuple(sorted(_attempted_servers)),
            successful_servers=tuple(sorted(_successful_servers)),
            failed_servers=failures,
            in_progress_servers=tuple(sorted(_in_progress_servers)),
            complete=_startup_complete,
            degraded_reason=degraded_reason_result.value,
        )
    )


def begin_downstream_startup_tracking(server_names: set[str]) -> Result[None, str]:
    """Mark downstream startup as in-progress before convergence begins."""

    _mark_startup_begin(set(server_names))
    return Result(value=None)


# @shell_orchestration: closes transport/session stack and suppresses cleanup failures
async def _close_handle_best_effort(handle: _ClientHandle) -> None:
    """Close one temporary handle without surfacing cleanup failures."""

    try:
        await handle.stack.aclose()
    except BaseException:
        return


async def _close_client_handles(handles: list[_ClientHandle]) -> None:
    """Close temporary client handles best-effort before registry publish."""

    for handle in handles:
        await _close_handle_best_effort(handle)


async def _enumerate_client_tools(
    server_name: str,
    handle: _ClientHandle,
) -> Result[list[dict], str]:
    """Enumerate tools for one connected client handle."""

    tools_result = await _enumerate_tools(handle.session)
    if tools_result.is_err:
        return Result(
            error=(
                f"{DOWNSTREAM_UNAVAILABLE}: "
                f"re-enumeration failed for server '{server_name}': {tools_result.error}"
            )
        )
    assert tools_result.value is not None
    return Result(value=tools_result.value)


# @shell_complexity: provider startup must preserve initialize/enumerate timeout, failure, success, and cleanup events.
async def _connect_server(
    server_name: str,
    server_config: ServerConfig,
) -> Result[_ConnectedServerData, str]:
    """Open one downstream client and enumerate its tools with bounded phases."""

    _provider_event(
        RuntimeEventKind.PROVIDER_STARTING,
        server_name=server_name,
        phase="initialize",
    )
    initialize_started = time.monotonic()
    try:
        open_result = await asyncio.wait_for(
            _open_client_for_server(
                server_name,
                server_config,
                message_handler=_build_downstream_message_handler(
                    server_name, server_config
                ),
            ),
            timeout=PROVIDER_INITIALIZE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - initialize_started) * 1000.0
        _provider_event(
            RuntimeEventKind.PROVIDER_TIMEOUT,
            server_name=server_name,
            phase="initialize",
            details={
                "timeout_seconds": PROVIDER_INITIALIZE_TIMEOUT_SECONDS,
                "elapsed_ms": elapsed_ms,
            },
        )
        return Result(
            error=(
                f"{DOWNSTREAM_CONNECT_FAILED}: "
                f"provider_initialize_timeout:{server_name} "
                f"timeout_seconds={PROVIDER_INITIALIZE_TIMEOUT_SECONDS}"
            )
        )

    if open_result.is_err:
        elapsed_ms = (time.monotonic() - initialize_started) * 1000.0
        _provider_event(
            RuntimeEventKind.PROVIDER_FAILED,
            server_name=server_name,
            phase="initialize",
            details={"error": open_result.error or "unknown", "elapsed_ms": elapsed_ms},
        )
        return Result(error=open_result.error)
    assert open_result.value is not None
    client_handle = open_result.value
    _provider_event(
        RuntimeEventKind.PROVIDER_INITIALIZED,
        server_name=server_name,
        phase="initialize",
        details={"elapsed_ms": (time.monotonic() - initialize_started) * 1000.0},
    )

    _provider_event(
        RuntimeEventKind.PROVIDER_TOOLS_LIST_STARTED,
        server_name=server_name,
        phase="tools_list",
    )
    tools_started = time.monotonic()
    try:
        tools_result = await asyncio.wait_for(
            _enumerate_tools(client_handle.session),
            timeout=PROVIDER_TOOLS_LIST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - tools_started) * 1000.0
        await _close_handle_best_effort(client_handle)
        _provider_event(
            RuntimeEventKind.PROVIDER_TIMEOUT,
            server_name=server_name,
            phase="tools_list",
            details={
                "timeout_seconds": PROVIDER_TOOLS_LIST_TIMEOUT_SECONDS,
                "elapsed_ms": elapsed_ms,
            },
        )
        return Result(
            error=(
                f"{DOWNSTREAM_CONNECT_FAILED}: "
                f"provider_tools_list_timeout:{server_name} "
                f"timeout_seconds={PROVIDER_TOOLS_LIST_TIMEOUT_SECONDS}"
            )
        )
    except asyncio.CancelledError:
        await _close_handle_best_effort(client_handle)
        raise

    if tools_result.is_err:
        elapsed_ms = (time.monotonic() - tools_started) * 1000.0
        await _close_handle_best_effort(client_handle)
        _provider_event(
            RuntimeEventKind.PROVIDER_FAILED,
            server_name=server_name,
            phase="tools_list",
            details={"error": tools_result.error or "unknown", "elapsed_ms": elapsed_ms},
        )
        return Result(
            error=(
                f"{DOWNSTREAM_CONNECT_FAILED}: "
                f"server '{server_name}' connection/enumeration failed: {tools_result.error}"
            )
        )
    assert tools_result.value is not None
    _provider_event(
        RuntimeEventKind.PROVIDER_TOOLS_LIST_COMPLETED,
        server_name=server_name,
        phase="tools_list",
        details={
            "tool_count": len(tools_result.value),
            "elapsed_ms": (time.monotonic() - tools_started) * 1000.0,
        },
    )
    return Result(
        value=_ConnectedServerData(
            server_name=server_name,
            raw_tools=tools_result.value,
            client_handle=client_handle,
            instructions=client_handle.instructions,
        )
    )


# @shell_orchestration: builds callbacks that delegate downstream notifications to recovery/reload handlers
def _build_downstream_message_handler(
    server_name: str,
    server_config: ServerConfig,
):
    """Build per-server message handler for downstream notifications/events."""

    # Lazy import to avoid circular dependency: _downstream_recovery imports
    # from this module at call time, so we cannot import it at module level.
    from tela.shell._downstream_recovery import (
        _handle_reconnect,
        _handle_tools_list_changed,
    )

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


# @shell_orchestration: closes module-owned downstream client sessions under registry lock
async def _close_all_clients_locked() -> None:
    """Close all connected downstream sessions/processes best-effort."""

    handles = list(_clients.values())
    _clients.clear()
    for handle in handles:
        try:
            await handle.stack.aclose()
        except BaseException:
            continue


# @invar:allow shell_result: pure diagnostic classifier for shell-owned provider startup errors
# @shell_orchestration: maps shell exception text into startup status diagnostics
def _startup_failure_from_error(
    server_name: str,
    error: str | None,
) -> ProviderStartupFailure:
    """Classify a provider startup error into phase-aware diagnostics."""

    message = error or "unknown"
    if f"provider_initialize_timeout:{server_name}" in message:
        return ProviderStartupFailure(server_name, "initialize", message, timeout=True)
    if f"provider_tools_list_timeout:{server_name}" in message:
        return ProviderStartupFailure(server_name, "tools_list", message, timeout=True)
    if "enumeration failed" in message or "DOWNSTREAM_ENUMERATE_FAILED" in message:
        return ProviderStartupFailure(server_name, "tools_list", message)
    return ProviderStartupFailure(server_name, "initialize", message)


# @invar:allow shell_result: returns registry object directly, not a failable I/O boundary.
def get_registry() -> DownstreamRegistry:
    """Return the module-level downstream registry."""
    return _registry


# @shell_complexity: startup path coordinates bounded transport connection, partial publication, and conflict rollback.
async def connect_all(
    servers: dict[str, ServerConfig],
    tool_lists: dict[str, list[dict]] | None = None,
) -> Result[None, str]:
    """Connect servers, publish successful providers, and record failures.

    Startup convergence is intentionally partial: one failed or timed-out
    downstream provider must not prevent successfully enumerated providers from
    being registered. Conflicts among successful providers remain fail-closed
    because the exposed tool namespace would be ambiguous.
    """

    async with _registry_lock:
        await _close_all_clients_locked()
        _registry.clear()
        _server_instructions.clear()
        _server_config_hints.clear()
        _mark_startup_begin(set(servers.keys()))

        all_resolved: dict[str, list[ResolvedTool]] = {}
        connected: dict[str, _ConnectedServerData] = {}
        failures: dict[str, ProviderStartupFailure] = {}

        if tool_lists is not None:
            for server_name in servers:
                if server_name not in tool_lists:
                    failures[server_name] = ProviderStartupFailure(
                        server_name=server_name,
                        phase="initialize",
                        reason="pre_enumerated_tool_list_missing",
                    )
                    continue
                connected[server_name] = _ConnectedServerData(
                    server_name=server_name,
                    raw_tools=tool_lists[server_name],
                )
        else:
            startup_results = await asyncio.gather(
                *[
                    _connect_server(server_name, server_config)
                    for server_name, server_config in servers.items()
                ]
            )
            server_names_list = list(servers.keys())
            for idx, startup_result in enumerate(startup_results):
                server_name = server_names_list[idx]
                if startup_result.is_err:
                    failures[server_name] = _startup_failure_from_error(
                        server_name, startup_result.error
                    )
                    continue
                assert startup_result.value is not None
                startup = startup_result.value
                connected[startup.server_name] = startup

        for server_name, startup in connected.items():
            server_config = servers[server_name]
            resolved = resolve_tools(server_name, server_config, startup.raw_tools)
            all_resolved[server_name] = resolved

        conflicts = detect_conflicts(all_resolved)
        if conflicts:
            await _close_client_handles(
                [
                    startup.client_handle
                    for startup in connected.values()
                    if startup.client_handle is not None
                ]
            )
            _registry.clear()
            conflict_desc = "; ".join(
                f"{c.tool_name} in [{', '.join(c.servers)}]" for c in conflicts
            )
            _mark_startup_finished(successful=set(), failures=failures)
            return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

        for server_name, startup in connected.items():
            server_config = servers[server_name]
            _server_config_hints[server_name] = server_config
            if startup.client_handle is not None:
                _clients[server_name] = startup.client_handle
            if startup.instructions:
                _server_instructions[server_name] = startup.instructions
            _registry.register(server_name, all_resolved[server_name])

        _mark_startup_finished(successful=set(connected.keys()), failures=failures)

    return Result(value=None)


async def disconnect_all() -> Result[None, str]:
    """Disconnect all servers and clear registry and connection tracking."""

    global _startup_complete
    async with _registry_lock:
        await _close_all_clients_locked()
        _registry.clear()
        _server_instructions.clear()
        _server_config_hints.clear()
        _attempted_servers.clear()
        _successful_servers.clear()
        _failed_servers.clear()
        _in_progress_servers.clear()
        _startup_complete = True
        _recovery_locks.clear()
    return Result(value=None)


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
    """Re-enumerate and re-register tools for a single connected server.

    Supported public surface for the shell module boundary.

    Classification: RESOLVED_EXTERNAL_CONTRACT — explicitly supported public API
    for manual re-enumeration of downstream server tools. Listed under Public API
    in docs/DESIGN.md. Consumed by reload.py as _manual_reenumerate_adapter.

    Callers may use this to trigger re-enumeration outside of automatic reconnect
    or reload events. The function validates that the server is connected and
    present in the runtime config before updating the registry.
    """

    async with _registry_lock:
        client = _clients.get(server_name)
        if client is None:
            return Result(
                error=(
                    f"{DOWNSTREAM_UNAVAILABLE}: downstream server '{server_name}' is not connected"
                )
            )

        config = get_runtime_config().value
        if config is None:
            return Result(
                error=f"{DOWNSTREAM_UNAVAILABLE}: gateway runtime config is not loaded"
            )

        server_config = config.servers.get(server_name)
        if server_config is None:
            return Result(
                error=(
                    f"{DOWNSTREAM_UNAVAILABLE}: server '{server_name}' not found in runtime config"
                )
            )

        tools_result = await _enumerate_tools(client.session)
        if tools_result.is_err:
            return Result(
                error=(
                    f"{DOWNSTREAM_UNAVAILABLE}: "
                    f"re-enumeration failed for server '{server_name}': {tools_result.error}"
                )
            )

        assert tools_result.value is not None
        snap = _registry.snapshot()
        resolved = resolve_tools(server_name, server_config, tools_result.value)
        _registry.register(server_name, resolved)
        conflicts = detect_conflicts(_registry.get_all_tools())
        if conflicts:
            _registry.restore(snap)
            conflict_desc = "; ".join(
                f"{c.tool_name} in [{', '.join(c.servers)}]" for c in conflicts
            )
            return Result(error=f"TOOL_CONFLICT: {conflict_desc}")
        return Result(value=resolved)
