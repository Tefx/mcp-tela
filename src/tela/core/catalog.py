"""Prebuilt profile catalog for tela.

Provides a set of built-in profiles matching the INTERFACES.md specification.
These are templates -- deployment-local configuration remains runtime source
of truth.

Core zone: no I/O imports allowed.
"""

from __future__ import annotations

from typing import Mapping

from tela.core.contracts import pre, post
from tela.core.models import Posture, ProfileConfig, SideEffectPolicy


# The 7 prebuilt profiles from INTERFACES.md v1 catalog.
#
# Each profile maps tool families to posture ceilings. The exact families
# are deployment-specific; these use the canonical family names from the spec.
BUILTIN_PROFILES: dict[str, ProfileConfig] = {
    "read_only": ProfileConfig(
        name="read_only",
        tools={"filesystem": Posture.READ_ONLY},
        side_effect_policy=SideEffectPolicy.READ_ONLY,
        default=False,
    ),
    "fetch_external": ProfileConfig(
        name="fetch_external",
        tools={
            "filesystem": Posture.READ_ONLY,
            "network": Posture.READ_ONLY,
        },
        side_effect_policy=SideEffectPolicy.READ_ONLY,
        default=False,
    ),
    "modify_local": ProfileConfig(
        name="modify_local",
        tools={
            "filesystem": Posture.READ_WRITE,
        },
        side_effect_policy=SideEffectPolicy.ALLOW,
        default=False,
    ),
    "send_external": ProfileConfig(
        name="send_external",
        tools={
            "filesystem": Posture.READ_ONLY,
            "network": Posture.READ_WRITE,
        },
        side_effect_policy=SideEffectPolicy.ALLOW,
        default=False,
    ),
    "orchestrate": ProfileConfig(
        name="orchestrate",
        tools={
            "filesystem": Posture.READ_ONLY,
            "network": Posture.READ_ONLY,
            "orchestration": Posture.READ_WRITE,
        },
        side_effect_policy=SideEffectPolicy.ALLOW,
        default=False,
    ),
    "execute_safe": ProfileConfig(
        name="execute_safe",
        tools={
            "filesystem": Posture.READ_WRITE,
            "network": Posture.READ_WRITE,
            "orchestration": Posture.READ_WRITE,
            "execution": Posture.READ_WRITE,
        },
        side_effect_policy=SideEffectPolicy.ALLOW,
        default=False,
    ),
    "execute_full": ProfileConfig(
        name="execute_full",
        tools={
            "filesystem": Posture.DESTRUCTIVE,
            "network": Posture.DESTRUCTIVE,
            "orchestration": Posture.DESTRUCTIVE,
            "execution": Posture.DESTRUCTIVE,
        },
        side_effect_policy=SideEffectPolicy.ALLOW,
        default=False,
    ),
}


@pre(lambda name: isinstance(name, str) and len(name) > 0)
@post(lambda result: result is None or isinstance(result, ProfileConfig))
def get_builtin_profile(name: str) -> ProfileConfig | None:
    """Look up a single builtin profile by name.

    Args:
        name: Profile name to look up.

    Returns:
        The ProfileConfig if found, else None.

    Examples:
        >>> get_builtin_profile("read_only").side_effect_policy.value
        'read_only'
        >>> get_builtin_profile("nonexistent") is None
        True
    """
    return BUILTIN_PROFILES.get(name)


@post(lambda result: len(result) == 7 and all(isinstance(n, str) for n in result))
def list_builtin_profiles() -> list[str]:
    """List all builtin profile names.

    Returns:
        Sorted list of builtin profile names.

    Examples:
        >>> list_builtin_profiles()[0]
        'execute_full'
    """
    return sorted(BUILTIN_PROFILES.keys())


@pre(
    lambda user_profiles: all(isinstance(k, str) for k in user_profiles.keys())
)
@post(lambda result: len(result) >= len(BUILTIN_PROFILES))
def merge_with_builtins(
    user_profiles: Mapping[str, ProfileConfig],
) -> dict[str, ProfileConfig]:
    """Merge user profiles with builtins, user takes precedence.

    If user_profiles is empty, returns builtins as defaults.
    User-defined profiles with the same name override builtins entirely.

    Args:
        user_profiles: User-defined profiles from config.

    Returns:
        Merged profile dict with user profiles taking precedence.

    Examples:
        >>> result = merge_with_builtins({})
        >>> len(result) == 7
        True
        >>> custom = ProfileConfig(name="read_only", tools={}, default=True)
        >>> result = merge_with_builtins({"read_only": custom})
        >>> result["read_only"].default
        True
    """
    merged = dict(BUILTIN_PROFILES)
    merged.update(user_profiles)
    return merged
