"""Regression tests for bridge consumer-only readiness contract freeze."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTERFACES_DOC = PROJECT_ROOT / "docs" / "INTERFACES.md"
SURFACE_CONTRACT_DOC = PROJECT_ROOT / "docs" / "CONFIRMED-SURFACE-CONTRACT.md"
ADR_DOC = PROJECT_ROOT / "docs" / "ADR-005-readiness-authority-boundary.md"
AGENT_INTERFACE_DOC = PROJECT_ROOT / "docs" / "AGENT_INTERFACE.md"
README_DOC = PROJECT_ROOT / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_interfaces_doc_freezes_status_authority_waiting_and_exit_rules() -> None:
    """Interface spec must freeze status-authority wait/retry/exit semantics."""
    interface_text = _read(INTERFACES_DOC)
    assert (
        "readiness waiting must be driven by `GET /status` observations"
        in interface_text
    )
    assert "fixed sleep intervals or bridge-local lifecycle guesses" in interface_text
    assert (
        "retry is authorized only when the gateway emits the transient non-ready contract"
        in interface_text
    )
    assert (
        "must exit cleanly and boundedly instead of looping indefinitely"
        in interface_text
    )


def test_surface_contract_freezes_consumer_only_bridge_readiness_behavior() -> None:
    """Surface contract must preserve consumer-only readiness wording."""
    surface_text = _read(SURFACE_CONTRACT_DOC)
    assert "Bridge consumer-only readiness freeze" in surface_text
    assert "waits for readiness by consulting `GET /status`" in surface_text
    assert "must not create,\ncache, or relabel readiness state locally" in surface_text
    assert (
        "Retry is allowed only when the gateway emits the transient non-ready contract"
        in surface_text
    )
    assert "must exit\ncleanly and boundedly" in surface_text


def test_adr_records_rationale_for_status_authority_contract() -> None:
    """ADR must freeze the architectural boundary and failure mode."""
    adr_text = _read(ADR_DOC)
    assert "readiness waiting must consult `GET /status`" in adr_text
    assert "fixed sleep intervals or bridge-local lifecycle inference" in adr_text
    assert (
        "retries are authorized only when the gateway emits an explicit transient"
        in adr_text
    )
    assert "must exit cleanly and boundedly" in adr_text


def test_agent_and_readme_surfaces_repeat_operator_contract() -> None:
    """Operator-facing docs must repeat the same readiness authority contract."""
    agent_text = _read(AGENT_INTERFACE_DOC)
    readme_text = _read(README_DOC)

    assert (
        "readiness waiting must consult `GET /status`" in agent_text
        and "not fixed sleep intervals" in agent_text
    )
    assert (
        "persistent `warming` or another non-admission state from `GET /status` must cause a clean bounded exit"
        in agent_text
    )

    assert (
        "readiness waiting must consult `GET /status`" in readme_text
        and "not fixed sleep delays" in readme_text
    )
    assert (
        "retry is allowed only when the gateway emits a transient non-ready contract signal"
        in readme_text
    )
    assert (
        "persistent `warming` or another non-admission state must end in a clean bounded exit"
        in readme_text
    )
