"""Run Tela's contract gates using only this repository.

Schemas live in contracts/; docs/INTERFACES.md and
docs/CONFIRMED-SURFACE-CONTRACT.md define runtime and surface semantics.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTEST_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("contract-schemas", ("tests/test_contract_schemas.py",)),
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
    if not expect_success:
        if completed.returncode == 0:
            raise SystemExit("Expected seeded drift to fail, but pytest exited successfully.")
        if completed.returncode != 1:
            raise SystemExit(completed.returncode)
    return completed


def run_green() -> None:
    """Run Tela-owned contract and runtime checks from this checkout."""

    for group_name, test_targets in PYTEST_GROUPS:
        print(f"\n== Running group: {group_name} ==")
        _run([sys.executable, "-m", "pytest", "-q", *test_targets], expect_success=True)


def run_expected_red() -> None:
    """Seed one forbidden-vocabulary drift and prove the gate fails."""

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
