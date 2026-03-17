"""Family mapping for downstream tools.

Maps tools to families using the server-is-family convention and explicit
overrides. Does NOT enumerate tools from servers.
"""

from __future__ import annotations


from tela.core.contracts import pre, post
from tela.core.models import ResolvedTool, ServerConfig
from tela.core.classification import classify_tool




@pre(lambda tool_name, server_config: isinstance(tool_name, str) and len(tool_name) > 0)
@post(lambda result: isinstance(result, str) and len(result) > 0)
def resolve_family(
    tool_name: str,
    server_config: ServerConfig,
) -> str:
    """Determine which family a tool belongs to.

    Priority:
    1. server_config.tool_overrides[tool_name].family (per-tool override)
    2. server_config.family (server-level override)
    3. server_config.name (server-is-family default)

    Examples:
        >>> from tela.core.models import ServerConfig, ToolOverride
        >>> cfg = ServerConfig(name="git", command="cmd", tool_overrides={"special": ToolOverride(family="custom")})
        >>> resolve_family("special", cfg)
        'custom'
        >>> resolve_family("git_status", cfg)
        'git'
        >>> cfg2 = ServerConfig(name="srv", command="cmd", family="override_family")
        >>> resolve_family("any_tool", cfg2)
        'override_family'

    Args:
        tool_name: Name of the tool.
        server_config: Server configuration with potential family overrides.

    Returns:
        Family name string.
    """

    override = server_config.tool_overrides.get(tool_name)
    if override is not None and override.family is not None:
        return override.family

    if server_config.family is not None:
        return server_config.family

    return server_config.name


@pre(lambda server_name, server_config, tool_list: isinstance(server_name, str) and isinstance(tool_list, list))
@post(lambda result: isinstance(result, list))
def resolve_tools(
    server_name: str,
    server_config: ServerConfig,
    tool_list: list[dict],
) -> list[ResolvedTool]:
    """Map a server's raw tool list to ResolvedTools with family and posture.

    Each tool in tool_list is a dict with at minimum 'name' and optionally
    'inputSchema' and 'annotations'.

    ResolvedTool.posture may be None (unclassified) when neither tool_overrides
    nor MCP annotations provide a posture.

    Examples:
        >>> from tela.core.models import ServerConfig
        >>> cfg = ServerConfig(name="fs", command="cmd")
        >>> tools = resolve_tools("fs", cfg, [{"name": "read_file", "inputSchema": {"type": "object"}}])
        >>> len(tools)
        1
        >>> tools[0].name
        'read_file'
        >>> tools[0].family
        'fs'
        >>> tools[0].server_name
        'fs'

    Args:
        server_name: Name of the server.
        server_config: Server configuration.
        tool_list: Raw tool dicts from downstream enumeration.

    Returns:
        List of ResolvedTools.
    """

    resolved = []
    for raw_tool in tool_list:
        name = raw_tool["name"]
        family = resolve_family(name, server_config)
        annotations = raw_tool.get("annotations")
        posture = classify_tool(name, server_config, annotations)
        schema = raw_tool.get("inputSchema", {})

        resolved.append(
            ResolvedTool(
                name=name,
                server_name=server_name,
                family=family,
                posture=posture,
                schema_=schema,
            )
        )

    return resolved
