"""ADR-008 Recovery Decision Behavior — expected-red tests (Branch B: CLI-only).

These tests define the expected behavior for the recover branch selected by
`tela.operator_p1.scope_decision.recover_exposure_decision`:

- Branch B CLI-only requirements:
  - remote recover surface is absent or returns a documented rejection
  - `tela doctor --recover` remains the only recovery mutation path
  - docs/contract explicitly state CLI-only status

These tests are expected-red because the selected recovery posture is not yet
fully encoded in product code or contract documentation.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tela.commands.doctor_cmd import doctor_command
from tela.commands.status_cmd import status_command
from tela.shell import http_routes
from tela.shell.result import Result


# =============================================================================
# Green baseline: passive status/probe must not trigger recovery
# =============================================================================


def test_handle_status_does_not_recover_doctor_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /status must never invoke doctor recovery mutations.

    ADR-008 Branch B: No passive status/probe recovery.
    GET /status must be observation-only and must not clean stale discovery,
    cold-start, reconnect, or otherwise invoke recovery.
    """
    called: list[bool] = []

    def _fake_recover(*args: object, **kwargs: object) -> object:
        called.append(True)
        return MagicMock()

    monkeypatch.setattr(
        "tela.commands.doctor_cmd._recover_doctor_runtime",
        _fake_recover,
    )

    http_routes.handle_status("valid-token", "valid-token")

    assert not called, "GET /status must not invoke doctor recovery"


def test_status_command_probe_does_not_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    """tela status --probe must not trigger recovery mutations.

    ADR-008 Branch B: status --probe actively checks the current lockfile
    endpoint only; it must not cold-start, clean stale lockfiles,
    reconnect, or otherwise invoke recovery.
    """
    called: list[bool] = []

    def _fake_recover(*args: object, **kwargs: object) -> object:
        called.append(True)
        return MagicMock()

    monkeypatch.setattr(
        "tela.commands.doctor_cmd._recover_doctor_runtime",
        _fake_recover,
    )

    result = status_command(probe=True)
    if result.is_err:
        pass  # absent lockfile is acceptable; still must not call recovery
    assert not called, "tela status --probe must not invoke recovery"


# =============================================================================
# Green baseline: remote recover surface is already absent
# =============================================================================


def test_no_handle_recover_defined() -> None:
    """http_routes must not define a remote recovery handler.

    ADR-008 Branch B requires CLI-only recovery: no HTTP mutation surface
    for recovery.
    """
    assert not hasattr(http_routes, "handle_recover"), (
        "Remote recovery handler must be absent per ADR-008 Branch B. "
        "CLI-only recovery means no HTTP mutation surface for recovery."
    )


def test_no_runtime_route_handler_registry_exported() -> None:
    """http_routes must not expose a stale runtime route-handler registry."""
    assert not hasattr(http_routes, "_ROUTE_HANDLERS")


def test_builtin_tools_do_not_include_recover() -> None:
    """Built-in MCP tools must not expose a remote recovery surface."""
    from tela.shell.builtin_tools import BUILTIN_TOOLS

    tool_names = {t["name"] for t in BUILTIN_TOOLS}
    recover_tool_names = {
        "tela_recover",
        "tela_doctor_recover",
        "tela_operator_recover",
    }
    overlap = tool_names & recover_tool_names
    assert not overlap, (
        f"Built-in tools contain recovery tool(s): {overlap}. "
        "ADR-008 Branch B forbids remote recovery tools."
    )


def test_remote_recover_returns_documented_rejection_if_present() -> None:
    """If any remote recovery surface exists, it must return documented rejection.

    This test documents the required explicit rejection behavior.
    If a recovery endpoint/tool exists, it must fail closed with a
    documented error rather than silently recovering.
    """
    if hasattr(http_routes, "handle_recover"):
        recover_fn = getattr(http_routes, "handle_recover")
        result = recover_fn("valid-token", "valid-token")
        assert (
            isinstance(result, Result)
            and result.is_err
            and "REMOTE_RECOVERY_NOT_ALLOWED" in (result.error or "")
        ), (
            "Remote recovery surface must return documented rejection "
            "REMOTE_RECOVERY_NOT_ALLOWED per ADR-008 Branch B."
        )


# =============================================================================
# Green baseline: doctor --recover is already the explicit mutation path
# =============================================================================


def test_doctor_parser_exposes_recover_flag() -> None:
    """tela doctor --recover must remain the explicit recovery flag."""
    from tela.cli import main as cli_main

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        cli_main(["doctor", "--help"])
    except SystemExit:
        pass
    finally:
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

    assert "--recover" in output, (
        "tela doctor --help must mention --recover flag per ADR-008."
    )


def test_doctor_without_recover_is_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    """tela doctor without --recover must be passive and not mutate."""
    called: list[bool] = []

    def _fake_recover(*args: object, **kwargs: object) -> object:
        called.append(True)
        return MagicMock()

    monkeypatch.setattr(
        "tela.commands.doctor_cmd._recover_doctor_runtime",
        _fake_recover,
    )

    result = doctor_command(recover=False)
    assert result.is_ok
    assert not called, "tela doctor without --recover must not mutate"


def test_doctor_with_recover_is_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """tela doctor --recover may mutate and append recovery events."""
    called: list[bool] = []

    from tela.commands.doctor_cmd import DoctorRecoverySummary

    def _fake_recover(
        *, discovery: object, probe_timeout: float, recover_timeout: float
    ) -> object:
        called.append(True)
        return Result(
            value=DoctorRecoverySummary(
                attempted=True,
                recovery_succeeded=False,
                already_ready=False,
                stale_cleanup=None,
                cold_start_attempted=False,
                actions=["probe"],
                events_appended=["recovery_probe"],
                error=None,
            )
        )

    monkeypatch.setattr(
        "tela.commands.doctor_cmd._recover_doctor_runtime",
        _fake_recover,
    )

    result = doctor_command(recover=True)
    assert result.is_ok
    assert called, "tela doctor --recover must perform mutation"


# =============================================================================
# Expected-red: docs/contract must explicitly state CLI-only status
# =============================================================================


class TestDocsStateCliOnlyRecovery:
    """Documentation must explicitly encode CLI-only recovery."""

    def test_interfaces_doc_states_cli_only_recovery(self) -> None:
        """docs/INTERFACES.md must state that remote recovery is absent or rejected."""
        interfaces_path = Path(__file__).resolve().parents[2] / "docs" / "INTERFACES.md"
        text = interfaces_path.read_text(encoding="utf-8")
        lowered = text.lower()

        assert (
            "remote recovery is absent" in lowered
            or "remote recovery is rejected" in lowered
            or "cli-only recovery" in lowered
        ), (
            "INTERFACES.md must explicitly state remote recovery is absent or rejected "
            "per ADR-008 Branch B."
        )

    def test_adr008_doc_references_branch_b(self) -> None:
        """docs/ADR-008-operator-recovery-exposure.md must reference Branch B decision."""
        adr_path = Path(__file__).resolve().parents[2] / "docs" / "ADR-008-operator-recovery-exposure.md"
        text = adr_path.read_text(encoding="utf-8")

        assert "Branch B" in text or "CLI-only recovery" in text.lower(), (
            "ADR-008-operator-recovery-exposure.md must reference Branch B or CLI-only recovery."
        )
