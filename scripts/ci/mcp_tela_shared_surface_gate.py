"""Repo-local shared-surface CI gate for mcp-tela.

Authority basis:
- user step `ci_repo_local_rollout.rollout_mcp_tela_ci`
- `../opifex/conformance/shared_surfaces.yaml`
- `../opifex/conformance/forbidden_vocabulary.yaml`
- `../opifex/design/final-canonical-contract.md`

This wrapper makes the repo-local gate executable in both developer worktrees
and GitHub Actions by resolving the opifex authority checkout explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import os
from pathlib import Path
import subprocess
import sys

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SURFACE_IDS = (
    "tela_initialize_token_mode",
    "tela_tools_call_builtin_tela_list_profiles",
    "tela_tools_call_builtin_tela_list_providers",
    "tela_tools_call_downstream",
    "tela_http_connect",
    "tela_shared_naming_docs",
    "tela_mcp_server_naming",
)
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
    shared_surfaces_path: Path
    forbidden_vocabulary_path: Path
    canonical_contract_path: Path
    owned_surface_ids: tuple[str, ...]
    forbidden_fields: tuple[str, ...]
    forbidden_tool_patterns: tuple[str, ...]


def _candidate_opifex_roots() -> tuple[Path, ...]:
    env_root = os.environ.get("OPIFEX_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(PROJECT_ROOT.parent / "opifex")
    if len(PROJECT_ROOT.parents) >= 5:
        candidates.append(PROJECT_ROOT.parents[4] / "opifex")
    candidates.append(Path("/Users/tefx/Projects/opifex"))

    deduped: list[Path] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return tuple(deduped)


def _resolve_opifex_root() -> Path:
    for candidate in _candidate_opifex_roots():
        if (candidate / "conformance" / "shared_surfaces.yaml").is_file():
            return candidate
    raise FileNotFoundError(
        "Could not locate opifex authority checkout. Set OPIFEX_ROOT to the "
        "opifex repository root. Tried: "
        + ", ".join(str(path) for path in _candidate_opifex_roots())
    )


def _load_yaml(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected mapping YAML in {path}")
    return payload


def load_authority_snapshot() -> AuthoritySnapshot:
    """Load and validate the authoritative opifex conformance inputs."""

    opifex_root = _resolve_opifex_root()
    shared_surfaces_path = opifex_root / "conformance" / "shared_surfaces.yaml"
    forbidden_vocabulary_path = opifex_root / "conformance" / "forbidden_vocabulary.yaml"
    canonical_contract_path = opifex_root / "design" / "final-canonical-contract.md"

    shared_surfaces = _load_yaml(shared_surfaces_path)
    forbidden_vocabulary = _load_yaml(forbidden_vocabulary_path)

    raw_surfaces = shared_surfaces.get("shared_surfaces")
    if not isinstance(raw_surfaces, list):
        raise RuntimeError(
            "shared_surfaces.yaml missing top-level 'shared_surfaces' list"
        )

    owned_surface_ids = tuple(
        str(surface["id"])
        for surface in raw_surfaces
        if isinstance(surface, dict) and surface.get("owner_repo") == "mcp-tela"
    )
    missing_surfaces = [
        surface_id for surface_id in EXPECTED_SURFACE_IDS if surface_id not in owned_surface_ids
    ]
    if missing_surfaces:
        raise RuntimeError(
            "opifex shared_surfaces.yaml missing expected mcp-tela surfaces: "
            + ", ".join(missing_surfaces)
        )

    forbidden_fields_raw = forbidden_vocabulary.get("shared_forbidden_fields")
    if not isinstance(forbidden_fields_raw, list):
        raise RuntimeError(
            "forbidden_vocabulary.yaml missing 'shared_forbidden_fields' list"
        )
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
        shared_surfaces_path=shared_surfaces_path,
        forbidden_vocabulary_path=forbidden_vocabulary_path,
        canonical_contract_path=canonical_contract_path,
        owned_surface_ids=owned_surface_ids,
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
        raise SystemExit(
            "Expected seeded drift to fail, but pytest exited successfully."
        )
    return completed


def _print_authority_summary(snapshot: AuthoritySnapshot) -> None:
    print(f"Authority repo: {snapshot.opifex_root}")
    print(f"Shared surfaces file: {snapshot.shared_surfaces_path}")
    print(f"Forbidden vocabulary file: {snapshot.forbidden_vocabulary_path}")
    print(f"Canonical contract: {snapshot.canonical_contract_path}")
    print("mcp-tela owned surfaces:")
    for surface_id in EXPECTED_SURFACE_IDS:
        print(f"- {surface_id}")
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
    parser = argparse.ArgumentParser(
        description="Run mcp-tela repo-local shared-surface CI gates.",
    )
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
