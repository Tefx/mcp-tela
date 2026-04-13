"""Runtime tests for downstream server management.

Tests cover:
- Tool registry lookup behavior (connect, enumerate, lookup, disconnect)
- ServerConfig shapes for stdio and SSE servers
- ResolvedTool model behavior and fields
- Tool conflict detection at startup
- Registry lifecycle (connect_all, disconnect_all)
- Downstream client lifecycle: stdio/SSE connection setup
- connect_all/disconnect_all session semantics
- MCP tools/list enumeration behavior
- Empty/failure registry handling
- Teardown cleanup semantics
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tela.core.models import (
    Posture,
    ResolvedTool,
    ServerConfig,
    TelaConfig,
    ToolOverride,
)
from tela.shell.downstream import (
    call_tool,
    connect_all,
    disconnect_all,
    get_all_tools,
    get_registry,
    get_tool_server,
    re_enumerate,
)
from tela.shell.gateway_runtime import get_runtime_config, set_runtime_config


# --- Fixtures for stdio and SSE server configurations ---


@pytest.fixture
def stdio_server_config() -> ServerConfig:
    """ServerConfig for a stdio-based downstream MCP server.

    Per INTERFACES.md section 9.1:
    - stdio server contract: ServerConfig.command is required
    - client connect uses command, args, and env from config
    """
    return ServerConfig(
        name="filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        env={"NODE_ENV": "production", "LOG_LEVEL": "info"},
    )


@pytest.fixture
def sse_server_config() -> ServerConfig:
    """ServerConfig for an SSE-based downstream MCP server.

    Per INTERFACES.md section 9.1:
    - SSE server contract: ServerConfig.url is required
    - client connect uses url
    """
    return ServerConfig(
        name="remote_service",
        url="http://localhost:8080/sse",
        transport="sse",
    )


@pytest.fixture
def minimal_stdio_config() -> ServerConfig:
    """Minimal stdio config with command only, no args or env."""
    return ServerConfig(
        name="minimal_stdio",
        command="/usr/local/bin/mcp-server",
    )


@pytest.fixture
def http_server_config() -> ServerConfig:
    """ServerConfig for a Streamable HTTP downstream MCP server."""
    return ServerConfig(
        name="remote_http",
        url="http://localhost:8080/mcp",
        transport="http",
    )


@pytest.fixture
def minimal_sse_config() -> ServerConfig:
    """Minimal SSE config with url and explicit transport."""
    return ServerConfig(
        name="minimal_sse",
        url="http://host:9999/mcp",
        transport="sse",
    )


# --- ServerConfig model tests ---


def test_server_config_stdio_shape() -> None:
    config = ServerConfig(
        name="filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )
    assert config.command == "npx"
    assert config.url is None
    assert len(config.args) == 3
    assert config.default_posture == Posture.NONE


def test_server_config_sse_shape() -> None:
    config = ServerConfig(name="remote", url="http://localhost:8080/sse")
    assert config.url == "http://localhost:8080/sse"
    assert config.command is None


def test_server_config_with_tool_overrides() -> None:
    config = ServerConfig(
        name="srv",
        command="cmd",
        tool_overrides={
            "dangerous_tool": ToolOverride(posture=Posture.DESTRUCTIVE),
            "reassigned_tool": ToolOverride(family="custom_family"),
        },
    )
    assert config.tool_overrides["dangerous_tool"].posture == Posture.DESTRUCTIVE
    assert config.tool_overrides["reassigned_tool"].family == "custom_family"


def test_server_config_explicit_family_override() -> None:
    config = ServerConfig(name="srv", command="cmd", family="custom_family")
    assert config.family == "custom_family"


def test_server_config_default_posture_options() -> None:
    for posture in Posture:
        config = ServerConfig(name="srv", command="cmd", default_posture=posture)
        assert config.default_posture == posture


# --- ResolvedTool model tests ---


def test_resolved_tool_carries_server_and_family() -> None:
    tool = ResolvedTool(
        name="read_file",
        server_name="filesystem",
        family="filesystem",
        posture=Posture.READ_ONLY,
        schema_={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    assert tool.name == "read_file"
    assert tool.server_name == "filesystem"
    assert tool.family == "filesystem"
    assert tool.posture == Posture.READ_ONLY


def test_resolved_tool_unclassified_posture() -> None:
    tool = ResolvedTool(name="unknown_tool", server_name="srv", family="srv")
    assert tool.posture is None
    assert tool.schema_ == {}


def test_resolved_tool_registry_grouping() -> None:
    tools = {
        "filesystem": [
            ResolvedTool(
                name="read_file", server_name="filesystem", family="filesystem"
            ),
            ResolvedTool(
                name="write_file", server_name="filesystem", family="filesystem"
            ),
        ],
        "git": [
            ResolvedTool(name="git_status", server_name="git", family="git"),
        ],
    }
    flat = {t.name: srv for srv, ts in tools.items() for t in ts}
    assert flat["read_file"] == "filesystem"
    assert flat["git_status"] == "git"


# --- connect_all / disconnect_all / registry lifecycle ---


def test_connect_all_registers_tools() -> None:
    """connect_all populates the registry with resolved tools."""
    servers = {
        "fs": ServerConfig(name="fs", command="cmd"),
        "git": ServerConfig(name="git", command="cmd"),
    }
    tool_lists = {
        "fs": [
            {"name": "read_file", "inputSchema": {"type": "object"}},
            {"name": "write_file", "inputSchema": {"type": "object"}},
        ],
        "git": [
            {"name": "git_status", "inputSchema": {}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    assert get_tool_server("read_file").value == "fs"
    assert get_tool_server("write_file").value == "fs"
    assert get_tool_server("git_status").value == "git"
    assert get_tool_server("nonexistent").value is None

    all_tools_result = get_all_tools()
    assert all_tools_result.value is not None
    all_tools = all_tools_result.value
    assert len(all_tools["fs"]) == 2
    assert len(all_tools["git"]) == 1


def test_connect_all_resolves_families() -> None:
    """connect_all uses Core family resolution for tool family assignment."""
    servers = {
        "srv": ServerConfig(
            name="srv",
            command="cmd",
            family="custom_family",
            tool_overrides={"special": ToolOverride(family="override_family")},
        ),
    }
    tool_lists = {
        "srv": [
            {"name": "normal_tool", "inputSchema": {}},
            {"name": "special", "inputSchema": {}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    normal = registry.get_tool("normal_tool")
    assert normal is not None
    assert normal.family == "custom_family"

    special = registry.get_tool("special")
    assert special is not None
    assert special.family == "override_family"


def test_connect_all_resolves_posture_from_overrides() -> None:
    """connect_all uses Core classification for posture from tool overrides."""
    servers = {
        "srv": ServerConfig(
            name="srv",
            command="cmd",
            tool_overrides={"dangerous": ToolOverride(posture=Posture.DESTRUCTIVE)},
        ),
    }
    tool_lists = {
        "srv": [
            {"name": "dangerous", "inputSchema": {}},
            {"name": "unclassified", "inputSchema": {}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    dangerous = registry.get_tool("dangerous")
    assert dangerous is not None
    assert dangerous.posture == Posture.DESTRUCTIVE
    unclassified = registry.get_tool("unclassified")
    assert unclassified is not None
    assert unclassified.posture is None


def test_connect_all_resolves_posture_from_annotations() -> None:
    """connect_all uses Core classification for posture from MCP annotations."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {
        "srv": [
            {
                "name": "reader",
                "inputSchema": {},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "destroyer",
                "inputSchema": {},
                "annotations": {"destructiveHint": True},
            },
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    reader = registry.get_tool("reader")
    assert reader is not None
    assert reader.posture == Posture.READ_ONLY
    destroyer = registry.get_tool("destroyer")
    assert destroyer is not None
    assert destroyer.posture == Posture.DESTRUCTIVE


def test_connect_all_fails_on_tool_conflict() -> None:
    """connect_all fails fast when two servers expose the same tool name."""
    servers = {
        "fs1": ServerConfig(name="fs1", command="cmd1"),
        "fs2": ServerConfig(name="fs2", command="cmd2"),
    }
    tool_lists = {
        "fs1": [{"name": "read_file", "inputSchema": {}}],
        "fs2": [{"name": "read_file", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_err
    assert "TOOL_CONFLICT" in (result.error or "")
    assert "read_file" in (result.error or "")

    # Registry must be cleared on conflict
    assert get_all_tools().value == {}


def test_connect_all_no_conflict_different_names() -> None:
    """connect_all succeeds when servers have unique tool names."""
    servers = {
        "fs": ServerConfig(name="fs", command="cmd1"),
        "git": ServerConfig(name="git", command="cmd2"),
    }
    tool_lists = {
        "fs": [{"name": "read_file", "inputSchema": {}}],
        "git": [{"name": "git_status", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok


def test_disconnect_all_clears_registry() -> None:
    """disconnect_all removes all tools from the registry."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {"srv": [{"name": "tool", "inputSchema": {}}]}
    asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert get_tool_server("tool").value == "srv"

    result = asyncio.run(disconnect_all())
    assert result.is_ok
    assert get_tool_server("tool").value is None
    assert get_all_tools().value == {}


def test_connect_all_empty_servers() -> None:
    """connect_all with no servers produces empty registry."""
    result = asyncio.run(connect_all({}))
    assert result.is_ok
    assert get_all_tools().value == {}


# --- Remaining stubs ---


def test_call_tool_returns_downstream_unavailable_when_not_connected() -> None:
    r = asyncio.run(call_tool("srv", "tool", {}))
    assert r.is_err
    assert r.error is not None
    assert r.error.code == "DOWNSTREAM_UNAVAILABLE"


def test_re_enumerate_returns_downstream_unavailable_when_not_connected() -> None:
    r = asyncio.run(re_enumerate("srv"))
    assert r.is_err
    assert r.error is not None
    assert r.error.startswith("DOWNSTREAM_UNAVAILABLE")


# --- Client lifecycle: stdio connection setup ---


def test_stdio_config_has_command_args_env(
    stdio_server_config: ServerConfig,
) -> None:
    """stdio ServerConfig has command, args, and env fields."""
    assert stdio_server_config.command == "npx"
    assert stdio_server_config.url is None
    assert len(stdio_server_config.args) == 3
    assert stdio_server_config.env == {"NODE_ENV": "production", "LOG_LEVEL": "info"}


def test_minimal_stdio_config_command_only(
    minimal_stdio_config: ServerConfig,
) -> None:
    """Minimal stdio config requires only command; args/env default to empty."""
    assert minimal_stdio_config.command == "/usr/local/bin/mcp-server"
    assert minimal_stdio_config.args == []
    assert minimal_stdio_config.env == {}
    assert minimal_stdio_config.url is None


def test_stdio_connect_all_registers_tools_from_tool_lists(
    stdio_server_config: ServerConfig,
) -> None:
    """connect_all with stdio server config registers tools via tool_lists injection.

    Until actual MCP transport is wired, the tool_lists parameter is used to
    inject pre-enumerated tool lists for testing the registration contract.

    Per INTERFACES.md 9.3:
    - startup establishes session then enumerates and registers tools
    - invariant: _clients key exists iff server is currently connected
    """
    servers = {"filesystem": stdio_server_config}
    tool_lists = {
        "filesystem": [
            {"name": "read_file", "inputSchema": {"type": "object"}},
            {"name": "write_file", "inputSchema": {"type": "object"}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    # Registry should contain all tools from tool_lists
    registry = get_registry()
    tools = registry.get_all_tools()
    assert "filesystem" in tools
    assert len(tools["filesystem"]) == 2

    # Verify individual tool lookup
    tool = registry.get_tool("read_file")
    assert tool is not None
    assert tool.server_name == "filesystem"

    # Verify server lookup
    assert registry.get_tool_server("read_file") == "filesystem"

    # Cleanup
    asyncio.run(disconnect_all())


def test_stdio_config_env_field_propagates_to_config() -> None:
    """ServerConfig.env field is preserved for downstream client spawn.

    Per INTERFACES.md 9.1 and rt.env_field:
    - env field is dict[str, str]
    - omitted env defaults to {}
    - explicit env: {} is equivalent to omitting env
    """
    config = ServerConfig(
        name="server_with_env",
        command="python",
        args=["-m", "mcp_server"],
        env={"PYTHONPATH": "/app/src", "DEBUG": "1"},
    )
    assert config.env == {"PYTHONPATH": "/app/src", "DEBUG": "1"}


def test_stdio_config_empty_env_is_valid() -> None:
    """Empty env dict is valid and equivalent to omitting env."""
    config = ServerConfig(name="no_env", command="cmd", env={})
    assert config.env == {}


def test_stdio_config_missing_env_defaults_to_empty() -> None:
    """Omitting env field defaults to empty dict, not None."""
    config = ServerConfig(name="implicit_env", command="cmd")
    assert config.env == {}


# --- Client lifecycle: SSE connection setup ---


def test_sse_config_has_url(sse_server_config: ServerConfig) -> None:
    """SSE ServerConfig has url field."""
    assert sse_server_config.url == "http://localhost:8080/sse"
    assert sse_server_config.command is None


def test_http_config_has_url_and_transport(http_server_config: ServerConfig) -> None:
    """Streamable HTTP ServerConfig has url and transport='http'."""
    assert http_server_config.url == "http://localhost:8080/mcp"
    assert http_server_config.transport == "http"
    assert http_server_config.command is None


def test_server_config_rejects_invalid_transport() -> None:
    """ServerConfig.transport only accepts 'http', 'sse', or None."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ServerConfig(name="bad", url="http://localhost/mcp", transport="ftp")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        ServerConfig(name="bad", url="http://localhost/mcp", transport="websocket")  # type: ignore[arg-type]

    # Valid values
    ok_none = ServerConfig(name="ok", url="http://localhost/mcp")
    assert ok_none.transport is None

    ok_http = ServerConfig(name="ok", url="http://localhost/mcp", transport="http")
    assert ok_http.transport == "http"

    ok_sse = ServerConfig(name="ok", url="http://localhost/sse", transport="sse")
    assert ok_sse.transport == "sse"


def test_minimal_sse_config_url_only(
    minimal_sse_config: ServerConfig,
) -> None:
    """Minimal SSE config requires only url; args/env are not applicable."""
    assert minimal_sse_config.url == "http://host:9999/mcp"
    assert minimal_sse_config.command is None
    assert minimal_sse_config.args == []
    assert minimal_sse_config.env == {}


def test_sse_connect_all_registers_tools_from_tool_lists(
    sse_server_config: ServerConfig,
) -> None:
    """connect_all with SSE server config registers tools via tool_lists injection.

    Until actual MCP transport is wired (fastmcp), tool_lists provides the
    enumeration result for testing the registration contract.
    """
    servers = {"remote_service": sse_server_config}
    tool_lists = {
        "remote_service": [
            {"name": "fetch_data", "inputSchema": {"type": "object"}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    tools = registry.get_all_tools()
    assert "remote_service" in tools
    assert len(tools["remote_service"]) == 1

    tool = registry.get_tool("fetch_data")
    assert tool is not None
    assert tool.server_name == "remote_service"

    asyncio.run(disconnect_all())


def test_mixed_stdio_and_sse_servers_in_connect_all(
    stdio_server_config: ServerConfig,
    sse_server_config: ServerConfig,
) -> None:
    """connect_all supports mixed stdio and SSE server configurations.

    Per INTERFACES.md 9.1:
    - each server defines exactly one transport (command OR url)
    - both stdio and SSE servers can coexist in server registry
    """
    servers = {
        "filesystem": stdio_server_config,
        "remote_service": sse_server_config,
    }
    tool_lists = {
        "filesystem": [{"name": "read_file", "inputSchema": {}}],
        "remote_service": [{"name": "fetch_data", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    all_tools = registry.get_all_tools()
    assert len(all_tools) == 2
    assert "filesystem" in all_tools
    assert "remote_service" in all_tools

    asyncio.run(disconnect_all())


def test_http_connect_all_registers_tools_from_tool_lists(
    http_server_config: ServerConfig,
) -> None:
    """connect_all with Streamable HTTP server config registers tools."""
    servers = {"remote_http": http_server_config}
    tool_lists = {
        "remote_http": [
            {"name": "query_api", "inputSchema": {"type": "object"}},
        ],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    tools = registry.get_all_tools()
    assert "remote_http" in tools
    assert len(tools["remote_http"]) == 1

    tool = registry.get_tool("query_api")
    assert tool is not None
    assert tool.server_name == "remote_http"

    asyncio.run(disconnect_all())


def test_mixed_stdio_sse_and_http_servers_in_connect_all(
    stdio_server_config: ServerConfig,
    sse_server_config: ServerConfig,
    http_server_config: ServerConfig,
) -> None:
    """connect_all supports mixed stdio, SSE, and HTTP server configurations."""
    servers = {
        "filesystem": stdio_server_config,
        "remote_sse": sse_server_config,
        "remote_http": http_server_config,
    }
    tool_lists = {
        "filesystem": [{"name": "read_file", "inputSchema": {}}],
        "remote_sse": [{"name": "fetch_data", "inputSchema": {}}],
        "remote_http": [{"name": "query_api", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    all_tools = registry.get_all_tools()
    assert len(all_tools) == 3
    assert "filesystem" in all_tools
    assert "remote_sse" in all_tools
    assert "remote_http" in all_tools

    asyncio.run(disconnect_all())


# --- connect_all / disconnect_all session lifecycle ---


def test_connect_all_returns_result_ok_on_success() -> None:
    """connect_all returns Result.ok on successful tool enumeration."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {"srv": [{"name": "tool1", "inputSchema": {}}]}
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok
    assert result.value is None  # Result[None, str] returns None on success


def test_connect_all_returns_error_on_conflict() -> None:
    """connect_all returns Result.err with TOOL_CONFLICT on duplicate tool names."""
    servers = {
        "s1": ServerConfig(name="s1", command="cmd1"),
        "s2": ServerConfig(name="s2", command="cmd2"),
    }
    tool_lists = {
        "s1": [{"name": "dup_tool", "inputSchema": {}}],
        "s2": [{"name": "dup_tool", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_err
    assert result.error is not None
    assert "TOOL_CONFLICT" in result.error


def test_disconnect_all_always_succeeds() -> None:
    """disconnect_all returns Result.ok even when registry is already empty."""
    result = asyncio.run(disconnect_all())
    assert result.is_ok

    # Calling again on empty registry also succeeds
    result2 = asyncio.run(disconnect_all())
    assert result2.is_ok


def test_disconnect_all_after_connect_clears_registry() -> None:
    """disconnect_all clears the registry after a successful connect_all."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {"srv": [{"name": "tool", "inputSchema": {}}]}

    asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert get_tool_server("tool").value == "srv"

    asyncio.run(disconnect_all())
    assert get_tool_server("tool").value is None
    assert get_all_tools().value == {}


def test_connect_all_clears_previous_state() -> None:
    """connect_all clears the registry before populating (no accumulation)."""
    # First connect
    servers1 = {"srv1": ServerConfig(name="srv1", command="cmd1")}
    tool_lists1 = {"srv1": [{"name": "tool_a", "inputSchema": {}}]}
    asyncio.run(connect_all(servers1, tool_lists=tool_lists1))
    assert get_tool_server("tool_a").value == "srv1"

    # Second connect with different server
    servers2 = {"srv2": ServerConfig(name="srv2", command="cmd2")}
    tool_lists2 = {"srv2": [{"name": "tool_b", "inputSchema": {}}]}
    asyncio.run(connect_all(servers2, tool_lists=tool_lists2))

    # Previous tools should be cleared
    assert get_tool_server("tool_a").value is None
    assert get_tool_server("tool_b").value == "srv2"

    asyncio.run(disconnect_all())


# --- MCP tools/list enumeration behavior ---


def test_tools_list_enumeration_populates_registry() -> None:
    """tools/list enumeration populates the tool registry with all tools.

    Per INTERFACES.md 9.3:
    - startup: establish sessions, then enumerate and register tools
    """
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {
        "srv": [
            {"name": "read", "inputSchema": {"type": "object"}},
            {"name": "write", "inputSchema": {"type": "object"}},
            {"name": "delete", "inputSchema": {"type": "object"}},
        ],
    }
    asyncio.run(connect_all(servers, tool_lists=tool_lists))

    registry = get_registry()
    assert registry.get_tool("read") is not None
    assert registry.get_tool("write") is not None
    assert registry.get_tool("delete") is not None

    asyncio.run(disconnect_all())


def test_tools_list_empty_for_server() -> None:
    """tools/list returning empty list results in empty tool set for that server."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists: dict[str, list[dict]] = {"srv": []}  # Empty tool list
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok

    registry = get_registry()
    tools = registry.get_all_tools()
    assert "srv" in tools
    assert tools["srv"] == []

    asyncio.run(disconnect_all())


def test_tools_list_with_schema_and_annotations() -> None:
    """tools/list carries schema and annotations for posture classification."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {
        "srv": [
            {
                "name": "read_only_tool",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "destructive_tool",
                "inputSchema": {"type": "object"},
                "annotations": {"destructiveHint": True},
            },
        ],
    }
    asyncio.run(connect_all(servers, tool_lists=tool_lists))

    registry = get_registry()
    read_tool = registry.get_tool("read_only_tool")
    dest_tool = registry.get_tool("destructive_tool")

    assert read_tool is not None
    assert read_tool.posture == Posture.READ_ONLY

    assert dest_tool is not None
    assert dest_tool.posture == Posture.DESTRUCTIVE

    asyncio.run(disconnect_all())


# --- Empty/failure registry handling ---


def test_connect_all_empty_server_dict() -> None:
    """connect_all with empty server dict returns success and empty registry."""
    result = asyncio.run(connect_all({}))
    assert result.is_ok
    assert result.value is None
    assert get_all_tools().value == {}


def test_registry_empty_after_failed_connect() -> None:
    """Registry is empty after connect_all fails due to conflict.

    Per INTERFACES.md 9.3:
    - failure during startup: close opened sessions, leave _clients empty
    - no partial connected state
    """
    servers = {
        "s1": ServerConfig(name="s1", command="cmd1"),
        "s2": ServerConfig(name="s2", command="cmd2"),
    }
    tool_lists = {
        "s1": [{"name": "conflict_tool", "inputSchema": {}}],
        "s2": [{"name": "conflict_tool", "inputSchema": {}}],
    }
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_err

    # Registry must be cleared on conflict
    assert get_all_tools().value == {}


def test_registry_snapshot_restore_mechanism() -> None:
    """DownstreamRegistry supports snapshot/restore for atomic rollback."""
    registry = get_registry()

    # Add some tools
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {"srv": [{"name": "tool1", "inputSchema": {}}]}
    asyncio.run(connect_all(servers, tool_lists=tool_lists))

    # Take snapshot
    snap = registry.snapshot()
    assert isinstance(snap, tuple)
    assert len(snap) == 2

    # Add more tools
    tool_lists2 = {
        "srv": [
            {"name": "tool1", "inputSchema": {}},
            {"name": "tool2", "inputSchema": {}},
        ]
    }
    asyncio.run(connect_all(servers, tool_lists=tool_lists2))
    assert registry.get_tool("tool2") is not None

    # Restore to snapshot (simulating rollback on conflict)
    registry.restore(snap)
    assert registry.get_tool("tool2") is None
    assert registry.get_tool("tool1") is not None

    asyncio.run(disconnect_all())


# --- Teardown cleanup semantics ---


def test_disconnect_all_is_idempotent() -> None:
    """disconnect_all can be called multiple times without error."""
    asyncio.run(disconnect_all())  # Empty
    asyncio.run(disconnect_all())  # Still empty
    asyncio.run(disconnect_all())  # Still empty

    result = asyncio.run(disconnect_all())
    assert result.is_ok


def test_disconnect_all_clears_all_servers() -> None:
    """disconnect_all clears all servers from registry regardless of count."""
    servers = {
        "s1": ServerConfig(name="s1", command="cmd1"),
        "s2": ServerConfig(name="s2", command="cmd2"),
        "s3": ServerConfig(name="s3", url="http://host/sse"),
    }
    tool_lists = {
        "s1": [{"name": "tool1", "inputSchema": {}}],
        "s2": [{"name": "tool2", "inputSchema": {}}],
        "s3": [{"name": "tool3", "inputSchema": {}}],
    }
    asyncio.run(connect_all(servers, tool_lists=tool_lists))

    all_tools_r = get_all_tools()
    assert all_tools_r.value is not None
    assert len(all_tools_r.value) == 3

    asyncio.run(disconnect_all())

    all_tools_r2 = get_all_tools()
    assert all_tools_r2.value is not None
    assert len(all_tools_r2.value) == 0


def test_connect_all_then_disconnect_all_is_clean_state() -> None:
    """Full lifecycle: connect_all -> tool enumeration -> disconnect_all -> clean state."""
    servers = {"srv": ServerConfig(name="srv", command="cmd")}
    tool_lists = {"srv": [{"name": "tool", "inputSchema": {}}]}

    # Connect
    result = asyncio.run(connect_all(servers, tool_lists=tool_lists))
    assert result.is_ok
    assert get_tool_server("tool").value == "srv"

    # Verify registry state
    registry = get_registry()
    tool = registry.get_tool("tool")
    assert tool is not None
    assert tool.name == "tool"
    assert tool.server_name == "srv"
    assert tool.family == "srv"  # Default family is server name

    # Disconnect
    result = asyncio.run(disconnect_all())
    assert result.is_ok

    # Verify clean state
    assert get_tool_server("tool").value is None
    assert registry.get_tool("tool") is None
    assert get_all_tools().value == {}


# --- Transport contract validation (INTERFACES.md 3.1, 9.1) ---


def test_transport_mixed_command_and_url_rejected_by_validate_config() -> None:
    """Mixed transport (both command and url) is rejected at validation boundary.

    Per INTERFACES.md sections 3.1 and 9.1:
    - Required transport choice: command OR url, not both
    - Mixed transport fields are invalid and must be rejected as config contract violation

    This test proves the authoritative validation path in `validate_config` enforces
    the contract, not permissive model construction.
    """
    from tela.core.config import validate_config
    from tela.core.models import (
        AuthConfig,
        AuthMode,
        ProfileConfig,
        ServerConfig,
        TelaConfig,
    )

    # Per spec 3.1: minimal server format is name + transport
    config = TelaConfig(
        servers={"bad": ServerConfig(name="bad", command="cmd", url="http://host/sse")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )
    errors = validate_config(config)

    # Contract boundary: validate_config must reject ambiguous transport
    assert len(errors) == 1
    assert "SERVER_AMBIGUOUS_TRANSPORT" in errors[0]
    assert "'bad'" in errors[0]


def test_transport_missing_both_command_and_url_rejected_by_validate_config() -> None:
    """Missing transport (neither command nor url) is rejected at validation boundary.

    Per INTERFACES.md sections 3.1 and 9.1:
    - Required transport choice: command OR url
    - A server without any transport is invalid

    This test proves the authoritative validation path in `validate_config` enforces
    the contract, not permissive model construction.
    """
    from tela.core.config import validate_config
    from tela.core.models import (
        AuthConfig,
        AuthMode,
        ProfileConfig,
        ServerConfig,
        TelaConfig,
    )

    # ServerConfig model is permissive at construction; validation happens at config boundary
    config = TelaConfig(
        servers={"bad": ServerConfig(name="bad")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )
    errors = validate_config(config)

    # Contract boundary: validate_config must reject missing transport
    assert len(errors) == 1
    assert "SERVER_MISSING_TRANSPORT" in errors[0]
    assert "'bad'" in errors[0]


def test_transport_command_only_is_valid_per_spec() -> None:
    """Server with only command is valid per INTERFACES.md 3.1 minimal server format.

    Per spec 3.1:
    - command for stdio
    - Minimal format: name + command (no convenience fields required)
    """
    from tela.core.config import validate_config
    from tela.core.models import (
        AuthConfig,
        AuthMode,
        ProfileConfig,
        ServerConfig,
        TelaConfig,
    )

    # Exact documented minimal server format: name + command
    config = TelaConfig(
        servers={"fs": ServerConfig(name="fs", command="npx")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )
    errors = validate_config(config)
    assert errors == []


def test_transport_url_only_is_valid_per_spec() -> None:
    """Server with only url is valid per INTERFACES.md 3.1 minimal server format.

    Per spec 3.1:
    - url for SSE
    - Minimal format: name + url (no convenience fields required)
    """
    from tela.core.config import validate_config
    from tela.core.models import (
        AuthConfig,
        AuthMode,
        ProfileConfig,
        ServerConfig,
        TelaConfig,
    )

    # Exact documented minimal server format: name + url
    config = TelaConfig(
        servers={"remote": ServerConfig(name="remote", url="http://host:8080/sse")},
        profiles={"dev": ProfileConfig(name="dev", default=True)},
        auth=AuthConfig(mode=AuthMode.OPEN),
    )
    errors = validate_config(config)
    assert errors == []


def test_transport_validation_via_load_config_path() -> None:
    """Transport validation is enforced through the full load_config path.

    Per INTERFACES.md 9.1: connect_all must reject mixed/missing transport as
    config/runtime contract violation. The validation happens in validate_config
    which is called by load_config.
    """
    from pathlib import Path
    from tela.shell.config_loader import load_config

    # Create temp config with missing transport
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(
            "servers:\n"
            "  bad:\n"
            "    name: bad\n"
            "profiles:\n"
            "  dev:\n"
            "    name: dev\n"
            "    default: true\n"
            "auth:\n"
            "  mode: open\n"
        )
        f.flush()
        config_path = Path(f.name)

    try:
        result = load_config(config_path)
        # Contract enforcement: load_config must return error for invalid config
        assert result.is_err
        assert "SERVER_MISSING_TRANSPORT" in (result.error or "")
    finally:
        config_path.unlink(missing_ok=True)


# --- Reconnect path: _handle_reconnect end-to-end tests ---
# Spec ref: docs/DESIGN.md Runtime Architecture / Connection lifecycle
# These tests verify the full reconnect flow does not trigger duplicate enumeration.


def test_handle_reconnect_calls_enumerate_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """_handle_reconnect must enumerate exactly once, not twice.

    Regression test: downstream reconnect already has fresh raw_tools from
    _enumerate_client_tools in _handle_reconnect. The on_server_reconnect
    handler must NOT re-enumerate (no second list_tools call).
    """
    from mcp.types import ListToolsResult, Tool

    from tela.shell import downstream
    from tela.shell.result import Result

    enumerate_calls: list[str] = []

    class FakeSession:
        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            enumerate_calls.append("list_tools")
            return ListToolsResult(
                tools=[Tool(name="after_reconnect", inputSchema={})],
                nextCursor=None,
            )

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        return Result(
            value=downstream._ClientHandle(session=FakeSession(), stack=FakeStack())  # type: ignore[arg-type]
        )

    async def _fake_on_server_reconnect(
        server_name: str,
        server_config: ServerConfig,
        tool_list: list[dict],
    ) -> Result[None, str]:
        # on_server_reconnect should be called with already-enumerated tools
        # NOT trigger another enumeration itself
        return Result(value=None)

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_server
    )
    monkeypatch.setattr(
        "tela.shell.reload.on_server_reconnect", _fake_on_server_reconnect
    )

    server_config = ServerConfig(name="mocked", command="unused")
    handler = downstream._build_downstream_message_handler("mocked", server_config)

    # Precondition: set up runtime config so _get_runtime_server_config("mocked") succeeds
    old_runtime = get_runtime_config().value
    set_runtime_config(
        TelaConfig(servers={"mocked": ServerConfig(name="mocked", command="unused")})
    )
    # Precondition: register client so reconnect finds it (wait_contended path)
    downstream._clients["mocked"] = downstream._ClientHandle(
        session=FakeSession(),  # type: ignore[arg-type]
        stack=FakeStack(),  # type: ignore[arg-type]
    )

    try:
        asyncio.run(handler(RuntimeError("downstream disconnected")))
    finally:
        downstream._clients.clear()
        set_runtime_config(old_runtime)

    # After fix: enumerate_calls should contain exactly ONE list_tools call
    # from _handle_reconnect's _enumerate_client_tools call.
    # The on_server_reconnect must NOT call re_enumerate (which would
    # trigger a second list_tools).
    # Currently this test will show 1 call - the bug fix will ensure
    # on_server_reconnect doesn't re-enumerate.
    assert len(enumerate_calls) == 1, (
        f"Expected exactly 1 enumerate call from _handle_reconnect, "
        f"got {len(enumerate_calls)}: {enumerate_calls}"
    )


def test_handle_reconnect_passes_enumerated_tools_to_on_server_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_handle_reconnect must pass the already-enumerated tools to on_server_reconnect."""
    from mcp.types import ListToolsResult, Tool

    from tela.shell import downstream
    from tela.shell.result import Result

    received_tool_list: list[dict] = []

    class FakeSession:
        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            return ListToolsResult(
                tools=[
                    Tool(name="tool_x", inputSchema={}),
                    Tool(name="tool_y", inputSchema={}),
                ],
                nextCursor=None,
            )

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        return Result(
            value=downstream._ClientHandle(session=FakeSession(), stack=FakeStack())  # type: ignore[arg-type]
        )

    async def _fake_on_server_reconnect(
        server_name: str,
        server_config: ServerConfig,
        tool_list: list[dict],
    ) -> Result[None, str]:
        received_tool_list.extend(tool_list)
        return Result(value=None)

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_server
    )
    monkeypatch.setattr(
        "tela.shell.reload.on_server_reconnect", _fake_on_server_reconnect
    )

    server_config = ServerConfig(name="mocked", command="unused")
    handler = downstream._build_downstream_message_handler("mocked", server_config)

    # Precondition: set up runtime config so _get_runtime_server_config("mocked") succeeds
    old_runtime = get_runtime_config().value
    set_runtime_config(
        TelaConfig(servers={"mocked": ServerConfig(name="mocked", command="unused")})
    )
    # Precondition: register client so reconnect finds it (wait_contended path)
    downstream._clients["mocked"] = downstream._ClientHandle(
        session=FakeSession(),  # type: ignore[arg-type]
        stack=FakeStack(),  # type: ignore[arg-type]
    )

    try:
        asyncio.run(handler(RuntimeError("downstream disconnected")))
    finally:
        downstream._clients.clear()
        set_runtime_config(old_runtime)

    # Verify the tools that were enumerated in _handle_reconnect are
    # correctly passed to on_server_reconnect
    tool_names = sorted(t["name"] for t in received_tool_list)
    assert tool_names == ["tool_x", "tool_y"], (
        f"Expected ['tool_x', 'tool_y'] to be passed to on_server_reconnect, "
        f"got {tool_names}"
    )


def test_handle_reconnect_swaps_client_before_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_handle_reconnect must swap client handle before calling on_server_reconnect.

    This ensures the new client session is active when tools are enumerated
    after reconnect.
    """
    from mcp.types import ListToolsResult, Tool

    from tela.shell import downstream
    from tela.shell.result import Result

    client_handle_order: list[str] = []

    class FakeSession:
        def __init__(self, name: str) -> None:
            self.name = name

        async def list_tools(
            self, cursor: str | None = None, *, params: Any = None
        ) -> ListToolsResult:
            client_handle_order.append(f"enumerate:{self.name}")
            return ListToolsResult(
                tools=[Tool(name=f"tool_from_{self.name}", inputSchema={})],
                nextCursor=None,
            )

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        client_handle_order.append(f"open:{server_name}")
        return Result(
            value=downstream._ClientHandle(
                session=FakeSession(server_name),  # type: ignore[arg-type]
                stack=FakeStack(),  # type: ignore[arg-type]
            )
        )

    async def _fake_on_server_reconnect(
        server_name: str,
        server_config: ServerConfig,
        tool_list: list[dict],
    ) -> Result[None, str]:
        client_handle_order.append(f"on_server_reconnect:{server_name}")
        return Result(value=None)

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_server
    )
    monkeypatch.setattr(
        "tela.shell.reload.on_server_reconnect", _fake_on_server_reconnect
    )

    server_config = ServerConfig(name="mocked", command="unused")
    handler = downstream._build_downstream_message_handler("mocked", server_config)

    # Precondition: set up runtime config so _get_runtime_server_config("mocked") succeeds
    old_runtime = get_runtime_config().value
    set_runtime_config(
        TelaConfig(servers={"mocked": ServerConfig(name="mocked", command="unused")})
    )

    try:
        asyncio.run(handler(RuntimeError("downstream disconnected")))
    finally:
        downstream._clients.clear()
        set_runtime_config(old_runtime)

    # Order should be: open -> swap -> enumerate -> on_server_reconnect
    assert client_handle_order == [
        "open:mocked",
        "enumerate:mocked",
        "on_server_reconnect:mocked",
    ], f"Unexpected order: {client_handle_order}"


def test_recover_server_client_fails_closed_when_server_removed_mid_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal from runtime config beats in-flight recovery."""
    from tela.shell import downstream
    from tela.shell.result import Result

    class FakeStack:
        async def aclose(self) -> None:
            return None

    class FakeSession:
        async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
            return None

    old_runtime = get_runtime_config().value
    set_runtime_config(
        TelaConfig(servers={"srv": ServerConfig(name="srv", command="cmd_a")})
    )

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        set_runtime_config(TelaConfig(servers={}))
        return Result(
            value=downstream._ClientHandle(
                session=FakeSession(),  # type: ignore[arg-type]
                stack=FakeStack(),  # type: ignore[arg-type]
            )
        )

    async def _fake_enumerate_client_tools(
        server_name: str,
        handle: downstream._ClientHandle,
    ) -> Result[list[dict], str]:
        return Result(value=[{"name": "tool_a", "inputSchema": {}}])

    async def _fake_on_server_reconnect(
        server_name: str,
        server_config: ServerConfig,
        tool_list: list[dict],
    ) -> Result[None, str]:
        return Result(value=None)

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_server
    )
    monkeypatch.setattr(
        downstream, "_enumerate_client_tools", _fake_enumerate_client_tools
    )
    monkeypatch.setattr(
        "tela.shell.reload.on_server_reconnect", _fake_on_server_reconnect
    )

    try:
        recovery_result = asyncio.run(
            downstream._recover_server_client(
                "srv",
                deadline_monotonic=1e12,
            )
        )
    finally:
        set_runtime_config(old_runtime)
        downstream._clients.clear()

    assert recovery_result.is_err
    assert recovery_result.error is not None
    assert recovery_result.error.details is not None
    assert recovery_result.error.details.get("config_missing") is True


def test_recover_server_client_config_remove_cleans_stale_client_and_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config removal during recovery drops stale client and prunes lock."""
    from tela.shell import downstream
    from tela.shell.result import Result

    class TrackCloseStack:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeSession:
        async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
            return None

    old_runtime = get_runtime_config().value
    set_runtime_config(
        TelaConfig(servers={"srv": ServerConfig(name="srv", command="cmd_a")})
    )

    stale_stack = TrackCloseStack()
    downstream._clients["srv"] = downstream._ClientHandle(
        session=FakeSession(),  # type: ignore[arg-type]
        stack=stale_stack,  # type: ignore[arg-type]
    )

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        set_runtime_config(TelaConfig(servers={}))
        return Result(
            value=downstream._ClientHandle(
                session=FakeSession(),  # type: ignore[arg-type]
                stack=TrackCloseStack(),  # type: ignore[arg-type]
            )
        )

    async def _fake_enumerate_client_tools(
        server_name: str,
        handle: downstream._ClientHandle,
    ) -> Result[list[dict], str]:
        return Result(value=[{"name": "tool_a", "inputSchema": {}}])

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_server
    )
    monkeypatch.setattr(
        downstream, "_enumerate_client_tools", _fake_enumerate_client_tools
    )

    try:
        recovery_result = asyncio.run(
            downstream._recover_server_client(
                "srv",
                deadline_monotonic=1e12,
            )
        )
    finally:
        set_runtime_config(old_runtime)
        downstream._clients.clear()
        downstream._recovery_locks.clear()

    assert recovery_result.is_err
    assert recovery_result.error is not None
    assert recovery_result.error.details is not None
    assert recovery_result.error.details.get("config_missing") is True
    assert stale_stack.closed is True
    assert "srv" not in downstream._clients
    assert "srv" not in downstream._recovery_locks


def test_recover_server_client_releases_registry_lock_around_network_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery network awaits run without holding _registry_lock."""
    from tela.shell import downstream
    from tela.shell.result import Result

    class FakeStack:
        async def aclose(self) -> None:
            return None

    class FakeSession:
        async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
            return None

    old_runtime = get_runtime_config().value
    set_runtime_config(
        TelaConfig(servers={"srv": ServerConfig(name="srv", command="cmd_a")})
    )

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        assert not downstream._registry_lock.locked()
        return Result(
            value=downstream._ClientHandle(
                session=FakeSession(),  # type: ignore[arg-type]
                stack=FakeStack(),  # type: ignore[arg-type]
            )
        )

    async def _fake_enumerate_client_tools(
        server_name: str,
        handle: downstream._ClientHandle,
    ) -> Result[list[dict], str]:
        assert not downstream._registry_lock.locked()
        return Result(value=[{"name": "tool_a", "inputSchema": {}}])

    async def _fake_on_server_reconnect(
        server_name: str,
        server_config: ServerConfig,
        tool_list: list[dict],
    ) -> Result[None, str]:
        assert not downstream._registry_lock.locked()
        return Result(value=None)

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_server
    )
    monkeypatch.setattr(
        downstream, "_enumerate_client_tools", _fake_enumerate_client_tools
    )
    monkeypatch.setattr(
        "tela.shell.reload.on_server_reconnect", _fake_on_server_reconnect
    )

    try:
        recovery_result = asyncio.run(
            downstream._recover_server_client(
                "srv",
                deadline_monotonic=1e12,
            )
        )
    finally:
        set_runtime_config(old_runtime)
        downstream._clients.clear()
        downstream._recovery_locks.clear()

    assert recovery_result.is_ok


def test_recover_server_client_success_closes_replaced_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful recovery closes stale client handle after swap."""
    from tela.shell import downstream
    from tela.shell.result import Result

    class TrackCloseStack:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeSession:
        async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
            return None

    old_runtime = get_runtime_config().value
    set_runtime_config(
        TelaConfig(servers={"srv": ServerConfig(name="srv", command="cmd_a")})
    )

    old_stack = TrackCloseStack()
    downstream._clients["srv"] = downstream._ClientHandle(
        session=FakeSession(),  # type: ignore[arg-type]
        stack=old_stack,  # type: ignore[arg-type]
    )

    new_stack = TrackCloseStack()

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        return Result(
            value=downstream._ClientHandle(
                session=FakeSession(),  # type: ignore[arg-type]
                stack=new_stack,  # type: ignore[arg-type]
            )
        )

    async def _fake_enumerate_client_tools(
        server_name: str,
        handle: downstream._ClientHandle,
    ) -> Result[list[dict], str]:
        return Result(value=[{"name": "tool_a", "inputSchema": {}}])

    async def _fake_on_server_reconnect(
        server_name: str,
        server_config: ServerConfig,
        tool_list: list[dict],
    ) -> Result[None, str]:
        return Result(value=None)

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_server
    )
    monkeypatch.setattr(
        downstream, "_enumerate_client_tools", _fake_enumerate_client_tools
    )
    monkeypatch.setattr(
        "tela.shell.reload.on_server_reconnect", _fake_on_server_reconnect
    )

    try:
        recovery_result = asyncio.run(
            downstream._recover_server_client(
                "srv",
                deadline_monotonic=1e12,
            )
        )
    finally:
        set_runtime_config(old_runtime)
        downstream._clients.clear()
        downstream._recovery_locks.clear()

    assert recovery_result.is_ok
    assert old_stack.closed is True
    assert new_stack.closed is False


def test_disconnect_all_cleans_recovery_lock_under_recovery_pressure() -> None:
    """disconnect_all clears recovery locks even when one is held."""
    from tela.shell import downstream

    class TrackCloseStack:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeSession:
        async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
            return None

    async def _exercise() -> None:
        held_lock = asyncio.Lock()
        await held_lock.acquire()
        downstream._recovery_locks["srv"] = held_lock

        stack = TrackCloseStack()
        downstream._clients["srv"] = downstream._ClientHandle(
            session=FakeSession(),  # type: ignore[arg-type]
            stack=stack,  # type: ignore[arg-type]
        )

        result = await downstream.disconnect_all()
        assert result.is_ok
        assert stack.closed is True
        assert "srv" not in downstream._clients
        assert "srv" not in downstream._recovery_locks

        held_lock.release()

    try:
        asyncio.run(_exercise())
    finally:
        downstream._clients.clear()
        downstream._recovery_locks.clear()


def test_recover_server_client_rejects_material_config_change_before_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Material config drift must block stale recovered handle swap."""
    from tela.shell import downstream
    from tela.shell.result import Result

    class FakeStack:
        async def aclose(self) -> None:
            return None

    class FakeSession:
        async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
            return None

    stale_handle = downstream._ClientHandle(
        session=FakeSession(),  # type: ignore[arg-type]
        stack=FakeStack(),  # type: ignore[arg-type]
    )
    downstream._clients["srv"] = stale_handle

    old_runtime = get_runtime_config().value
    set_runtime_config(
        TelaConfig(servers={"srv": ServerConfig(name="srv", command="cmd_a")})
    )

    async def _fake_open_client_for_server(
        server_name: str,
        server_config: ServerConfig,
        message_handler: Any | None = None,
    ) -> Result[downstream._ClientHandle, str]:
        set_runtime_config(
            TelaConfig(servers={"srv": ServerConfig(name="srv", command="cmd_b")})
        )
        return Result(
            value=downstream._ClientHandle(
                session=FakeSession(),  # type: ignore[arg-type]
                stack=FakeStack(),  # type: ignore[arg-type]
            )
        )

    async def _fake_enumerate_client_tools(
        server_name: str,
        handle: downstream._ClientHandle,
    ) -> Result[list[dict], str]:
        return Result(value=[{"name": "tool_a", "inputSchema": {}}])

    monkeypatch.setattr(
        downstream, "_open_client_for_server", _fake_open_client_for_server
    )
    monkeypatch.setattr(
        downstream, "_enumerate_client_tools", _fake_enumerate_client_tools
    )

    try:
        recovery_result = asyncio.run(
            downstream._recover_server_client(
                "srv",
                deadline_monotonic=1e12,
            )
        )
    finally:
        set_runtime_config(old_runtime)

    assert recovery_result.is_err
    assert recovery_result.error is not None
    assert recovery_result.error.details is not None
    assert recovery_result.error.details.get("config_missing") is False
    assert downstream._clients.get("srv") is stale_handle


def test_get_runtime_server_config_uses_runtime_authority_over_hints() -> None:
    """Recovery config lookup must resolve from runtime config, not stale hints."""
    from tela.shell import downstream
    from tela.shell.gateway_runtime import get_runtime_config, set_runtime_config

    old_runtime = get_runtime_config().value
    old_hints = dict(downstream._server_config_hints)

    runtime_config = ServerConfig(name="srv", command="runtime_cmd")
    stale_hint = ServerConfig(name="srv", command="stale_hint_cmd")

    set_runtime_config(TelaConfig(servers={"srv": runtime_config}))
    downstream._server_config_hints["srv"] = stale_hint

    try:
        lookup_result = downstream._get_runtime_server_config("srv")
    finally:
        downstream._server_config_hints.clear()
        downstream._server_config_hints.update(old_hints)
        set_runtime_config(old_runtime)

    assert lookup_result.is_ok
    assert lookup_result.value == runtime_config


def test_get_runtime_server_config_fails_when_server_removed_even_with_hint() -> None:
    """Runtime removal must beat stale hint fallback for recovery lookup."""
    from tela.shell import downstream
    from tela.shell.gateway_runtime import get_runtime_config, set_runtime_config

    old_runtime = get_runtime_config().value
    old_hints = dict(downstream._server_config_hints)

    removed_server_hint = ServerConfig(name="srv", command="stale_hint_cmd")

    set_runtime_config(TelaConfig(servers={}))
    downstream._server_config_hints["srv"] = removed_server_hint

    try:
        lookup_result = downstream._get_runtime_server_config("srv")
    finally:
        downstream._server_config_hints.clear()
        downstream._server_config_hints.update(old_hints)
        set_runtime_config(old_runtime)

    assert lookup_result.is_err
    assert lookup_result.error is not None
    assert lookup_result.error.details is not None
    assert lookup_result.error.details.get("config_missing") is True


def test_call_tool_stops_after_convergence_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Convergence rejection is terminal for the original call."""
    from tela.core.models import TelaError
    from tela.shell import downstream
    from tela.shell import _downstream_recovery
    from tela.shell.result import Result

    class FailingSession:
        call_count = 0

        async def call_tool(self, tool_name: str, *, arguments: dict) -> Any:
            self.call_count += 1
            raise RuntimeError(
                "Client is not connected. Use the 'async with client:' context manager first."
            )

    class FakeStack:
        async def aclose(self) -> None:
            return None

    failing_session = FailingSession()
    downstream._clients["srv"] = downstream._ClientHandle(
        session=failing_session,  # type: ignore[arg-type]
        stack=FakeStack(),  # type: ignore[arg-type]
    )

    async def _fake_recover_server_client(
        server_name: str,
        *,
        deadline_monotonic: float,
    ) -> Result[None, TelaError]:
        return Result(
            error=TelaError(
                code="DOWNSTREAM_UNAVAILABLE",
                message="Convergence rejected",
                details={
                    "server_name": server_name,
                    "recovery_attempted": True,
                    "recovery_eligible": True,
                    "recovery_stage": "convergence_rejected",
                    "underlying_error": "TOOL_CONFLICT",
                    "config_missing": False,
                },
            )
        )

    monkeypatch.setattr(
        _downstream_recovery,
        "_recover_server_client",
        _fake_recover_server_client,
    )

    try:
        result = asyncio.run(downstream.call_tool("srv", "tool_a", {}))
    finally:
        downstream._clients.clear()

    assert result.is_err
    assert result.error is not None
    assert result.error.details is not None
    assert result.error.details.get("recovery_stage") == "convergence_rejected"
    assert failing_session.call_count == 1
