"""Loop4 repro coverage for operator-surface remediation blockers.

These checks assert runtime/docs behavior directly (not meta-assertions over test
source text) to keep the blocker fix pack stable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tela.commands.profiles_cmd import profiles_command
from tela.shell import surface_instructions


_LEGACY_PROFILE_RESOURCE = "tela" + ".profiles"


PROJECT_ROOT = Path(__file__).resolve().parents[2]
README_PATH = PROJECT_ROOT / "README.md"
EVIDENCE_PATH = PROJECT_ROOT / "evidence" / "surface_taxonomy_verification.md"


def _readme_operator_section() -> str:
    text = README_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"### Operator Surfaces\n\n.*?\n\n\*\*Note:\*\*",
        text,
        re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def _surface_names_from_markdown_bullets(section: str) -> set[str]:
    return set(re.findall(r"- `([^`]+)`", section))


class TestLoop4OperatorSurfaceRemediation:
    """Behavioral regressions for loop4 operator-surface fixes."""

    def test_runtime_gateway_operator_surfaces_include_profiles(self) -> None:
        """Runtime gateway contract must include tela profiles operator surface."""
        gateway_result = surface_instructions.get_gateway_surface_instructions()
        assert gateway_result.is_ok
        assert gateway_result.value is not None
        assert "`tela profiles`" in gateway_result.value

    def test_readme_operator_surface_summary_uses_cli_http_names(self) -> None:
        """README operator summary must include CLI names (including tela profiles)."""
        section = _readme_operator_section()
        listed = _surface_names_from_markdown_bullets(section)
        assert "tela profiles" in listed
        assert "tela status" in listed
        assert "tela connections" in listed
        assert "tela audit" in listed
        assert _LEGACY_PROFILE_RESOURCE not in listed

    def test_profiles_command_runs_and_lists_configured_profile(
        self, tmp_path: Path, capsys
    ) -> None:
        """Operator CLI surface must be executable at runtime, not just documented."""
        config_path = tmp_path / "tela.yaml"
        config_path.write_text(
            """
servers: {}
profiles:
  developer:
    capabilities: {}
    default: true
auth:
  mode: open
audit:
  level: L1
            """.strip()
            + "\n",
            encoding="utf-8",
        )

        result = profiles_command(config_path=str(config_path), json_output=True)
        assert result.is_ok
        assert result.value == 0

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "developer" in payload

    def test_evidence_tracks_readme_consistency_claims(self) -> None:
        """Evidence artifact must explicitly record README consistency checks."""
        evidence = EVIDENCE_PATH.read_text(encoding="utf-8")
        assert "README consistency" in evidence
        assert "README operator summary includes `tela profiles`" in evidence
