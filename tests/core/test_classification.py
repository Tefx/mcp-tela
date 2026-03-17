"""Tests for tool posture classification."""

from __future__ import annotations

from tela.core.classification import classify_tool, posture_from_annotations
from tela.core.models import Posture, ServerConfig, ToolOverride


def test_classify_tool_from_override() -> None:
    cfg = ServerConfig(
        name="srv", command="cmd",
        tool_overrides={"t": ToolOverride(posture=Posture.READ_ONLY)},
    )
    assert classify_tool("t", cfg) == Posture.READ_ONLY


def test_classify_tool_from_annotations() -> None:
    cfg = ServerConfig(name="srv", command="cmd")
    assert classify_tool("t", cfg, {"readOnlyHint": True}) == Posture.READ_ONLY


def test_classify_tool_override_wins_over_annotations() -> None:
    cfg = ServerConfig(
        name="srv", command="cmd",
        tool_overrides={"t": ToolOverride(posture=Posture.DESTRUCTIVE)},
    )
    assert classify_tool("t", cfg, {"readOnlyHint": True}) == Posture.DESTRUCTIVE


def test_classify_tool_unclassified() -> None:
    cfg = ServerConfig(name="srv", command="cmd")
    assert classify_tool("t", cfg) is None


def test_posture_from_annotations_readonly() -> None:
    assert posture_from_annotations({"readOnlyHint": True}) == Posture.READ_ONLY


def test_posture_from_annotations_destructive() -> None:
    assert posture_from_annotations({"destructiveHint": True}) == Posture.DESTRUCTIVE


def test_posture_from_annotations_both_destructive_wins() -> None:
    assert posture_from_annotations({"readOnlyHint": True, "destructiveHint": True}) == Posture.DESTRUCTIVE


def test_posture_from_annotations_both_false_readwrite() -> None:
    assert posture_from_annotations({"readOnlyHint": False, "destructiveHint": False}) == Posture.READ_WRITE


def test_posture_from_annotations_empty() -> None:
    assert posture_from_annotations({}) is None
