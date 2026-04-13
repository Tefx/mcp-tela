"""Downstream server management and runtime coordination boundaries.

This module owns the downstream public authority surface: connect/disconnect
lifecycle, registry state, and query APIs. Recovery and call-path logic is
extracted to ``tela.shell._downstream_recovery``; this module re-exports
``call_tool`` and internal hooks for monkeypatching compatibility.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _ConnectedServerData:
    """Temporary successful downstream startup result before registry publish."""

    server_name: str
    raw_tools: list[dict]
    client_handle: _ClientHandle | None = None
    instructions: str | None = None


async def _close_handle_best_effort(handle: _ClientHandle) -> None:
    """Close one temporary handle without surfacing cleanup failures."""

    try:
        await handle.stack.aclose()
    except Exception:
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
                f"{DOWNSTREAM_CONNECT_FAILED}: "
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
        resolved = resolve_tools(server_name, server_config, tools_result.value)
        _registry.register(server_name, resolved)
        return Result(value=resolved)
