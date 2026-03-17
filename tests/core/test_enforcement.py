"""Tests for the 7-step enforcement chain."""

from __future__ import annotations

from tela.core.enforcement import (
    check_family_admission,
    check_posture,
    check_side_effect,
    check_tool_override,
    enforce,
    posture_le,
)
from tela.core.models import (
    EnforcementResult,
    EnforcementVerdict,
    Posture,
    ProfileConfig,
    ProfileToolOverrides,
    ResolvedTool,
    SideEffectPolicy,
)

ALLOW = EnforcementResult(verdict=EnforcementVerdict.ALLOW)


# --- posture_le ---

def test_posture_le_same() -> None:
    assert posture_le(Posture.READ_ONLY, Posture.READ_ONLY) is True

def test_posture_le_lower() -> None:
    assert posture_le(Posture.READ_ONLY, Posture.DESTRUCTIVE) is True

def test_posture_le_higher() -> None:
    assert posture_le(Posture.DESTRUCTIVE, Posture.READ_ONLY) is False


# --- check_family_admission ---

def test_family_admission_allowed() -> None:
    p = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    assert check_family_admission("fs", p).verdict == EnforcementVerdict.ALLOW

def test_family_admission_denied() -> None:
    p = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    r = check_family_admission("shell", p)
    assert r.verdict == EnforcementVerdict.DENY
    assert r.denied_by == "family_admission"


# --- check_tool_override ---

def test_tool_override_none() -> None:
    p = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    assert check_tool_override("read_file", "fs", p) is None

def test_tool_override_deny() -> None:
    p = ProfileConfig(
        name="dev",
        tools={"fs": Posture.READ_WRITE},
        tool_overrides={"fs": ProfileToolOverrides(overrides={"delete_file": EnforcementVerdict.DENY})},
    )
    r = check_tool_override("delete_file", "fs", p)
    assert r is not None
    assert r.verdict == EnforcementVerdict.DENY

def test_tool_override_allow() -> None:
    p = ProfileConfig(
        name="dev",
        tools={"fs": Posture.READ_ONLY},
        tool_overrides={"fs": ProfileToolOverrides(overrides={"special": EnforcementVerdict.ALLOW})},
    )
    r = check_tool_override("special", "fs", p)
    assert r is not None
    assert r.verdict == EnforcementVerdict.ALLOW


# --- check_posture ---

def test_posture_within_ceiling() -> None:
    assert check_posture(Posture.READ_ONLY, Posture.READ_WRITE, Posture.NONE).verdict == EnforcementVerdict.ALLOW

def test_posture_exceeds_ceiling() -> None:
    r = check_posture(Posture.DESTRUCTIVE, Posture.READ_ONLY, Posture.NONE)
    assert r.verdict == EnforcementVerdict.DENY

def test_posture_unclassified_with_default() -> None:
    assert check_posture(None, Posture.READ_WRITE, Posture.READ_ONLY).verdict == EnforcementVerdict.ALLOW

def test_posture_unclassified_with_none_default() -> None:
    r = check_posture(None, Posture.READ_WRITE, Posture.NONE)
    assert r.verdict == EnforcementVerdict.DENY
    assert r.error_code == "TOOL_UNCLASSIFIED"


# --- check_side_effect ---

def test_side_effect_allow_policy() -> None:
    assert check_side_effect(Posture.DESTRUCTIVE, SideEffectPolicy.ALLOW).verdict == EnforcementVerdict.ALLOW

def test_side_effect_readonly_policy_readonly_tool() -> None:
    assert check_side_effect(Posture.READ_ONLY, SideEffectPolicy.READ_ONLY).verdict == EnforcementVerdict.ALLOW

def test_side_effect_readonly_policy_readwrite_tool() -> None:
    r = check_side_effect(Posture.READ_WRITE, SideEffectPolicy.READ_ONLY)
    assert r.verdict == EnforcementVerdict.DENY
    assert r.denied_by == "side_effect_check"


# --- enforce (full chain) ---

def test_enforce_allows_valid_tool() -> None:
    tool = ResolvedTool(name="read_file", server_name="fs", family="fs", posture=Posture.READ_ONLY)
    profile = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    assert enforce("read_file", tool, profile, ALLOW, Posture.NONE).verdict == EnforcementVerdict.ALLOW

def test_enforce_denies_unadmitted_family() -> None:
    tool = ResolvedTool(name="cmd", server_name="shell", family="shell", posture=Posture.DESTRUCTIVE)
    profile = ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE})
    r = enforce("cmd", tool, profile, ALLOW, Posture.NONE)
    assert r.verdict == EnforcementVerdict.DENY
    assert r.denied_by == "family_admission"

def test_enforce_denies_posture_exceedance() -> None:
    tool = ResolvedTool(name="write_file", server_name="fs", family="fs", posture=Posture.READ_WRITE)
    profile = ProfileConfig(name="reader", tools={"fs": Posture.READ_ONLY})
    r = enforce("write_file", tool, profile, ALLOW, Posture.NONE)
    assert r.verdict == EnforcementVerdict.DENY

def test_enforce_override_allows_despite_posture() -> None:
    tool = ResolvedTool(name="special", server_name="fs", family="fs", posture=Posture.DESTRUCTIVE)
    profile = ProfileConfig(
        name="dev",
        tools={"fs": Posture.READ_ONLY},
        tool_overrides={"fs": ProfileToolOverrides(overrides={"special": EnforcementVerdict.ALLOW})},
    )
    assert enforce("special", tool, profile, ALLOW, Posture.NONE).verdict == EnforcementVerdict.ALLOW

def test_enforce_override_denies() -> None:
    tool = ResolvedTool(name="danger", server_name="fs", family="fs", posture=Posture.READ_ONLY)
    profile = ProfileConfig(
        name="dev",
        tools={"fs": Posture.READ_WRITE},
        tool_overrides={"fs": ProfileToolOverrides(overrides={"danger": EnforcementVerdict.DENY})},
    )
    r = enforce("danger", tool, profile, ALLOW, Posture.NONE)
    assert r.verdict == EnforcementVerdict.DENY
    assert r.denied_by == "tool_override"

def test_enforce_side_effect_blocks_write_in_readonly() -> None:
    tool = ResolvedTool(name="write", server_name="fs", family="fs", posture=Posture.READ_WRITE)
    profile = ProfileConfig(
        name="safe",
        tools={"fs": Posture.READ_WRITE},
        side_effect_policy=SideEffectPolicy.READ_ONLY,
    )
    r = enforce("write", tool, profile, ALLOW, Posture.NONE)
    assert r.verdict == EnforcementVerdict.DENY
    assert r.denied_by == "side_effect_check"
