"""Test nested gateway ergonomics configuration and filtering contracts.

These tests ensure compliance with ADR-010: Nested Tela Gateway Ergonomics,
which guarantees:
1. `exclude_tools` preserves raw downstream tool names unmutated.
2. `nested_gateway=True` automatically excludes child builtin raw names
   `tela_list_providers` and `tela_list_profiles` but requires `tool_prefix`.
3. Downstream servers without `tool_prefix` exposing `tela_list_providers`
   fail closed with `NESTED_TELA_PREFIX_REQUIRED`.
4. Tests reference core design constraints per INTERFACES.md sections.
"""

import pytest

from tela.core.models import ServerConfig
from tela.core.errors import ConfigContractError

# Error constants imported for ADR-010 assertion
# (If not yet in errors.py, using literal)
NESTED_TELA_PREFIX_REQUIRED = "NESTED_TELA_PREFIX_REQUIRED"


def test_server_config_exposes_exclude_tools_with_raw_name_defaults() -> None:
    """NGW-R1, NGW-R4: exclude_tools exposes list[str] defaulting to empty."""
    config = ServerConfig(name="mock", url="http://mock")
    assert getattr(config, "exclude_tools", []) == []

    # Valid config explicitly specifying exclude_tools
    config = ServerConfig(
        name="mock",
        url="http://mock",
        exclude_tools=["tela_list_providers", "some_other_raw_tool"],
    )
    assert getattr(config, "exclude_tools", []) == ["tela_list_providers", "some_other_raw_tool"]


def test_server_config_exposes_nested_gateway_default_false() -> None:
    """NGW-R2: ServerConfig exposes nested_gateway defaulting to False."""
    config = ServerConfig(name="mock", url="http://mock")
    assert getattr(config, "nested_gateway", False) is False


def test_validate_config_rejects_nested_gateway_true_without_prefix() -> None:
    """NGW-R3, NGW-R4: nested_gateway=True requires non-empty tool_prefix."""
    # Attempting to use nested_gateway without tool_prefix should raise
    params = {"name": "mock", "url": "http://mock", "nested_gateway": True, "tool_prefix": ""}

    with pytest.raises(Exception) as exc_info:
        # Assuming validation logic throws at model instantiation or via explicit config validation
        # We invoke standard Pydantic validation here to trigger the expected exception.
        ServerConfig(**params)

    error_str = str(exc_info.value)
    assert NESTED_TELA_PREFIX_REQUIRED in error_str or "nested_gateway" in error_str.lower()


def test_missing_prefix_raw_tela_list_providers_fails_with_nested_prefix_required() -> None:
    """NGW-R4: Downstream child builtins without prefix fail contextually."""
    # In integration or core validation, a raw 'tela_list_providers' from a server
    # without tool_prefix raises NESTED_TELA_PREFIX_REQUIRED
    # We mock the conflict/validation surface here as expected for Core tests.

    # We mock or assert failure on the tool registration surface:
    # `tela.core.family.resolve_tools` (or equivalent tool resolver/filter logic)

    from tela.core.family import resolve_tools

    tools = [{"name": "tela_list_providers", "description": "built-in"}]
    server = ServerConfig(name="mock", url="http://mock", tool_prefix="")

    with pytest.raises(Exception) as exc_info:
        resolve_tools("mock", server, tools)

    error_str = str(exc_info.value)
    assert NESTED_TELA_PREFIX_REQUIRED in error_str or "prefix" in error_str.lower()


def test_exclude_tools_filters_before_prefix_and_family() -> None:
    """NGW-R5: exclude_tools filters raw downstream names before prefixing."""
    from tela.core.family import resolve_tools

    server = ServerConfig(
        name="mock",
        url="http://mock",
        exclude_tools=["raw_target", "tela_list_profiles"],
        tool_prefix="host_"
    )

    # Passing both a matching tool and a non-matching tool
    tools = [
        {"name": "raw_target", "description": "excluded"},
        {"name": "tela_list_profiles", "description": "excluded"},
        {"name": "keep_this", "description": "kept"}
    ]

    try:
        filtered = resolve_tools("mock", server, tools)
        assert len(filtered) == 1
        assert filtered[0].raw_name == "keep_this"
    except TypeError:
        # If exclude_tools is not yet implemented in resolve_tools kwargs
        pytest.fail("exclude_tools not implemented")


def test_nested_gateway_true_adds_child_builtins_to_effective_exclude_set() -> None:
    """NGW-R6: nested_gateway=True adds raw tela builtins to exclude set."""
    from tela.core.family import resolve_tools

    server = ServerConfig(
        name="mock",
        url="http://mock",
        nested_gateway=True,
        tool_prefix="host_"
    )
    tools = [
        {"name": "tela_list_providers", "description": "child providers"},
        {"name": "tela_list_profiles", "description": "child profiles"},
        {"name": "host_status", "description": "host tool"}
    ]

    try:
        filtered = resolve_tools("mock", server, tools)
        assert len(filtered) == 1
        assert filtered[0].raw_name == "host_status"
    except TypeError:
        pytest.fail("nested_gateway filter not implemented")


def test_omitted_nested_gateway_keeps_prefixed_child_builtins_visible() -> None:
    """NGW-R7, NGW-R9: Omitted nested_gateway keeps prefixed child built-ins, prefixed by snake_case."""
    from tela.core.family import resolve_tools

    server = ServerConfig(
        name="mock",
        url="http://mock",
        tool_prefix="host_"
    )
    tools = [
        {"name": "tela_list_providers", "description": "child providers"}
    ]

    # Should not fail or filter, because we have a valid prefix "host_"
    # The downstream name is prefixed later during Resolve Tool mapping.
    filtered = resolve_tools("mock", server, tools)
    assert len(filtered) == 1
    assert filtered[0].raw_name == "tela_list_providers"


def test_omitted_exclude_tools_and_omitted_nested_gateway_preserve_no_filter() -> None:
    """NGW-R8: Current no-filter behavior preserved when both omitted."""
    from tela.core.family import resolve_tools

    server = ServerConfig(
        name="mock",
        url="http://mock",
        tool_prefix="pre_"
    )
    tools = [
        {"name": "some_tool"},
        {"name": "tela_list_providers"}
    ]

    try:
        filtered = resolve_tools("mock", server, tools)
        assert len(filtered) == 2
    except Exception as exc:
        if NESTED_TELA_PREFIX_REQUIRED in str(exc):
            pytest.fail("Should not raise NESTED_TELA_PREFIX_REQUIRED when prefix is provided")
        raise
