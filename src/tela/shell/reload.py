"""Hot reload orchestration.

Implements re-enumeration, conflict checking, upstream notification
callbacks, and TOOL_CONFLICT audit warning emission.

No-drop-connection invariant: active upstream connections are never dropped
during hot reload. On conflict, the previous tool list is preserved.
"""

from __future__ import annotations

import hashlib

from typing import Awaitable, Callable

from tela.core.conflict import detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import (
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    ServerConfig,
    TelaConfig,
)
from tela.shell.audit import audit_write, build_audit_entry
from tela.shell.config_loader import Result
from tela.shell.gateway import get_runtime
from tela.shell.downstream import (
    _registry_lock,
    connect_all,
    disconnect_all,
    get_registry,
    re_enumerate,
)


# Callback types for upstream notification
NotifyCallback = Callable[[str], Awaitable[None]]  # tools_digest -> None

_notify_callback: NotifyCallback | None = None


def set_notify_callback(callback: NotifyCallback | None) -> Result[None, str]:
    """Set the upstream notification callback for tools/list_changed."""
    global _notify_callback
    _notify_callback = callback
    return Result(value=None)


async def on_tools_changed(
    server_name: str,
    server_config: ServerConfig,
    new_tool_list: list[dict],
) -> Result[None, str]:
    """Handle a downstream server's tools/list_changed notification.

    1. Re-enumerate the server's tool list
    2. Re-assign families
    3. Re-run conflict detection against all servers
    4. No conflict: update resolved tool set, notify upstream via callback
    5. Conflict: reject change, keep previous tools, emit TOOL_CONFLICT warning

    Active upstream connections are NOT dropped during this process.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import ServerConfig
        >>> from tela.shell.downstream import connect_all, disconnect_all
        >>> servers = {"fs": ServerConfig(name="fs", command="cmd")}
        >>> asyncio.run(connect_all(servers, tool_lists={"fs": [{"name": "t1", "inputSchema": {}}]}))
        Result(value=None, error=None)
        >>> r = asyncio.run(on_tools_changed("fs", servers["fs"], [{"name": "t1", "inputSchema": {}}, {"name": "t2", "inputSchema": {}}]))
        >>> r.is_ok
        True
        >>> asyncio.run(disconnect_all())
        Result(value=None, error=None)

    Args:
        server_name: Name of the server whose tools changed.
        server_config: Server configuration for family/classification.
        new_tool_list: New raw tool list from the server.

    Returns:
        Result[None, str] on success, or error string if conflict detected.
    """
    async with _registry_lock:
        registry = get_registry()
        # Snapshot FULL registry state before tentative register for atomic rollback.
        # Previous approach only saved one server's tools, corrupting the flat
        # _tool_to_server map for other servers on conflict rollback (B4).
        snap = registry.snapshot()

        # Re-enumerate
        resolved = resolve_tools(server_name, server_config, new_tool_list)

        # Temporarily update registry
        registry.register(server_name, resolved)

        # Check conflicts
        conflicts = detect_conflicts(registry.get_all_tools())
        if conflicts:
            # Rollback entire registry to pre-change state
            registry.restore(snap)

            conflict_desc = "; ".join(
                f"{c.tool_name} in [{', '.join(c.servers)}]" for c in conflicts
            )

            # Emit TOOL_CONFLICT audit warning
            warning_entry_result = build_audit_entry(
                level=AuditLevel.L1,
                connection=ConnectionContext(
                    connection_id="system",
                    profile_name="system",
                    connected_at="",
                ),
                tool_name=conflicts[0].tool_name,
                server_name=server_name,
                result=EnforcementResult(
                    verdict=EnforcementVerdict.DENY,
                    denied_by="tool_conflict",
                    error_code="TOOL_CONFLICT",
                    error_message=conflict_desc,
                ),
            )
            if warning_entry_result.is_err:
                return Result(error=warning_entry_result.error)
            assert warning_entry_result.value is not None
            _ = await audit_write(warning_entry_result.value)

            return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

        # Success: notify upstream if callback set
        if _notify_callback is not None:
            tool_names = sorted(
                t.name for ts in registry.get_all_tools().values() for t in ts
            )
            raw = ":".join(tool_names).encode()
            digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
            await _notify_callback(digest)

    return Result(value=None)


async def on_server_reconnect(
    server_name: str,
    server_config: ServerConfig,
    tool_list: list[dict],
) -> Result[None, str]:
    """Handle a downstream server reconnecting after disconnect.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import ServerConfig
        >>> from tela.shell.downstream import connect_all, disconnect_all
        >>> servers = {"fs": ServerConfig(name="fs", command="cmd")}
        >>> asyncio.run(connect_all(servers, tool_lists={"fs": [{"name": "t1", "inputSchema": {}}]}))
        Result(value=None, error=None)
        >>> r = asyncio.run(on_server_reconnect("fs", servers["fs"], [{"name": "t1", "inputSchema": {}}]))
        >>> r.is_ok
        True
        >>> asyncio.run(disconnect_all())
        Result(value=None, error=None)

    Args:
        server_name: Name of the reconnecting server.
        server_config: Server configuration.
        tool_list: New tool list from the reconnected server.

    Returns:
        Result[None, str].
    """
    _ = await re_enumerate(server_name)
    return await on_tools_changed(server_name, server_config, tool_list)


# Production callback target for runtime config-file watcher wiring.
async def on_config_changed(new_config: TelaConfig) -> Result[None, str]:
    """Handle configuration file change.

    Handle configuration file change. Updates runtime config.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import TelaConfig
        >>> r = asyncio.run(on_config_changed(TelaConfig()))
        >>> r.is_ok
        True

    Args:
        new_config: New TelaConfig.

    Returns:
        Result[None, str] once implemented.
    """
    runtime = get_runtime()
    old_config = runtime.config

    # Update runtime config
    runtime.config = new_config

    # Detect server changes and re-connect changed/new servers
    if old_config is not None:
        old_servers = set(old_config.servers.keys())
        new_servers = set(new_config.servers.keys())

        removed = old_servers - new_servers
        added = new_servers - old_servers
        # Servers present in both configs but with changed settings
        changed = {
            name
            for name in old_servers & new_servers
            if old_config.servers[name] != new_config.servers[name]
        }

        servers_to_reconnect = added | changed

        if removed or servers_to_reconnect:
            # Disconnect all and reconnect with new config.
            # Per-server disconnect is not yet supported; full reconnect
            # is the safe path that preserves conflict-detection invariants.
            await disconnect_all()
            connect_result = await connect_all(new_config.servers)
            if connect_result.is_err:
                return Result(error=connect_result.error)

    return Result(value=None)
