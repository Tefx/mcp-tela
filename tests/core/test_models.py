from __future__ import annotations

from pathlib import Path


def test_profile_config_contract_includes_default_field() -> None:
    models_path = Path("src/tela/core/models.py")
    source = models_path.read_text(encoding="utf-8")
    assert "class ProfileConfig(BaseModel):" in source
    assert "default: bool = False" in source


def test_tela_config_contract_includes_resolved_default_profile_field() -> None:
    models_path = Path("src/tela/core/models.py")
    source = models_path.read_text(encoding="utf-8")
    assert "resolved_default_profile: str | None = None" in source
