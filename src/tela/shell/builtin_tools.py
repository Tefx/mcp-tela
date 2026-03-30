"""Built-in tools owned by tela gateway (not downstream servers)."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tela.core.models import Posture, ProviderInfo, ProfileConfig
from tela.shell.downstream import (
    get_all_tools,
    get_attempted_servers,
    get_successful_servers,
)
from tela.shell.gateway_runtime import get_runtime_config
from tela.shell.upstream_utils import filter_tools_for_profile

if TYPE_CHECKING:
    pass

BUILTIN_TOOLS: list[dict] = [
    {
        "name": "tela_list_providers",
        "description": "List connected downstream providers with their status and tool counts.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

BUILTIN_TOOL_NAMES: set[str] = {t["name"] for t in BUILTIN_TOOLS}


# @invar:allow shell_result: builtin tools follow FastMCP @tool pattern (raise on error, not Result wrap)
# @shell_complexity: branching is unavoidable for per-server status determination and profile enforcement filtering
async def handle_list_providers() -> list["ProviderInfo"]:
    """Return per-provider summary from live DownstreamRegistry.

    Reads connected servers, their tool counts (post-enforcement-filter),
    and connection status. Includes failed servers with status "failed".

    Raises:
        RuntimeError: if runtime config is not available (gateway not started).

    Returns:
        List of ProviderInfo dicts, one per configured server.
    """
    # Get runtime config to find all configured servers
    config_result = get_runtime_config()
    if config_result.is_err or config_result.value is None:
        raise RuntimeError(
            f"handle_list_providers requires a valid runtime config: "
            f"{config_result.error!r}"
        )
    config = config_result.value

    # Get successful servers (those that connected successfully)
    successful_result = get_successful_servers()
    successful = (
        successful_result.value if successful_result.is_ok else set()
    ) or set()

    # Get attempted servers (those that were part of a connection attempt)
    attempted_result = get_attempted_servers()
    attempted = (attempted_result.value if attempted_result.is_ok else set()) or set()

    # Get all tools from registry
    all_tools_result = get_all_tools()
    all_tools: dict[str, list]
    if all_tools_result.is_err:
        all_tools = {}
    else:
        all_tools = all_tools_result.value or {}

    # Build server default postures map
    server_default_postures: dict[str, Posture] = {}
    for sname, scfg in config.servers.items():
        server_default_postures[sname] = scfg.default_posture

    # Get active profile (use resolved_default_profile from config)
    profile_name = config.resolved_default_profile
    profile: ProfileConfig | None = None
    if profile_name and profile_name in config.profiles:
        profile = config.profiles[profile_name]

    providers: list[ProviderInfo] = []

    for server_name, server_config in config.servers.items():
        is_successful = server_name in successful
        is_attempted = server_name in attempted
        is_registered = server_name in all_tools

        if is_successful:
            status = "connected"
        elif is_attempted:
            # Server was attempted but did not connect successfully
            status = "failed"
        else:
            # Server is configured but was never attempted
            status = "disconnected"

        tool_prefix = server_config.tool_prefix

        # Get filtered tool names based on profile enforcement
        if is_registered and profile is not None:
            server_tools = all_tools.get(server_name, [])
            # Build a single-server dict for filter_tools_for_profile
            single_server_tools = {server_name: server_tools}
            filtered_result = filter_tools_for_profile(
                single_server_tools, profile, server_default_postures
            )
            if filtered_result.is_ok and filtered_result.value:
                # filter_tools_for_profile returns flat list
                filtered_tools = filtered_result.value
                tool_names = [t.name for t in filtered_tools]
                tool_count = len(tool_names)
            else:
                tool_names = []
                tool_count = 0
        else:
            tool_names = []
            tool_count = 0

        providers.append(
            ProviderInfo(
                name=server_name,
                status=status,
                tool_prefix=tool_prefix,
                tool_count=tool_count,
                tool_names=tool_names,
            )
        )

    return providers
