"""Tests for the mcp-tela repo-local shared-surface gate."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_repo_text(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _load_gate_module():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "scripts/ci/mcp_tela_shared_surface_gate.py"
    spec = importlib.util.spec_from_file_location("mcp_tela_shared_surface_gate", module_path)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load repo-local gate module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_nested_gateway_docs_parity() -> None:
    """Repo-local parity check for nested-gateway docs and examples."""

    docs = {
        "adr": _read_repo_text("docs/ADR-010-nested-gateway-ergonomics.md"),
        "interfaces": _read_repo_text("docs/INTERFACES.md"),
        "usage": _read_repo_text("docs/USAGE.md"),
        "design": _read_repo_text("docs/DESIGN.md"),
        "agent": _read_repo_text("docs/AGENT_INTERFACE.md"),
        "confirmed": _read_repo_text("docs/CONFIRMED-SURFACE-CONTRACT.md"),
        "readme": _read_repo_text("README.md"),
        "example": _read_repo_text("tela.yaml.example"),
    }
    combined = "\n".join(docs.values())

    assert "v1/v2" not in combined
    assert "CLI/HTTP" not in combined
    assert "CLI /" not in combined
    assert "tool_prefix" in combined
    assert "exclude_tools" in combined
    assert "nested_gateway: true" in combined
    assert "headers:" in docs["usage"] and "headers:" in docs["example"]
    assert "instructions" in docs["usage"] and "instructions" in docs["interfaces"]

    assert "tool_prefix: \"host_\"" in docs["usage"]
    assert "tool_prefix: \"prod_\"" in docs["usage"]
    assert "tool_prefix: \"work_\"" in docs["usage"] or "tool_prefix: \"work_\"" in docs["example"]
    assert "host_tela_list_providers" in combined
    assert "tela.list" not in combined
    assert "host.tela" not in combined

    for text in (docs["usage"], docs["example"]):
        assert "exclude_tools:" in text
        assert '- "tela_list_providers"' in text
        assert '- "tela_list_profiles"' in text

    assert (
        "downstream exposes `tela_list_providers` or `tela_list_profiles` "
        "with omitted/empty `tool_prefix`"
    ) in docs["adr"]
    assert "nested_gateway: true` requires non-empty `tool_prefix`" in docs["interfaces"]
    assert "raw child Tela built-ins with omitted/empty `tool_prefix`" in docs["usage"]
    assert "same diagnostic" in docs["usage"]
    assert "valid prefix preserves prefixed child built-ins" in docs["usage"]
    assert "Detection never silently hides tools" in docs["usage"]
    assert "does not silently hide tools" not in docs["readme"]

    assert "CLI surfaces:" in docs["usage"]
    assert "HTTP surfaces:" in docs["usage"]
    assert "only gateway-owned built-in MCP tools" in docs["agent"]
    assert "gateway-owned built-in MCP tools" in docs["confirmed"]


@pytest.mark.parametrize(("code", "expect_success"), [(0, True), (5, True), (2, False)])
def test_gate_propagates_real_subprocess_exit_status(
    code: int, expect_success: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_gate_module()
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    command = [sys.executable, "-c", f"raise SystemExit({code})"]
    if code:
        with pytest.raises(SystemExit) as exc:
            module._run(command, expect_success=expect_success)
        assert exc.value.code == code
    else:
        assert module._run(command, expect_success=True).returncode == 0


@pytest.mark.parametrize("detect_drift", [True, False])
def test_expected_red_restores_readme_after_real_pytest(
    detect_drift: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_gate_module()
    original = "# Test project\n"
    _write(tmp_path / "README.md", original)
    # Exercise subprocess execution and restoration in an isolated checkout.
    # The real documentation test is exercised by the repository gate itself.
    assertion = (
        "assert 'profile_name' not in Path('README.md').read_text()"
        if detect_drift else "assert Path('README.md').exists()"
    )
    _write(
        tmp_path / "test_docs.py",
        "from pathlib import Path\ndef test_docs():\n    " + assertion + "\n",
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "EXPECTED_RED_TEST", "test_docs.py::test_docs")
    if detect_drift:
        module.run_expected_red()
    else:
        with pytest.raises(SystemExit, match="Expected seeded drift to fail"):
            module.run_expected_red()
    assert (tmp_path / "README.md").read_text() == original
