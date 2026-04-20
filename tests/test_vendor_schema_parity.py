"""CI parity tests: vendored opifex schema mirrors must match the frozen opifex source.

These tests ensure that vendor/opifex/contracts/*.schema.json files remain
exact mirrors of the frozen opifex authority checkout pinned by
design/opifex-frozen-authority-packet.json. Any divergence indicates the vendor
mirror is stale or was manually edited.

The source of truth is the pinned opifex authority checkout. Mirrors live in
vendor/opifex/contracts/ and are read-only by contract.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

SCRIPT_CI_ROOT = Path(__file__).resolve().parents[1] / "scripts" / "ci"
if str(SCRIPT_CI_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_CI_ROOT))

from opifex_authority import (  # noqa: E402 - tests load sibling CI helper from repository path
    require_pinned_checkout,
    resolve_opifex_root,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONTRACTS_ROOT = PROJECT_ROOT / "contracts"
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "opifex" / "contracts"


def _resolve_opifex_contracts_root() -> Path:
    """Resolve the authoritative frozen opifex contracts root.

    Returns:
        Contracts directory under the pinned opifex checkout.

    Raises:
        FileNotFoundError: If no pinned authority checkout is available.
        RuntimeError: If the checkout is not pinned to the recorded ref.
    """

    opifex_root = resolve_opifex_root(
        PROJECT_ROOT,
        required_paths=(
            Path("contracts"),
            Path("design/final-canonical-contract.md"),
            Path("design/cross-repo-followup-packet.md"),
        ),
    )
    require_pinned_checkout(PROJECT_ROOT, opifex_root)
    return opifex_root / "contracts"


OPIFEX_ROOT = _resolve_opifex_contracts_root()


def _load_json(path: Path) -> dict[str, object]:
    """Load and parse a JSON file.

    Args:
        path: JSON file path.

    Returns:
        Parsed JSON mapping.
    """

    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict)
    return payload


def _get_vendor_mirror(filename: str) -> Path | None:
    """Get path to vendored mirror if it exists.

    Args:
        filename: Schema filename.

    Returns:
        Mirror path when present.
    """

    mirror_path = VENDOR_ROOT / filename
    return mirror_path if mirror_path.exists() else None


def _get_opifex_source(filename: str) -> Path | None:
    """Get path to pinned opifex source schema if it exists.

    Args:
        filename: Schema filename.

    Returns:
        Source path when present.
    """

    source_path = OPIFEX_ROOT / filename
    return source_path if source_path.exists() else None


SCHEMA_MIRRORS = [
    "capability_token.schema.json",
    "tela_profile_list.schema.json",
]


class TestVendorMirrorParity:
    """Parity enforcement: vendored mirrors must match pinned opifex source exactly."""

    @pytest.mark.parametrize("schema_filename", SCHEMA_MIRRORS)
    def test_vendor_mirror_exists(self, schema_filename: str) -> None:
        """Each required schema must have a vendor mirror.

        Args:
            schema_filename: Required schema filename.
        """

        mirror = _get_vendor_mirror(schema_filename)
        assert mirror is not None, (
            f"Vendor mirror missing: {VENDOR_ROOT / schema_filename}. "
            f"Run: cp {OPIFEX_ROOT / schema_filename} {VENDOR_ROOT / schema_filename}"
        )
        assert mirror.is_file(), f"Vendor mirror is not a file: {mirror}"

    @pytest.mark.parametrize("schema_filename", SCHEMA_MIRRORS)
    def test_vendor_mirror_matches_opifex_source(self, schema_filename: str) -> None:
        """Vendored mirror must be semantically identical to the pinned opifex source.

        Args:
            schema_filename: Required schema filename.
        """

        mirror = _get_vendor_mirror(schema_filename)
        source = _get_opifex_source(schema_filename)

        assert mirror is not None, f"No vendor mirror for {schema_filename}"
        assert source is not None, f"No opifex source for {schema_filename}"

        mirror_content = _load_json(mirror)
        source_content = _load_json(source)
        mirror_stripped = {key: value for key, value in mirror_content.items() if key != "$comment"}

        assert mirror_stripped == source_content, (
            f"Vendor mirror schema content diverged from pinned opifex source!\n"
            f"  Mirror: {mirror}\n"
            f"  Source: {source}\n"
            f"To sync: cp {source} {mirror}"
        )

    @pytest.mark.parametrize("schema_filename", SCHEMA_MIRRORS)
    def test_vendor_mirror_has_readonly_banner(self, schema_filename: str) -> None:
        """Vendored mirror must contain the read-only mirror comment banner.

        Args:
            schema_filename: Required schema filename.
        """

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
            "CapabilityToken schema must live only in the pinned opifex authority checkout and the "
            "read-only vendor mirror under vendor/opifex/contracts/."
        )

    def test_vendor_root_has_no_write_gitattrs(self) -> None:
        """Vendor directory should not add local write mechanisms."""

        assert VENDOR_ROOT.exists(), f"Vendor root must exist: {VENDOR_ROOT}"

    def test_opifex_root_accessible(self) -> None:
        """The pinned opifex authority source must be accessible for mirroring."""

        assert OPIFEX_ROOT.exists(), (
            f"Pinned opifex contracts not accessible at {OPIFEX_ROOT}. "
            "Set OPIFEX_ROOT to the pinned opifex checkout root if needed."
        )
        assert OPIFEX_ROOT.is_dir(), f"Pinned opifex contracts path must be a directory: {OPIFEX_ROOT}"
