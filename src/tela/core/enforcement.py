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


@pre(
    lambda family, profile: (
        isinstance(family, str)
        and len(family) > 0
        and isinstance(profile, ProfileConfig)
    )
)
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
        error_message=f"Family '{family}' is not admitted by profile_id '{profile.name}'",
    )


@pre(
    lambda tool_name, family, profile: (
        isinstance(tool_name, str)
        and len(tool_name) > 0
        and isinstance(family, str)
        and len(family) > 0
        and isinstance(profile, ProfileConfig)
    )
)
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
    lambda tool_posture, family_ceiling, default_posture: (
        (tool_posture is None or isinstance(tool_posture, Posture))
        and isinstance(family_ceiling, Posture)
        and isinstance(default_posture, Posture)
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
        isinstance(tool_name, str)
        and len(tool_name) > 0
        and isinstance(tool, ResolvedTool)
        and isinstance(profile, ProfileConfig)
        and token_result.verdict == EnforcementVerdict.ALLOW
        and isinstance(default_posture, Posture)
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


@pre(
    lambda tool_name, tool, profile, token_result, default_posture: (
        isinstance(tool_name, str)
        and len(tool_name) > 0
        and isinstance(tool, ResolvedTool)
        and isinstance(profile, ProfileConfig)
        and isinstance(token_result, EnforcementResult)
        and isinstance(default_posture, Posture)
    )
)
@post(
    lambda result: (
        isinstance(result, dict)
        and isinstance(result.get("visible"), bool)
        and isinstance(result.get("hidden"), bool)
        and result.get("hidden") is not result.get("visible")
        and isinstance(result.get("allowed"), bool)
        and isinstance(result.get("denied"), bool)
    )
)
def explain_authorization(
    tool_name: str,
    tool: ResolvedTool,
    profile: ProfileConfig,
    token_result: EnforcementResult,
    default_posture: Posture,
) -> dict[str, object]:
    """Explain the visibility and call authorization outcome for one tool.

    The diagnostic follows the same ordered decision helpers as ``enforce``:
    token binding, family admission, profile tool override, then posture
    ceiling/default posture. It does not mutate runtime state or introduce a
    separate authorization vocabulary.

    Examples:
        >>> tool = ResolvedTool(name="read_file", server_name="fs", family="fs", posture=Posture.READ_ONLY)
        >>> profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
        >>> token = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        >>> explain_authorization("read_file", tool, profile, token, Posture.NONE)["allowed"]
        True
        >>> denied = ProfileConfig(name="none", capabilities={})
        >>> explain_authorization("read_file", tool, denied, token, Posture.NONE)["stage"]
        'family_admission'

    Args:
        tool_name: Routing name supplied to enforcement.
        tool: Resolved tool metadata.
        profile: Profile whose capabilities and overrides are evaluated.
        token_result: Token/open-mode binding result produced upstream.
        default_posture: Server default posture for unclassified tools.

    Returns:
        Diagnostic mapping with visible/hidden and allowed/denied booleans,
        plus denial stage and reason when authorization denies the tool.
    """

    if token_result.verdict == EnforcementVerdict.DENY:
        return _authorization_explain_denied(token_result, "token_validation")

    family_result = check_family_admission(tool.family, profile)
    if family_result.verdict == EnforcementVerdict.DENY:
        return _authorization_explain_denied(family_result, "family_admission")

    override_result = check_tool_override(tool_name, tool.family, profile)
    if override_result is not None and override_result.verdict == EnforcementVerdict.DENY:
        return _authorization_explain_denied(override_result, "tool_override")

    family_ceiling = profile.capabilities[tool.family]
    posture_result = check_posture(tool.posture, family_ceiling, default_posture)
    if posture_result.verdict == EnforcementVerdict.DENY:
        return _authorization_explain_denied(posture_result, "posture_ceiling")

    return {
        "visible": True,
        "hidden": False,
        "allowed": True,
        "denied": False,
        "stage": "allowed",
        "reason": "authorized",
    }


@pre(
    lambda result, stage: isinstance(result, EnforcementResult)
    and result.verdict == EnforcementVerdict.DENY
    and isinstance(stage, str)
    and len(stage) > 0
)
@post(
    lambda result: (
        result["visible"] is False
        and result["hidden"] is True
        and result["allowed"] is False
        and result["denied"] is True
        and isinstance(result.get("stage"), str)
        and isinstance(result.get("reason"), str)
    )
)
def _authorization_explain_denied(
    result: EnforcementResult,
    stage: str,
) -> dict[str, object]:
    """Build a denial explanation payload from an enforcement denial.

    Examples:
        >>> denied = EnforcementResult(verdict=EnforcementVerdict.DENY, denied_by="family_admission", error_message="Family blocked")
        >>> _authorization_explain_denied(denied, "family_admission")["hidden"]
        True

    Args:
        result: Denying enforcement result.
        stage: Enforcement stage responsible for the denial.

    Returns:
        Diagnostic mapping for a hidden and denied tool.
    """

    detail = result.error_message or result.error_code or result.denied_by or stage
    reason = f"{stage}: {detail}"
    return {
        "visible": False,
        "hidden": True,
        "allowed": False,
        "denied": True,
        "stage": stage,
        "reason": reason,
        "error_code": result.error_code,
        "denied_by": result.denied_by,
    }
