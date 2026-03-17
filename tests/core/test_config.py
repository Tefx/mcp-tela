from __future__ import annotations

from pathlib import Path


def test_open_mode_precedence_contract_is_documented() -> None:
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert "CLI `--default-profile` if provided." in source
    assert "Else exactly one profile with `default=True`." in source


def test_open_mode_rejection_contract_is_documented() -> None:
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert 'code="OPEN_MODE_DEFAULT_PROFILE_MISSING"' in source
    assert 'code="OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS"' in source


def test_core_contract_surfaces_have_pre_post_and_doctest_placeholders() -> None:
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert "@pre(" in source
    assert "@post(" in source
    assert ">>> parse_config(" in source


def test_contract_surfaces_are_stubs_only() -> None:
    source = Path("src/tela/core/config.py").read_text(encoding="utf-8")
    assert 'raise NotImplementedError("Contract stub: parse_config")' in source
    assert 'raise NotImplementedError("Contract stub: validate_config")' in source
    assert (
        'raise NotImplementedError("Contract stub: resolve_open_mode_default_profile")'
        in source
    )
