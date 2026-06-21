"""Expected-red core contracts for nested gateway ergonomics.

These tests define the pure model/resolution ownership required by ADR-010
before implementation exists.  They intentionally exercise Core-only behavior:
ServerConfig shape/validation and resolve_tools raw-name semantics.  Runtime
wiring is covered in tests/shell/test_nested_gateway_runtime.py.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import ValidationError

from tela.core.family import resolve_tools
from tela.core.models import ResolvedTool, ServerConfig

NESTED_TELA_PREFIX_REQUIRED = "NESTED_TELA_PREFIX_REQUIRED"
CHILD_BUILTINS = {"tela_list_providers", "tela_list_profiles"}


def _tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"raw downstream {name}", "inputSchema": {}}


def _resolved_names(tools: list[ResolvedTool]) -> set[str]:
    return {tool.name for tool in tools}


def _raw_names(tools: list[ResolvedTool]) -> set[str | None]:
    return {tool.raw_name for tool in tools}


def test_server_config_declares_exclude_tools_and_nested_gateway_defaults() -> None:
    """NGW-R2/R3: public config fields exist with safe defaults."""

    field_names = set(ServerConfig.model_fields)
    missing = {"exclude_tools", "nested_gateway"} - field_names
    assert not missing, (
        "ServerConfig missing nested gateway contract fields: "
        f"{sorted(missing)}; exclude_tools must default to [] and "
        "nested_gateway must default to false"
    )

    config = ServerConfig(name="child", command="cmd")
    assert config.exclude_tools == []
    assert config.nested_gateway is False


def test_exclude_tools_rejects_invalid_shapes_and_aliases_without_cleanup() -> None:
    """NGW-R2/B4: no accept-and-clean, scalar shorthand, or alias spellings."""

    invalid_payloads = [
        {"exclude_tools": "tela_list_profiles"},
        {"exclude_tools": ["ok", 3]},
        {"exclude_tools": {"name": "tela_list_profiles"}},
        {"exclude_tool": ["tela_list_profiles"]},
        {"excluded_tools": ["tela_list_profiles"]},
        {"hide_tools": ["tela_list_profiles"]},
    ]

    for payload in invalid_payloads:
        with pytest.raises((TypeError, ValueError, ValidationError)):
            ServerConfig(name="child", command="cmd", **payload)


def test_nested_gateway_true_requires_non_empty_tool_prefix_with_stable_code() -> None:
    """NGW-R3/R5: explicit nested mode fails closed without a prefix."""

    with pytest.raises((TypeError, ValueError, ValidationError)) as exc_info:
        ServerConfig(name="child", command="cmd", nested_gateway=True)

    assert NESTED_TELA_PREFIX_REQUIRED in str(exc_info.value), (
        "nested_gateway true without tool_prefix must fail with "
        f"{NESTED_TELA_PREFIX_REQUIRED}"
    )

    with pytest.raises((TypeError, ValueError, ValidationError)) as empty_exc:
        ServerConfig(
            name="child",
            command="cmd",
            nested_gateway=True,
            tool_prefix="",
        )

    assert NESTED_TELA_PREFIX_REQUIRED in str(empty_exc.value), (
        "nested_gateway true with empty tool_prefix must fail with "
        f"{NESTED_TELA_PREFIX_REQUIRED}"
    )


def test_raw_child_builtins_without_prefix_fail_with_nested_prefix_required() -> None:
    """NGW-R5: raw child Tela built-ins without a prefix get the ADR code."""

    server = ServerConfig(name="child", command="cmd")

    with pytest.raises(ValueError) as exc_info:
        resolve_tools("child", server, [_tool("tela_list_providers")])

    assert NESTED_TELA_PREFIX_REQUIRED in str(exc_info.value), (
        "raw downstream child builtins with omitted tool_prefix must fail with "
        f"{NESTED_TELA_PREFIX_REQUIRED}, not a generic reserved-prefix error"
    )


def test_exclude_tools_filters_raw_names_before_prefix_family_and_reserved_checks() -> None:
    """NGW-R1/R2: raw-name filtering precedes prefixing and registration."""

    server = ServerConfig(
        name="child",
        command="cmd",
        family="child_family",
        tool_prefix="host_",
        exclude_tools=["raw_target", "tela_list_profiles"],
    )
    resolved = resolve_tools(
        "child",
        server,
        [_tool("raw_target"), _tool("tela_list_profiles"), _tool("keep_this")],
    )

    assert _raw_names(resolved) == {"keep_this"}, (
        "NESTED_CHILD_TOOL_NOT_FILTERED: exclude_tools must match raw "
        "downstream names before prefixing/family classification"
    )
    assert _resolved_names(resolved) == {"host_keep_this"}
    assert resolved[0].family == "child_family"


def test_nested_gateway_true_adds_child_builtins_to_effective_exclude_set() -> None:
    """NGW-R3/R8: nested_gateway filters child built-ins but not normal tools."""

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        nested_gateway=True,
    )
    resolved = resolve_tools(
        "child",
        server,
        [_tool("tela_list_providers"), _tool("tela_list_profiles"), _tool("status")],
    )

    assert _raw_names(resolved) == {"status"}, (
        "NESTED_CHILD_TOOL_NOT_FILTERED: nested_gateway must add child "
        "tela_list_providers/tela_list_profiles to the effective raw exclude set"
    )
    assert _resolved_names(resolved) == {"host_status"}


def test_prefix_only_mode_keeps_prefixed_child_builtins_visible() -> None:
    """NGW-R4/R6: detection must not silently auto-hide with a valid prefix."""

    server = ServerConfig(name="child", command="cmd", tool_prefix="host_")
    resolved = resolve_tools(
        "child",
        server,
        [_tool("tela_list_providers"), _tool("tela_list_profiles")],
    )

    assert _resolved_names(resolved) == {
        "host_tela_list_providers",
        "host_tela_list_profiles",
    }, (
        "no silent auto-hide: omitted nested_gateway/exclude_tools with valid "
        "host_ prefix must preserve prefixed child builtins"
    )
    assert _raw_names(resolved) == CHILD_BUILTINS


def test_tool_prefix_validation_covers_snake_case_reserved_and_dotted_prefixes() -> None:
    """NGW-R9/B4: host_/prod_/work_ are valid, dotted/reserved are rejected."""

    for prefix in ("host_", "prod_", "work_"):
        resolved = resolve_tools(
            "child",
            ServerConfig(name="child", command="cmd", tool_prefix=prefix),
            [_tool("read_file")],
        )
        assert resolved[0].name == f"{prefix}read_file"

    for invalid_prefix in ("tela_", "tela.", "host.", "prod.", "work."):
        with pytest.raises((TypeError, ValueError, ValidationError)):
            ServerConfig(name="child", command="cmd", tool_prefix=invalid_prefix)


def test_core_shell_ownership_boundary_remains_pure_for_filter_semantics() -> None:
    """NGW-R7/B4: Core owns filtering decisions; Shell owns lifecycle wiring."""

    server_fields = set(ServerConfig.model_fields)
    assert {"exclude_tools", "nested_gateway"}.issubset(server_fields), (
        "Core model must own exclude_tools/nested_gateway contract fields; "
        "Shell must not invent compatibility aliases or clean malformed input"
    )

    resolve_source = inspect.getsource(resolve_tools)
    assert "connect_all" not in resolve_source and "on_tools_changed" not in resolve_source
    assert "exclude_tools" in resolve_source and "nested_gateway" in resolve_source, (
        "Core resolve_tools must own raw-name exclude/nested_gateway filtering "
        "before Shell connect_all/reload registry wiring"
    )
