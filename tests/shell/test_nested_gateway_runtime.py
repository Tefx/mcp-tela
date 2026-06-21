"""Expected-red shell/runtime probes for ADR-010 nested gateway behavior.

These tests intentionally exercise observable runtime paths rather than stubs:
startup connect_all, registry snapshots, exposed-name routing lookup,
builtin provider metadata, and reload/on_tools_changed notification behavior.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from tela.core.models import (
    ConnectionContext,
    Posture,
    ProfileConfig,
    ServerConfig,
    TelaConfig,
)
from tela.shell.builtin_tools import (
    BUILTIN_TOOL_NAMES,
    handle_list_providers,
    register_builtin_tools,
)
from tela.shell.downstream import (
    connect_all,
    disconnect_all,
    get_all_tools,
    get_registry,
    get_tool_server,
)
from tela.shell.gateway_runtime import get_runtime_config, set_runtime_config
from tela.shell.reload import on_tools_changed, set_notify_callback
from tela.shell.upstream import handle_tools_call, handle_tools_list

NESTED_TELA_PREFIX_REQUIRED = "NESTED_TELA_PREFIX_REQUIRED"
CHILD_PROVIDER = "tela_list_providers"
CHILD_PROFILE = "tela_list_profiles"


@dataclass(frozen=True)
class ConnectObservation:
    accepted: bool
    message: str


def _tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"downstream {name}", "inputSchema": {}}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _connect_observation(
    servers: dict[str, ServerConfig],
    tool_lists: dict[str, list[dict[str, Any]]],
) -> ConnectObservation:
    try:
        result = _run(connect_all(servers, tool_lists=tool_lists))
    except ValueError as exc:
        return ConnectObservation(accepted=False, message=str(exc))

    if result.is_err:
        return ConnectObservation(accepted=False, message=result.error or "")
    return ConnectObservation(accepted=True, message="")


def _registry_names(server_name: str = "child") -> set[str]:
    tools = get_all_tools().value or {}
    return {tool.name for tool in tools.get(server_name, [])}


def _registry_raw_names(server_name: str = "child") -> set[str | None]:
    tools = get_all_tools().value or {}
    return {tool.raw_name for tool in tools.get(server_name, [])}


def _gateway_config(server: ServerConfig) -> TelaConfig:
    return TelaConfig(
        servers={"child": server},
        profiles={
            "dev": ProfileConfig(
                name="dev",
                capabilities={"child": Posture.DESTRUCTIVE},
                default=True,
            )
        },
        resolved_default_profile="dev",
    )


def _connection() -> ConnectionContext:
    return ConnectionContext(
        connection_id="conn-dev",
        profile_id="dev",
        connected_at="2026-01-01T00:00:00Z",
    )


@pytest.fixture(autouse=True)
def _clean_runtime_state() -> None:
    old_config = get_runtime_config().value
    set_notify_callback(None)
    _run(disconnect_all())
    yield
    set_notify_callback(None)
    _run(disconnect_all())
    set_runtime_config(old_config)


def test_startup_nested_gateway_filters_child_builtins_from_registry_snapshot() -> None:
    """B1/NGW-R3: connect_all + registry snapshot hide child built-ins."""

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        nested_gateway=True,
    )
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={
                "child": [_tool(CHILD_PROVIDER), _tool(CHILD_PROFILE), _tool("safe_tool")]
            },
        )
    )
    assert result.is_ok, result.error

    snapshot_tools_by_server, snapshot_lookup = get_registry().snapshot()
    child_names = {tool.name for tool in snapshot_tools_by_server.get("child", [])}

    assert child_names == {"host_safe_tool"}, (
        "NESTED_CHILD_TOOL_NOT_FILTERED nested_gateway registry snapshot "
        f"still exposed child builtins: {sorted(child_names)}"
    )
    assert set(snapshot_lookup) == {"host_safe_tool"}


def test_exclude_tools_removes_child_tool_from_registry_and_call_routing_lookup() -> None:
    """B1/NGW-R1: excluded raw tool is absent from registry and routing lookup."""

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        exclude_tools=[CHILD_PROFILE],
    )
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={"child": [_tool(CHILD_PROFILE), _tool("safe_tool")]},
        )
    )
    assert result.is_ok, result.error

    names = _registry_names()
    assert "host_safe_tool" in names
    assert "host_tela_list_profiles" not in names, (
        "NESTED_CHILD_TOOL_NOT_FILTERED exclude_tools call routing lookup: "
        f"registry names={sorted(names)}"
    )
    assert get_tool_server("host_tela_list_profiles").value is None, (
        "exclude_tools must remove excluded child tool from tools/call routing lookup"
    )


def test_provider_metadata_reports_filtered_tool_count_and_names() -> None:
    """B1/NGW-R1/R8: builtin provider metadata reflects filtered surface."""

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        exclude_tools=[CHILD_PROVIDER],
        default_posture=Posture.READ_ONLY,
    )
    set_runtime_config(_gateway_config(server))
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={"child": [_tool(CHILD_PROVIDER), _tool("safe_tool")]},
        )
    )
    assert result.is_ok, result.error

    providers = _run(handle_list_providers(_connection()))
    child = next(provider for provider in providers if provider["provider_name"] == "child")

    assert child["tool_count"] == 1, (
        "tool_count exclude_tools tela_list_providers: provider metadata must "
        f"count only filtered exposed child tools, got {child}"
    )
    assert child["tool_names"] == ["host_safe_tool"]


def test_reload_on_tools_changed_recomputes_filtered_surface_and_notifies_list_changed() -> None:
    """B1/NGW-R1: reload path applies exclude_tools and emits list change."""

    initial = ServerConfig(name="child", command="cmd", tool_prefix="host_")
    initial_connect = _run(
        connect_all({"child": initial}, tool_lists={"child": [_tool("safe_tool")]})
    )
    assert initial_connect.is_ok, initial_connect.error

    notifications: list[str] = []

    async def capture_notify(digest: str) -> None:
        notifications.append(digest)

    set_notify_callback(capture_notify)
    changed = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        exclude_tools=[CHILD_PROFILE],
    )
    reload_result = _run(
        on_tools_changed(
            "child",
            changed,
            [_tool("safe_tool"), _tool(CHILD_PROFILE)],
        )
    )
    assert reload_result.is_ok, reload_result.error
    assert notifications and notifications[-1].startswith("sha256:")

    names = _registry_names()
    assert names == {"host_safe_tool"}, (
        "tools/list_changed exclude_tools reload did not recompute filtered "
        f"tool surface; names={sorted(names)} notifications={notifications}"
    )


def test_parent_builtins_remain_gateway_owned_while_child_builtins_follow_config() -> None:
    """B2/NGW-R8: parent built-ins stay visible; child built-ins are configurable."""

    builtin_result = register_builtin_tools()
    assert builtin_result.is_ok, builtin_result.error
    assert {tool["name"] for tool in builtin_result.value or []} == BUILTIN_TOOL_NAMES
    assert {CHILD_PROVIDER, CHILD_PROFILE}.issubset(BUILTIN_TOOL_NAMES)

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        nested_gateway=True,
    )
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={"child": [_tool(CHILD_PROVIDER), _tool(CHILD_PROFILE)]},
        )
    )
    assert result.is_ok, result.error

    child_names = _registry_names()
    assert child_names == set(), (
        "parent builtins remain gateway-owned/visible, but child builtins must "
        f"follow nested_gateway config; child registry names={sorted(child_names)}"
    )


def test_valid_prefix_without_nested_gateway_or_exclude_tools_keeps_child_builtins_visible() -> None:
    """B3/NGW-R4/R6: no silent auto-hide in prefix-only compatibility mode."""

    server = ServerConfig(name="child", command="cmd", tool_prefix="host_")
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={"child": [_tool(CHILD_PROVIDER), _tool(CHILD_PROFILE)]},
        )
    )
    assert result.is_ok, result.error

    assert _registry_names() == {"host_tela_list_providers", "host_tela_list_profiles"}
    assert _registry_raw_names() == {CHILD_PROVIDER, CHILD_PROFILE}
    assert {CHILD_PROVIDER, CHILD_PROFILE}.issubset(BUILTIN_TOOL_NAMES), (
        "parent builtins should remain gateway-owned while prefixed child builtins "
        "stay visible when nested_gateway/exclude_tools are omitted"
    )


def test_handle_tools_list_hides_nested_child_builtins_but_parent_builtins_register() -> None:
    """Risk probe: tools/list hides nested child built-ins; parent built-ins remain."""

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        nested_gateway=True,
        default_posture=Posture.READ_ONLY,
    )
    set_runtime_config(_gateway_config(server))
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={
                "child": [
                    _tool(CHILD_PROVIDER),
                    _tool(CHILD_PROFILE),
                    _tool("safe_tool"),
                ]
            },
        )
    )
    assert result.is_ok, result.error

    tools_list = _run(handle_tools_list(_connection()))
    assert tools_list.is_ok, tools_list.error
    assert {tool["name"] for tool in tools_list.value or []} == {"host_safe_tool"}

    builtin_result = register_builtin_tools()
    assert builtin_result.is_ok, builtin_result.error
    assert {tool["name"] for tool in builtin_result.value or []} == BUILTIN_TOOL_NAMES


def test_provider_metadata_prefix_only_child_builtins_remain_visible() -> None:
    """Risk probe: prefix-only nested child exposes prefixed child built-ins."""

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        default_posture=Posture.READ_ONLY,
    )
    set_runtime_config(_gateway_config(server))
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={"child": [_tool(CHILD_PROVIDER), _tool(CHILD_PROFILE)]},
        )
    )
    assert result.is_ok, result.error

    tools_list = _run(handle_tools_list(_connection()))
    assert tools_list.is_ok, tools_list.error
    expected = {"host_tela_list_providers", "host_tela_list_profiles"}
    assert {tool["name"] for tool in tools_list.value or []} == expected

    providers = _run(handle_list_providers(_connection()))
    child = next(
        provider for provider in providers if provider["provider_name"] == "child"
    )
    assert child["tool_count"] == 2
    assert set(child["tool_names"]) == expected


def test_prefixed_non_builtin_call_routes_with_resolved_raw_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Risk probe: prefixed exposed name calls downstream with ResolvedTool.raw_name."""

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="host_",
        default_posture=Posture.READ_ONLY,
    )
    set_runtime_config(_gateway_config(server))
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={"child": [_tool("safe_tool")]},
        )
    )
    assert result.is_ok, result.error

    routed: list[tuple[str, str, dict[str, Any]]] = []

    async def _fake_call_tool(
        server_name: str,
        tool_name: str,
        arguments: dict,
    ) -> Any:
        routed.append((server_name, tool_name, arguments))
        from tela.shell.result import Result

        return Result(value={"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr("tela.shell.upstream.call_tool", _fake_call_tool)

    call = _run(handle_tools_call(_connection(), "host_safe_tool", {"x": 1}))
    assert call.is_ok, call.error
    assert routed == [("child", "safe_tool", {"x": 1})]


def test_nested_gateway_missing_prefix_diagnostic_is_actionable_runtime_contract() -> None:
    """B4/NGW-R5: explicit nested mode without prefix fails at Core gate."""

    with pytest.raises(ValueError) as exc_info:
        ServerConfig(name="child", command="cmd", nested_gateway=True)

    assert NESTED_TELA_PREFIX_REQUIRED in str(exc_info.value), (
        "NESTED_TELA_PREFIX_REQUIRED nested_gateway missing prefix diagnostic "
        f"not emitted; observed={exc_info.value!r}"
    )


def test_raw_child_builtins_without_prefix_fail_with_nested_prefix_required() -> None:
    """B4/NGW-R5: detected raw child built-ins without prefix fail closed."""

    observation = _connect_observation(
        {"child": ServerConfig(name="child", command="cmd")},
        {"child": [_tool(CHILD_PROVIDER)]},
    )

    assert not observation.accepted
    assert NESTED_TELA_PREFIX_REQUIRED in observation.message, (
        "NESTED_TELA_PREFIX_REQUIRED raw child builtin without prefix must use "
        f"actionable nested gateway diagnostic; observed={observation.message!r}"
    )


def test_raw_child_builtins_with_empty_prefix_fail_with_nested_prefix_required() -> None:
    """B4/NGW-R5: empty prefix follows omitted-prefix runtime diagnostic."""

    observation = _connect_observation(
        {"child": ServerConfig(name="child", command="cmd", tool_prefix="")},
        {"child": [_tool(CHILD_PROFILE)]},
    )

    assert not observation.accepted
    assert NESTED_TELA_PREFIX_REQUIRED in observation.message, (
        "NESTED_TELA_PREFIX_REQUIRED raw child builtin with empty prefix must use "
        f"actionable nested gateway diagnostic; observed={observation.message!r}"
    )
    assert "tool_prefix cannot be empty" not in observation.message


def test_shell_rejects_exclude_tools_aliases_instead_of_accepting_and_cleaning() -> None:
    """B4/NGW-R2: shell-bound config construction rejects aliases."""

    with pytest.raises((TypeError, ValueError)):
        ServerConfig(name="child", command="cmd", exclude_tool=[CHILD_PROFILE])


def test_shell_runtime_uses_core_filtering_and_keeps_lifecycle_ownership_separate() -> None:
    """B4/NGW-R7: Shell wires connect/reload, Core owns filter semantics."""

    core_contract_fields = set(ServerConfig.model_fields)
    assert {"exclude_tools", "nested_gateway"}.issubset(core_contract_fields), (
        "core/shell ownership: ServerConfig must expose exclude_tools and "
        "nested_gateway before shell connect_all/on_tools_changed can wire them"
    )

    server = ServerConfig(
        name="child",
        command="cmd",
        tool_prefix="work_",
        exclude_tools=["blocked_tool"],
    )
    result = _run(
        connect_all(
            {"child": server},
            tool_lists={"child": [_tool("blocked_tool"), _tool("allowed_tool")]},
        )
    )
    assert result.is_ok, result.error
    assert _registry_names() == {"work_allowed_tool"}, (
        "Shell connect_all must publish the Core-filtered registry surface only"
    )
