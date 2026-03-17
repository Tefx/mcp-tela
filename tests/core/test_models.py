from __future__ import annotations

from pathlib import Path


def test_profile_config_contract_includes_default_field() -> None:
    models_path = Path("src/tela/core/models.py")
    source = models_path.read_text(encoding="utf-8")
    assert "class ProfileConfig(BaseModel):" in source
    assert "default: bool = False" in source
