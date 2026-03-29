"""Built-in tools owned by tela gateway (not downstream servers)."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tela.core.models import ProviderInfo

BUILTIN_TOOLS: list[dict] = [
    {
        "name": "tela_list_providers",
        "description": "List connected downstream providers with their status and tool counts.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

BUILTIN_TOOL_NAMES: set[str] = {t["name"] for t in BUILTIN_TOOLS}


# @invar:allow shell_result: builtin tools follow FastMCP @tool pattern (raise on error, not Result wrap)
# @shell_orchestration: reads from DownstreamRegistry at runtime
async def handle_list_providers(
    # No parameters needed — reads live from DownstreamRegistry
) -> list["ProviderInfo"]:
    """Return per-provider summary from live DownstreamRegistry.

    Reads connected servers, their tool counts (post-enforcement-filter),
    and connection status. Includes failed servers with status "failed".

    Examples:
        >>> import asyncio
        >>> result = asyncio.run(handle_list_providers())
        >>> isinstance(result, list)
        True

    Returns:
        List of ProviderInfo dicts, one per configured server.
    """
    raise NotImplementedError
