"""Authoritative runtime instruction text for tela-owned surfaces."""

from __future__ import annotations

from tela.core.models import ResolvedTool, ServerConfig
from tela.shell.config_loader import Result


def build_manifest_header(
    servers: dict[str, "ServerConfig"],
    connected_names: set[str],
    tools_by_server: dict[str, list["ResolvedTool"]],
) -> str:
    """Build provider manifest header for instructions.

    Format: "Connected at startup: server_a (N tools), server_b (M tools)"

    Only connected servers are listed. Disconnected/failed servers are omitted
    from the manifest header (they are discoverable via tela_list_providers).

    Examples:
        >>> from tela.core.models import ServerConfig, ResolvedTool
        >>> servers = {"fs": ServerConfig(name="fs", command="cmd")}
        >>> connected = {"fs"}
        >>> tools = {"fs": [ResolvedTool(name="read_file", server_name="fs", family="fs")]}
        >>> header = build_manifest_header(servers, connected, tools)
        >>> "Connected at startup:" in header
        True
        >>> "fs (1 tools)" in header
        True

    Args:
        servers: Server name to config mapping from TelaConfig.
        connected_names: Set of server names with active connections.
        tools_by_server: Registry tool map (exposed names).

    Returns:
        Manifest header string.
    """
    # @invar:allow shell_result: pure data transformation, no I/O - per pm.p1 contract
    parts = []
    for server_name in sorted(servers.keys()):
        if server_name in connected_names:
            tool_count = len(tools_by_server.get(server_name, []))
            parts.append(f"{server_name} ({tool_count} tools)")
    if not parts:
        return "Connected at startup: (none)"
    return "Connected at startup: " + ", ".join(parts)


def get_gateway_surface_instructions(
    manifest_header: str | None = None,
) -> Result[str, str]:
    """Return gateway-authoritative runtime instructions text.

    When manifest_header is provided, prepend it to the surface text.
    Updates built-in MCP tools line to include tela_list_providers.
    """
    base_instructions = """# tela gateway surface contract

This document defines the authoritative instruction surface presented to
upstream MCP clients connecting to the tela gateway.

## Gateway capabilities

- Built-in MCP tools: `tela_list_providers`.
- Operator-only surfaces (not MCP built-ins): `tela profiles`, `tela status`, `tela connections`, `tela audit`, `GET /status`, `GET /health`, `POST /connect`, `POST /disconnect`, `POST /mcp`.
- Gateway does not proxy or forward to operator surfaces.

## Server instructions

Instructions for individual servers are provided below, where available.

"""
    if manifest_header is None:
        return Result(value=base_instructions)
    return Result(value=f"{manifest_header}\n\n{base_instructions}")


def compose_gateway_and_downstream(
    gateway_instructions: str,
    downstream_instructions: str | None,
) -> Result[str, str]:
    """Compose gateway text followed by downstream instruction sections."""

    if gateway_instructions.strip() == "":
        return Result(
            error="INSTRUCTIONS_COMPOSE_ERROR: gateway instructions are empty"
        )
    if downstream_instructions is None:
        return Result(value=gateway_instructions)
    return Result(value=f"{gateway_instructions}\n\n{downstream_instructions}")
