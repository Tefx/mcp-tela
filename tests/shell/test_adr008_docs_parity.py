"""ADR-008 documentation parity checks."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCS = {
    "USAGE": PROJECT_ROOT / "docs" / "USAGE.md",
    "INTERFACES": PROJECT_ROOT / "docs" / "INTERFACES.md",
    "DESIGN": PROJECT_ROOT / "docs" / "DESIGN.md",
}

REQUIRED_CLIENT_NEUTRAL_TERMS = (
    "client attachment",
    "client-neutral",
    "shared runtime",
    "status --clients",
    "doctor --recover",
)

FORBIDDEN_ATTACHMENT_TERMS = (
    "opencode session",
    "opencode sessions",
    "opencode-only",
    "opencode attachment",
)


def _doc_text(name: str) -> str:
    """Read a documentation file by logical name.

    Args:
        name: One of ``USAGE``, ``INTERFACES``, or ``DESIGN``.

    Returns:
        Markdown contents for the requested document.
    """

    return DOCS[name].read_text(encoding="utf-8")


def test_usage_interfaces_design_document_client_neutral_attachments() -> None:
    """Core docs must describe ADR-008 as client-neutral attachments."""

    for name in DOCS:
        lowered = _doc_text(name).lower()
        missing = [term for term in REQUIRED_CLIENT_NEUTRAL_TERMS if term not in lowered]
        assert missing == [], f"{name} missing ADR-008 terms: {missing}"


def test_docs_do_not_describe_adr008_as_opencode_specific_sessions() -> None:
    """ADR-008 docs must not regress to opencode-specific attachment wording."""

    for name in DOCS:
        lowered = _doc_text(name).lower()
        forbidden = [term for term in FORBIDDEN_ATTACHMENT_TERMS if term in lowered]
        assert forbidden == [], f"{name} contains forbidden attachment wording: {forbidden}"


def test_docs_parity_lists_required_operator_behaviors() -> None:
    """Docs must carry the workflow promises covered by ADR-008 tests."""

    combined = "\n".join(_doc_text(name).lower() for name in DOCS)
    required_phrases = {
        "lockfile presence does not imply readiness": "lockfile readiness boundary",
        "does not cold-start": "status probe is non-mutating",
        "without --recover": "doctor passive mode is non-mutating",
        "host_transport_closed": "host EOF diagnostic event",
        "recovery budgets are per event": "per-event recovery budget",
    }
    missing = [label for phrase, label in required_phrases.items() if phrase not in combined]
    assert missing == []
