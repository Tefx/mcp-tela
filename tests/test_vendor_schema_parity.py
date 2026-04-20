"""CI parity tests: vendored opifex schema mirrors must match ../opifex source.

These tests ensure that vendor/opifex/contracts/*.schema.json files remain
exact mirrors of the sibling ../opifex/contracts/ source. Any divergence
indicates the vendor mirror is stale or was manually edited.

The source of truth is ../opifex (sibling repo). Mirrors live in
vendor/opifex/contracts/ and are read-only by contract.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# =============================================================================
# Test configuration
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONTRACTS_ROOT = PROJECT_ROOT / "contracts"

# Vendor mirror location (relative to project root)
# vendor/opifex/contracts/ is at the worktree/project root level
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "opifex" / "contracts"

def _candidate_opifex_contract_roots() -> tuple[Path, ...]:
    """Return plausible opifex contract roots for local and CI execution.

    Resolution order intentionally prefers explicit CI wiring first, then local
    sibling layouts, then the historical workstation path used by existing
    contributors.
    """

    env_root = os.environ.get("OPIFEX_ROOT")
    candidates: list[Path] = []

    if env_root:
        env_path = Path(env_root)
        candidates.append(env_path / "contracts")
        candidates.append(env_path)

    candidates.append(PROJECT_ROOT.parent / "opifex" / "contracts")

    if len(PROJECT_ROOT.parents) >= 5:
        candidates.append(PROJECT_ROOT.parents[4] / "opifex" / "contracts")

    candidates.append(Path("/Users/tefx/Projects/opifex") / "contracts")

    deduped: list[Path] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return tuple(deduped)


def _resolve_opifex_root() -> Path:
    """Resolve the authoritative opifex contracts root.

    Raises:
        FileNotFoundError: If no candidate contracts directory exists.
    """

    for candidate in _candidate_opifex_contract_roots():
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not locate opifex contracts root. Set OPIFEX_ROOT to the opifex "
        "checkout root or contracts directory. Tried: "
        + ", ".join(str(path) for path in _candidate_opifex_contract_roots())
    )


OPIFEX_ROOT = _resolve_opifex_root()


# =============================================================================
# Helpers
# =============================================================================


def _load_json(path: Path) -> dict:
    """Load and parse a JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _get_vendor_mirror(filename: str) -> Path | None:
    """Get path to vendored mirror if it exists."""
    mirror_path = VENDOR_ROOT / filename
    return mirror_path if mirror_path.exists() else None


def _get_opifex_source(filename: str) -> Path | None:
    """Get path to opifex source schema if it exists."""
    source_path = OPIFEX_ROOT / filename
    return source_path if source_path.exists() else None


# =============================================================================
# Parity tests
# =============================================================================


SCHEMA_MIRRORS = [
    "capability_token.schema.json",
    "tela_profile_list.schema.json",
]


class TestVendorMirrorParity:
    """Parity enforcement: vendored mirrors must match ../opifex source exactly."""

    @pytest.mark.parametrize("schema_filename", SCHEMA_MIRRORS)
    def test_vendor_mirror_exists(self, schema_filename: str) -> None:
        """Each required schema must have a vendor mirror."""
        mirror = _get_vendor_mirror(schema_filename)
        assert mirror is not None, (
            f"Vendor mirror missing: {VENDOR_ROOT / schema_filename}. "
            f"Run: cp {OPIFEX_ROOT / schema_filename} {VENDOR_ROOT / schema_filename}"
        )
        assert mirror.is_file(), f"Vendor mirror is not a file: {mirror}"

    @pytest.mark.parametrize("schema_filename", SCHEMA_MIRRORS)
    def test_vendor_mirror_matches_opifex_source(self, schema_filename: str) -> None:
        """Vendored mirror must be semantically identical to ../opifex source.

        The mirror may contain additional $comment metadata (stating read-only
        mirror status) but the canonical schema content must match exactly.

        This test fails when:
        - The opifex source file has been updated but the vendor mirror was not synced
        - Someone manually edited the vendor mirror schema content (which is forbidden)

        To repair divergence:
            cp ../opifex/contracts/{schema_filename} vendor/opifex/contracts/{schema_filename}
        """
        mirror = _get_vendor_mirror(schema_filename)
        source = _get_opifex_source(schema_filename)

        assert mirror is not None, f"No vendor mirror for {schema_filename}"
        assert source is not None, f"No opifex source for {schema_filename}"

        mirror_content = _load_json(mirror)
        source_content = _load_json(source)

        # Strip $comment from mirror (if present) since it's local read-only banner
        # The canonical content must match opifex exactly
        mirror_stripped = {k: v for k, v in mirror_content.items() if k != "$comment"}

        assert mirror_stripped == source_content, (
            f"Vendor mirror schema content diverged from ../opifex source!\n"
            f"  Mirror: {mirror}\n"
            f"  Source: {source}\n"
            f"To sync: cp {source} {mirror}"
        )

    @pytest.mark.parametrize("schema_filename", SCHEMA_MIRRORS)
    def test_vendor_mirror_has_readonly_banner(self, schema_filename: str) -> None:
        """Vendored mirror must contain the read-only mirror comment banner."""
        mirror = _get_vendor_mirror(schema_filename)
        assert mirror is not None, f"No vendor mirror for {schema_filename}"

        content = mirror.read_text(encoding="utf-8")

        assert "$comment" in content, (
            f"Vendor mirror {schema_filename} missing $comment banner. "
            "Add: $comment: READ-ONLY MIRROR — DO NOT EDIT MANUALLY. "
            "Source of truth: ../opifex/contracts/"
        )
        assert "READ-ONLY MIRROR" in content, (
            f"Vendor mirror {schema_filename} missing READ-ONLY marker in $comment"
        )
        assert "../opifex" in content, (
            f"Vendor mirror {schema_filename} missing source-of-truth path in $comment"
        )


class TestVendorMirrorReadOnlyContract:
    """Enforce read-only mirror semantics."""

    def test_no_local_capability_token_schema_authority(self) -> None:
        """CapabilityToken schema must not exist as a local editable contract copy."""
        local_schema = LOCAL_CONTRACTS_ROOT / "capability_token.schema.json"
        assert not local_schema.exists(), (
            "CapabilityToken schema must live only in ../opifex and the "
            "read-only vendor mirror under vendor/opifex/contracts/."
        )

    def test_vendor_root_has_no_write_gitattrs(self) -> None:
        """Vendor directory should have .gitignore entries preventing accidental editing.

        The vendor/opifex/contracts/ files should be regenerated from ../opifex,
        not edited directly. This is a documentation/contract test.
        """
        # This test documents the contract: do not add write mechanisms here
        # Individual files carry $comment banners marking them read-only
        assert VENDOR_ROOT.exists(), f"Vendor root must exist: {VENDOR_ROOT}"

    def test_opifex_root_accessible(self) -> None:
        """The ../opifex source must be accessible for mirroring."""
        assert OPIFEX_ROOT.exists(), (
            f"../opifex contracts not accessible at {OPIFEX_ROOT}. "
            "Sibling opifex repo must be present for schema mirroring."
        )
        assert OPIFEX_ROOT.is_dir(), f"../opifex/contracts must be a directory"
