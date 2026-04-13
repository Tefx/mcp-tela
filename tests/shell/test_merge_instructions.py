"""Tests for _merge_downstream_instructions behavior.

Tests cover:
1. Passthrough (instructions=None) - include downstream
2. Suppress (instructions=False) - exclude
3. Override (instructions="text") - use override
4. Mixed servers
5. Markdown format with H2 headers + tools list
6. Edge cases (all suppressed, none available, override without downstream)
"""

from __future__ import annotations

import pytest

from tela.core.models import (
    ResolvedTool,
    ServerConfig,
    TelaConfig,
)
from tela.shell.result import Result
from tela.shell import gateway as gateway_module
from tela.shell import surface_instructions


def test_passthrough_instructions_none_includes_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instructions=None (default) should passthrough downstream instructions."""

    def mock_get_server_instructions():
        return Result(value={"fs": "Use tools safely"})

    def mock_get_all_tools():
        return Result(
            value={
                "fs": [
                    ResolvedTool(
                        name="read_file",
                        server_name="fs",
                        family="fs",
                        schema_={},
                    )
                ]
            }
        )

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(servers={"fs": ServerConfig(name="fs", command="cmd")})
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is not None
    assert "## fs" in result.value
    assert "Use tools safely" in result.value


def test_suppress_instructions_false_excludes_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instructions=False should suppress that server's instructions."""

    def mock_get_server_instructions():
        return Result(value={"fs": "Should be suppressed"})

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(
        servers={"fs": ServerConfig(name="fs", command="cmd", instructions=False)}
    )
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    # Should be None since only server is suppressed
    assert result.value is None


def test_override_instructions_string_replaces_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instructions="custom text" should override downstream instructions."""

    def mock_get_server_instructions():
        return Result(value={"fs": "Original instructions"})

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(
        servers={
            "fs": ServerConfig(
                name="fs", command="cmd", instructions="Custom instructions"
            )
        }
    )
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is not None
    assert "Custom instructions" in result.value
    assert "Original instructions" not in result.value


def test_mixed_servers_combines_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed server configs (passthrough/suppress/override) combine correctly."""

    def mock_get_server_instructions():
        return Result(
            value={
                "passthrough": "From passthrough",
                "suppressed": "From suppressed",
                "overridden": "From overridden",
            }
        )

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(
        servers={
            "passthrough": ServerConfig(name="passthrough", command="cmd"),
            "suppressed": ServerConfig(
                name="suppressed", command="cmd", instructions=False
            ),
            "overridden": ServerConfig(
                name="overridden", command="cmd", instructions="Override text"
            ),
        }
    )
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is not None
    # Check passthrough is included
    assert "## passthrough" in result.value
    assert "From passthrough" in result.value
    # Check override is included
    assert "## overridden" in result.value
    assert "Override text" in result.value
    # Check suppressed is excluded
    assert "## suppressed" not in result.value
    assert "From suppressed" not in result.value


def test_markdown_format_h2_headers_and_tools_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output format uses Markdown H2 headers with server names and tools list."""

    def mock_get_server_instructions():
        return Result(
            value={
                "fs": "Filesystem operations here.",
                "shell": "Shell commands. Use with caution.",
            }
        )

    def mock_get_all_tools():
        return Result(
            value={
                "fs": [
                    ResolvedTool(
                        name="read_file", server_name="fs", family="fs", schema_={}
                    ),
                    ResolvedTool(
                        name="write_file", server_name="fs", family="fs", schema_={}
                    ),
                ],
                "shell": [
                    ResolvedTool(
                        name="exec", server_name="shell", family="shell", schema_={}
                    ),
                    ResolvedTool(
                        name="run_script",
                        server_name="shell",
                        family="shell",
                        schema_={},
                    ),
                ],
            }
        )

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(
        servers={
            "fs": ServerConfig(name="fs", command="cmd"),
            "shell": ServerConfig(name="shell", command="cmd"),
        }
    )
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is not None

    # Check H2 headers and content
    assert "## fs" in result.value
    assert "Filesystem operations here." in result.value
    assert "Available tools:" in result.value
    assert "- read_file" in result.value
    assert "- write_file" in result.value

    assert "## shell" in result.value
    assert "Shell commands. Use with caution." in result.value
    assert "- exec" in result.value
    assert "- run_script" in result.value


def test_edge_case_all_suppressed_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all servers have instructions=False, returns Result(value=None)."""

    def mock_get_server_instructions():
        return Result(value={"fs": "Should be suppressed", "shell": "Also suppressed"})

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(
        servers={
            "fs": ServerConfig(name="fs", command="cmd", instructions=False),
            "shell": ServerConfig(name="shell", command="cmd", instructions=False),
        }
    )
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is None


def test_edge_case_none_available_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no downstream provides instructions and no overrides, returns None."""

    def mock_get_server_instructions():
        return Result(value={})  # No instructions from any server

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(
        servers={
            "fs": ServerConfig(name="fs", command="cmd"),
            "shell": ServerConfig(name="shell", command="cmd"),
        }
    )
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is None


def test_edge_case_override_without_downstream_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override instructions work even if downstream provides nothing."""

    def mock_get_server_instructions():
        return Result(value={})  # Empty - fs not in downstream instructions

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(
        servers={
            "fs": ServerConfig(name="fs", command="cmd", instructions="My override")
        }
    )
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is not None
    assert "My override" in result.value
    assert "## fs" in result.value


def test_empty_config_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty config (no servers) returns Result(value=None)."""

    def mock_get_server_instructions():
        return Result(value={})

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(servers={})
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is None


def test_result_type_is_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """_merge_downstream_instructions returns Result[T, E] type."""

    def mock_get_server_instructions():
        return Result(value={})

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    result = gateway_module._merge_downstream_instructions(TelaConfig())
    assert isinstance(result, Result)


def test_server_order_preserved_in_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server order in output matches insertion order (dict order preserved)."""

    def mock_get_server_instructions():
        return Result(value={})

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    # Python 3.7+ preserves dict insertion order
    config = TelaConfig(
        servers={
            "alpha": ServerConfig(name="alpha", command="cmd", instructions="A"),
            "beta": ServerConfig(name="beta", command="cmd", instructions="B"),
            "gamma": ServerConfig(name="gamma", command="cmd", instructions="C"),
        }
    )
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is not None
    # Verify order is preserved
    assert result.value.index("## alpha") < result.value.index("## beta")
    assert result.value.index("## beta") < result.value.index("## gamma")


def test_tools_list_sorted_alphabetically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tools in output are sorted alphabetically."""

    def mock_get_server_instructions():
        return Result(value={"fs": "Filesystem"})

    def mock_get_all_tools():
        return Result(
            value={
                "fs": [
                    ResolvedTool(
                        name="zebra", server_name="fs", family="fs", schema_={}
                    ),
                    ResolvedTool(
                        name="alpha", server_name="fs", family="fs", schema_={}
                    ),
                    ResolvedTool(
                        name="middle", server_name="fs", family="fs", schema_={}
                    ),
                ]
            }
        )

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(servers={"fs": ServerConfig(name="fs", command="cmd")})
    result = gateway_module._merge_downstream_instructions(config)

    assert result.is_ok
    assert result.value is not None
    # Check tools are in sorted order
    assert result.value.index("- alpha") < result.value.index("- middle")
    assert result.value.index("- middle") < result.value.index("- zebra")


def test_compose_gateway_and_downstream_includes_gateway_authoritative_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composed instructions must include the gateway-authoritative block first."""

    def mock_get_server_instructions():
        return Result(value={"fs": "Filesystem guidance"})

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    config = TelaConfig(servers={"fs": ServerConfig(name="fs", command="cmd")})
    downstream = gateway_module._merge_downstream_instructions(config)
    assert downstream.is_ok

    gateway = surface_instructions.get_gateway_surface_instructions()
    assert gateway.is_ok
    assert gateway.value is not None

    composed = surface_instructions.compose_gateway_and_downstream(
        gateway.value,
        downstream.value,
    )
    assert composed.is_ok
    assert composed.value is not None
    assert composed.value.startswith("# tela gateway surface contract")
    assert "Built-in MCP tools: `tela_list_providers`." in composed.value
    # Downstream section remains appended after authoritative block.
    assert "## fs" in composed.value


def test_compose_gateway_and_downstream_does_not_advertise_unsupported_mcp_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime instructions must not advertise absent MCP built-ins."""

    def mock_get_server_instructions():
        return Result(value={})

    def mock_get_all_tools():
        return Result(value={})

    monkeypatch.setattr(
        gateway_module, "get_server_instructions", mock_get_server_instructions
    )
    monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

    downstream = gateway_module._merge_downstream_instructions(TelaConfig(servers={}))
    assert downstream.is_ok

    gateway = surface_instructions.get_gateway_surface_instructions()
    assert gateway.is_ok
    assert gateway.value is not None

    result = surface_instructions.compose_gateway_and_downstream(
        gateway.value,
        downstream.value,
    )

    assert result.is_ok
    assert result.value is not None
    assert "tela.status" not in result.value
    assert "tela.connections" not in result.value
    assert "tela.audit" not in result.value
    # Guard: tela.profiles is a resource, not a tool
    assert "tela profiles" in result.value
