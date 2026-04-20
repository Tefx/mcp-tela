"""Repo-local shared-surface CI gate for mcp-tela.

Authority basis:
- user step `ci_repo_local_rollout.rollout_mcp_tela_ci`
- `design/opifex-frozen-authority-packet.json`
- `opifex/conformance/shared_surfaces.yaml`
- `opifex/conformance/forbidden_vocabulary.yaml`
- `opifex/design/final-canonical-contract.md`

This wrapper makes the repo-local gate executable in both developer worktrees
and GitHub Actions by resolving the pinned opifex authority checkout explicitly
and deriving repo-local scope from authoritative surface metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import os
from pathlib import Path
import subprocess
import sys

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from opifex_authority import (  # noqa: E402 - direct script execution needs sibling helper import after sys.path bootstrap
    AUTHORITY_LOCK_PATH,
    FrozenAuthorityPin,
    load_frozen_authority_pin,
    require_pinned_checkout,
    resolve_opifex_root,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTEST_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "schema-parity",
        (
            "tests/test_vendor_schema_parity.py",
        ),
    ),
    (
        "shared-surface-runtime",
        (
            "tests/core/test_hard_cut_vocab.py",
            "tests/shell/test_hard_cut_shared_surfaces.py",
            "tests/shell/test_shared_surface_runtime_hard_cut.py",
        ),
    ),
    (
        "shared-surface-contract",
        (
            "tests/shell/test_surface_contract.py",
            "tests/shell/test_builtin_tools.py",
            "tests/shell/test_upstream.py",
            "tests/shell/test_gateway.py",
        ),
    ),
)
EXPECTED_RED_TEST = (
    "tests/shell/test_surface_contract.py::"
    "TestNoCurrentBuiltinTelaTools::"
    "test_primary_docs_do_not_teach_legacy_profile_surface_or_alias_fields"
)
EXPECTED_RED_SENTINEL = (
    "\n\n<!-- repo-local-expected-red-sentinel -->\n"
    "Do not teach shared forbidden vocabulary such as `profile_name`.\n"
)


@dataclass(frozen=True)
class AuthoritySnapshot:
    """Resolved opifex authority files required by the repo-local gate."""

    opifex_root: Path
    packet_repository: str
    packet_ref: str
    packet_doc: str
    shared_surfaces_path: Path
    forbidden_vocabulary_path: Path
    canonical_contract_path: Path
    owned_surface_ids: tuple[str, ...]
    blocking_surface_ids: tuple[str, ...]
    owned_case_matrix_paths: tuple[str, ...]
    owned_case_classes: tuple[str, ...]
    forbidden_fields: tuple[str, ...]
    forbidden_tool_patterns: tuple[str, ...]


def _resolve_opifex_root() -> Path:
    return resolve_opifex_root(
        PROJECT_ROOT,
        required_paths=(
            Path("conformance/shared_surfaces.yaml"),
            Path("conformance/forbidden_vocabulary.yaml"),
            Path("design/final-canonical-contract.md"),
        ),
    )


def _load_yaml(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected mapping YAML in {path}")
    return payload


def _validate_case_matrix_coverage(
    *,
    case_path: Path,
    surface_id: str,
    coverage_states: tuple[str, ...],
    minimum_coverage: tuple[str, ...],
) -> tuple[str, ...]:
    case_payload = _load_yaml(case_path)
    declared_surface_id = case_payload.get("surface_id")
    if declared_surface_id != surface_id:
        raise RuntimeError(
            f"case_matrix surface mismatch for {case_path}: expected {surface_id}, got {declared_surface_id}"
        )

    coverage = case_payload.get("coverage")
    if not isinstance(coverage, dict):
        raise RuntimeError(f"case_matrix {case_path} missing top-level 'coverage' mapping")
    for coverage_key in minimum_coverage:
        coverage_state = coverage.get(coverage_key)
        if not isinstance(coverage_state, str):
            raise RuntimeError(f"case_matrix {case_path} missing coverage key: {coverage_key}")
        if coverage_state not in coverage_states:
            raise RuntimeError(
                f"case_matrix {case_path} has invalid coverage state for {coverage_key}: {coverage_state}"
            )

    raw_cases = case_payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise RuntimeError(f"case_matrix {case_path} missing non-empty 'cases' list")
    case_classes: list[str] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise RuntimeError(f"case_matrix {case_path} contains a non-mapping case entry")
        case_class = raw_case.get("class")
        if not isinstance(case_class, str) or not case_class:
            raise RuntimeError(f"case_matrix {case_path} contains a case without a non-empty 'class'")
        if case_class not in case_classes:
            case_classes.append(case_class)
    return tuple(case_classes)


def load_authority_snapshot() -> AuthoritySnapshot:
    """Load and validate the authoritative opifex conformance inputs."""

    opifex_root = _resolve_opifex_root()
    pin: FrozenAuthorityPin = load_frozen_authority_pin(PROJECT_ROOT)
    shared_surfaces_path = opifex_root / "conformance" / "shared_surfaces.yaml"
    forbidden_vocabulary_path = opifex_root / "conformance" / "forbidden_vocabulary.yaml"
    canonical_contract_path = opifex_root / "design" / "final-canonical-contract.md"

    shared_surfaces = _load_yaml(shared_surfaces_path)
    forbidden_vocabulary = _load_yaml(forbidden_vocabulary_path)

    global_controls = shared_surfaces.get("global_controls")
    if not isinstance(global_controls, dict):
        raise RuntimeError("shared_surfaces.yaml missing top-level 'global_controls' mapping")
    case_matrix_policy = global_controls.get("case_matrix_policy")
    if not isinstance(case_matrix_policy, dict):
        raise RuntimeError("shared_surfaces.yaml missing 'global_controls.case_matrix_policy' mapping")
    gate_policy = shared_surfaces.get("gate_policy")
    if not isinstance(gate_policy, dict):
        raise RuntimeError("shared_surfaces.yaml missing top-level 'gate_policy' mapping")
    frozen_followup_packet = shared_surfaces.get("frozen_followup_packet")
    if not isinstance(frozen_followup_packet, dict):
        raise RuntimeError("shared_surfaces.yaml missing top-level 'frozen_followup_packet' mapping")

    frozen_followup_packet_ref = global_controls.get("frozen_followup_packet_ref")
    if not isinstance(frozen_followup_packet_ref, str) or not frozen_followup_packet_ref:
        raise RuntimeError("shared_surfaces.yaml missing 'global_controls.frozen_followup_packet_ref'")
    frozen_followup_packet_doc = frozen_followup_packet.get("packet_doc")
    if not isinstance(frozen_followup_packet_doc, str) or not frozen_followup_packet_doc:
        raise RuntimeError("shared_surfaces.yaml missing 'frozen_followup_packet.packet_doc'")
    if frozen_followup_packet_ref != frozen_followup_packet_doc:
        raise RuntimeError("shared_surfaces.yaml disagrees on the frozen follow-up packet path")
    if pin.packet_doc != frozen_followup_packet_ref:
        raise RuntimeError(
            f"{AUTHORITY_LOCK_PATH} packet_doc drift: expected {frozen_followup_packet_ref}, got {pin.packet_doc}"
        )

    frozen_required = gate_policy.get("frozen_followup_packet_required_before_downstream_ci")
    if frozen_required is not True:
        raise RuntimeError(
            "shared_surfaces.yaml must require a frozen follow-up packet before downstream CI"
        )
    require_pinned_checkout(PROJECT_ROOT, opifex_root)

    minimum_coverage_raw = case_matrix_policy.get("minimum_coverage")
    if not isinstance(minimum_coverage_raw, list) or not all(
        isinstance(coverage_key, str) for coverage_key in minimum_coverage_raw
    ):
        raise RuntimeError(
            "shared_surfaces.yaml missing 'global_controls.case_matrix_policy.minimum_coverage' string list"
        )
    minimum_coverage = tuple(str(coverage_key) for coverage_key in minimum_coverage_raw)

    coverage_states_raw = case_matrix_policy.get("coverage_states")
    if not isinstance(coverage_states_raw, list) or not all(
        isinstance(coverage_state, str) for coverage_state in coverage_states_raw
    ):
        raise RuntimeError(
            "shared_surfaces.yaml missing 'global_controls.case_matrix_policy.coverage_states' string list"
        )
    coverage_states = tuple(str(coverage_state) for coverage_state in coverage_states_raw)

    raw_surfaces = shared_surfaces.get("shared_surfaces")
    if not isinstance(raw_surfaces, list):
        raise RuntimeError("shared_surfaces.yaml missing top-level 'shared_surfaces' list")

    switch_blocking = gate_policy.get("switch_blocking")
    if not isinstance(switch_blocking, dict):
        raise RuntimeError("shared_surfaces.yaml missing 'gate_policy.switch_blocking' mapping")
    switch_blocking_by_exposure = switch_blocking.get("by_exposure")
    if not isinstance(switch_blocking_by_exposure, list) or not all(
        isinstance(exposure, str) for exposure in switch_blocking_by_exposure
    ):
        raise RuntimeError(
            "shared_surfaces.yaml missing 'gate_policy.switch_blocking.by_exposure' string list"
        )

    owned_surface_ids_list: list[str] = []
    blocking_surface_ids_list: list[str] = []
    owned_case_matrix_paths: list[str] = []
    owned_case_classes: list[str] = []
    for surface in raw_surfaces:
        if not isinstance(surface, dict) or surface.get("owner_repo") != "mcp-tela":
            continue
        surface_id = surface.get("id")
        if not isinstance(surface_id, str) or not surface_id:
            raise RuntimeError("mcp-tela surface missing non-empty 'id'")
        exposure = surface.get("exposure")
        if not isinstance(exposure, str) or not exposure:
            raise RuntimeError(f"mcp-tela surface {surface_id} missing non-empty 'exposure'")
        case_matrix = surface.get("case_matrix")
        if not isinstance(case_matrix, list) or not case_matrix:
            raise RuntimeError(f"mcp-tela surface {surface_id} missing non-empty 'case_matrix'")
        case_matrix_paths: list[str] = []
        for raw_case_path in case_matrix:
            if not isinstance(raw_case_path, str) or not raw_case_path:
                raise RuntimeError(f"mcp-tela surface {surface_id} has invalid case_matrix entry")
            case_path = opifex_root / raw_case_path
            if not case_path.is_file():
                raise RuntimeError(
                    f"mcp-tela surface {surface_id} case_matrix file missing: {raw_case_path}"
                )
            for case_class in _validate_case_matrix_coverage(
                case_path=case_path,
                surface_id=surface_id,
                coverage_states=coverage_states,
                minimum_coverage=minimum_coverage,
            ):
                if case_class not in owned_case_classes:
                    owned_case_classes.append(case_class)
            case_matrix_paths.append(raw_case_path)
        owned_surface_ids_list.append(surface_id)
        owned_case_matrix_paths.extend(case_matrix_paths)
        if exposure in switch_blocking_by_exposure:
            blocking_surface_ids_list.append(surface_id)

    if not owned_surface_ids_list:
        raise RuntimeError("shared_surfaces.yaml defines no mcp-tela owned surfaces")
    if not blocking_surface_ids_list:
        raise RuntimeError("shared_surfaces.yaml defines no switch-blocking mcp-tela surfaces")

    forbidden_fields_raw = forbidden_vocabulary.get("shared_forbidden_fields")
    if not isinstance(forbidden_fields_raw, list):
        raise RuntimeError("forbidden_vocabulary.yaml missing 'shared_forbidden_fields' list")
    forbidden_fields = tuple(str(field) for field in forbidden_fields_raw)

    tool_names_raw = forbidden_vocabulary.get("forbidden_shared_tool_names")
    if not isinstance(tool_names_raw, list):
        raise RuntimeError(
            "forbidden_vocabulary.yaml missing 'forbidden_shared_tool_names' list"
        )
    forbidden_tool_patterns = tuple(
        str(entry["pattern"])
        for entry in tool_names_raw
        if isinstance(entry, dict) and "pattern" in entry
    )

    return AuthoritySnapshot(
        opifex_root=opifex_root,
        packet_repository=pin.repository,
        packet_ref=pin.ref,
        packet_doc=pin.packet_doc,
        shared_surfaces_path=shared_surfaces_path,
        forbidden_vocabulary_path=forbidden_vocabulary_path,
        canonical_contract_path=canonical_contract_path,
        owned_surface_ids=tuple(owned_surface_ids_list),
        blocking_surface_ids=tuple(blocking_surface_ids_list),
        owned_case_matrix_paths=tuple(owned_case_matrix_paths),
        owned_case_classes=tuple(owned_case_classes),
        forbidden_fields=forbidden_fields,
        forbidden_tool_patterns=forbidden_tool_patterns,
    )


def _run(command: list[str], *, expect_success: bool) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    sys.stdout.write(f"$ {' '.join(command)}\n")
    if completed.stdout:
        sys.stdout.write(completed.stdout)
        if not completed.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if completed.stderr:
        sys.stderr.write(completed.stderr)
        if not completed.stderr.endswith("\n"):
            sys.stderr.write("\n")

    if expect_success and completed.returncode != 0:
        raise SystemExit(completed.returncode)
    if not expect_success and completed.returncode == 0:
        raise SystemExit("Expected seeded drift to fail, but pytest exited successfully.")
    return completed


def _print_authority_summary(snapshot: AuthoritySnapshot) -> None:
    print(f"Authority repo: {snapshot.opifex_root}")
    print(f"Frozen authority repository: {snapshot.packet_repository}")
    print(f"Frozen authority ref: {snapshot.packet_ref}")
    print(f"Frozen authority packet: {snapshot.packet_doc}")
    print(f"Shared surfaces file: {snapshot.shared_surfaces_path}")
    print(f"Forbidden vocabulary file: {snapshot.forbidden_vocabulary_path}")
    print(f"Canonical contract: {snapshot.canonical_contract_path}")
    print("mcp-tela owned surfaces:")
    for surface_id in snapshot.owned_surface_ids:
        print(f"- {surface_id}")
    print("Switch-blocking mcp-tela surfaces:")
    for surface_id in snapshot.blocking_surface_ids:
        print(f"- {surface_id}")
    print("Owned case-matrix files:")
    for case_matrix_path in snapshot.owned_case_matrix_paths:
        print(f"- {case_matrix_path}")
    print("Owned case-matrix classes:")
    for case_class in snapshot.owned_case_classes:
        print(f"- {case_class}")
    print("Forbidden shared fields:")
    for field in snapshot.forbidden_fields:
        print(f"- {field}")
    print("Forbidden shared tool-name patterns:")
    for pattern in snapshot.forbidden_tool_patterns:
        print(f"- {pattern}")


def run_green() -> None:
    """Run repo-local shared-surface gates against authoritative opifex metadata."""

    snapshot = load_authority_snapshot()
    _print_authority_summary(snapshot)
    for group_name, test_targets in PYTEST_GROUPS:
        print(f"\n== Running group: {group_name} ==")
        _run([sys.executable, "-m", "pytest", "-q", *test_targets], expect_success=True)


def run_expected_red() -> None:
    """Seed one forbidden-vocabulary drift and prove the gate fails."""

    snapshot = load_authority_snapshot()
    _print_authority_summary(snapshot)

    readme_path = PROJECT_ROOT / "README.md"
    original = readme_path.read_text(encoding="utf-8")
    readme_path.write_text(original + EXPECTED_RED_SENTINEL, encoding="utf-8")
    try:
        completed = _run(
            [sys.executable, "-m", "pytest", "-q", EXPECTED_RED_TEST],
            expect_success=False,
        )
        combined_output = f"{completed.stdout}\n{completed.stderr}"
        if "retired alias field" not in combined_output and "profile_name" not in combined_output:
            raise SystemExit(
                "Expected-red failure did not mention the seeded forbidden vocabulary drift."
            )
    finally:
        readme_path.write_text(original, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run mcp-tela repo-local shared-surface CI gates.")
    parser.add_argument(
        "mode",
        choices=("expected-red", "green"),
        help="Whether to run the seeded-drift proof or the green gate.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.mode == "expected-red":
        run_expected_red()
        return 0
    run_green()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
