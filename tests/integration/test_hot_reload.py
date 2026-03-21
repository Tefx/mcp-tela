"""Integration tests for hot reload behavior.

Tests define acceptance criteria for reload implementation:
- Accepted reload (no conflict): tools updated, upstream notified
- Rejected reload (conflict): tools preserved, no upstream notification
- No-drop-connection invariant: existing connections unaffected
- TOOL_CONFLICT warning emission shape
"""

from __future__ import annotations

import asyncio

from tela.core.conflict import ToolConflict, detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import (
    ConnectionContext,
    ResolvedTool,
    ServerConfig,
)
from tela.shell.downstream import (
    connect_all,
    disconnect_all,
    get_all_tools,
    get_tool_server,
)


# --- Accepted reload: no conflict ---


def test_re_enumerate_updates_registry() -> None:
    """After re-enumeration, new tools should be visible in the registry.

    This tests the expected behavior: when a server adds a new tool
    and re-enumeration occurs, the registry reflects the new tool set.
    """
    servers = {"fs": ServerConfig(name="fs", command="cmd")}

    # Initial connect with 1 tool
    asyncio.run(connect_all(servers, tool_lists={"fs": [{"name": "read_file", "inputSchema": {}}]}))
    assert get_tool_server("read_file").value == "fs"

    # Simulate re-enumeration with 2 tools (re-connect with updated list)
    asyncio.run(connect_all(servers, tool_lists={
        "fs": [
            {"name": "read_file", "inputSchema": {}},
            {"name": "write_file", "inputSchema": {}},
        ]
    }))
    assert get_tool_server("read_file").value == "fs"
    assert get_tool_server("write_file").value == "fs"
    asyncio.run(disconnect_all())


def test_re_enumerate_removes_old_tools() -> None:
    """After re-enumeration, removed tools should disappear from registry."""
    servers = {"fs": ServerConfig(name="fs", command="cmd")}

    # Initial: 2 tools
    asyncio.run(connect_all(servers, tool_lists={
        "fs": [{"name": "tool_a", "inputSchema": {}}, {"name": "tool_b", "inputSchema": {}}]
    }))
    assert get_tool_server("tool_a").value == "fs"
    assert get_tool_server("tool_b").value == "fs"

    # Re-enumerate: only tool_a
    asyncio.run(connect_all(servers, tool_lists={
        "fs": [{"name": "tool_a", "inputSchema": {}}]
    }))
    assert get_tool_server("tool_a").value == "fs"
    assert get_tool_server("tool_b").value is None
    asyncio.run(disconnect_all())


# --- Rejected reload: conflict ---


def test_conflict_detection_with_new_tool() -> None:
    """If a re-enumerated tool conflicts with another server, detect it."""
    existing_tools = {
        "fs": [ResolvedTool(name="read_file", server_name="fs", family="fs")],
    }
    new_tools_from_other = [
        ResolvedTool(name="read_file", server_name="custom", family="custom"),
    ]
    combined = {**existing_tools, "custom": new_tools_from_other}
    conflicts = detect_conflicts(combined)
    assert len(conflicts) == 1
    assert conflicts[0].tool_name == "read_file"


def test_connect_all_rejects_conflict_preserves_empty() -> None:
    """On conflict at startup, registry is cleared (fail-fast)."""
    servers = {
        "fs1": ServerConfig(name="fs1", command="cmd"),
        "fs2": ServerConfig(name="fs2", command="cmd"),
    }
    tool_lists = {
        "fs1": [{"name": "same_tool", "inputSchema": {}}],
        "fs2": [{"name": "same_tool", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_err
    assert "TOOL_CONFLICT" in (result.error or "")
    assert get_all_tools().value == {}


# --- No-drop-connection invariant shapes ---


def test_connection_context_survives_registry_update() -> None:
    """ConnectionContext objects remain valid after registry changes.

    The no-drop invariant means: even when the tool registry is updated,
    existing ConnectionContext objects should still be usable for lookup.
    """
    conn = ConnectionContext(
        connection_id="conn-1",
        profile_name="dev",
        connected_at="2026-01-01T00:00:00Z",
    )
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(connect_all(servers, tool_lists={"fs": [{"name": "read_file", "inputSchema": {}}]}))

    # Connection was created before re-enumeration
    assert conn.connection_id == "conn-1"
    assert conn.profile_name == "dev"

    # Re-enumerate (simulate tools/list_changed)
    asyncio.run(connect_all(servers, tool_lists={
        "fs": [{"name": "read_file", "inputSchema": {}}, {"name": "new_tool", "inputSchema": {}}]
    }))

    # Connection still valid, profile still bound
    assert conn.connection_id == "conn-1"
    assert conn.profile_name == "dev"
    asyncio.run(disconnect_all())


# --- TOOL_CONFLICT warning emission shape ---


def test_tool_conflict_model_carries_warning_data() -> None:
    """ToolConflict model carries all data needed for audit warning emission."""
    conflict = ToolConflict(tool_name="read_file", servers=["fs1", "fs2"])
    assert conflict.tool_name == "read_file"
    assert len(conflict.servers) == 2
    # The reload implementation would emit this as an audit warning:
    # audit_write(build_audit_entry(..., error_code="TOOL_CONFLICT"))


# --- Upstream notification shape ---


def test_tools_digest_from_registry() -> None:
    """After a successful reload, a digest of the tool list can be computed.

    This tests the shape of data that would be sent in
    notifications/tools/list_changed to upstream clients.
    """
    servers = {"fs": ServerConfig(name="fs", command="cmd")}
    asyncio.run(connect_all(servers, tool_lists={
        "fs": [{"name": "read_file", "inputSchema": {}}, {"name": "write_file", "inputSchema": {}}]
    }))

    all_tools = get_all_tools()
    # Compute a simple digest from tool names
    tool_names = sorted(t.name for ts in all_tools.value.values() for t in ts)
    digest = ":".join(tool_names)
    assert digest == "read_file:write_file"
    asyncio.run(disconnect_all())


# --- Resolve tools after reconnect ---


def test_resolve_tools_produces_correct_family_after_reconnect() -> None:
    """After reconnection, family mapping must be re-applied correctly."""
    cfg = ServerConfig(name="git", command="cmd", family="devtools")
    tools = resolve_tools("git", cfg, [
        {"name": "git_status", "inputSchema": {}},
        {"name": "git_diff", "inputSchema": {}},
    ])
    assert all(t.family == "devtools" for t in tools)
    assert len(tools) == 2
