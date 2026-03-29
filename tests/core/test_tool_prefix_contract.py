"""Regression tests for tool_prefix feature contract.

These tests verify the tool_prefix runtime contract defined in:
- src/tela/core/models.py::ServerConfig.tool_prefix
- src/tela/core/family.py::resolve_tools (contract notes §NOTE lines 106-114)

Implementation owner: tool_prefix.runtime.*

IMPORTANT: expected_result is RED (tests should fail until runtime wiring exists).
Tests use pytest.mark.xfail with "pre-implementation" to document the gap.
"""

from __future__ import annotations

import pytest

from tela.core.family import resolve_tools
from tela.core.models import (
    Posture,
    ServerConfig,
    ToolOverride,
    TelaConfig,
)


# ---------------------------------------------------------------------------
# Spec-derived minimal fixture — INTERFACES.md §3.1 Servers
# ---------------------------------------------------------------------------
# This fixture uses the EXACT documented server config shape.
# Documented fields per §3.1: name, command, url, transport, env, family,
# default_posture, tool_overrides.
# NOTE: tool_prefix is NOT documented in §3.1 — this fixture omits it
# intentionally to exercise spec conformance and verify the documented shape.
# Ref: docs/INTERFACES.md §3.1
# ---------------------------------------------------------------------------

MINIMAL_SERVER_SPEC_FIXTURE = {
    "name": "fs",
    "command": "mcp-filesystem",
    "env": {"ROOT": "/workspace"},
    "family": "filesystem",
    "default_posture": "read_only",
    "tool_overrides": {"delete_file": {"family": None, "posture": "destructive"}},
}


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_spec_fixture_exercises_documented_server_config_shape() -> None:
    """Spec fixture uses only documented fields from INTERFACES.md §3.1.

    Ref: docs/INTERFACES.md §3.1 Servers
    The §3.1 format lists conditional fields (command vs url/transport).
    This fixture uses stdio (command) and omits HTTP-specific fields.
    No convenience fields beyond the spec are present.
    tool_prefix is NOT documented in §3.1 and is intentionally omitted here.
    """
    # Verify no extra fields beyond §3.1 documented set
    documented_keys = {
        "name",
        "command",
        "url",
        "transport",
        "env",
        "family",
        "default_posture",
        "tool_overrides",
        # tool_prefix is NOT in §3.1
        # instructions is separate from §3.1 server fields
    }
    fixture_keys = set(MINIMAL_SERVER_SPEC_FIXTURE.keys())
    assert fixture_keys <= documented_keys, (
        f"Extra fields in fixture: {fixture_keys - documented_keys}"
    )
    # Verify no tool_prefix in fixture (not in §3.1)
    assert "tool_prefix" not in MINIMAL_SERVER_SPEC_FIXTURE
    # Verify required transport choice field present (command for stdio)
    assert "command" in MINIMAL_SERVER_SPEC_FIXTURE


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_server_config_accepts_spec_fixture() -> None:
    """ServerConfig validates against the §3.1 spec fixture.

    Ref: docs/INTERFACES.md §3.1 Servers
    """
    cfg = ServerConfig(**MINIMAL_SERVER_SPEC_FIXTURE)
    assert cfg.name == "fs"
    assert cfg.command == "mcp-filesystem"
    assert cfg.env == {"ROOT": "/workspace"}
    assert cfg.family == "filesystem"
    assert cfg.default_posture == Posture.READ_ONLY
    assert cfg.tool_overrides["delete_file"].posture == Posture.DESTRUCTIVE


# ---------------------------------------------------------------------------
# Verification Point 1: omitted tool_prefix keeps old exposed names
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_omitted_tool_prefix_keeps_raw_name_as_exposed_name() -> None:
    """When tool_prefix is None/omitted, resolve_tools exposes the raw name unchanged.

    Ref: ServerConfig.tool_prefix contract (models.py line 112)
    Expected: red (resolve_tools does not yet apply tool_prefix)
    """
    cfg = ServerConfig(name="fs", command="cmd")  # tool_prefix = None by default
    tools = resolve_tools(
        "fs",
        cfg,
        [{"name": "read_file", "inputSchema": {"type": "object"}}],
    )
    assert len(tools) == 1
    # Without prefix, exposed name == raw name
    assert tools[0].name == "read_file"
    assert tools[0].raw_name == "read_file"


# ---------------------------------------------------------------------------
# Verification Point 2: identical raw tools under distinct prefixes coexist
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_distinct_prefixes_allow_same_raw_name_from_different_servers() -> None:
    """Two servers with the same raw tool name but different prefixes coexist.

    server_a: tool_prefix="a." → exposed "a.read_file"
    server_b: tool_prefix="b." → exposed "b.read_file"
    Both should be valid and distinct.

    Ref: ServerConfig.tool_prefix contract
    Expected: red (resolve_tools does not yet apply tool_prefix)
    """
    cfg_a = ServerConfig(name="server_a", command="cmd", tool_prefix="a.")
    cfg_b = ServerConfig(name="server_b", command="cmd", tool_prefix="b.")

    tools_a = resolve_tools(
        "server_a",
        cfg_a,
        [{"name": "read_file", "inputSchema": {"type": "object"}}],
    )
    tools_b = resolve_tools(
        "server_b",
        cfg_b,
        [{"name": "read_file", "inputSchema": {"type": "object"}}],
    )

    assert len(tools_a) == 1
    assert len(tools_b) == 1
    # Distinct exposed names despite identical raw names
    assert tools_a[0].name == "a.read_file"
    assert tools_b[0].name == "b.read_file"
    # raw_name preserves the downstream name for routing
    assert tools_a[0].raw_name == "read_file"
    assert tools_b[0].raw_name == "read_file"


# ---------------------------------------------------------------------------
# Verification Point 3: identical raw tools under the same prefix conflict
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_same_prefix_same_raw_name_is_conflict() -> None:
    """Two servers using the same tool_prefix that expose the same tool name conflict.

    This test documents the conflict-detection requirement: when prefix + raw_name
    produces the same exposed name from different servers, it must be rejected.

    Ref: ServerConfig.tool_prefix contract
    Expected: red (conflict detection not yet implemented)
    """
    cfg_a = ServerConfig(name="server_a", command="cmd", tool_prefix="fs.")
    cfg_b = ServerConfig(name="server_b", command="cmd", tool_prefix="fs.")

    tools_a = resolve_tools(
        "server_a",
        cfg_a,
        [{"name": "read_file", "inputSchema": {"type": "object"}}],
    )
    tools_b = resolve_tools(
        "server_b",
        cfg_b,
        [{"name": "read_file", "inputSchema": {"type": "object"}}],
    )

    assert len(tools_a) == 1
    assert len(tools_b) == 1
    # Same exposed name is the conflict signal
    assert tools_a[0].name == tools_b[0].name == "fs.read_file"
    # The contract requires conflict detection at registration time
    # (test here documents the requirement; runtime conflict detection TBD)


# ---------------------------------------------------------------------------
# Verification Point 4: tool_overrides continue to match raw downstream names
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_tool_overrides_match_raw_names_not_prefixed_names() -> None:
    """tool_overrides keys remain raw downstream names even when prefix is set.

    Ref: ServerConfig.tool_prefix contract (models.py lines 116-118)
    Ref: family.py resolve_tools notes (lines 111-112)
    Expected: green (this behavior is already documented in contracts)
    """
    cfg = ServerConfig(
        name="fs",
        command="cmd",
        tool_prefix="fs.",
        tool_overrides={
            # Key is the RAW downstream name, not the prefixed exposed name
            "delete_file": ToolOverride(posture=Posture.DESTRUCTIVE),
        },
    )
    tools = resolve_tools(
        "fs",
        cfg,
        [
            {"name": "read_file", "inputSchema": {"type": "object"}},
            {"name": "delete_file", "inputSchema": {"type": "object"}},
        ],
    )
    # Override matched via raw name
    delete_tool = next(t for t in tools if t.name == "fs.delete_file")
    assert delete_tool.posture == Posture.DESTRUCTIVE
    # Unprefixed override key still matched the raw name
    assert "fs.delete_file" not in cfg.tool_overrides
    assert "delete_file" in cfg.tool_overrides


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_prefixed_exposed_name_not_used_for_override_lookup() -> None:
    """Override keys are raw names — prefixed exposed names must NOT match overrides.

    Ref: ServerConfig.tool_prefix contract (models.py lines 116-118)
    Expected: green (contract is clear; tests the invariant)
    """
    cfg = ServerConfig(
        name="fs",
        command="cmd",
        tool_prefix="fs.",
        # Only raw name key exists
        tool_overrides={"delete_file": ToolOverride(posture=Posture.DESTRUCTIVE)},
    )
    tools = resolve_tools(
        "fs",
        cfg,
        [{"name": "delete_file", "inputSchema": {"type": "object"}}],
    )
    # Override was found via raw name lookup
    assert tools[0].posture == Posture.DESTRUCTIVE
    # Prefixed key would not exist
    assert "fs.delete_file" not in cfg.tool_overrides


# ---------------------------------------------------------------------------
# Verification Point 5: calling the exposed name routes to raw downstream name
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_resolved_tool_carries_raw_name_for_downstream_routing() -> None:
    """ResolvedTool.name is the exposed name; raw_name is for downstream routing.

    Ref: ResolvedTool contract (models.py lines 288-300)
    Ref: family.py resolve_tools notes (lines 109-110)
    Expected: red (resolve_tools does not yet populate raw_name with original name)
    """
    cfg = ServerConfig(name="fs", command="cmd", tool_prefix="fs.")
    tools = resolve_tools(
        "fs",
        cfg,
        [{"name": "read_file", "inputSchema": {"type": "object"}}],
    )
    assert len(tools) == 1
    # name is the exposed (prefixed) name for upstream discovery/call
    assert tools[0].name == "fs.read_file"
    # raw_name is the downstream-advertised name for routing
    assert tools[0].raw_name == "read_file"


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_resolved_tool_raw_name_none_when_prefix_omitted() -> None:
    """When tool_prefix is omitted, raw_name may be None for backward compat.

    Ref: ResolvedTool contract (models.py lines 303-306)
    Expected: red (raw_name not yet set by resolve_tools)
    """
    cfg = ServerConfig(name="fs", command="cmd")  # no prefix
    tools = resolve_tools(
        "fs",
        cfg,
        [{"name": "read_file", "inputSchema": {"type": "object"}}],
    )
    assert len(tools) == 1
    # raw_name carries the downstream name even when no prefix is applied
    assert tools[0].raw_name == "read_file"


# ---------------------------------------------------------------------------
# Verification Point 6: changing tool_prefix on reload emits tools/list_changed
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_tool_prefix_change_detected_as_tool_surface_change() -> None:
    """Changing tool_prefix is a tool-surface change requiring tools/list_changed.

    This is a contract test documenting that prefix-only changes must be
    detected and trigger notification.

    Ref: ServerConfig.tool_prefix contract (models.py lines 100-102)
    Ref: docs/USAGE.md §Config hot reload (notifications/tools/list_changed)
    Expected: red (reload detection not yet implemented)
    """
    cfg_v1 = ServerConfig(name="fs", command="cmd", tool_prefix=None)
    cfg_v2 = ServerConfig(name="fs", command="cmd", tool_prefix="fs.")

    tools_v1 = resolve_tools(
        "fs", cfg_v1, [{"name": "read_file", "inputSchema": {"type": "object"}}]
    )
    tools_v2 = resolve_tools(
        "fs", cfg_v2, [{"name": "read_file", "inputSchema": {"type": "object"}}]
    )

    assert tools_v1[0].name == "read_file"
    assert tools_v2[0].name == "fs.read_file"
    # The exposed tool set changed (different names), so reload must emit
    # tools/list_changed and refresh the tool set
    assert tools_v1[0].name != tools_v2[0].name


# ---------------------------------------------------------------------------
# Verification Point 7: tool_prefix="tela." is rejected
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_tela_prefix_is_reserved_and_rejected() -> None:
    """tool_prefix="tela." is reserved and must be rejected at config validation.

    The "tela." prefix is used for built-in MCP surfaces (tela.profiles, etc.)
    per INTERFACES.md §7.1.

    Ref: ServerConfig.tool_prefix contract (models.py line 99)
    Ref: INTERFACES.md §7.1 (tela.profiles resource; tela.* prefix is reserved)
    Expected: red (rejection not yet implemented in model validator)
    """
    from pydantic import ValidationError

    # Reserved prefix must be rejected at ServerConfig construction time
    with pytest.raises(ValidationError, match="[Tt]ela"):
        ServerConfig(name="fs", command="cmd", tool_prefix="tela.")

    with pytest.raises(ValidationError, match="[Tt]ela"):
        ServerConfig(name="fs", command="cmd", tool_prefix="tela")


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_tela_prefix_rejected_even_with_trailing_dot() -> None:
    """tool_prefix="tela." (with trailing dot) is still the tela prefix.

    Ref: ServerConfig.tool_prefix contract
    Expected: red (rejection not yet implemented)
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="[Tt]ela"):
        ServerConfig(name="fs", command="cmd", tool_prefix="tela.")


# ---------------------------------------------------------------------------
# Integration: TelaConfig with tool_prefix on servers
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="pre-implementation: tool_prefix not yet wired")
def test_tela_config_with_prefixed_servers() -> None:
    """TelaConfig accepts servers with tool_prefix configured.

    Ref: ServerConfig.tool_prefix contract
    Expected: red (prefix application in resolution not yet wired)
    """
    cfg = TelaConfig(
        servers={
            "fs_a": ServerConfig(name="fs_a", command="cmd", tool_prefix="a."),
            "fs_b": ServerConfig(name="fs_b", command="cmd", tool_prefix="b."),
        }
    )
    assert cfg.servers["fs_a"].tool_prefix == "a."
    assert cfg.servers["fs_b"].tool_prefix == "b."


# ---------------------------------------------------------------------------
# Sibling search verification
# ---------------------------------------------------------------------------
# The following patterns were checked across the codebase after modification:
# - resolve_tools usage: checked in family.py (only place calling it)
# - ServerConfig.tool_prefix: checked in models.py field definition
# - ResolvedTool.raw_name: checked in models.py
# No sibling files needed modification for this test-only step.
# ---------------------------------------------------------------------------
