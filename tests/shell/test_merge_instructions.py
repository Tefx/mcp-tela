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
    Posture,
    ProfileConfig,
    ServerConfig,
    TelaConfig,
)
from tela.shell.config_loader import Result


# --- Test helpers for the planned _merge_downstream_instructions signature ---
# The function will accept TelaConfig and return Result[str | None, str]


def test_passthrough_instructions_none_includes_downstream() -> None:
    """instructions=None (default) should passthrough downstream instructions."""
    # Given: Server with instructions=None (passthrough mode)
    # When: downstream provides "Use tools safely"
    # Then: merged output includes "Use tools safely" under server header

    # Placeholder assertion - will fail until implementation
    # The actual test will verify:
    # config = TelaConfig(servers={"fs": ServerConfig(name="fs", command="cmd")})
    # mock downstream instructions = {"fs": "Use tools safely"}
    # result = _merge_downstream_instructions(config)
    # assert result.value contains "## fs\n\nUse tools safely"
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_suppress_instructions_false_excludes_server() -> None:
    """instructions=False should suppress that server's instructions."""
    # Given: Server with instructions=False
    # When: downstream provides instructions
    # Then: merged output excludes this server entirely

    # Placeholder assertion - will fail until implementation
    # config = TelaConfig(
    #     servers={"fs": ServerConfig(name="fs", command="cmd", instructions=False)}
    # )
    # mock downstream instructions = {"fs": "Should be suppressed"}
    # result = _merge_downstream_instructions(config)
    # assert result.value is None or "fs" not in result.value
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_override_instructions_string_replaces_downstream() -> None:
    """instructions="custom text" should override downstream instructions."""
    # Given: Server with instructions="Override text"
    # When: downstream provides different instructions
    # Then: merged output uses "Override text" instead

    # Placeholder assertion - will fail until implementation
    # config = TelaConfig(
    #     servers={"fs": ServerConfig(name="fs", command="cmd", instructions="Custom instructions")}
    # )
    # mock downstream instructions = {"fs": "Original instructions"}
    # result = _merge_downstream_instructions(config)
    # assert "Custom instructions" in result.value
    # assert "Original instructions" not in result.value
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_mixed_servers_combines_correctly() -> None:
    """Mixed server configs (passthrough/suppress/override) combine correctly."""
    # Given: Three servers with different instruction modes
    #   - "passthrough": instructions=None
    #   - "suppressed": instructions=False
    #   - "overridden": instructions="Override text"
    # When: all downstreams provide instructions
    # Then: output has 2 sections (passthrough + override), suppressed excluded

    # Placeholder assertion - will fail until implementation
    # config = TelaConfig(
    #     servers={
    #         "passthrough": ServerConfig(name="passthrough", command="cmd"),
    #         "suppressed": ServerConfig(name="suppressed", command="cmd", instructions=False),
    #         "overridden": ServerConfig(name="overridden", command="cmd", instructions="Override"),
    #     }
    # )
    # mock downstream = {"passthrough": "From A", "suppressed": "From B", "overridden": "From C"}
    # result = _merge_downstream_instructions(config)
    # assert "passthrough" in result.value
    # assert "From A" in result.value
    # assert "Override" in result.value
    # assert "suppressed" not in result.value
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_markdown_format_h2_headers_and_tools_list() -> None:
    """Output format uses Markdown H2 headers with server names and tools list."""
    # Given: Servers with instructions
    # When: merged
    # Then: output uses "## ServerName\n\nInstructions content" format

    # Placeholder assertion - will fail until implementation
    # Expected format:
    # ## fs
    #
    # Filesystem operations here. Available tools:
    # - read_file
    # - write_file
    #
    # ## shell
    #
    # Shell commands. Use with caution. Tools:
    # - exec
    # - run_script

    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_edge_case_all_suppressed_returns_none() -> None:
    """When all servers have instructions=False, returns Result(value=None)."""
    # Given: All servers have instructions=False
    # When: downstreams provide instructions
    # Then: returns Result(value=None) - no content to merge

    # Placeholder assertion - will fail until implementation
    # config = TelaConfig(
    #     servers={
    #         "fs": ServerConfig(name="fs", command="cmd", instructions=False),
    #         "shell": ServerConfig(name="shell", command="cmd", instructions=False),
    #     }
    # )
    # result = _merge_downstream_instructions(config)
    # assert result.value is None
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_edge_case_none_available_returns_none() -> None:
    """When no downstream provides instructions and no overrides, returns None."""
    # Given: Servers with instructions=None, but downstreams return no instructions
    # When: merging
    # Then: returns Result(value=None)

    # Placeholder assertion - will fail until implementation
    # config = TelaConfig(
    #     servers={
    #         "fs": ServerConfig(name="fs", command="cmd"),
    #         "shell": ServerConfig(name="shell", command="cmd"),
    #     }
    # )
    # mock downstream = {}  # No instructions from any server
    # result = _merge_downstream_instructions(config)
    # assert result.value is None
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_edge_case_override_without_downstream_included() -> None:
    """Override instructions work even if downstream provides nothing."""
    # Given: Server with instructions="Override" but downstream has no instructions
    # When: merging
    # Then: override text is still included in output

    # Placeholder assertion - will fail until implementation
    # config = TelaConfig(
    #     servers={"fs": ServerConfig(name="fs", command="cmd", instructions="My override")}
    # )
    # mock downstream = {}  # fs not in downstream instructions
    # result = _merge_downstream_instructions(config)
    # assert result.value is not None
    # assert "My override" in result.value
    # assert "## fs" in result.value
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_empty_config_returns_none() -> None:
    """Empty config (no servers) returns Result(value=None)."""
    # Given: TelaConfig with no servers
    # When: merging instructions
    # Then: returns Result(value=None)

    # Placeholder assertion - will fail until implementation
    # config = TelaConfig(servers={})
    # result = _merge_downstream_instructions(config)
    # assert result.value is None
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_result_type_is_result() -> None:
    """_merge_downstream_instructions returns Result[T, E] type."""
    # Placeholder assertion - verifies return type contract
    # result = _merge_downstream_instructions(TelaConfig())
    # assert isinstance(result, Result)
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )


def test_server_order_preserved_in_output() -> None:
    """Server order in output matches insertion order (dict order preserved)."""
    # Given: Servers added in specific order
    # When: merged
    # Then: output preserves that order

    # Python 3.7+ preserves dict insertion order
    # config = TelaConfig(
    #     servers={
    #         "alpha": ServerConfig(name="alpha", command="cmd", instructions="A"),
    #         "beta": ServerConfig(name="beta", command="cmd", instructions="B"),
    #         "gamma": ServerConfig(name="gamma", command="cmd", instructions="C"),
    #     }
    # )
    # result = _merge_downstream_instructions(config)
    # assert result.value.index("## alpha") < result.value.index("## beta")
    # assert result.value.index("## beta") < result.value.index("## gamma")
    pytest.fail(
        "Implementation pending: _merge_downstream_instructions needs config parameter"
    )
