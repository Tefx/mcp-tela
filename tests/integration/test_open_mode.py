"""Integration-level contract tests for open-mode runtime boundaries."""

from __future__ import annotations

from pathlib import Path


def test_start_contract_declares_stdio_default_and_sse_opt_in() -> None:
    source = Path("src/tela/commands/start.py").read_text(encoding="utf-8")
    assert "Default transport is stdio" in source
    assert "SSE transport is selected only when `--port` is provided" in source


def test_upstream_contract_declares_no_profile_selection_from_metadata() -> None:
    source = Path("src/tela/shell/upstream.py").read_text(encoding="utf-8")
    assert "must not influence profile selection" in source


def test_upstream_contract_declares_missing_or_ambiguous_rejection() -> None:
    source = Path("src/tela/shell/upstream.py").read_text(encoding="utf-8")
    assert "Missing default-profile resolution rejects initialize" in source
    assert "Ambiguous default-profile resolution rejects initialize" in source


def test_gateway_startup_contract_declares_open_mode_without_token() -> None:
    source = Path("src/tela/shell/gateway.py").read_text(encoding="utf-8")
    assert "open mode requires no token" in source
