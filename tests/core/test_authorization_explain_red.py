"""Expected-red tests for the authorization explain surface.

These tests fail because ``explain_authorization`` does not yet exist in
``tela.core.enforcement``.  When implemented, the function must derive
visible / hidden / allowed / denied outcomes purely from the existing
enforcement logic (family admission, posture ceiling, tool override,
token binding).  The downstream implementation step is
``tela.operator_p1.surfaces.authorization_explain``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

# RED trigger: this import will raise ImportError until the surface is
# implemented, proving the gap exists.
from tela.core.enforcement import enforce, explain_authorization

from tela.core.models import (
    EnforcementResult,
    EnforcementVerdict,
    Posture,
    ProfileConfig,
    ProfileToolOverrides,
    ResolvedTool,
)

# ---------------------------------------------------------------------------
# Shared fixtures / constants
# ---------------------------------------------------------------------------

_ALLOW = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
_DENY_TOKEN = EnforcementResult(
    verdict=EnforcementVerdict.DENY,
    denied_by="token_validation",
    error_code="TOKEN_INVALID",
    error_message="Token signature verification failed",
)


@dataclass(frozen=True)
class _Expectation:
    visible: bool
    allowed: bool
    denied: bool
    reason_contains: str | None = None
    stage: str | None = None


def _tool(name: str, family: str, posture: Posture | None) -> ResolvedTool:
    return ResolvedTool(name=name, server_name=family, family=family, posture=posture)


TOOLS = {
    "fs_read": _tool("read_file", "fs", Posture.READ_ONLY),
    "fs_write": _tool("write_file", "fs", Posture.READ_WRITE),
    "fs_delete": _tool("delete_file", "fs", Posture.DESTRUCTIVE),
    "fs_unclassified": _tool("unclassified", "fs", None),
    "shell_exec": _tool("exec", "shell", Posture.DESTRUCTIVE),
    "net_get": _tool("net_get", "network", Posture.READ_ONLY),
    "net_post": _tool("net_post", "network", Posture.READ_WRITE),
}


# ---------------------------------------------------------------------------
# Parametrized per-tool explain property tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "tool", "default_posture", "expected"),
    [
        # 1. Family admitted, posture within ceiling => visible + allowed
        (
            ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE}),
            TOOLS["fs_read"],
            Posture.NONE,
            _Expectation(
                visible=True,
                allowed=True,
                denied=False,
            ),
        ),
        # 2. Family NOT admitted => hidden + denied by family_admission
        (
            ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE}),
            TOOLS["shell_exec"],
            Posture.NONE,
            _Expectation(
                visible=False,
                allowed=False,
                denied=True,
                reason_contains="family",
                stage="family_admission",
            ),
        ),
        # 3. Posture exceeds family ceiling => hidden + denied by posture_ceiling
        (
            ProfileConfig(name="reader", capabilities={"fs": Posture.READ_ONLY}),
            TOOLS["fs_write"],
            Posture.NONE,
            _Expectation(
                visible=False,
                allowed=False,
                denied=True,
                reason_contains="posture",
                stage="posture_ceiling",
            ),
        ),
        # 4. Tool override DENY => hidden + denied by tool_override
        (
            ProfileConfig(
                name="dev",
                capabilities={"fs": Posture.READ_WRITE},
                tool_overrides={
                    "fs": ProfileToolOverrides(
                        overrides={"write_file": EnforcementVerdict.DENY}
                    )
                },
            ),
            TOOLS["fs_write"],
            Posture.NONE,
            _Expectation(
                visible=False,
                allowed=False,
                denied=True,
                reason_contains="override",
                stage="tool_override",
            ),
        ),
        # 5. Unclassified + default_posture=NONE => hidden + denied (TOOL_UNCLASSIFIED)
        (
            ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE}),
            TOOLS["fs_unclassified"],
            Posture.NONE,
            _Expectation(
                visible=False,
                allowed=False,
                denied=True,
                reason_contains="unclassified",
                stage="posture_ceiling",
            ),
        ),
        # 6. Unclassified + default_posture<=ceiling => visible + allowed
        (
            ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE}),
            TOOLS["fs_unclassified"],
            Posture.READ_ONLY,
            _Expectation(
                visible=True,
                allowed=True,
                denied=False,
            ),
        ),
        # 7. Override ALLOW does NOT bypass posture ceiling (still hidden)
        (
            ProfileConfig(
                name="reader",
                capabilities={"fs": Posture.READ_ONLY},
                tool_overrides={
                    "fs": ProfileToolOverrides(
                        overrides={"write_file": EnforcementVerdict.ALLOW}
                    )
                },
            ),
            TOOLS["fs_write"],
            Posture.NONE,
            _Expectation(
                visible=False,
                allowed=False,
                denied=True,
                reason_contains="posture",
                stage="posture_ceiling",
            ),
        ),
        # 8. DESTRUCTIVE posture within DESTRUCTIVE ceiling => visible + allowed
        (
            ProfileConfig(name="admin", capabilities={"fs": Posture.DESTRUCTIVE}),
            TOOLS["fs_delete"],
            Posture.NONE,
            _Expectation(
                visible=True,
                allowed=True,
                denied=False,
            ),
        ),
        # 9. READ_ONLY posture within READ_WRITE ceiling => visible + allowed
        (
            ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE}),
            TOOLS["fs_read"],
            Posture.NONE,
            _Expectation(
                visible=True,
                allowed=True,
                denied=False,
            ),
        ),
        # 10. READ_WRITE posture within READ_ONLY ceiling => hidden + denied
        (
            ProfileConfig(name="strict", capabilities={"fs": Posture.READ_ONLY}),
            TOOLS["fs_write"],
            Posture.NONE,
            _Expectation(
                visible=False,
                allowed=False,
                denied=True,
                reason_contains="posture",
                stage="posture_ceiling",
            ),
        ),
        # 11. Network tool with read_only profile => hidden
        (
            ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE}),
            TOOLS["net_get"],
            Posture.NONE,
            _Expectation(
                visible=False,
                allowed=False,
                denied=True,
                reason_contains="family",
                stage="family_admission",
            ),
        ),
        # 12. Empty profile / no capabilities => everything hidden
        (
            ProfileConfig(name="empty", capabilities={}),
            TOOLS["fs_read"],
            Posture.NONE,
            _Expectation(
                visible=False,
                allowed=False,
                denied=True,
                reason_contains="family",
                stage="family_admission",
            ),
        ),
    ],
    ids=[
        "admitted_within_ceiling",
        "unadmitted_family",
        "posture_exceeds_ceiling",
        "tool_override_deny",
        "unclassified_default_none",
        "unclassified_default_ok",
        "override_allow_overruled_by_posture",
        "destructive_within_ceiling",
        "read_only_within_read_write",
        "read_write_exceeds_read_only",
        "network_not_in_fs_profile",
        "empty_profile_blocks_all",
    ],
)
def test_explain_per_tool_outcomes(
    profile: ProfileConfig,
    tool: ResolvedTool,
    default_posture: Posture,
    expected: _Expectation,
) -> None:
    """explain_authorization must return correct visible/hidden/allowed/denied."""
    result = explain_authorization(
        tool_name=tool.name,
        tool=tool,
        profile=profile,
        token_result=_ALLOW,
        default_posture=default_posture,
    )
    assert (
        result["visible"] == expected.visible
    ), f"visible mismatch for {tool.name} under {profile.name}"
    assert (
        result["hidden"] is not expected.visible
    ), f"hidden must be inverse of visible for {tool.name}"
    assert (
        result["allowed"] == expected.allowed
    ), f"allowed mismatch for {tool.name} under {profile.name}"
    assert (
        result["denied"] == expected.denied
    ), f"denied mismatch for {tool.name} under {profile.name}"
    if expected.reason_contains:
        reason = result.get("reason") or ""
        assert expected.reason_contains in reason, (
            f"reason should mention {expected.reason_contains!r}, got {reason!r}"
        )
    if expected.stage:
        assert result.get("stage") == expected.stage, (
            f"stage should be {expected.stage!r}, got {result.get('stage')!r}"
        )


# ---------------------------------------------------------------------------
# Token / open-mode binding property tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token_result", "expected_all_denied"),
    [
        (_ALLOW, False),
        (_DENY_TOKEN, True),
    ],
    ids=["token_allow", "token_deny"],
)
def test_explain_token_binding_outcome(
    token_result: EnforcementResult, expected_all_denied: bool
) -> None:
    """When token_result is DENY every tool must be denied and hidden."""
    profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
    tool = TOOLS["fs_read"]
    result = explain_authorization(
        tool_name=tool.name,
        tool=tool,
        profile=profile,
        token_result=token_result,
        default_posture=Posture.NONE,
    )
    if expected_all_denied:
        assert result["denied"] is True
        assert result["visible"] is False
        assert result["allowed"] is False
        assert result.get("stage") == "token_validation"
    else:
        assert result["denied"] is False
        assert result["visible"] is True
        assert result["allowed"] is True


# ---------------------------------------------------------------------------
# Builtin profile coverage (property-like across catalog)
# ---------------------------------------------------------------------------


def test_explain_builtin_profile_coverage() -> None:
    """Vary profiles across the built-in catalog with a fixed tool space."""
    from tela.core.catalog import BUILTIN_PROFILES

    tool_space = list(TOOLS.values())

    # For each builtin profile, every tool should produce a definitive
    # visible/hidden/allowed/denied outcome.
    for profile in BUILTIN_PROFILES.values():
        for tool in tool_space:
            result = explain_authorization(
                tool_name=tool.name,
                tool=tool,
                profile=profile,
                token_result=_ALLOW,
                default_posture=Posture.NONE,
            )
            # Must return dict-like structure with required keys
            assert isinstance(result, dict)
            assert "visible" in result
            assert "hidden" in result
            assert "allowed" in result
            assert "denied" in result
            # Boolean consistency
            assert isinstance(result["visible"], bool)
            assert result["hidden"] is not result["visible"]
            # If denied, there must be a reason and stage
            if result["denied"]:
                assert "reason" in result
                assert "stage" in result
                assert result["stage"] in (
                    "family_admission",
                    "tool_override",
                    "posture_ceiling",
                    "token_validation",
                )
            # allowed/denied consistency (they need not be strict inverses
            # but in current enforcement logic a tool that is allowed is
            # never simultaneously denied).
            if result["allowed"]:
                assert result["denied"] is False


# ---------------------------------------------------------------------------
# Non-mutation proof: explain is diagnostic only
# ---------------------------------------------------------------------------


def test_explain_is_diagnostic_no_mutation() -> None:
    """Running explain_authorization must not alter enforcement results."""
    profile = ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE})
    tool = TOOLS["fs_read"]

    # Before explain
    before = enforce("read_file", tool, profile, _ALLOW, Posture.NONE)

    # Explain call (should be pure / no side effects)
    _ = explain_authorization(
        tool_name="read_file",
        tool=tool,
        profile=profile,
        token_result=_ALLOW,
        default_posture=Posture.NONE,
    )

    # After explain
    after = enforce("read_file", tool, profile, _ALLOW, Posture.NONE)

    assert before.verdict == after.verdict
    assert before.denied_by == after.denied_by
    assert before.error_code == after.error_code
    assert before.model_dump() == after.model_dump()


# ---------------------------------------------------------------------------
# Visibility-filter alignment: explain matches filter_tools_for_profile
# ---------------------------------------------------------------------------


def test_explain_visible_matches_filter_tools_for_profile() -> None:
    """A tool marked visible by explain must also be permitted by filter_tools_for_profile."""
    from tela.shell.upstream_utils import filter_tools_for_profile

    profile = ProfileConfig(
        name="dev",
        capabilities={"fs": Posture.READ_WRITE, "network": Posture.READ_ONLY},
    )
    all_tools = {
        "fs": [TOOLS["fs_read"], TOOLS["fs_write"], TOOLS["fs_delete"]],
        "network": [TOOLS["net_get"], TOOLS["net_post"]],
        "shell": [TOOLS["shell_exec"]],
    }
    server_defaults = {
        "fs": Posture.NONE,
        "network": Posture.NONE,
        "shell": Posture.NONE,
    }

    # Tools visible per filter
    filtered = filter_tools_for_profile(all_tools, profile, server_defaults)
    assert filtered.is_ok
    visible_names = {t.name for t in filtered.value or []}

    # Explain should agree on visibility for every tool
    for server_name, tools in all_tools.items():
        default_posture = server_defaults[server_name]
        for tool in tools:
            result = explain_authorization(
                tool_name=tool.name,
                tool=tool,
                profile=profile,
                token_result=_ALLOW,
                default_posture=default_posture,
            )
            if result["visible"]:
                assert (
                    tool.name in visible_names
                ), f"explain says {tool.name} visible but filter excluded it"
            else:
                assert (
                    tool.name not in visible_names
                ), f"explain says {tool.name} hidden but filter included it"


# ---------------------------------------------------------------------------
# Edge / override property tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ceiling", "tool_posture", "visible"),
    [
        (Posture.NONE, Posture.NONE, True),
        (Posture.NONE, Posture.READ_ONLY, False),
        (Posture.NONE, Posture.READ_WRITE, False),
        (Posture.NONE, Posture.DESTRUCTIVE, False),
        (Posture.READ_ONLY, Posture.READ_ONLY, True),
        (Posture.READ_ONLY, Posture.READ_WRITE, False),
        (Posture.READ_ONLY, Posture.DESTRUCTIVE, False),
        (Posture.READ_WRITE, Posture.READ_WRITE, True),
        (Posture.READ_WRITE, Posture.DESTRUCTIVE, False),
        (Posture.DESTRUCTIVE, Posture.DESTRUCTIVE, True),
    ],
    ids=[
        "none_none",
        "none_readonly",
        "none_readwrite",
        "none_destructive",
        "readonly_readonly",
        "readonly_readwrite",
        "readonly_destructive",
        "readwrite_readwrite",
        "readwrite_destructive",
        "destructive_destructive",
    ],
)
def test_explain_all_posture_pairs(
    ceiling: Posture, tool_posture: Posture, visible: bool
) -> None:
    """Cover the full posture-comparison matrix."""
    profile = ProfileConfig(name="p", capabilities={"fs": ceiling})
    tool = _tool("t", "fs", tool_posture)
    result = explain_authorization(
        tool_name="t",
        tool=tool,
        profile=profile,
        token_result=_ALLOW,
        default_posture=Posture.NONE,
    )
    assert result["visible"] == visible
    assert result["hidden"] is not visible
    if visible:
        assert result["allowed"] is True
        assert result["denied"] is False
    else:
        assert result["allowed"] is False
        assert result["denied"] is True
        assert result["stage"] == "posture_ceiling"
