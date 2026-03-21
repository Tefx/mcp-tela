"""Black-box repro tests for low-severity findings L1-L21."""

from __future__ import annotations

from pathlib import Path

import pytest


# --- L1: CLI no direct gateway import ---


class TestL1CliNoDirectGatewayImport:
    """L1: cli.py should not import gateway internals directly."""

    def test_cli_no_direct_gateway_state_import(self) -> None:
        """cli.py may import bind_gateway_startup but not internal state."""
        source = Path("src/tela/cli.py").read_text()
        # Should not import private state like _registry, _connections, etc.
        forbidden = ["_registry", "_connections", "_audit_entries"]
        for item in forbidden:
            assert item not in source, f"cli.py imports internal {item}"


# --- L5: Pure functions in core ---


class TestL5PureFunctionsInCore:
    """L5: Core zone must not have I/O imports."""

    def test_no_io_imports_in_core(self) -> None:
        core_dir = Path("src/tela/core")
        io_modules = {
            "os",
            "subprocess",
            "shutil",
            "socket",
            "http",
            "urllib",
            "requests",
        }

        for py_file in core_dir.glob("*.py"):
            if py_file.name in ("__init__.py", "contracts.py"):
                continue
            source = py_file.read_text()
            for mod in io_modules:
                for line in source.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if f"import {mod}" in stripped or f"from {mod}" in stripped:
                        pytest.fail(f"{py_file.name} has I/O import: {stripped}")


# --- L11: __init__.py exists ---


class TestL11InitPy:
    """L11: All packages must have __init__.py."""

    def test_all_packages_have_init(self) -> None:
        src = Path("src/tela")
        for d in src.rglob("*"):
            if d.is_dir() and any(d.glob("*.py")):
                if d.name == "__pycache__":
                    continue
                init = d / "__init__.py"
                assert init.exists(), f"Missing __init__.py in {d}"


# --- L13: TelaError used ---


class TestL13TelaErrorUsed:
    """L13: TelaError model must be importable and usable."""

    def test_tela_error_importable(self) -> None:
        from tela.core.models import TelaError

        err = TelaError(code="TEST_ERR", message="something broke")
        assert err.code == "TEST_ERR"
        assert err.details is None

    def test_tela_error_with_details(self) -> None:
        from tela.core.models import TelaError

        err = TelaError(
            code="TEST_ERR", message="something broke", details={"key": "val"}
        )
        assert err.details == {"key": "val"}


# --- L17: Digest is SHA-256 ---


class TestL17DigestSHA256:
    """L17: reload module imports hashlib for SHA-256 digest computation."""

    def test_hashlib_imported_in_reload(self) -> None:
        """Verify reload.py uses hashlib (SHA-256 capability)."""
        from pathlib import Path

        source = Path("src/tela/shell/reload.py").read_text()
        assert "import hashlib" in source

    def test_audit_param_hash_is_sha256(self) -> None:
        """Verify audit param hash uses SHA-256."""
        from tela.shell.audit import _compute_param_hash

        result = _compute_param_hash({"key": "value"})
        assert result.is_ok
        assert result.value is not None
        assert result.value.startswith("sha256:")


# --- L18: License exists ---


class TestL18LicenseExists:
    """L18: Repository must have a LICENSE file."""

    def test_license_file_exists(self) -> None:
        # Check common locations
        assert any(
            Path(p).exists()
            for p in ["LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE"]
        ), "No LICENSE file found in repository root"


# --- L19: No source-text tests ---


class TestL19NoSourceTextTests:
    """L19: test_models.py must not use source-text matching."""

    def test_no_read_text_in_test_models(self) -> None:
        source = Path("tests/core/test_models.py").read_text()
        assert "read_text" not in source
        assert "models_path" not in source


# --- L20: Profile catalog exists ---


class TestL20ProfileCatalog:
    """L20: Builtin profile catalog must exist with 7 profiles."""

    def test_catalog_module_exists(self) -> None:
        from tela.core.catalog import BUILTIN_PROFILES

        assert len(BUILTIN_PROFILES) == 7

    def test_catalog_profile_names(self) -> None:
        from tela.core.catalog import BUILTIN_PROFILES

        expected = {
            "read_only",
            "fetch_external",
            "modify_local",
            "send_external",
            "orchestrate",
            "execute_safe",
            "execute_full",
        }
        assert set(BUILTIN_PROFILES.keys()) == expected


# --- L21: expand_env post meaningful ---


class TestL21ExpandEnvPost:
    """L21: _expand_env_token post-condition must be meaningful."""

    def test_expand_env_token_basic(self) -> None:
        from tela.core.config import _expand_env_token

        result = _expand_env_token("hello $NAME world", {"NAME": "tela"})
        assert result == "hello tela world"

    def test_expand_env_braces(self) -> None:
        from tela.core.config import _expand_env_token

        result = _expand_env_token("${VAR}_suffix", {"VAR": "value"})
        assert result == "value_suffix"
