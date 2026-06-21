"""Black-box verification for tool_prefix.deep_review.black_box.

Per docs/USAGE.md §Tool Prefix Configuration and the ServerConfig/resolve_tools
runtime contract:
  - tool_prefix namespace all tools from a server
  - Two servers with same raw tool name can coexist when prefixes differ
  - tool_overrides keys remain raw downstream names (not prefixed)
  - tool_prefix=null (omitted) preserves backward-compatible behavior
  - tool_prefix values in the reserved `tela.` / `tela_` namespaces are rejected
  - plain `tool_prefix="tela"` remains allowed because it does not enter those namespaces

Mode A (Indictment) tests use spec-derived fixtures:
  - Fixtures mimic config from YAML examples, not implementation assumptions
  - All assertions test observable behavior via public API
  - Tests import from tela.core.family and tela.core.conflict (public API)

Verification required:
  1. Prefix coexistence: distinct prefixes → distinct exposed names
  2. Prefix routing: exposed name resolves to correct downstream tool
  3. Backward compatibility: omitted prefix → raw name as exposed name
  4. Reserved prefix: reserved `tela.` / `tela_` namespaces rejected by validation/runtime
"""

from __future__ import annotations

import sys

import pytest

# Public API imports only - no implementation source imports
from tela.core.conflict import ConflictType, detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import Posture, ServerConfig, ToolOverride


# ---------------------------------------------------------------------------
# Spec-derived fixtures from docs/USAGE.md and tela.yaml.example
# ---------------------------------------------------------------------------

# From USAGE.md lines 103-114:
# servers:
#   fs-prod:
#     command: "mcp-filesystem"
#     args: ["--root", "/prod"]
#     family: "filesystem"
#     tool_prefix: "prod_"
#   fs-staging:
#     command: "mcp-filesystem"
#     args: ["--root", "/staging"]
#     family: "filesystem"
#     tool_prefix: "staging_"

SPEC_SERVER_PROD = {
    "name": "fs-prod",
    "command": "mcp-filesystem",
    "args": ["--root", "/prod"],
    "family": "filesystem",
    "tool_prefix": "prod_",
}

SPEC_SERVER_STAGING = {
    "name": "fs-staging",
    "command": "mcp-filesystem",
    "args": ["--root", "/staging"],
    "family": "filesystem",
    "tool_prefix": "staging_",
}

# From USAGE.md lines 132-145:
# servers:
#   git-work:
#     command: "mcp-github"
#     env:
#       GITHUB_TOKEN: "${WORK_GITHUB_TOKEN}"
#     family: "git"
#     tool_prefix: "work_"
#   git-personal:
#     command: "mcp-github"
#     env:
#       GITHUB_TOKEN: "${PERSONAL_GITHUB_TOKEN}"
#     family: "git"
#     tool_prefix: "personal_"

SPEC_SERVER_WORK = {
    "name": "git-work",
    "command": "mcp-github",
    "env": {"GITHUB_TOKEN": "${WORK_GITHUB_TOKEN}"},
    "family": "git",
    "tool_prefix": "work_",
}

SPEC_SERVER_PERSONAL = {
    "name": "git-personal",
    "command": "mcp-github",
    "env": {"GITHUB_TOKEN": "${PERSONAL_GITHUB_TOKEN}"},
    "family": "git",
    "tool_prefix": "personal_",
}


# Downstream tool inventory mock (from MCP tools/list response)
DOWNSTREAM_TOOLS_GITHUB = [
    {"name": "search_repos", "inputSchema": {"type": "object"}},
    {"name": "create_issue", "inputSchema": {"type": "object"}},
    {"name": "list_prs", "inputSchema": {"type": "object"}},
]

DOWNSTREAM_TOOLS_FS = [
    {"name": "read_file", "inputSchema": {"type": "object"}},
    {"name": "write_file", "inputSchema": {"type": "object"}},
    {"name": "delete_file", "inputSchema": {"type": "object"}},
]


# ---------------------------------------------------------------------------
# Verification Point 1: Prefix coexistence
# ---------------------------------------------------------------------------


def test_two_servers_same_raw_tool_with_distinct_prefixes_coexist():
    """Two servers with same raw tool name can coexist when prefixes differ.

    Spec: docs/USAGE.md lines 132-147
    Expected: Both servers expose distinct prefixed names, no conflict.

    Evidence:
      - git-work with tool_prefix="work_" → work_search_repos
      - git-personal with tool_prefix="personal_" → personal_search_repos
      - Same downstream tool "search_repos", different exposed names
      - detect_conflicts finds no conflict
    """
    cfg_work = ServerConfig(**SPEC_SERVER_WORK)
    cfg_personal = ServerConfig(**SPEC_SERVER_PERSONAL)

    # Resolve tools for each server
    tools_work = resolve_tools("git-work", cfg_work, DOWNSTREAM_TOOLS_GITHUB)
    tools_personal = resolve_tools(
        "git-personal", cfg_personal, DOWNSTREAM_TOOLS_GITHUB
    )

    # Verify prefix application
    assert len(tools_work) == 3, (
        f"Issue PREFIX_COEXIST: git-work should have 3 tools, got {len(tools_work)}"
    )
    assert len(tools_personal) == 3, (
        f"Issue PREFIX_COEXIST: git-personal should have 3 tools, got {len(tools_personal)}"
    )

    # Verify exposed names are prefixed
    work_names = {t.name for t in tools_work}
    personal_names = {t.name for t in tools_personal}

    assert work_names == {"work_search_repos", "work_create_issue", "work_list_prs"}, (
        f"Issue PREFIX_COEXIST: git-work exposed names should be prefixed, got {work_names}"
    )
    assert personal_names == {
        "personal_search_repos",
        "personal_create_issue",
        "personal_list_prs",
    }, (
        f"Issue PREFIX_COEXIST: git-personal exposed names should be prefixed, got {personal_names}"
    )

    # Verify raw names are preserved for routing
    for t in tools_work:
        assert t.raw_name in {"search_repos", "create_issue", "list_prs"}, (
            f"Issue PREFIX_COEXIST: raw_name should be downstream name, got {t.raw_name}"
        )

    # Verify no conflict when combined
    all_tools = {"git-work": tools_work, "git-personal": tools_personal}
    conflicts = detect_conflicts(all_tools)
    assert len(conflicts) == 0, (
        f"Issue PREFIX_COEXIST: no conflicts expected, got {len(conflicts)}: {conflicts}"
    )
    print(
        "PASS: Two servers with distinct prefixes expose different names, no conflict"
    )


def test_two_servers_same_prefix_produces_conflict():
    """Two servers with same prefix expose conflicting tool names.

    When two servers use the same tool_prefix and advertise the same raw tool,
    the exposed names collide. This is detected as a conflict.

    Evidence:
      - Both servers use tool_prefix="fs_"
      - Both advertise "read_file"
      - Exposed names are identical: "fs_read_file"
      - detect_conflicts reports NAME_COLLISION
    """
    cfg_a = ServerConfig(name="fs-a", command="cmd", tool_prefix="fs_")
    cfg_b = ServerConfig(name="fs-b", command="cmd", tool_prefix="fs_")

    tools_a = resolve_tools("fs-a", cfg_a, DOWNSTREAM_TOOLS_FS)
    tools_b = resolve_tools("fs-b", cfg_b, DOWNSTREAM_TOOLS_FS)

    # Both expose prefixed names
    assert tools_a[0].name == "fs_read_file", (
        f"Issue PREFIX_CONFLICT: fs-a should expose 'fs_read_file', got {tools_a[0].name}"
    )
    assert tools_b[0].name == "fs_read_file", (
        f"Issue PREFIX_CONFLICT: fs-b should expose 'fs_read_file', got {tools_b[0].name}"
    )

    # Conflict detection finds the collision
    all_tools = {"fs-a": tools_a, "fs-b": tools_b}
    conflicts = detect_conflicts(all_tools)

    assert len(conflicts) == 3, (
        f"Issue PREFIX_CONFLICT: expected 3 conflicts (one per tool name), got {len(conflicts)}"
    )

    conflict_names = {c.tool_name for c in conflicts}
    expected_conflicts = {"fs_read_file", "fs_write_file", "fs_delete_file"}
    assert conflict_names == expected_conflicts, (
        f"Issue PREFIX_CONFLICT: expected conflicts {expected_conflicts}, got {conflict_names}"
    )

    for c in conflicts:
        assert c.conflict_type == ConflictType.NAME_COLLISION, (
            f"Issue PREFIX_CONFLICT: expected NAME_COLLISION, got {c.conflict_type}"
        )

    print("PASS: Same prefix with same raw tools produces NAME_COLLISION conflicts")


# ---------------------------------------------------------------------------
# Verification Point 2: Prefix routing (raw_name vs exposed name)
# ---------------------------------------------------------------------------


def test_resolved_tool_carries_raw_name_for_downstream_routing():
    """ResolvedTool has both exposed name (for upstream) and raw_name (for downstream).

    Spec: docs/USAGE.md lines 116-119
      - tool_overrides keys remain raw downstream names, not prefixed names
      - Prefix changes the exposed name (name field)
      - raw_name preserves the downstream-advertised name

    Evidence:
      - Exposed name (name): prefixed, used in tools/list
      - Raw name (raw_name): downstream tool name, used for routing
    """
    cfg = ServerConfig(name="fs-prod", command="cmd", tool_prefix="prod_")

    tools = resolve_tools("fs-prod", cfg, DOWNSTREAM_TOOLS_FS)

    for t in tools:
        # Exposed name is prefixed
        assert t.name.startswith("prod_"), (
            f"Issue PREFIX_ROUTING: exposed name should start with 'prod_', got {t.name}"
        )
        # Raw name is unprefixed
        assert not t.raw_name.startswith("prod_"), (
            f"Issue PREFIX_ROUTING: raw_name should not be prefixed, got {t.raw_name}"
        )
        # raw_name matches the downstream name
        assert t.raw_name in {"read_file", "write_file", "delete_file"}, (
            f"Issue PREFIX_ROUTING: raw_name should be downstream name, got {t.raw_name}"
        )
        # server_name is preserved
        assert t.server_name == "fs-prod", (
            f"Issue PREFIX_ROUTING: server_name should be preserved, got {t.server_name}"
        )

    print("PASS: ResolvedTool has correct name (exposed) and raw_name (downstream)")


# ---------------------------------------------------------------------------
# Verification Point 3: Backward compatibility (no prefix)
# ---------------------------------------------------------------------------


def test_omitted_tool_prefix_preserves_raw_name_as_exposed():
    """When tool_prefix is omitted (None/raw/missing), raw_name equals exposed name.

    Spec: docs/USAGE.md lines 125-126
      - tool_prefix: null (or omitted) preserves backward-compatible exposed names

    Evidence:
      - ServerConfig without tool_prefix
      - Exposed name equals raw_name
      - This is the default behavior
    """
    # Explicit None
    cfg_none = ServerConfig(name="fs", command="cmd", tool_prefix=None)
    tools_none = resolve_tools("fs", cfg_none, DOWNSTREAM_TOOLS_FS)

    # Implicit missing (default)
    cfg_default = ServerConfig(name="fs", command="cmd")  # no tool_prefix specified
    tools_default = resolve_tools("fs", cfg_default, DOWNSTREAM_TOOLS_FS)

    # Both should have raw_name == exposed name
    for t in tools_none:
        assert t.name == t.raw_name, (
            f"Issue BACKWARD_COMPAT: without prefix, name should equal raw_name, "
            f"got name={t.name}, raw_name={t.raw_name}"
        )

    for t in tools_default:
        assert t.name == t.raw_name, (
            f"Issue BACKWARD_COMPAT: default (no tool_prefix), name should equal raw_name, "
            f"got name={t.name}, raw_name={t.raw_name}"
        )

    print("PASS: Omitted tool_prefix preserves backward compatibility")


# ---------------------------------------------------------------------------
# Verification Point 4: Reserved prefix rejection
# ---------------------------------------------------------------------------


def test_tela_prefix_is_reserved_and_rejected():
    """Reserved tela namespaces must be rejected.

    Spec: docs/USAGE.md line 126
      - `tool_prefix="tela."` and `tool_prefix="tela_"` are reserved

    Authority: model-level validation in ServerConfig mirrors config-level
    validate_config() and resolve-time reject in resolve_tools().

    Evidence: ServerConfig raises ValidationError for reserved tela prefixes.
    """
    from pydantic import ValidationError

    # "tela." with trailing dot must be rejected at construction
    with pytest.raises(ValidationError, match="[Tt]ela"):
        ServerConfig(name="fs", command="cmd", tool_prefix="tela.")

    # "tela_" prefix (underscore form) is also reserved
    with pytest.raises(ValidationError, match="[Tt]ela"):
        ServerConfig(name="fs", command="cmd", tool_prefix="tela_")


def test_tela_prefix_without_dot_is_accepted():
    """tool_prefix="tela" (without trailing delimiter) is accepted.

    Per authoritative spec (USAGE.md tool_prefix contract) and runtime implementation
    (family.py resolve_tools, config.py validate_config), only "tela." and
    "tela_" prefixed values are reserved. Plain "tela" (no delimiter) does
    not produce exposed names in the reserved "tela." or "tela_" namespace
    and is therefore accepted.

    Contrast: tool_prefix="tela" produces names like "telaread_file", which
    does not collide with the "tela." namespace used by built-in tools such
    as tela_list_profiles and tela_list_providers.
    """
    # Plain "tela" (no dot, no underscore) is accepted
    cfg = ServerConfig(name="fs", command="cmd", tool_prefix="tela")
    assert cfg.tool_prefix == "tela"


# ---------------------------------------------------------------------------
# Verification Point 5: tool_overrides match raw names
# ---------------------------------------------------------------------------


def test_tool_overrides_match_raw_names_not_prefixed_names():
    """tool_overrides keys remain raw downstream names even with prefix.

    Spec: docs/USAGE.md lines 116-119
      - tool_overrides in profiles still reference raw downstream names
      - Even when tool_prefix changes the exposed name

    Evidence:
      - tool_overrides dict keys are raw names
      - Posture resolved via raw_name lookup
    """
    cfg = ServerConfig(
        name="fs",
        command="cmd",
        tool_prefix="prod_",
        tool_overrides={
            # Key is raw downstream name, NOT "prod_delete_file"
            "delete_file": ToolOverride(posture=Posture.DESTRUCTIVE),
        },
    )

    tools = resolve_tools("fs", cfg, DOWNSTREAM_TOOLS_FS)

    # Find the delete_file tool (exposed as "prod_delete_file")
    delete_tool = next((t for t in tools if t.raw_name == "delete_file"), None)
    assert delete_tool is not None, "delete_file tool should exist"
    assert delete_tool.name == "prod_delete_file", (
        f"Issue OVERRIDE_KEY: exposed name should be 'prod_delete_file', got {delete_tool.name}"
    )
    assert delete_tool.posture == Posture.DESTRUCTIVE, (
        f"Issue OVERRIDE_KEY: posture should be destructive from override, got {delete_tool.posture}"
    )

    # Verify the override was keyed by raw name
    assert "prod_delete_file" not in cfg.tool_overrides, (
        "Issue OVERRIDE_KEY: override key should NOT be prefixed name"
    )
    assert "delete_file" in cfg.tool_overrides, (
        "Issue OVERRIDE_KEY: override key should be raw name"
    )

    print("PASS: tool_overrides matched via raw_name, not prefixed name")


# ---------------------------------------------------------------------------
# Verification Point 6: Exposed name conflict detection
# ---------------------------------------------------------------------------


def test_conflict_detection_keys_off_exposed_name():
    """detect_conflicts uses the exposed name (ResolvedTool.name) for conflict detection.

    Spec: Per conflict.py doctests and contract
      - Conflict detection keys off exposed upstream name
      - Two servers with same exposed name = conflict

    Evidence:
      - Servers with different prefixes have distinct exposed names
      - Servers with same prefix and same raw tool = conflict
    """
    # Server A: work_search_repos
    cfg_a = ServerConfig(name="git-work", command="cmd", tool_prefix="work_")
    tools_a = resolve_tools("git-work", cfg_a, DOWNSTREAM_TOOLS_GITHUB)

    # Server B: personal_search_repos (distinct prefix, same raw tool)
    cfg_b = ServerConfig(name="git-personal", command="cmd", tool_prefix="personal_")
    tools_b = resolve_tools("git-personal", cfg_b, DOWNSTREAM_TOOLS_GITHUB)

    # No conflict because exposed names are different
    all_tools_distinct = {"git-work": tools_a, "git-personal": tools_b}
    conflicts_distinct = detect_conflicts(all_tools_distinct)
    assert len(conflicts_distinct) == 0, (
        f"Issue CONFLICT_KEY: distinct prefixes should not conflict, got {conflicts_distinct}"
    )

    # Server C: work_search_repos (same prefix as A)
    cfg_c = ServerConfig(name="git-another", command="cmd", tool_prefix="work_")
    tools_c = resolve_tools("git-another", cfg_c, DOWNSTREAM_TOOLS_GITHUB)

    # Conflict because exposed names are identical
    all_tools_conflict = {"git-work": tools_a, "git-another": tools_c}
    conflicts_same = detect_conflicts(all_tools_conflict)
    assert len(conflicts_same) == 3, (
        f"Issue CONFLICT_KEY: same prefix should conflict, got {len(conflicts_same)} conflicts"
    )

    conflict_names = {c.tool_name for c in conflicts_same}
    assert "work_search_repos" in conflict_names, (
        f"Issue CONFLICT_KEY: 'work_search_repos' should be in conflicts, got {conflict_names}"
    )

    print("PASS: detect_conflicts keys off exposed name (name field)")


# ---------------------------------------------------------------------------
# Verification Point 7: Reserved prefix rejection at resolution time
# ---------------------------------------------------------------------------


def test_downstream_tela_prefix_rejected_at_resolution():
    """A downstream tool named "tela.something" is rejected at resolve_tools time.

    Per IMPLEMENTATION: resolve_tools enforces reserved prefix rejection.
    When a downstream tool name starts with "tela.", resolve_tools raises ValueError.

    Spec: docs/USAGE.md line 126 and tela.yaml.example line 64
      - tool_prefix="tela." is reserved and will be rejected

    Evidence:
      - Downstream advertises "tela.custom" as raw name
      - resolve_tools raises ValueError for reserved prefix violation
    """
    downstream_tools_with_tela = [
        {"name": "tela.custom", "inputSchema": {"type": "object"}},
        {"name": "normal_tool", "inputSchema": {"type": "object"}},
    ]

    cfg = ServerConfig(name="srv", command="cmd")  # no prefix

    # resolve_tools should reject the reserved prefix
    try:
        tools = resolve_tools("srv", cfg, downstream_tools_with_tela)
        # If we reach here, the rejection is NOT implemented
        raise AssertionError(
            f"Issue RESERVED_PREFIX: resolve_tools should reject 'tela.custom' "
            f"but returned {len(tools)} tools: {[t.name for t in tools]}"
        )
    except ValueError as e:
        assert "tela" in str(e).lower() or "reserved" in str(e).lower(), (
            f"Expected error message about 'tela' or 'reserved', got: {e}"
        )
        print(f"PASS: resolve_tools rejected 'tela.custom' with: {e}")

    print("PASS: Downstream reserved dotted tela prefix rejected at resolution time")


def test_prefix_combination_producing_tela_namespace_rejected():
    """Prefix + downstream combination that produces "tela.xxx" is rejected.

    Evidence:
      - tool_prefix="tel" + downstream tool "a.custom" → exposed name "tela.custom"
      - This should be rejected at resolve_tools time

    Note: This tests the composition path, not just the direct "tela." suffix.
    """
    downstream_tools = [
        {"name": "a.custom", "inputSchema": {"type": "object"}},
    ]

    # prefix "tel" + "a.custom" = "tela.custom"
    cfg = ServerConfig(name="srv", command="cmd", tool_prefix="tel")

    try:
        tools = resolve_tools("srv", cfg, downstream_tools)
        # If we reach here, the rejection is NOT implemented
        exposed_names = [t.name for t in tools]
        if exposed_names and exposed_names[0].startswith("tela."):
            raise AssertionError(
                f"Issue RESERVED_PREFIX: prefix 'tel' + 'a.custom' produces "
                f"'tela.custom' which should be rejected. Got: {exposed_names}"
            )
        # If exposed name doesn't start with tela, test passes
        print(f"PASS: prefix 'tel' + 'a.custom' = '{exposed_names[0]}' (not tela.)")
    except ValueError as e:
        assert "tela" in str(e).lower() or "reserved" in str(e).lower(), (
            f"Expected error message about 'tela' or 'reserved', got: {e}"
        )
        print(
            "PASS: resolve_tools rejected prefix combination producing 'tela.' namespace"
        )


# ---------------------------------------------------------------------------
# Verification Point 8: Reserved prefix rejection in ServerConfig
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main():
    """Run all black-box verification tests."""
    print("=" * 70)
    print("Black-Box Verification: tool_prefix.deep_review.black_box")
    print("=" * 70)

    tests = [
        (
            "prefix_coexistence",
            test_two_servers_same_raw_tool_with_distinct_prefixes_coexist,
        ),
        ("prefix_conflict", test_two_servers_same_prefix_produces_conflict),
        ("prefix_routing", test_resolved_tool_carries_raw_name_for_downstream_routing),
        ("backward_compat", test_omitted_tool_prefix_preserves_raw_name_as_exposed),
        ("reserved_prefix_dot", test_tela_prefix_is_reserved_and_rejected),
        ("reserved_prefix_no_dot_accepted", test_tela_prefix_without_dot_is_accepted),
        ("tool_overrides", test_tool_overrides_match_raw_names_not_prefixed_names),
        ("conflict_detection", test_conflict_detection_keys_off_exposed_name),
        ("reserved_downstream", test_downstream_tela_prefix_rejected_at_resolution),
        (
            "reserved_combination",
            test_prefix_combination_producing_tela_namespace_rejected,
        ),
    ]

    results = {}
    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            results[name] = "PASS"
            passed += 1
        except AssertionError as e:
            results[name] = f"FAIL: {e}"
            failed += 1
            print(f"FAIL[{name}]: {e}")
        except Exception as e:
            results[name] = f"ERROR: {e}"
            failed += 1
            print(f"ERROR[{name}]: {e}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for name, result in sorted(results.items()):
        status = result.split(":")[0]
        print(f"  {name}: {status}")

    print(f"\nPassed: {passed}, Failed: {failed}")

    if failed == 0:
        print("\nVERDICT: PASS")
        return 0
    else:
        print("\nVERDICT: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
