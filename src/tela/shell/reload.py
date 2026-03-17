"""Hot reload orchestration contracts.

Defines acceptance-only interfaces for handling downstream tool list changes,
server reconnection, and configuration reloads. Runtime invariant: active
upstream connections are never dropped during hot reload.

No-drop-connection invariant: during a reload cycle, existing upstream
connections continue to see their currently-bound tool list until the
reload completes successfully. If a conflict is detected, the previous
tool list is preserved and no upstream notification is sent.
"""

from __future__ import annotations

from tela.core.models import TelaConfig
from tela.shell.config_loader import Result


# @invar:allow dead_export: reload wiring is connected in reload.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def on_tools_changed(server_name: str) -> Result[None, str]:
    """Handle a downstream server's tools/list_changed notification.

    1. Re-enumerate the server's tool list
    2. Re-assign families
    3. Re-run conflict detection against all servers
    4. No conflict: update resolved tool set, notify upstream clients
    5. Conflict: reject change, keep previous tools, log TOOL_CONFLICT warning

    Active upstream connections are NOT dropped during this process.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(on_tools_changed("fs"))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: on_tools_changed pending

    Args:
        server_name: Name of the server whose tools changed.

    Returns:
        Result[None, str] once implemented.
    """
    raise NotImplementedError("Contract stub: on_tools_changed pending")


# @invar:allow dead_export: reload wiring is connected in reload.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def on_server_reconnect(server_name: str) -> Result[None, str]:
    """Handle a downstream server reconnecting after disconnect.

    Re-enumerates tools and updates the registry. Active upstream
    connections are NOT dropped during this process.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(on_server_reconnect("fs"))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: on_server_reconnect pending

    Args:
        server_name: Name of the reconnecting server.

    Returns:
        Result[None, str] once implemented.
    """
    raise NotImplementedError("Contract stub: on_server_reconnect pending")


# @invar:allow dead_export: reload wiring is connected in reload.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def on_config_changed(new_config: TelaConfig) -> Result[None, str]:
    """Handle configuration file change.

    Re-loads configuration and updates runtime state. Active upstream
    connections are NOT dropped during this process.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import TelaConfig
        >>> asyncio.run(on_config_changed(TelaConfig()))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: on_config_changed pending

    Args:
        new_config: New TelaConfig from the changed config file.

    Returns:
        Result[None, str] once implemented.
    """
    raise NotImplementedError("Contract stub: on_config_changed pending")
