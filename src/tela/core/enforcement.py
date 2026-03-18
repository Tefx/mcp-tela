"""3-step enforcement chain for per-call authorization.

Pure decision logic: receives all inputs and returns an EnforcementResult.
Does NOT perform I/O, validate tokens, or look up profiles.
"""

from __future__ import annotations


from tela.core.contracts import pre, post
from tela.core.models import (
    EnforcementResult,
    EnforcementVerdict,
    Posture,
    ProfileConfig,
    ResolvedTool,
)


_POSTURE_ORDER = {
    Posture.NONE: 0,
    Posture.READ_ONLY: 1,
    Posture.READ_WRITE: 2,
    Posture.DESTRUCTIVE: 3,
}


@pre(lambda a, b: isinstance(a, Posture) and isinstance(b, Posture))
@post(lambda result: isinstance(result, bool))
def posture_le(a: Posture, b: Posture) -> bool:
    """Compare postures: is a <= b in the ordering?

    Ordering: NONE < READ_ONLY < READ_WRITE < DESTRUCTIVE.

    Examples:
        >>> posture_le(Posture.READ_ONLY, Posture.READ_WRITE)
        True
        >>> posture_le(Posture.DESTRUCTIVE, Posture.READ_ONLY)
        False
        >>> posture_le(Posture.NONE, Posture.NONE)
        True

    Args:
        a: First posture.
        b: Second posture.

    Returns:
        True if a <= b.
    """

    return _POSTURE_ORDER[a] <= _POSTURE_ORDER[b]


@pre(lambda family, profile: isinstance(family, str) and len(family) > 0)
@post(lambda result: isinstance(result, EnforcementResult))
def check_family_admission(
    family: str,
    profile: ProfileConfig,
) -> EnforcementResult:
    """Check if a tool's family is admitted by the profile.

    Examples:
        >>> from tela.core.models import ProfileConfig, Posture
        >>> p = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
        >>> check_family_admission("fs", p).verdict
        <EnforcementVerdict.ALLOW: 'allow'>
        >>> check_family_admission("shell", p).verdict
        <EnforcementVerdict.DENY: 'deny'>

    Args:
        family: Tool family name.
        profile: Profile configuration.

    Returns:
        EnforcementResult with ALLOW or DENY.
    """

    if family in profile.capabilities:
        return EnforcementResult(verdict=EnforcementVerdict.ALLOW)

    return EnforcementResult(
        verdict=EnforcementVerdict.DENY,
        denied_by="family_admission",
        error_code="AUTHZ_DENY",
        error_message=f"Family '{family}' is not admitted by profile '{profile.name}'",
    )


@pre(lambda tool_name, family, profile: isinstance(tool_name, str))
@post(lambda result: result is None or isinstance(result, EnforcementResult))
def check_tool_override(
    tool_name: str,
    family: str,
    profile: ProfileConfig,
) -> EnforcementResult | None:
    """Check if the profile has a specific override for this tool.

    Returns None if no override exists.
    Returns EnforcementResult(ALLOW) if override = allow.
    Returns EnforcementResult(DENY) if override = deny.

    Note: ALLOW overrides do not bypass posture-ceiling checks in ``enforce``.

    Examples:
        >>> from tela.core.models import ProfileConfig, Posture, EnforcementVerdict
        >>> p = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
        >>> check_tool_override("read_file", "fs", p) is None
        True

    Args:
        tool_name: Tool name.
        family: Tool family.
        profile: Profile configuration.

    Returns:
        EnforcementResult or None.
    """

    family_overrides = profile.tool_overrides.get(family)
    if family_overrides is None:
        return None

    verdict_str = family_overrides.overrides.get(tool_name)
    if verdict_str is None:
        return None

    if verdict_str == EnforcementVerdict.ALLOW:
        return EnforcementResult(verdict=EnforcementVerdict.ALLOW)

    return EnforcementResult(
        verdict=EnforcementVerdict.DENY,
        denied_by="tool_override",
        error_code="AUTHZ_DENY",
        error_message=f"Tool '{tool_name}' explicitly denied by profile override",
    )


@pre(
    lambda tool_posture, family_ceiling, default_posture: isinstance(
        family_ceiling, Posture
    )
)
@post(lambda result: isinstance(result, EnforcementResult))
def check_posture(
    tool_posture: Posture | None,
    family_ceiling: Posture,
    default_posture: Posture,
) -> EnforcementResult:
    """Check if a tool's posture is within the family ceiling.

    If tool_posture is None (unclassified), uses default_posture.
    If default_posture is NONE and tool is unclassified, returns DENY.

    Examples:
        >>> check_posture(Posture.READ_ONLY, Posture.READ_WRITE, Posture.NONE).verdict
        <EnforcementVerdict.ALLOW: 'allow'>
        >>> check_posture(Posture.DESTRUCTIVE, Posture.READ_ONLY, Posture.NONE).verdict
        <EnforcementVerdict.DENY: 'deny'>
        >>> check_posture(None, Posture.READ_WRITE, Posture.NONE).verdict
        <EnforcementVerdict.DENY: 'deny'>

    Args:
        tool_posture: Classified posture (None if unclassified).
        family_ceiling: Profile's posture ceiling for the tool's family.
        default_posture: Server's default posture for unclassified tools.

    Returns:
        EnforcementResult with ALLOW or DENY.
    """

    effective = tool_posture if tool_posture is not None else default_posture

    if effective == Posture.NONE and tool_posture is None:
        return EnforcementResult(
            verdict=EnforcementVerdict.DENY,
            denied_by="posture_ceiling",
            error_code="TOOL_UNCLASSIFIED",
            error_message="Tool is unclassified and server default_posture is NONE",
        )

    if posture_le(effective, family_ceiling):
        return EnforcementResult(verdict=EnforcementVerdict.ALLOW)

    return EnforcementResult(
        verdict=EnforcementVerdict.DENY,
        denied_by="posture_ceiling",
        error_code="AUTHZ_DENY",
        error_message=f"Tool posture {effective.value} exceeds ceiling {family_ceiling.value}",
    )


@pre(
    lambda tool_name, tool, profile, token_result, default_posture: (
        token_result.verdict == EnforcementVerdict.ALLOW
    )
)
@post(lambda result: isinstance(result, EnforcementResult))
def enforce(
    tool_name: str,
    tool: ResolvedTool,
    profile: ProfileConfig,
    token_result: EnforcementResult,
    default_posture: Posture,
) -> EnforcementResult:
    """Run the 3-step per-call enforcement chain for a tool call.

    Precondition: token_result.verdict MUST be ALLOW.

    Examples:
        >>> from tela.core.models import ResolvedTool, ProfileConfig, Posture, EnforcementResult, EnforcementVerdict
        >>> tool = ResolvedTool(name="read_file", server_name="fs", family="fs", posture=Posture.READ_ONLY)
        >>> profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
        >>> allow = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        >>> enforce("read_file", tool, profile, allow, Posture.NONE).verdict
        <EnforcementVerdict.ALLOW: 'allow'>

    Args:
        tool_name: Name of the tool being called.
        tool: Resolved tool metadata.
        profile: Bound profile configuration.
        token_result: Pre-computed token validation result (ALLOW for open mode).
        default_posture: Server's default_posture for unclassified tools.

    Returns:
        EnforcementResult with final verdict.
    """

    # Step 1: Family admission
    family_result = check_family_admission(tool.family, profile)
    if family_result.verdict == EnforcementVerdict.DENY:
        return family_result

    # Step 2: Tool override check
    override_result = check_tool_override(tool_name, tool.family, profile)
    if (
        override_result is not None
        and override_result.verdict == EnforcementVerdict.DENY
    ):
        return override_result

    # Step 3: Posture ceiling
    family_ceiling = profile.capabilities[tool.family]
    posture_result = check_posture(tool.posture, family_ceiling, default_posture)
    if posture_result.verdict == EnforcementVerdict.DENY:
        return posture_result

    # Final verdict
    return EnforcementResult(verdict=EnforcementVerdict.ALLOW)
