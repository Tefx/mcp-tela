"""Family mapping for downstream tools.

Maps tools to families using the server-is-family convention and explicit
overrides. Does NOT enumerate tools from servers.
"""

from __future__ import annotations


import re

from tela.core.contracts import pre, post
from tela.core.errors import NESTED_TELA_PREFIX_REQUIRED
from tela.core.models import ResolvedTool, ServerConfig
from tela.core.classification import classify_tool


_CHILD_TELA_BUILTINS = frozenset({"tela_list_providers", "tela_list_profiles"})


@pre(
    lambda tool_name, server_config: (
        isinstance(tool_name, str)
        and len(tool_name) > 0
        and isinstance(server_config, ServerConfig)
    )
)
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


@pre(
    lambda description, raw_names_to_exposed: (
        isinstance(description, str)
        and isinstance(raw_names_to_exposed, dict)
        and all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in raw_names_to_exposed.items()
        )
    )
)
@post(lambda result: isinstance(result, str))
def rewrite_tool_description(
    description: str,
    raw_names_to_exposed: dict[str, str],
) -> str:
    """Replace backtick-quoted raw tool names with prefixed exposed names.

    Only replaces exact matches within backtick pairs. Does not touch
    names that are not in the raw_names_to_exposed mapping (i.e., does
    not rewrite cross-server references).

    Examples:
        >>> rewrite_tool_description("Use `read_file` to read.", {"read_file": "fs_read_file"})
        'Use `fs_read_file` to read.'
        >>> rewrite_tool_description("Use `read_file` and `write_file`.", {"read_file": "fs_read_file"})
        'Use `fs_read_file` and `write_file`.'
        >>> rewrite_tool_description("No backticks here.", {"read_file": "fs_read_file"})
        'No backticks here.'

    Args:
        description: Tool description text.
        raw_names_to_exposed: Mapping from raw downstream names to exposed prefixed names.

    Returns:
        Description with backtick-quoted names replaced.
    """

    def _replacer(match: re.Match) -> str:
        name = match.group(1)
        if name in raw_names_to_exposed:
            return f"`{raw_names_to_exposed[name]}`"
        return match.group(0)

    return re.sub(r"`([^`]+)`", _replacer, description)


@pre(lambda server_config: isinstance(server_config, ServerConfig))
@post(
    lambda result: (
        isinstance(result, frozenset)
        and all(isinstance(name, str) and len(name) > 0 for name in result)
    )
)
def effective_exclude_tools(server_config: ServerConfig) -> frozenset[str]:
    """Return raw downstream tool names filtered before resolution.

    Examples:
        >>> from tela.core.models import ServerConfig
        >>> effective_exclude_tools(ServerConfig(name="child", command="cmd", exclude_tools=["raw"]))
        frozenset({'raw'})
        >>> sorted(effective_exclude_tools(ServerConfig(name="child", command="cmd", tool_prefix="host_", nested_gateway=True)))
        ['tela_list_profiles', 'tela_list_providers']
    """

    excludes = set(server_config.exclude_tools)
    if server_config.nested_gateway:
        excludes.update(_CHILD_TELA_BUILTINS)
    return frozenset(excludes)


@pre(
    lambda server_name, server_config, tool_list: (
        isinstance(server_name, str)
        and len(server_name) > 0
        and isinstance(server_config, ServerConfig)
        and isinstance(tool_list, list)
    )
)
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

    # NOTE: Contract semantics only. Any configured ``ServerConfig.tool_prefix``
    # is applied here during registration/resolution, not later at tools/call
    # routing time.
    # NOTE: ``ResolvedTool.raw_name`` is the downstream-advertised inventory
    # name. ``ResolvedTool.name`` is the final exposed upstream name.
    # NOTE: ``server_config.tool_overrides`` continue to match raw downstream
    # names even when the exposed name is prefixed.
    # NOTE: Producing ``tela.`` through prefix + downstream name composition is
    # reserved-prefix input and must be rejected by the resolution path.
    """

    # Core owns raw-name filtering for exclude_tools and nested_gateway before
    # prefixing/classification; Shell only wires enumeration/lifecycle paths.
    resolved = []
    effective_excludes = effective_exclude_tools(server_config)
    for raw_tool in tool_list:
        raw_name = raw_tool["name"]
        if raw_name in effective_excludes:
            continue

        tool_prefix = server_config.tool_prefix
        if tool_prefix is not None and (
            tool_prefix.startswith("tela.") or tool_prefix.startswith("tela_")
        ):
            raise ValueError(
                f"ServerConfig.tool_prefix '{tool_prefix}' uses reserved namespace"
            )

        if raw_name in _CHILD_TELA_BUILTINS and not tool_prefix:
            raise ValueError(
                f"{NESTED_TELA_PREFIX_REQUIRED}: downstream child Tela built-in "
                f"'{raw_name}' requires a non-empty tool_prefix unless filtered"
            )

        exposed_name = raw_name if tool_prefix is None else f"{tool_prefix}{raw_name}"
        if exposed_name.startswith("tela.") or exposed_name.startswith("tela_"):
            raise ValueError(
                f"Resolved tool name '{exposed_name}' enters reserved namespace"
            )

        family = resolve_family(raw_name, server_config)
        annotations = raw_tool.get("annotations")
        posture = classify_tool(raw_name, server_config, annotations)
        schema = raw_tool.get("inputSchema", {})
        description = raw_tool.get("description", "")
        title = raw_tool.get("title")
        output_schema = raw_tool.get("outputSchema")

        resolved.append(
            ResolvedTool(
                name=exposed_name,
                raw_name=raw_name,
                server_name=server_name,
                family=family,
                posture=posture,
                schema_=schema,
                description=description,
                annotations=annotations,
                title=title,
                output_schema=output_schema,
            )
        )

    # Description rewriting (opt-in per server)
    if server_config.rewrite_descriptions and server_config.tool_prefix is not None:
        raw_to_exposed = {
            rt.raw_name: rt.name for rt in resolved if rt.raw_name is not None
        }
        for i, rt in enumerate(resolved):
            if rt.description:
                new_desc = rewrite_tool_description(rt.description, raw_to_exposed)
                if new_desc != rt.description:
                    resolved[i] = rt.model_copy(update={"description": new_desc})

    return resolved
