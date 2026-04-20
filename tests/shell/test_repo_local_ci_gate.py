"""Tests for the mcp-tela repo-local shared-surface gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


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


def _seed_layout(tmp_path: Path) -> tuple[Path, Path, str]:
    tela_root = tmp_path / "mcp-tela"
    opifex_root = tmp_path / "opifex"
    frozen_ref = "981a3294fe363ea137547ffa292829e21206e981"

    _write(
        tela_root / "design/opifex-frozen-authority-packet.json",
        json.dumps(
            {
                "repository": "Tefx/opifex",
                "ref": frozen_ref,
                "packet_doc": "design/cross-repo-followup-packet.md",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _write(opifex_root / "design/final-canonical-contract.md", "# contract\n")
    _write(opifex_root / "design/cross-repo-followup-packet.md", "# packet\n")
    _write(
        opifex_root / "conformance/forbidden_vocabulary.yaml",
        json.dumps(
            {
                "shared_forbidden_fields": ["profile_name", "tools_profile", "families"],
                "forbidden_shared_tool_names": [{"pattern": r"^[a-z]+\.[a-z_]+$"}],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _write(
        opifex_root / "conformance/shared_surfaces.yaml",
        json.dumps(
            {
                "global_controls": {
                    "frozen_followup_packet_ref": "design/cross-repo-followup-packet.md"
                },
                "gate_policy": {
                    "switch_blocking": {"by_exposure": ["shared", "alternate", "local_runtime"]},
                    "frozen_followup_packet_required_before_downstream_ci": True,
                },
                "frozen_followup_packet": {
                    "packet_doc": "design/cross-repo-followup-packet.md",
                    "required_before_downstream_ci": True,
                },
                "shared_surfaces": [
                    {
                        "id": "tela_initialize_token_mode",
                        "owner_repo": "mcp-tela",
                        "exposure": "shared",
                        "case_matrix": [
                            "conformance/case_matrix/mcp-tela/tela.initialize.token_mode.yaml"
                        ],
                    },
                    {
                        "id": "tela_tools_call_builtin_tela_list_providers",
                        "owner_repo": "mcp-tela",
                        "exposure": "supporting",
                        "case_matrix": [
                            "conformance/case_matrix/mcp-tela/tela.tools.call.builtin.tela_list_providers.yaml"
                        ],
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _write(
        opifex_root / "conformance/case_matrix/mcp-tela/tela.initialize.token_mode.yaml",
        json.dumps(
            {
                "surface_id": "tela_initialize_token_mode",
                "cases": [{"id": "happy_path", "class": "happy_path"}],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _write(
        opifex_root
        / "conformance/case_matrix/mcp-tela/tela.tools.call.builtin.tela_list_providers.yaml",
        json.dumps(
            {
                "surface_id": "tela_tools_call_builtin_tela_list_providers",
                "cases": [{"id": "happy_path", "class": "happy_path"}],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return tela_root, opifex_root, frozen_ref


def test_load_authority_snapshot_derives_owned_and_blocking_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_gate_module()
    tela_root, opifex_root, frozen_ref = _seed_layout(tmp_path)
    monkeypatch.setattr(module, "PROJECT_ROOT", tela_root)
    monkeypatch.setattr(module, "_resolve_opifex_root", lambda: opifex_root)
    monkeypatch.setattr(module, "_git_head", lambda root: frozen_ref)

    snapshot = module.load_authority_snapshot()

    assert snapshot.packet_repository == "Tefx/opifex"
    assert snapshot.packet_ref == frozen_ref
    assert snapshot.packet_doc == "design/cross-repo-followup-packet.md"
    assert snapshot.owned_surface_ids == (
        "tela_initialize_token_mode",
        "tela_tools_call_builtin_tela_list_providers",
    )
    assert snapshot.blocking_surface_ids == ("tela_initialize_token_mode",)
    assert snapshot.owned_case_matrix_paths == (
        "conformance/case_matrix/mcp-tela/tela.initialize.token_mode.yaml",
        "conformance/case_matrix/mcp-tela/tela.tools.call.builtin.tela_list_providers.yaml",
    )


def test_load_authority_snapshot_rejects_head_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_gate_module()
    tela_root, opifex_root, _ = _seed_layout(tmp_path)
    monkeypatch.setattr(module, "PROJECT_ROOT", tela_root)
    monkeypatch.setattr(module, "_resolve_opifex_root", lambda: opifex_root)
    monkeypatch.setattr(
        module,
        "_git_head",
        lambda root: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )

    with pytest.raises(RuntimeError, match="pinned authority ref"):
        module.load_authority_snapshot()
