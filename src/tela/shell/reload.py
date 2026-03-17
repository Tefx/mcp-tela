"""Hot reload orchestration.

Implements re-enumeration, conflict checking, upstream notification
callbacks, and TOOL_CONFLICT audit warning emission.

No-drop-connection invariant: active upstream connections are never dropped
during hot reload. On conflict, the previous tool list is preserved.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from tela.core.conflict import detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import (
    AuditEntry,
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    ServerConfig,
    TelaConfig,
)
from tela.shell.audit import audit_write, build_audit_entry
from tela.shell.config_loader import Result
from tela.shell.downstream import get_all_tools, get_registry


# Callback types for upstream notification
NotifyCallback = Callable[[str], Awaitable[None]]  # tools_digest -> None

_notify_callback: NotifyCallback | None = None


# @invar:allow dead_export: reload wiring is connected in reload.runtime step.
# @invar:allow shell_result: sets callback, not a failable I/O boundary.
def set_notify_callback(callback: NotifyCallback | None) -> None:
    """Set the upstream notification callback for tools/list_changed."""
    global _notify_callback
    _notify_callback = callback


# @invar:allow dead_export: reload wiring is connected in reload.runtime step.
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
        warning_entry = build_audit_entry(
            level=AuditLevel.L1,
            connection=ConnectionContext(
                connection_id="system", profile_name="system",
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
        await audit_write(warning_entry)

        return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

    # Success: notify upstream if callback set
    if _notify_callback is not None:
        tool_names = sorted(t.name for ts in get_all_tools().values() for t in ts)
        digest = ":".join(tool_names)
        await _notify_callback(digest)

    return Result(value=None)


# @invar:allow dead_export: reload wiring is connected in reload.runtime step.
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
    return await on_tools_changed(server_name, server_config, tool_list)


# @invar:allow dead_export: reload wiring is connected in reload.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def on_config_changed(new_config: TelaConfig) -> Result[None, str]:
    """Handle configuration file change.

    Contract stub: actual config reload deferred.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import TelaConfig
        >>> asyncio.run(on_config_changed(TelaConfig()))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: on_config_changed pending

    Args:
        new_config: New TelaConfig.

    Returns:
        Result[None, str] once implemented.
    """
    raise NotImplementedError("Contract stub: on_config_changed pending")
