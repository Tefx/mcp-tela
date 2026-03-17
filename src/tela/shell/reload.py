"""Hot reload orchestration.

Implements re-enumeration, conflict checking, and registry updates for
tools/list_changed handling, server reconnection, and config changes.

No-drop-connection invariant: active upstream connections are never dropped
during hot reload. On conflict, the previous tool list is preserved.
"""

from __future__ import annotations

from tela.core.conflict import detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import ServerConfig, TelaConfig
from tela.shell.config_loader import Result
from tela.shell.downstream import get_all_tools, get_registry


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
    4. No conflict: update resolved tool set
    5. Conflict: reject change, keep previous tools

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

    # Save previous state for rollback
    previous_tools = registry.get_all_tools().get(server_name, [])

    # Re-enumerate
    resolved = resolve_tools(server_name, server_config, new_tool_list)

    # Temporarily update registry to check for conflicts
    registry.register(server_name, resolved)

    # Check conflicts across all servers
    conflicts = detect_conflicts(registry.get_all_tools())
    if conflicts:
        # Rollback: restore previous tools
        if previous_tools:
            registry.register(server_name, previous_tools)
        else:
            registry.unregister(server_name)

        conflict_desc = "; ".join(
            f"{c.tool_name} in [{', '.join(c.servers)}]" for c in conflicts
        )
        return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

    # No conflict: accept the update
    return Result(value=None)


# @invar:allow dead_export: reload wiring is connected in reload.runtime step.
async def on_server_reconnect(
    server_name: str,
    server_config: ServerConfig,
    tool_list: list[dict],
) -> Result[None, str]:
    """Handle a downstream server reconnecting after disconnect.

    Re-enumerates tools and updates the registry. Delegates to
    on_tools_changed for the actual re-enumeration and conflict checking.

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

    Contract stub: actual config reload deferred to full integration.

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
