"""Regression tests for the transient ``POST /mcp`` readiness contract freeze."""

from __future__ import annotations

from pathlib import Path
import json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTERFACES_DOC = PROJECT_ROOT / "docs" / "INTERFACES.md"
SURFACE_CONTRACT_DOC = PROJECT_ROOT / "docs" / "CONFIRMED-SURFACE-CONTRACT.md"
TRANSIENT_SCHEMA = (
    PROJECT_ROOT / "contracts" / "mcp_admission_transient_503.schema.json"
)
TYPE_CONTRACT = PROJECT_ROOT / "src" / "tela" / "shell" / "mcp_admission_contract.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_schema() -> dict[str, object]:
    return json.loads(_read(TRANSIENT_SCHEMA))


def test_transient_schema_freezes_required_machine_readable_fields() -> None:
    """Schema must freeze the gateway-authored retry contract fields."""
    schema = _read_schema()
    assert schema["required"] == [
        "error",
        "code",
        "transient",
        "retry",
        "gateway_state",
    ]

    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["code"]["const"] == "ADMISSION_REJECTED_WARMING"
    assert properties["transient"]["const"] is True
    assert properties["gateway_state"]["const"] == "warming"

    retry = properties["retry"]
    assert isinstance(retry, dict)
    retry_properties = retry["properties"]
    assert isinstance(retry_properties, dict)
    assert retry_properties["authorized"]["const"] is True
    assert retry_properties["basis"]["const"] == "gateway_signal"
    assert retry_properties["expectation"]["const"] == "bounded"


def test_interface_contract_explicitly_rejects_client_guesswork() -> None:
    """Interface spec must bind retry authorization to gateway-authored signal."""
    interface_text = _read(INTERFACES_DOC)
    assert "must reject ordinary MCP admission with HTTP\n`503`" in interface_text
    assert (
        "gateway-authored fields `code`, `transient`, and `retry.authorized`"
        in interface_text
    )
    assert (
        "must not** authorize retry from bare\n  client inference over HTTP `503`, connection timing, or prior `/connect`\n  success"
        in interface_text
    )
    assert "`POST /connect` remains registration plumbing only" in interface_text
    assert "`gateway_state` remains `warming`" in interface_text


def test_confirmed_surface_contract_preserves_boundary_and_vocabulary_freeze() -> None:
    """Surface contract must preserve /connect and lifecycle-vocabulary boundaries."""
    surface_text = _read(SURFACE_CONTRACT_DOC)
    assert (
        "`POST /connect` remains connection registration and lifecycle plumbing only."
        in surface_text
    )
    assert (
        "gateway runtime lifecycle plus `GET /status` is the sole readiness authority."
        in surface_text
    )
    assert (
        "Retry authorization must not be inferred from bare client guesswork,"
        in surface_text
    )
    assert (
        "does not add any new public lifecycle label beyond `warming`." in surface_text
    )


def test_type_contract_module_documents_non_bridge_readiness_ownership() -> None:
    """Type-level contract must state that bridge code does not own readiness truth."""
    contract_text = _read(TYPE_CONTRACT)
    assert "HTTP ``503`` alone is insufficient" in contract_text
    assert "``POST /connect`` remains registration plumbing only" in contract_text
    assert "No new public lifecycle state is introduced here." in contract_text
