"""Built-in tools owned by tela gateway (not downstream servers)."""

from __future__ import annotations
import re
from typing import TYPE_CHECKING

from tela.core.errors import GATEWAY_NOT_STARTED
from tela.core.models import Posture, ProfileConfig, ProfileInfo, ProviderInfo
from tela.shell.downstream import (
    get_all_tools,
    get_attempted_servers,
    get_successful_servers,
)
from tela.shell.gateway_runtime import get_runtime_config
from tela.shell.upstream_utils import filter_tools_for_profile

if TYPE_CHECKING:
    from tela.core.models import ConnectionContext

_SHARED_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_CANONICAL_PROFILE_KEYS = frozenset({"profile_id", "capabilities", "default"})
_CANONICAL_POSTURES = frozenset(posture.value for posture in Posture)


def _raise_if_invalid_shared_tool_name(tool_name: str) -> None:
    """Reject non-canonical shared MCP tool names on emitted surfaces."""

    if _SHARED_TOOL_NAME_PATTERN.fullmatch(tool_name) is None:
        raise RuntimeError(
            "INVALID_TOOL_NAME: shared MCP tool names must be snake_case; "
            f"got '{tool_name}'"
        )


# @invar:allow shell_result: pure fail-closed validator used only by the builtin profile-list shell boundary.
# @shell_complexity: payload validation checks canonical key/type/default invariants before emitting shared MCP output.
def _validate_profile_list_payload(
    profiles: list["ProfileInfo"],
) -> list["ProfileInfo"]:
    """Fail closed unless the emitted profile list is canonical."""

    default_profiles: list[str] = []
    for entry in profiles:
        entry_keys = set(entry.keys())
        if entry_keys != _CANONICAL_PROFILE_KEYS:
            raise RuntimeError(
                "INVALID_PROFILE_LIST_PAYLOAD: expected keys "
                f"{sorted(_CANONICAL_PROFILE_KEYS)}, got {sorted(entry_keys)}"
            )

        profile_id = entry["profile_id"]
        capabilities = entry["capabilities"]
        default = entry["default"]

        if not isinstance(profile_id, str):
            raise RuntimeError(
                "INVALID_PROFILE_LIST_PAYLOAD: profile_id must be a string"
            )
        if not isinstance(default, bool):
            raise RuntimeError(
                "INVALID_PROFILE_LIST_PAYLOAD: default must be a boolean"
            )
        if not isinstance(capabilities, dict):
            raise RuntimeError(
                "INVALID_PROFILE_LIST_PAYLOAD: capabilities must be an object"
            )

        invalid_capabilities = sorted(
            f"{family}={posture}"
            for family, posture in capabilities.items()
            if not isinstance(family, str)
            or not isinstance(posture, str)
            or posture not in _CANONICAL_POSTURES
        )
        if invalid_capabilities:
            raise RuntimeError(
                "INVALID_PROFILE_LIST_PAYLOAD: invalid capabilities: "
                + ", ".join(invalid_capabilities)
            )

        if default:
            default_profiles.append(profile_id)

    if len(default_profiles) > 1:
        raise RuntimeError(
            "INVALID_DEFAULT_PROFILE_STATE: multiple profiles marked default=true: "
            + ", ".join(sorted(default_profiles))
        )

    return profiles


BUILTIN_TOOLS: list[dict] = [
    {
        "name": "tela_list_providers",
        "description": "List connected downstream providers with their status and tool counts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "tela_list_profiles",
        "description": "List configured profiles with their capabilities and default status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
]

BUILTIN_TOOL_NAMES: set[str] = {t["name"] for t in BUILTIN_TOOLS}

for _tool in BUILTIN_TOOLS:
    _raise_if_invalid_shared_tool_name(_tool["name"])


# @invar:allow shell_result: builtin tools follow FastMCP @tool pattern (raise on error, not Result wrap)
# @shell_complexity: branching is unavoidable for per-server status determination and profile enforcement filtering
async def handle_list_providers(
    connection: "ConnectionContext | None" = None,
) -> list["ProviderInfo"]:
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
        runtime_error = config_result.error or (
            f"{GATEWAY_NOT_STARTED}: gateway has not been started"
        )
        raise RuntimeError(
            f"handle_list_providers requires a valid runtime config: {runtime_error!r}"
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

    profile_id = (
        connection.profile_id
        if connection is not None
        else config.resolved_default_profile
    )
    profile: ProfileConfig | None = None
    if profile_id and profile_id in config.profiles:
        profile = config.profiles[profile_id]

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
                for tool_name in tool_names:
                    _raise_if_invalid_shared_tool_name(tool_name)
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


# @invar:allow shell_result: builtin tools follow FastMCP @tool pattern (raise on error, not Result wrap)
# @shell_complexity: gateway runtime config access is indirect I/O; function is a Shell boundary adapter
def handle_list_profiles() -> list["ProfileInfo"]:
    """Return per-profile summary from live runtime config.

    Reads configured profiles and emits canonical payload:
    ``profile_id``, ``capabilities``, ``default`` only.
    No retired payload keys are ever emitted.

    Raises:
        RuntimeError: if runtime config is not available (gateway not started).

    Returns:
    List of ProfileInfo dicts, one per configured profile.
    """
    config_result = get_runtime_config()
    if config_result.is_err or config_result.value is None:
        runtime_error = config_result.error or (
            f"{GATEWAY_NOT_STARTED}: gateway has not been started"
        )
        raise RuntimeError(
            f"handle_list_profiles requires a valid runtime config: {runtime_error!r}"
        )
    config = config_result.value

    profiles: list[ProfileInfo] = []
    for name, p in config.profiles.items():
        profiles.append(
            ProfileInfo(
                profile_id=name,
                capabilities={k: v.value for k, v in p.capabilities.items()},
                default=p.default,
            )
        )

    return _validate_profile_list_payload(profiles)
