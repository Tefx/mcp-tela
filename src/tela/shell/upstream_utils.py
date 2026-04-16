"""Upstream utility functions: tool filtering, _meta stripping, enforcement bridging.

Pure/synchronous helpers extracted from upstream.py to keep each module
under the 300-line DX threshold. These functions have no I/O dependencies
and operate on core models only.
"""

from __future__ import annotations

from tela.core.enforcement import enforce
from tela.core.models import (
    EnforcementResult,
    EnforcementVerdict,
    Posture,
    ProfileConfig,
    ResolvedTool,
)
from tela.shell.result import Result


# --- tools/list filtering ---


def filter_tools_for_profile(
    all_tools: dict[str, list[ResolvedTool]],
    profile: ProfileConfig,
    server_default_postures: dict[str, Posture],
) -> Result[list[ResolvedTool], str]:
    """Filter resolved tools to those permitted by a profile.

    A tool is included if and only if:
    1. Its family exists in the profile's capabilities map
    2. Its posture (classified or default) <= the profile's ceiling
    3. It is not explicitly denied by a profile tool_overrides entry

    Examples:
        >>> from tela.core.models import Posture, ProfileConfig, ResolvedTool
        >>> tools = {"fs": [ResolvedTool(name="read_file", server_name="fs", family="fs", posture=Posture.READ_ONLY)]}
        >>> profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
        >>> result = filter_tools_for_profile(tools, profile, {"fs": Posture.NONE})
        >>> len(result.value)
        1
        >>> result.value[0].name
        'read_file'

    Args:
        all_tools: Server name to resolved tool list mapping.
        profile: Bound profile configuration.
        server_default_postures: Server name to default posture mapping.

    Returns:
        Result containing tools permitted by the profile.
    """

    allowed_token = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
    permitted: list[ResolvedTool] = []

    for server_name, tools in all_tools.items():
        default_posture = server_default_postures.get(server_name, Posture.NONE)
        for tool in tools:
            routing_name = tool.raw_name or tool.name
            result = enforce(
                routing_name,
                tool,
                profile,
                allowed_token,
                default_posture,
            )
            if result.verdict == EnforcementVerdict.ALLOW:
                permitted.append(tool)

    return Result(value=permitted)


# --- tools/call with _meta stripping ---


def strip_meta(arguments: dict) -> Result[tuple[dict, dict | None], str]:
    """Strip _meta from tool call arguments.

    Returns (stripped_arguments, held_meta). held_meta is None if _meta
    was not present.

    Examples:
        >>> strip_meta({"path": "/tmp", "_meta": {"trace_id": "t1"}}).value
        ({'path': '/tmp'}, {'trace_id': 't1'})
        >>> strip_meta({"path": "/tmp"}).value
        ({'path': '/tmp'}, None)

    Args:
        arguments: Raw tool call arguments.

    Returns:
        Result containing tuple of (stripped arguments, held _meta or None).
    """

    meta = arguments.get("_meta")
    stripped = {k: v for k, v in arguments.items() if k != "_meta"}
    return Result(value=(stripped, meta))


def enforce_tool_call(
    tool_name: str,
    tool: ResolvedTool,
    profile: ProfileConfig,
    default_posture: Posture,
) -> Result[EnforcementResult, str]:
    """Run enforcement chain for a tool call in open mode (no token).

    Open mode uses a pre-allowed token result.

    Examples:
        >>> from tela.core.models import ResolvedTool, ProfileConfig, Posture
        >>> tool = ResolvedTool(name="read_file", server_name="fs", family="fs", posture=Posture.READ_ONLY)
        >>> profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
        >>> enforce_tool_call("read_file", tool, profile, Posture.NONE).value.verdict
        <EnforcementVerdict.ALLOW: 'allow'>

    Args:
        tool_name: Name of the tool.
        tool: Resolved tool metadata.
        profile: Bound profile configuration.
        default_posture: Server's default posture.

    Returns:
        Result containing enforcement verdict.
    """

    allowed_token = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
    return Result(
        value=enforce(tool_name, tool, profile, allowed_token, default_posture)
    )
