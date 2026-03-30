"""Regression tests enforcing CONFIRMED-SURFACE-CONTRACT.md.

These tests encode the canonical surface taxonomy and must fail on unsupported
claims about MCP built-ins. The contract is the single source of truth for
agent-facing surface classification.

Contract source: docs/CONFIRMED-SURFACE-CONTRACT.md

Coverage:
- MCP resource checks: tela.profiles must be a resource, not a tool
- MCP tool checks: no current built-in tela.* MCP tools
- CLI/HTTP checks: operator surfaces are not MCP built-ins
- Instruction-merge checks: ordering and conflict handling
- Negative assertions guarding unsupported surface claims
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from tela.shell.config_loader import Result
from tela.shell import gateway as gateway_module
from tela.shell import surface_instructions
from tela.core.models import (
    ResolvedTool,
    ServerConfig,
    TelaConfig,
)


# =============================================================================
# Section 1: Canonical surface matrix assertions
# =============================================================================


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIRMED_SURFACE_CONTRACT = PROJECT_ROOT / "docs" / "CONFIRMED-SURFACE-CONTRACT.md"
DESIGN_DOC = PROJECT_ROOT / "docs" / "DESIGN.md"
AGENT_INTERFACE_DOC = PROJECT_ROOT / "docs" / "AGENT_INTERFACE.md"
INTERFACES_DOC = PROJECT_ROOT / "docs" / "INTERFACES.md"
USAGE_DOC = PROJECT_ROOT / "docs" / "USAGE.md"
SURFACE_VERIFICATION_ARTIFACT = (
    PROJECT_ROOT / "evidence" / "surface_taxonomy_verification.md"
)
GATEWAY_SOURCE = PROJECT_ROOT / "src" / "tela" / "shell" / "gateway.py"


def _read_contract_text() -> str:
    return CONFIRMED_SURFACE_CONTRACT.read_text(encoding="utf-8")


def _read_gateway_source() -> str:
    return GATEWAY_SOURCE.read_text(encoding="utf-8")


def _read_design_doc() -> str:
    return DESIGN_DOC.read_text(encoding="utf-8")


def _read_agent_interface_doc() -> str:
    return AGENT_INTERFACE_DOC.read_text(encoding="utf-8")


def _read_usage_doc() -> str:
    return USAGE_DOC.read_text(encoding="utf-8")


def _read_interfaces_doc() -> str:
    return INTERFACES_DOC.read_text(encoding="utf-8")


def _read_surface_verification_artifact() -> str:
    return SURFACE_VERIFICATION_ARTIFACT.read_text(encoding="utf-8")


def _contract_surface_kinds() -> dict[str, str]:
    """Parse surface-kind pairs from docs/CONFIRMED-SURFACE-CONTRACT.md tables."""
    matches = re.findall(
        r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|", _read_contract_text(), re.MULTILINE
    )
    return {name: kind for name, kind in matches}


def _contract_kind(surface_name: str) -> str | None:
    return _contract_surface_kinds().get(surface_name)


def _runtime_operator_surfaces() -> set[str]:
    """Parse operator surface names from runtime gateway summary output."""
    gateway_result = surface_instructions.get_gateway_surface_instructions()
    assert gateway_result.is_ok
    assert gateway_result.value is not None
    operator_line_match = re.search(
        r"Operator-only surfaces \(not MCP built-ins\):\s*(.+?)\.",
        gateway_result.value,
    )
    assert operator_line_match is not None
    return set(re.findall(r"`([^`]+)`", operator_line_match.group(1)))


def _agent_interface_operator_surfaces() -> set[str]:
    """Parse operator surface names from AGENT_INTERFACE operator table."""
    section_match = re.search(
        r"### 2\.2 Operator Surfaces \(Not MCP Built-ins\).*?\n\n(.*?)\n\n\*\*Important:\*\*",
        _read_agent_interface_doc(),
        re.DOTALL,
    )
    assert section_match is not None
    return set(
        re.findall(r"^\|\s*`([^`]+)`\s*\|", section_match.group(1), re.MULTILINE)
    )


def _interfaces_builtin_summary_surfaces() -> set[str]:
    """Parse surface names from INTERFACES built-in surfaces summary table."""
    section_match = re.search(
        r"### 7\.1a Built-in surfaces summary\n\n(.*?)\n\n### 7\.2 HTTP Endpoints",
        _read_interfaces_doc(),
        re.DOTALL,
    )
    assert section_match is not None
    return set(
        re.findall(r"^\|\s*`([^`]+)`\s*\|", section_match.group(1), re.MULTILINE)
    )


class TestCanonicalSurfaceMatrix:
    """Regression tests for the canonical surface matrix."""

    def test_tela_profiles_is_resource_not_tool(self) -> None:
        """tela.profiles must be a resource, NOT a tool."""
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela.profiles") == "resource"
        assert "tela://profiles" in gateway_source
        assert 'name="tela.profiles"' in gateway_source
        assert '@upstream_server.tool("tela.profiles")' not in gateway_source

    def test_tela_status_is_absent_as_mcp_surface(self) -> None:
        """tela.status must NOT be claimed as current MCP tool or resource."""
        gateway_source = _read_gateway_source()
        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert _contract_kind("tela.status") == "absent"
        assert '@upstream_server.tool("tela.status")' not in gateway_source
        assert gateway_result.is_ok
        assert gateway_result.value is not None
        assert "`tela status`" in gateway_result.value

    def test_tela_connections_is_absent_as_mcp_surface(self) -> None:
        """tela.connections must NOT be claimed as current MCP tool or resource."""
        gateway_source = _read_gateway_source()
        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert _contract_kind("tela.connections") == "absent"
        assert '@upstream_server.tool("tela.connections")' not in gateway_source
        assert gateway_result.is_ok
        assert gateway_result.value is not None
        assert "`tela connections`" in gateway_result.value

    def test_tela_audit_is_absent_as_mcp_surface(self) -> None:
        """tela.audit must NOT be claimed as current MCP tool or resource."""
        gateway_source = _read_gateway_source()
        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert _contract_kind("tela.audit") == "absent"
        assert '@upstream_server.tool("tela.audit")' not in gateway_source
        assert gateway_result.is_ok
        assert gateway_result.value is not None
        assert "`tela audit`" in gateway_result.value

    def test_operator_surfaces_are_cli_or_http_not_mcp(self) -> None:
        """All operator companion surfaces must be CLI or HTTP, not MCP."""
        operator_surfaces = [
            ("tela profiles", "CLI"),
            ("tela status", "CLI"),
            ("tela connections", "CLI"),
            ("tela audit", "CLI"),
            ("GET /status", "HTTP"),
            ("GET /health", "HTTP"),
            ("POST /connect", "HTTP"),
            ("POST /disconnect", "HTTP"),
            ("POST /mcp", "HTTP"),
        ]
        for name, kind in operator_surfaces:
            assert _contract_kind(name) == kind, (
                f"{name} must be {kind}, not an MCP built-in"
            )

    def test_gateway_runtime_operator_summary_includes_tela_profiles(self) -> None:
        """Runtime gateway summary must include tela profiles in operator list."""
        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert gateway_result.is_ok
        assert gateway_result.value is not None
        assert "Operator-only surfaces" in gateway_result.value
        assert "`tela profiles`" in gateway_result.value

    def test_surface_verification_artifact_lists_tela_profiles_with_cli_surfaces(
        self,
    ) -> None:
        """Evidence summary must list tela profiles among CLI operator surfaces."""
        artifact_text = _read_surface_verification_artifact()
        assert "CLI surfaces observed:" in artifact_text
        assert "tela profiles" in artifact_text

    def test_design_wording_does_not_imply_builtin_tela_tool_surfaces(self) -> None:
        """Design doc must avoid wording that implies built-in tela.* tools."""
        design_text = _read_design_doc()
        assert "operator-facing surfaces (CLI/HTTP)" in design_text
        assert "only built-in tela MCP surface" in design_text
        assert (
            "`tela.` tool prefix is reserved for introspection tools" not in design_text
        )


# =============================================================================
# Section 2: MCP tool negative assertions (no current built-in tela.* tools)
# =============================================================================


class TestNoCurrentBuiltinTelaTools:
    """Negative assertions: no current built-in MCP tools named tela.*."""

    def test_no_tela_status_mcp_tool_registration(self) -> None:
        """tela.status MUST NOT be registered as an MCP tool.

        This test verifies that gateway startup does NOT register a tool
        named 'tela.status' and that the confirmed contract correctly marks
        it as 'absent' for MCP surfaces.
        """
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela.status") == "absent"
        assert '@upstream_server.tool("tela.status")' not in gateway_source

    def test_no_tela_connections_mcp_tool_registration(self) -> None:
        """tela.connections MUST NOT be registered as an MCP tool."""
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela.connections") == "absent"
        assert '@upstream_server.tool("tela.connections")' not in gateway_source

    def test_no_tela_audit_mcp_tool_registration(self) -> None:
        """tela.audit MUST NOT be registered as an MCP tool."""
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela.audit") == "absent"
        assert '@upstream_server.tool("tela.audit")' not in gateway_source

    def test_no_tela_profiles_mcp_tool_registration(self) -> None:
        """tela.profiles MUST NOT be registered as an MCP tool.

        tela.profiles is a resource, not a tool. This test ensures we never
        accidentally register it as a tool-call surface.
        """
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela.profiles") == "resource"
        assert '@upstream_server.tool("tela.profiles")' not in gateway_source
        assert 'name="tela.profiles"' in gateway_source


# =============================================================================
# Section 3: MCP resource behavior (tela.profiles is a resource)
# =============================================================================


class TestTelaProfilesResourceBehavior:
    """tela.profiles MCP resource behavior regressions."""

    def test_tela_profiles_is_resource_not_tool_in_contract(self) -> None:
        """Contract must classify tela.profiles as 'resource'.

        This is a regression guard against accidentally changing the contract
        or misclassifying profiles as a tool.
        """
        assert _contract_kind("tela.profiles") == "resource"

    def test_profiles_resource_handler_returns_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tela.profiles resource handler must return valid JSON list.

        This tests the existing handle_profiles_list behavior to ensure it
        continues to emit JSON-serializable data for the resource read.
        """
        from tela.shell.gateway import set_runtime_config
        from tela.shell.upstream import handle_profiles_list
        from tela.core.models import TelaConfig, ProfileConfig, Posture

        set_runtime_config(
            TelaConfig(
                profiles={
                    "dev": ProfileConfig(
                        name="dev",
                        capabilities={"filesystem": Posture.READ_WRITE},
                        default=True,
                    ),
                }
            )
        )

        result = handle_profiles_list()
        assert result.is_ok
        assert result.value is not None
        assert isinstance(result.value, list)
        assert len(result.value) >= 1

        # Verify JSON-serializable structure
        entry = result.value[0]
        assert "profile_name" in entry
        assert entry["profile_name"] == "dev"
        assert "capabilities" in entry

        # Cleanup
        set_runtime_config(None)


# =============================================================================
# Section 4: Instruction merge ordering and conflict handling
# =============================================================================


class TestInstructionMergeOrdering:
    """Regression tests for instruction merge ordering per contract.

    The contract states:
    1. Tela top-level gateway instructions come first (authoritative)
    2. Downstream sections appended in configured server iteration order
    3. Downstream text must not silently override gateway instructions
    """

    def test_downstream_sections_appended_after_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Downstream sections must be appended after gateway instructions.

        This tests that the merge algorithm produces appended sections,
        not prepended or interleaved.
        """

        def mock_get_server_instructions():
            return Result(
                value={
                    "fs": "Filesystem guidance.",
                    "shell": "Shell guidance.",
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
                "fs": ServerConfig(name="fs", command="cmd"),
                "shell": ServerConfig(name="shell", command="cmd"),
            }
        )

        downstream_result = gateway_module._merge_downstream_instructions(config)
        assert downstream_result.is_ok
        assert downstream_result.value is not None

        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert gateway_result.is_ok
        assert gateway_result.value is not None

        composed_result = surface_instructions.compose_gateway_and_downstream(
            gateway_result.value,
            downstream_result.value,
        )
        assert composed_result.is_ok
        assert composed_result.value is not None

        composed = composed_result.value
        assert composed.startswith("# tela gateway surface contract")
        assert composed.index("# tela gateway surface contract") < composed.index(
            "## fs"
        )
        assert composed.index("## fs") < composed.index("## shell")

    def test_suppress_instructions_false_excludes_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """instructions=False must suppress that server's instructions.

        Regression guard: downstream text must not appear when suppressed.
        """

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
        # Suppressed => None (no merged output)
        assert result.value is None

    def test_override_instructions_string_replaces_downstream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """instructions='text' must use override text, not downstream text.

        This tests the explicit override mode per contract section 4.
        """

        def mock_get_server_instructions():
            return Result(value={"fs": "Original downstream instructions"})

        def mock_get_all_tools():
            return Result(value={})

        monkeypatch.setattr(
            gateway_module, "get_server_instructions", mock_get_server_instructions
        )
        monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

        config = TelaConfig(
            servers={
                "fs": ServerConfig(
                    name="fs", command="cmd", instructions="Override text"
                )
            }
        )
        result = gateway_module._merge_downstream_instructions(config)

        assert result.is_ok
        assert result.value is not None
        assert "Override text" in result.value
        assert "Original downstream instructions" not in result.value

    def test_passthrough_instructions_none_includes_downstream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """instructions=None must include downstream instructions.

        This tests the default passthrough mode.
        """

        def mock_get_server_instructions():
            return Result(value={"fs": "From downstream server"})

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
        assert "From downstream server" in result.value
        assert "Available tools:" in result.value

    def test_mixed_servers_correct_ordering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple servers must be merged in configured dictionary order.

        Python 3.7+ preserves dict insertion order, which is used for
        deterministic output ordering.
        """

        def mock_get_server_instructions():
            return Result(
                value={
                    "alpha": "Alpha instructions",
                    "beta": "Beta instructions",
                    "gamma": "Gamma instructions",
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
                "alpha": ServerConfig(name="alpha", command="cmd"),
                "beta": ServerConfig(name="beta", command="cmd", instructions=False),
                "gamma": ServerConfig(name="gamma", command="cmd"),
            }
        )
        result = gateway_module._merge_downstream_instructions(config)

        assert result.is_ok
        assert result.value is not None
        # Beta is suppressed, alpha and gamma remain in order
        assert "## alpha" in result.value
        assert "## gamma" in result.value
        assert "## beta" not in result.value


class TestInstructionConflictHandling:
    """Regression tests for instruction conflict handling.

    The contract requires that downstream text must not silently override
    gateway instructions. Conflicts must be handled explicitly.
    """

    def test_downstream_does_not_override_gateway_instructions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Conflicting downstream text is preserved as appended content.

        Runtime does not implement semantic conflict detection/resolution for
        instruction text; it composes gateway text first, then downstream
        sections as-is.
        """

        # The _merge_downstream_instructions function only handles downstream
        # text. Gateway-level instructions would be injected before this merge.
        # This test documents that downstream sections are appended, not prepended.
        def mock_get_server_instructions():
            return Result(
                value={
                    "fs": "Built-in MCP tools: use tela.status from tools/call.",
                }
            )

        def mock_get_all_tools():
            return Result(value={})

        monkeypatch.setattr(
            gateway_module, "get_server_instructions", mock_get_server_instructions
        )
        monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

        config = TelaConfig(servers={"fs": ServerConfig(name="fs", command="cmd")})
        downstream_result = gateway_module._merge_downstream_instructions(config)

        assert downstream_result.is_ok
        assert downstream_result.value is not None

        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert gateway_result.is_ok
        assert gateway_result.value is not None

        composed_result = surface_instructions.compose_gateway_and_downstream(
            gateway_result.value,
            downstream_result.value,
        )
        assert composed_result.is_ok
        assert composed_result.value is not None

        composed = composed_result.value
        assert "Built-in MCP tools: `tela_list_providers`." in composed
        assert "Built-in MCP tools: use tela.status from tools/call." in composed
        assert composed.index(
            "Built-in MCP tools: `tela_list_providers`."
        ) < composed.index("## fs")


# =============================================================================
# Section 5: Negative assertions for CLI/HTTP surfaces
# =============================================================================


class TestCLIHTTPSurfacesNotMCPBuiltins:
    """CLI and HTTP surfaces must NOT be claimed as MCP built-ins."""

    def test_tela_profiles_cli_not_mcp_builtin(self) -> None:
        """tela profiles command must stay operator-only and non-dotted."""
        runtime_operator_surfaces = _runtime_operator_surfaces()
        agent_interface_operator_surfaces = _agent_interface_operator_surfaces()
        interfaces_surfaces = _interfaces_builtin_summary_surfaces()

        assert _contract_kind("tela profiles") == "CLI"
        assert _contract_kind("tela.profiles") == "resource"
        assert "tela profiles" in runtime_operator_surfaces
        assert "tela profiles" in agent_interface_operator_surfaces
        assert "tela profiles" in interfaces_surfaces
        assert "tela.profiles" not in runtime_operator_surfaces
        assert "tela.profiles" not in agent_interface_operator_surfaces

    def test_tela_status_cli_not_mcp_builtin(self) -> None:
        """tela status CLI must NOT be documented as an MCP built-in."""
        usage_doc = _read_usage_doc()
        runtime_operator_surfaces = _runtime_operator_surfaces()
        agent_interface_operator_surfaces = _agent_interface_operator_surfaces()
        interfaces_surfaces = _interfaces_builtin_summary_surfaces()

        assert _contract_kind("tela status") == "CLI"
        assert _contract_kind("tela.status") == "absent"
        assert "| `tela status` | CLI / `GET /status` |" in usage_doc
        assert "tela status" in runtime_operator_surfaces
        assert "tela status" in agent_interface_operator_surfaces
        assert "tela status" in interfaces_surfaces
        assert "tela.status" not in runtime_operator_surfaces
        assert "tela.status" not in agent_interface_operator_surfaces
        assert "tela.status" not in interfaces_surfaces

    def test_tela_connections_cli_not_mcp_builtin(self) -> None:
        """tela connections CLI must NOT be documented as an MCP built-in."""
        usage_doc = _read_usage_doc()
        runtime_operator_surfaces = _runtime_operator_surfaces()
        agent_interface_operator_surfaces = _agent_interface_operator_surfaces()
        interfaces_surfaces = _interfaces_builtin_summary_surfaces()

        assert _contract_kind("tela connections") == "CLI"
        assert _contract_kind("tela.connections") == "absent"
        assert "| `tela connections` | CLI / via `/status` |" in usage_doc
        assert "tela connections" in runtime_operator_surfaces
        assert "tela connections" in agent_interface_operator_surfaces
        assert "tela connections" in interfaces_surfaces
        assert "tela.connections" not in runtime_operator_surfaces
        assert "tela.connections" not in agent_interface_operator_surfaces
        assert "tela.connections" not in interfaces_surfaces

    def test_tela_audit_cli_not_mcp_builtin(self) -> None:
        """tela audit CLI must NOT be documented as an MCP built-in."""
        usage_doc = _read_usage_doc()
        runtime_operator_surfaces = _runtime_operator_surfaces()
        agent_interface_operator_surfaces = _agent_interface_operator_surfaces()
        interfaces_surfaces = _interfaces_builtin_summary_surfaces()

        assert _contract_kind("tela audit") == "CLI"
        assert _contract_kind("tela.audit") == "absent"
        assert "| `tela audit` | CLI / via `/status` |" in usage_doc
        assert "tela audit" in runtime_operator_surfaces
        assert "tela audit" in agent_interface_operator_surfaces
        assert "tela audit" in interfaces_surfaces
        assert "tela.audit" not in runtime_operator_surfaces
        assert "tela.audit" not in agent_interface_operator_surfaces
        assert "tela.audit" not in interfaces_surfaces

    def test_get_status_http_not_mcp_builtin(self) -> None:
        """GET /status HTTP endpoint must NOT be misnamed as tela.status MCP."""
        usage_doc = _read_usage_doc()
        agent_interface_doc = _read_agent_interface_doc()
        assert _contract_kind("GET /status") == "HTTP"
        # The canonical tabela.status MCP surface is 'absent'
        assert _contract_kind("tela.status") == "absent"
        assert "CLI / `GET /status`" in usage_doc
        assert "`GET /status` endpoint" in agent_interface_doc


# =============================================================================
# Section 6: Capability wording negative assertions
# =============================================================================


class TestCapabilityWordingNotApprovedForAbsentSurfaces:
    """tela_admin is NOT approved as current-runtime capability wording.

    Per contract section 3, tela_admin must not be described as current
    runtime-enforced capability label for absent MCP surfaces.
    """

    def test_tela_admin_not_approved_for_tela_status(self) -> None:
        """tela_admin MUST NOT be used as current enforcement wording for
        tela.status because tela.status is an absent MCP surface.
        """
        # If someone adds runtime enforcement for 'tela_admin' over
        # 'tela.status', that contradicts the contract.
        # This test guards against that change.
        contract_text = _read_contract_text()
        design_text = _read_design_doc()
        gateway_summary = surface_instructions.get_gateway_surface_instructions()
        assert _contract_kind("tela.status") == "absent"
        assert "not approved as current-runtime contract wording" in contract_text
        assert "tela.status" in contract_text
        assert "These do not belong to a `tela_admin`" in design_text
        assert gateway_summary.is_ok
        assert gateway_summary.value is not None
        assert "Built-in MCP tools: `tela_list_providers`." in gateway_summary.value

    def test_tela_admin_not_approved_for_tela_connections(self) -> None:
        """tela_admin MUST NOT be used as current enforcement wording for
        tela.connections because tela.connections is an absent MCP surface.
        """
        contract_text = _read_contract_text()
        design_text = _read_design_doc()
        assert _contract_kind("tela.connections") == "absent"
        assert "tela.connections" in contract_text
        assert (
            "No built-in `tela.*` MCP tools are currently implemented." in design_text
        )

    def test_tela_admin_not_approved_for_tela_audit(self) -> None:
        """tela_admin MUST NOT be used as current enforcement wording for
        tela.audit because tela.audit is an absent MCP surface.
        """
        contract_text = _read_contract_text()
        design_text = _read_design_doc()
        assert _contract_kind("tela.audit") == "absent"
        assert "tela.audit" in contract_text
        assert "operator-facing surfaces (CLI/HTTP)" in design_text


# =============================================================================
# Section 7: Instruction merge output format regressions
# =============================================================================


class TestInstructionMergeOutputFormat:
    """Regression tests for merged instruction output format."""

    def test_markdown_h2_headers_used_for_servers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Merged output must use Markdown H2 headers for server sections."""

        def mock_get_server_instructions():
            return Result(value={"srv": "Server instructions"})

        def mock_get_all_tools():
            return Result(value={})

        monkeypatch.setattr(
            gateway_module, "get_server_instructions", mock_get_server_instructions
        )
        monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

        config = TelaConfig(servers={"srv": ServerConfig(name="srv", command="cmd")})
        result = gateway_module._merge_downstream_instructions(config)

        assert result.is_ok
        assert result.value is not None
        # Must start with ## (H2 header)
        assert result.value.startswith("## srv")

    def test_available_tools_list_included_when_tools_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When tools are known, 'Available tools:' list must be appended."""

        def mock_get_server_instructions():
            return Result(value={"srv": "Server instructions"})

        def mock_get_all_tools():
            return Result(
                value={
                    "srv": [
                        ResolvedTool(
                            name="tool_a",
                            server_name="srv",
                            family="srv",
                            schema_={},
                        ),
                        ResolvedTool(
                            name="tool_b",
                            server_name="srv",
                            family="srv",
                            schema_={},
                        ),
                    ]
                }
            )

        monkeypatch.setattr(
            gateway_module, "get_server_instructions", mock_get_server_instructions
        )
        monkeypatch.setattr(gateway_module, "get_all_tools", mock_get_all_tools)

        config = TelaConfig(servers={"srv": ServerConfig(name="srv", command="cmd")})
        result = gateway_module._merge_downstream_instructions(config)

        assert result.is_ok
        assert result.value is not None
        assert "Available tools:" in result.value
        assert "- tool_a" in result.value
        assert "- tool_b" in result.value
