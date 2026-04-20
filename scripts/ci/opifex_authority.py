"""Helpers for resolving the frozen opifex authority checkout.

Authority basis:
- user step `ci_repo_local_rollout.remediate-frozen-authority-packet-pinning-and-authority-derived-scope-in-mcp-tela`
- `design/opifex-frozen-authority-packet.json`
- `opifex/design/final-canonical-contract.md`
- `opifex/conformance/shared_surfaces.yaml`
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess


AUTHORITY_LOCK_PATH = Path("design/opifex-frozen-authority-packet.json")
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class FrozenAuthorityPin:
    """Pinned opifex authority packet metadata."""

    repository: str
    ref: str
    packet_doc: str


def load_frozen_authority_pin(project_root: Path) -> FrozenAuthorityPin:
    """Load the frozen opifex authority pin for this repository."""

    authority_lock_path = project_root / AUTHORITY_LOCK_PATH
    with authority_lock_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected mapping JSON in {authority_lock_path}")

    repository = payload.get("repository")
    if not isinstance(repository, str) or not repository:
        raise RuntimeError(f"{AUTHORITY_LOCK_PATH} missing non-empty 'repository'")
    ref = payload.get("ref")
    if not isinstance(ref, str) or FULL_SHA_PATTERN.fullmatch(ref) is None:
        raise RuntimeError(f"{AUTHORITY_LOCK_PATH} must pin a full 40-character git SHA in 'ref'")
    packet_doc = payload.get("packet_doc")
    if not isinstance(packet_doc, str) or not packet_doc:
        raise RuntimeError(f"{AUTHORITY_LOCK_PATH} missing non-empty 'packet_doc'")
    return FrozenAuthorityPin(repository=repository, ref=ref, packet_doc=packet_doc)


def candidate_opifex_roots(project_root: Path) -> tuple[Path, ...]:
    """Return plausible opifex repository roots."""

    env_root = os.environ.get("OPIFEX_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(project_root.parent / "opifex")
    if len(project_root.parents) >= 3:
        candidates.append(project_root.parents[2].parent / "opifex")
    if len(project_root.parents) >= 5:
        candidates.append(project_root.parents[4] / "opifex")

    deduped: list[Path] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return tuple(deduped)


def resolve_opifex_root(project_root: Path, required_paths: tuple[Path, ...]) -> Path:
    """Resolve the opifex repository root matching required authority files."""

    candidates = candidate_opifex_roots(project_root)
    for candidate in candidates:
        if all((candidate / path).exists() for path in required_paths):
            return candidate
    joined_candidates = ", ".join(str(path) for path in candidates)
    joined_required = ", ".join(str(path) for path in required_paths)
    raise FileNotFoundError(
        "Could not locate opifex authority checkout containing required paths: "
        f"{joined_required}. Set OPIFEX_ROOT to the opifex repository root. Tried: {joined_candidates}"
    )


def git_head(root: Path) -> str | None:
    """Resolve the git HEAD for a checkout when available."""

    if not (root / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Failed to resolve git HEAD for {root}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def require_pinned_checkout(project_root: Path, opifex_root: Path) -> FrozenAuthorityPin:
    """Validate that the opifex checkout matches the frozen authority pin."""

    pin = load_frozen_authority_pin(project_root)
    packet_path = opifex_root / pin.packet_doc
    if not packet_path.is_file():
        raise RuntimeError(
            f"Pinned authority packet is missing from OPIFEX_ROOT: expected {packet_path}"
        )
    checkout_head = git_head(opifex_root)
    if checkout_head is not None and checkout_head != pin.ref:
        raise RuntimeError(
            "OPIFEX_ROOT checkout does not match pinned authority ref: "
            f"expected {pin.ref}, got {checkout_head}"
        )
    return pin
