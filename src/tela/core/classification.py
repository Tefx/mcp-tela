"""Tool posture classification from available sources.

Determines a tool's posture from explicit overrides, MCP annotations,
or leaves unclassified for the caller to apply a server default.
"""

from __future__ import annotations


from tela.core.contracts import pre, post
from tela.core.models import Posture, ServerConfig




@pre(lambda tool_name, server_config, mcp_annotations=None: isinstance(tool_name, str) and len(tool_name) > 0 and isinstance(server_config, ServerConfig) and (mcp_annotations is None or isinstance(mcp_annotations, dict)))
@post(lambda result: result is None or isinstance(result, Posture))
def classify_tool(
    tool_name: str,
    server_config: ServerConfig,
    mcp_annotations: dict | None = None,
) -> Posture | None:
    """Determine posture for a tool from available sources.

    Priority:
    1. server_config.tool_overrides[tool_name].posture (explicit override)
    2. MCP tool annotations (readOnlyHint, destructiveHint)
    3. None (unclassified -- caller uses server default_posture)

    Examples:
        >>> from tela.core.models import ServerConfig, ToolOverride, Posture
        >>> cfg = ServerConfig(name="srv", command="cmd", tool_overrides={"t": ToolOverride(posture=Posture.READ_ONLY)})
        >>> classify_tool("t", cfg)
        <Posture.READ_ONLY: 'read_only'>
        >>> classify_tool("other", cfg)

    Args:
        tool_name: Name of the tool to classify.
        server_config: Server configuration with potential overrides.
        mcp_annotations: Optional MCP tool annotations dict.

    Returns:
        Classified Posture, or None if unclassified.
    """

    override = server_config.tool_overrides.get(tool_name)
    if override is not None and override.posture is not None:
        return override.posture

    if mcp_annotations is not None:
        return posture_from_annotations(mcp_annotations)

    return None


@pre(lambda annotations: isinstance(annotations, dict))
@post(lambda result: result is None or isinstance(result, Posture))
def posture_from_annotations(annotations: dict) -> Posture | None:
    """Extract posture from MCP tool annotations.

    readOnlyHint=True, destructiveHint=True -> DESTRUCTIVE (most restrictive wins)
    readOnlyHint=True  -> READ_ONLY
    destructiveHint=True -> DESTRUCTIVE
    readOnlyHint=False, destructiveHint=False -> READ_WRITE
    No relevant annotations -> None

    Examples:
        >>> posture_from_annotations({"readOnlyHint": True})
        <Posture.READ_ONLY: 'read_only'>
        >>> posture_from_annotations({"destructiveHint": True})
        <Posture.DESTRUCTIVE: 'destructive'>
        >>> posture_from_annotations({"readOnlyHint": True, "destructiveHint": True})
        <Posture.DESTRUCTIVE: 'destructive'>
        >>> posture_from_annotations({"readOnlyHint": False, "destructiveHint": False})
        <Posture.READ_WRITE: 'read_write'>
        >>> posture_from_annotations({})

    Args:
        annotations: MCP annotations dict.

    Returns:
        Classified Posture, or None if no relevant annotations.
    """

    read_only = annotations.get("readOnlyHint")
    destructive = annotations.get("destructiveHint")

    if read_only is None and destructive is None:
        return None

    if destructive is True:
        return Posture.DESTRUCTIVE

    if read_only is True:
        return Posture.READ_ONLY

    if read_only is False and destructive is False:
        return Posture.READ_WRITE

    if read_only is False:
        return Posture.READ_WRITE

    return None
