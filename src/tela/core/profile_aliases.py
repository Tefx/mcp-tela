"""Core helpers for profile configuration alias normalization."""

from __future__ import annotations

from typing import Any, Mapping

from tela.core.contracts import post, pre


@pre(lambda raw: raw is None or isinstance(raw, Mapping))
@post(
    lambda result: (
        isinstance(result, dict) and ("capabilities" in result or "tools" not in result)
    )
)
def normalize_profile_config_aliases(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize migration aliases for ``ProfileConfig`` inputs.

    Migration contract:
    - ``tools`` is accepted as an alias for ``capabilities``.
    - If both are provided they must be equal.

    Examples:
        >>> normalize_profile_config_aliases({"name": "dev", "tools": {"fs": "read_only"}})["capabilities"]["fs"]
        'read_only'
        >>> normalize_profile_config_aliases({"name": "dev", "capabilities": {"fs": "read_write"}})["capabilities"]["fs"]
        'read_write'
        >>> normalize_profile_config_aliases({"tools": {"fs": "read_only"}, "capabilities": {"fs": "read_write"}})
        Traceback (most recent call last):
        ...
        ValueError: ProfileConfig.tools and ProfileConfig.capabilities must match when both are provided
    """

    normalized: dict[str, Any] = {} if raw is None else dict(raw)
    capabilities = normalized.get("capabilities")
    tools = normalized.get("tools")

    if capabilities is None and tools is not None:
        normalized["capabilities"] = tools
    elif capabilities is not None and tools is not None and capabilities != tools:
        raise ValueError(
            "ProfileConfig.tools and ProfileConfig.capabilities must match when both are provided"
        )

    return normalized
