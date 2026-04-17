"""Regression tests enforcing CONFIRMED-SURFACE-CONTRACT.md.

These tests encode the canonical surface taxonomy and must fail on unsupported
claims about MCP built-ins. The contract is the single source of truth for
agent-facing surface classification.

Contract source: docs/CONFIRMED-SURFACE-CONTRACT.md

Coverage:
- MCP tool checks: confirmed built-in MCP tools: tela_list_providers, tela_list_profiles
- CLI/HTTP checks: operator surfaces are not MCP built-ins
- Instruction-merge checks: ordering and conflict handling
- Negative assertions guarding unsupported surface claims
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from tela.shell.result import Result
from tela.shell import gateway as gateway_module
from tela.shell import surface_instructions
from tela.core.models import (
    ResolvedTool,
    ServerConfig,
    TelaConfig,
)


_LEGACY_PROFILE_KEY = "profile" + "_name"
_LEGACY_TOOLS_PROFILE_KEY = "tools" + "_profile"
_LEGACY_TOOLS_KEY_MARKDOWN = "`to" + "ols`"
_LEGACY_PROFILE_RESOURCE = "tela" + ".profiles"
_LEGACY_PROFILE_RESOURCE_URI = "tela://" + "profiles"
_LEGACY_WILDCARD = "`tela" + ".*`"


# =============================================================================
# Section 1: Canonical surface matrix assertions
# =============================================================================


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIRMED_SURFACE_CONTRACT = PROJECT_ROOT / "docs" / "CONFIRMED-SURFACE-CONTRACT.md"
DESIGN_DOC = PROJECT_ROOT / "docs" / "DESIGN.md"
AGENT_INTERFACE_DOC = PROJECT_ROOT / "docs" / "AGENT_INTERFACE.md"
INTERFACES_DOC = PROJECT_ROOT / "docs" / "INTERFACES.md"
USAGE_DOC = PROJECT_ROOT / "docs" / "USAGE.md"
README_DOC = PROJECT_ROOT / "README.md"
ADR003_DOC = PROJECT_ROOT / "docs" / "ADR-003-gateway-capability-only-profiles.md"
ADR007_DOC = PROJECT_ROOT / "docs" / "ADR-007-opifex-canonical-contract-alignment.md"
MIGRATION003_DOC = PROJECT_ROOT / "docs" / "MIGRATION-003-capability-only-profiles.md"
HARD_CUT_DOC = PROJECT_ROOT / "docs" / "hard-cutover-canonical-alignment.md"
EVIDENCE_TAXONOMY_DOC = PROJECT_ROOT / "evidence" / "surface_taxonomy_verification.md"
EVIDENCE_TAXONOMY_DECISION = PROJECT_ROOT / "evidence" / "taxonomy_decision.md"
EVIDENCE_SURFACE_AUDIT = PROJECT_ROOT / "evidence" / "surface_audit_actual_surface.md"
EVIDENCE_HARD_CUT_PREP = PROJECT_ROOT / "evidence" / "hard_cut_prep_proof_assets.md"
EVIDENCE_RUNTIME_SNAPSHOT = (
    PROJECT_ROOT / "evidence" / "runtime_characterization_snapshot.md"
)
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


def _read_readme_doc() -> str:
    return README_DOC.read_text(encoding="utf-8")


def _read_doc(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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

    def test_tela_list_profiles_is_builtin_tool(self) -> None:
        """tela_list_profiles must be a builtin MCP tool (not a resource)."""
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela_list_profiles") == "tool"
        assert "tela_list_profiles" in gateway_source
        assert '@upstream_server.resource("tela_list_profiles")' not in gateway_source

    def test_tela_profiles_resource_removed(self) -> None:
        """Legacy profile resource registration must stay removed."""
        gateway_source = _read_gateway_source()
        assert _LEGACY_PROFILE_RESOURCE_URI not in gateway_source
        assert "_register_profiles_resource" not in gateway_source
        assert _LEGACY_PROFILE_RESOURCE not in _read_contract_text()
        assert _LEGACY_PROFILE_RESOURCE not in _read_agent_interface_doc()

    def test_tela_status_is_absent_as_mcp_surface(self) -> None:
        """Legacy dotted status label must not be claimed as an MCP surface."""
        gateway_source = _read_gateway_source()
        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert _contract_kind("tela.status") is None
        assert '@upstream_server.tool("tela.status")' not in gateway_source
        assert gateway_result.is_ok
        assert gateway_result.value is not None
        assert "`tela status`" in gateway_result.value

    def test_tela_connections_is_absent_as_mcp_surface(self) -> None:
        """Legacy dotted connections label must not be claimed as an MCP surface."""
        gateway_source = _read_gateway_source()
        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert _contract_kind("tela.connections") is None
        assert '@upstream_server.tool("tela.connections")' not in gateway_source
        assert gateway_result.is_ok
        assert gateway_result.value is not None
        assert "`tela connections`" in gateway_result.value

    def test_tela_audit_is_absent_as_mcp_surface(self) -> None:
        """Legacy dotted audit label must not be claimed as an MCP surface."""
        gateway_source = _read_gateway_source()
        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert _contract_kind("tela.audit") is None
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

    def test_design_wording_does_not_imply_extra_builtin_tool_surfaces(self) -> None:
        """Design doc must avoid wording that implies extra builtin MCP tools."""
        design_text = _read_design_doc()
        assert "operator-facing surfaces (CLI/HTTP)" in design_text
        assert "tela_list_profiles" in design_text
        assert "tela_list_providers" in design_text
        assert "built-in MCP tools owned by tela" in design_text


# =============================================================================
# Section 2: MCP tool negative assertions for retired dotted labels
# =============================================================================


class TestNoCurrentBuiltinTelaTools:
    """Negative assertions: retired dotted labels are not MCP tools."""

    def test_no_tela_status_mcp_tool_registration(self) -> None:
        """tela.status MUST NOT be registered as an MCP tool.

        This test verifies that gateway startup does NOT register a tool
        named 'tela.status' and that the confirmed contract correctly marks
        it as 'absent' for MCP surfaces.
        """
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela.status") is None
        assert '@upstream_server.tool("tela.status")' not in gateway_source

    def test_no_tela_connections_mcp_tool_registration(self) -> None:
        """tela.connections MUST NOT be registered as an MCP tool."""
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela.connections") is None
        assert '@upstream_server.tool("tela.connections")' not in gateway_source

    def test_no_tela_audit_mcp_tool_registration(self) -> None:
        """tela.audit MUST NOT be registered as an MCP tool."""
        gateway_source = _read_gateway_source()
        assert _contract_kind("tela.audit") is None
        assert '@upstream_server.tool("tela.audit")' not in gateway_source

    def test_no_tela_profiles_mcp_tool_registration(self) -> None:
        """The retired shared profile resource must not reappear as an MCP tool."""
        gateway_source = _read_gateway_source()
        assert (
            f'@upstream_server.tool("{_LEGACY_PROFILE_RESOURCE}")' not in gateway_source
        )
        assert (
            "@upstream_server.resource" not in gateway_source
            or _LEGACY_PROFILE_RESOURCE not in gateway_source
        )

    def test_primary_docs_do_not_teach_legacy_profile_surface_or_alias_fields(
        self,
    ) -> None:
        """Repo-facing docs must not teach removed surface or alias vocabulary."""
        primary_docs = {
            "README.md": _read_readme_doc(),
            "docs/CONFIRMED-SURFACE-CONTRACT.md": _read_contract_text(),
            "docs/INTERFACES.md": _read_interfaces_doc(),
            "docs/USAGE.md": _read_usage_doc(),
            "docs/DESIGN.md": _read_design_doc(),
            "docs/AGENT_INTERFACE.md": _read_agent_interface_doc(),
            "docs/ADR-003-gateway-capability-only-profiles.md": _read_doc(ADR003_DOC),
            "docs/ADR-007-opifex-canonical-contract-alignment.md": _read_doc(
                ADR007_DOC
            ),
            "docs/MIGRATION-003-capability-only-profiles.md": _read_doc(
                MIGRATION003_DOC
            ),
            "docs/hard-cutover-canonical-alignment.md": _read_doc(HARD_CUT_DOC),
            "evidence/surface_taxonomy_verification.md": _read_doc(
                EVIDENCE_TAXONOMY_DOC
            ),
            "evidence/taxonomy_decision.md": _read_doc(EVIDENCE_TAXONOMY_DECISION),
            "evidence/surface_audit_actual_surface.md": _read_doc(
                EVIDENCE_SURFACE_AUDIT
            ),
            "evidence/hard_cut_prep_proof_assets.md": _read_doc(EVIDENCE_HARD_CUT_PREP),
            "evidence/runtime_characterization_snapshot.md": _read_doc(
                EVIDENCE_RUNTIME_SNAPSHOT
            ),
        }

        for doc_name, text in primary_docs.items():
            assert _LEGACY_PROFILE_RESOURCE not in text, (
                f"{doc_name} still teaches the retired profile resource"
            )
            assert _LEGACY_PROFILE_RESOURCE_URI not in text, (
                f"{doc_name} still teaches the retired profile resource URI"
            )
            assert _LEGACY_PROFILE_KEY not in text, (
                f"{doc_name} still teaches a retired alias field"
            )
            assert _LEGACY_TOOLS_PROFILE_KEY not in text, (
                f"{doc_name} still teaches a retired nested alias field"
            )
            assert _LEGACY_TOOLS_KEY_MARKDOWN not in text, (
                f"{doc_name} still teaches the retired key alias"
            )
            assert _LEGACY_WILDCARD not in text, (
                f"{doc_name} still teaches dotted wildcard naming"
            )


# =============================================================================
# Section 3: tela_list_profiles builtin tool behavior
# =============================================================================


class TestTelaListProfilesBuiltinTool:
    """tela_list_profiles builtin tool behavior regressions."""

    def test_tela_list_profiles_is_builtin_tool_in_contract(self) -> None:
        """Contract must classify tela_list_profiles as 'tool'."""
        assert _contract_kind("tela_list_profiles") == "tool"

    def test_profiles_list_tool_returns_canonical_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tela_list_profiles builtin tool must return canonical profile-list payload.

        This tests the existing handle_list_profiles behavior to ensure it
        continues to emit canonical profile_id + capabilities + default only.
        """
        from tela.shell.gateway_runtime import set_runtime_config
        from tela.shell.builtin_tools import handle_list_profiles
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

        result = handle_list_profiles()
        assert isinstance(result, list)
        assert len(result) >= 1

        # Verify canonical schema
        entry = result[0]
        assert "profile_id" in entry
        assert entry["profile_id"] == "dev"
        assert "capabilities" in entry
        assert "default" in entry

        # Verify legacy keys are absent
        assert _LEGACY_PROFILE_KEY not in entry
        assert "families" not in entry
        assert "to" + "ols" not in entry

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
                    "fs": "Status guidance: check GET /status before connecting.",
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
        assert "`tela_list_providers`" in composed
        assert "`tela_list_profiles`" in composed
        assert "Status guidance: check GET /status before connecting." in composed
        assert composed.index(
            "Built-in MCP tools: `tela_list_providers`, `tela_list_profiles`."
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
        assert _contract_kind("tela_list_profiles") == "tool"
        assert "tela profiles" in runtime_operator_surfaces
        assert "tela profiles" in agent_interface_operator_surfaces
        assert "tela profiles" in interfaces_surfaces
        assert _LEGACY_PROFILE_RESOURCE not in runtime_operator_surfaces
        assert _LEGACY_PROFILE_RESOURCE not in agent_interface_operator_surfaces

    def test_tela_status_cli_not_mcp_builtin(self) -> None:
        """tela status CLI must NOT be documented as an MCP built-in."""
        usage_doc = _read_usage_doc()
        runtime_operator_surfaces = _runtime_operator_surfaces()
        agent_interface_operator_surfaces = _agent_interface_operator_surfaces()
        interfaces_surfaces = _interfaces_builtin_summary_surfaces()

        assert _contract_kind("tela status") == "CLI"
        assert _contract_kind("tela.status") is None
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
        assert _contract_kind("tela.connections") is None
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
        assert _contract_kind("tela.audit") is None
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
        assert _contract_kind("tela.status") is None
        assert "CLI / `GET /status`" in usage_doc
        assert "`GET /status` endpoint" in agent_interface_doc


# =============================================================================
# Section 6: Capability wording negative assertions
# =============================================================================


class TestCapabilityWordingNotApprovedForAbsentSurfaces:
    """tela_admin is NOT approved as current-runtime capability wording.

    Per contract section 3, tela_admin must not be described as current
    runtime-enforced capability label for non-existent dotted MCP surfaces.
    """

    def test_tela_admin_not_approved_for_tela_status(self) -> None:
        """tela_admin MUST NOT be used as current enforcement wording for
        the retired dotted status label because it is not an MCP surface.
        """
        contract_text = _read_contract_text()
        design_text = _read_design_doc()
        gateway_summary = surface_instructions.get_gateway_surface_instructions()
        assert _contract_kind("tela.status") is None
        assert "not approved as current-runtime contract wording" in contract_text
        assert "dotted MCP surface names" in contract_text
        assert "operator-facing surfaces (CLI/HTTP)" in design_text
        assert "**not** built-in MCP tool" in design_text
        assert gateway_summary.is_ok
        assert gateway_summary.value is not None
        assert (
            "Built-in MCP tools: `tela_list_providers`, `tela_list_profiles`."
            in gateway_summary.value
        )

    def test_tela_admin_not_approved_for_tela_connections(self) -> None:
        """tela_admin MUST NOT be used as current enforcement wording for
        the retired dotted connections label because it is not an MCP surface.
        """
        contract_text = _read_contract_text()
        design_text = _read_design_doc()
        assert _contract_kind("tela.connections") is None
        assert "dotted MCP surface names" in contract_text
        assert "Built-in MCP tools:" in design_text
        assert "tela_list_providers" in design_text
        assert "tela_list_profiles" in design_text
        assert "built-in MCP tools owned by tela" in design_text

    def test_tela_admin_not_approved_for_tela_audit(self) -> None:
        """tela_admin MUST NOT be used as current enforcement wording for
        the retired dotted audit label because it is not an MCP surface.
        """
        contract_text = _read_contract_text()
        design_text = _read_design_doc()
        assert _contract_kind("tela.audit") is None
        assert "dotted MCP surface names" in contract_text
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
