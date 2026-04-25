"""Expected-red tests for ADR-008 client-neutral attachment lifecycle.

These tests validate the core-only helpers and models before shell I/O
is added. They should fail until the contracts and implementations are
in place.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tela.core.classification import (
    AttachmentDisplayState,
    AttachmentRegistry,
    ClientAttachment,
    Recoverability,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeState,
    classify_attachment_display_state,
    classify_recoverability,
    classify_runtime_state,
)


# --------------------------------------------------------------------
# AttachmentDisplayState enum tests
# --------------------------------------------------------------------


class TestAttachmentDisplayState:
    def test_display_state_values(self) -> None:
        assert AttachmentDisplayState.UNKNOWN.value == "unknown"
        assert AttachmentDisplayState.STARTED.value == "started"
        assert AttachmentDisplayState.HEALTHY.value == "healthy"
        assert AttachmentDisplayState.DEGRADED.value == "degraded"
        assert AttachmentDisplayState.STALE_CANDIDATE.value == "stale_candidate"
        assert AttachmentDisplayState.RECOVERING.value == "recovering"
        assert AttachmentDisplayState.EXITED.value == "exited"


# --------------------------------------------------------------------
# RuntimeState enum tests
# --------------------------------------------------------------------


class TestRuntimeState:
    def test_runtime_state_values(self) -> None:
        assert RuntimeState.UNKNOWN.value == "unknown"
        assert RuntimeState.INITIALIZING.value == "initializing"
        assert RuntimeState.ACTIVE.value == "active"
        assert RuntimeState.IDLE.value == "idle"
        assert RuntimeState.RECOVERING.value == "recovering"
        assert RuntimeState.EXITED.value == "exited"


# --------------------------------------------------------------------
# Recoverability enum tests
# --------------------------------------------------------------------


class TestRecoverability:
    def test_recoverability_values(self) -> None:
        assert Recoverability.UNKNOWN.value == "unknown"
        assert Recoverability.RECOVERABLE.value == "recoverable"
        assert Recoverability.NOT_RECOVERABLE.value == "not_recoverable"
        assert Recoverability.STALE.value == "stale"


# --------------------------------------------------------------------
# RuntimeEventKind enum tests
# --------------------------------------------------------------------


class TestRuntimeEventKind:
    def test_event_kind_values(self) -> None:
        assert RuntimeEventKind.CLIENT_ATTACHMENT_STARTED.value == "client_attachment_started"
        assert RuntimeEventKind.HEARTBEAT.value == "heartbeat"
        assert RuntimeEventKind.RECOVERY_PROBE.value == "recovery_probe"
        assert RuntimeEventKind.CLIENT_PROVIDER_EXIT.value == "client_provider_exit"
        assert RuntimeEventKind.RECOVERY_FAILED.value == "recovery_failed"
        assert RuntimeEventKind.RECOVERY_SUCCEEDED.value == "recovery_succeeded"


# --------------------------------------------------------------------
# ClientAttachment model tests
# --------------------------------------------------------------------


class TestClientAttachment:
    def test_minimal_valid_construction(self) -> None:
        att = ClientAttachment(
            client_id="c1",
            client_kind="cli",
            display_state=AttachmentDisplayState.HEALTHY,
            runtime_state=RuntimeState.ACTIVE,
            recoverability=Recoverability.RECOVERABLE,
            connected_at="2026-01-01T00:00:00Z",
            last_heartbeat="2026-01-01T00:01:00Z",
        )
        assert att.client_id == "c1"
        assert att.client_kind == "cli"
        assert att.display_state == AttachmentDisplayState.HEALTHY
        assert att.runtime_state == RuntimeState.ACTIVE
        assert att.recoverability == Recoverability.RECOVERABLE
        assert att.stale_candidate is False
        assert att.unknown_state is False

    def test_stale_candidate_defaults_to_false(self) -> None:
        att = ClientAttachment(
            client_id="c1",
            client_kind="cli",
            display_state=AttachmentDisplayState.HEALTHY,
            runtime_state=RuntimeState.ACTIVE,
            recoverability=Recoverability.RECOVERABLE,
            connected_at="2026-01-01T00:00:00Z",
            last_heartbeat="2026-01-01T00:01:00Z",
        )
        assert att.stale_candidate is False

    def test_unknown_state_defaults_to_false(self) -> None:
        att = ClientAttachment(
            client_id="c1",
            client_kind="cli",
            display_state=AttachmentDisplayState.HEALTHY,
            runtime_state=RuntimeState.ACTIVE,
            recoverability=Recoverability.RECOVERABLE,
            connected_at="2026-01-01T00:00:00Z",
            last_heartbeat="2026-01-01T00:01:00Z",
        )
        assert att.unknown_state is False

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ClientAttachment.model_validate({
                "client_id": "c1",
                "client_kind": "cli",
                "display_state": "healthy",
                "runtime_state": "active",
                "recoverability": "recoverable",
                "connected_at": "2026-01-01T00:00:00Z",
                "last_heartbeat": "2026-01-01T00:01:00Z",
                "extra_field": "not_allowed",
            })

    def test_requires_client_id(self) -> None:
        with pytest.raises(ValidationError):
            ClientAttachment.model_validate({
                "client_kind": "cli",
                "display_state": "healthy",
                "runtime_state": "active",
                "recoverability": "recoverable",
                "connected_at": "2026-01-01T00:00:00Z",
                "last_heartbeat": "2026-01-01T00:01:00Z",
            })

    def test_requires_min_length_client_id(self) -> None:
        with pytest.raises(ValidationError):
            ClientAttachment.model_validate({
                "client_id": "",
                "client_kind": "cli",
                "display_state": "healthy",
                "runtime_state": "active",
                "recoverability": "recoverable",
                "connected_at": "2026-01-01T00:00:00Z",
                "last_heartbeat": "2026-01-01T00:01:00Z",
            })

    def test_requires_client_kind(self) -> None:
        with pytest.raises(ValidationError):
            ClientAttachment.model_validate({
                "client_id": "c1",
                "display_state": "healthy",
                "runtime_state": "active",
                "recoverability": "recoverable",
                "connected_at": "2026-01-01T00:00:00Z",
                "last_heartbeat": "2026-01-01T00:01:00Z",
            })

    def test_requires_connected_at(self) -> None:
        with pytest.raises(ValidationError):
            ClientAttachment.model_validate({
                "client_id": "c1",
                "client_kind": "cli",
                "display_state": "healthy",
                "runtime_state": "active",
                "recoverability": "recoverable",
                "last_heartbeat": "2026-01-01T00:01:00Z",
            })

    def test_requires_last_heartbeat(self) -> None:
        with pytest.raises(ValidationError):
            ClientAttachment.model_validate({
                "client_id": "c1",
                "client_kind": "cli",
                "display_state": "healthy",
                "runtime_state": "active",
                "recoverability": "recoverable",
                "connected_at": "2026-01-01T00:00:00Z",
            })


# --------------------------------------------------------------------
# AttachmentRegistry model tests
# --------------------------------------------------------------------


class TestAttachmentRegistry:
    def test_empty_registry(self) -> None:
        reg = AttachmentRegistry(attachments=[])
        assert len(reg.attachments) == 0

    def test_multiple_attachments(self) -> None:
        att1 = ClientAttachment(
            client_id="c1",
            client_kind="cli",
            display_state=AttachmentDisplayState.HEALTHY,
            runtime_state=RuntimeState.ACTIVE,
            recoverability=Recoverability.RECOVERABLE,
            connected_at="2026-01-01T00:00:00Z",
            last_heartbeat="2026-01-01T00:01:00Z",
        )
        att2 = ClientAttachment(
            client_id="c2",
            client_kind="env",
            display_state=AttachmentDisplayState.STALE_CANDIDATE,
            runtime_state=RuntimeState.IDLE,
            recoverability=Recoverability.STALE,
            connected_at="2026-01-01T00:00:00Z",
            last_heartbeat="2026-01-01T00:02:00Z",
            stale_candidate=True,
        )
        reg = AttachmentRegistry(attachments=[att1, att2])
        assert len(reg.attachments) == 2
        assert reg.attachments[0].client_id == "c1"
        assert reg.attachments[1].client_id == "c2"

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            AttachmentRegistry.model_validate({
                "attachments": [],
                "extra_field": "not_allowed",
            })


# --------------------------------------------------------------------
# RuntimeEvent model tests
# --------------------------------------------------------------------


class TestRuntimeEvent:
    def test_minimal_valid_construction(self) -> None:
        evt = RuntimeEvent(
            kind=RuntimeEventKind.CLIENT_ATTACHMENT_STARTED,
            client_id="c1",
            client_kind="cli",
            timestamp="2026-01-01T00:00:00Z",
        )
        assert evt.kind == RuntimeEventKind.CLIENT_ATTACHMENT_STARTED
        assert evt.client_id == "c1"
        assert evt.client_kind == "cli"
        assert evt.details == {}

    def test_with_details(self) -> None:
        evt = RuntimeEvent(
            kind=RuntimeEventKind.RECOVERY_FAILED,
            client_id="c1",
            client_kind="cli",
            timestamp="2026-01-01T00:00:00Z",
            details={"reason": "timeout", "attempt": 3},
        )
        assert evt.details["reason"] == "timeout"
        assert evt.details["attempt"] == 3

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeEvent.model_validate({
                "kind": "client_attachment_started",
                "client_id": "c1",
                "client_kind": "cli",
                "timestamp": "2026-01-01T00:00:00Z",
                "extra_field": "not_allowed",
            })

    def test_requires_client_id(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeEvent.model_validate({
                "kind": "client_attachment_started",
                "client_kind": "cli",
                "timestamp": "2026-01-01T00:00:00Z",
            })

    def test_requires_timestamp(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeEvent.model_validate({
                "kind": "client_attachment_started",
                "client_id": "c1",
                "client_kind": "cli",
            })

    def test_guard_size_allows_small_event(self) -> None:
        evt = RuntimeEvent(
            kind=RuntimeEventKind.HEARTBEAT,
            client_id="c1",
            client_kind="cli",
            timestamp="2026-01-01T00:00:00Z",
        )
        assert evt.guard_size() is True

    def test_guard_size_rejects_oversized_event(self) -> None:
        large_details = {"data": "x" * 20000}  # ~20KB
        evt = RuntimeEvent(
            kind=RuntimeEventKind.RECOVERY_FAILED,
            client_id="c1",
            client_kind="cli",
            timestamp="2026-01-01T00:00:00Z",
            details=large_details,
        )
        assert evt.guard_size() is False


# --------------------------------------------------------------------
# classify_attachment_display_state tests
# --------------------------------------------------------------------


class TestClassifyAttachmentDisplayState:
    def test_unknown_state_wins(self) -> None:
        result = classify_attachment_display_state(
            RuntimeState.ACTIVE,
            Recoverability.RECOVERABLE,
            False,
            True,
        )
        assert result == AttachmentDisplayState.UNKNOWN

    def test_stale_candidate_wins(self) -> None:
        result = classify_attachment_display_state(
            RuntimeState.ACTIVE,
            Recoverability.RECOVERABLE,
            True,
            False,
        )
        assert result == AttachmentDisplayState.STALE_CANDIDATE

    def test_exited_state(self) -> None:
        result = classify_attachment_display_state(
            RuntimeState.EXITED,
            Recoverability.NOT_RECOVERABLE,
            False,
            False,
        )
        assert result == AttachmentDisplayState.EXITED

    def test_recovering_state(self) -> None:
        result = classify_attachment_display_state(
            RuntimeState.RECOVERING,
            Recoverability.RECOVERABLE,
            False,
            False,
        )
        assert result == AttachmentDisplayState.RECOVERING

    def test_stale_recoverability_degraded(self) -> None:
        result = classify_attachment_display_state(
            RuntimeState.ACTIVE,
            Recoverability.STALE,
            False,
            False,
        )
        assert result == AttachmentDisplayState.DEGRADED

    def test_active_recoverable_healthy(self) -> None:
        result = classify_attachment_display_state(
            RuntimeState.ACTIVE,
            Recoverability.RECOVERABLE,
            False,
            False,
        )
        assert result == AttachmentDisplayState.HEALTHY

    def test_idle_degraded(self) -> None:
        result = classify_attachment_display_state(
            RuntimeState.IDLE,
            Recoverability.RECOVERABLE,
            False,
            False,
        )
        assert result == AttachmentDisplayState.DEGRADED

    def test_default_unknown(self) -> None:
        result = classify_attachment_display_state(
            RuntimeState.INITIALIZING,
            Recoverability.RECOVERABLE,
            False,
            False,
        )
        assert result == AttachmentDisplayState.UNKNOWN


# --------------------------------------------------------------------
# classify_runtime_state tests
# --------------------------------------------------------------------


class TestClassifyRuntimeState:
    def test_unknown_client_kind(self) -> None:
        result = classify_runtime_state("unknown", "normal", True)
        assert result == RuntimeState.UNKNOWN

    def test_inactive_connection_exited(self) -> None:
        result = classify_runtime_state("cli", "normal", False)
        assert result == RuntimeState.EXITED

    def test_no_init_mode_initializing(self) -> None:
        result = classify_runtime_state("cli", None, True)
        assert result == RuntimeState.INITIALIZING

    def test_recovery_mode_recovering(self) -> None:
        result = classify_runtime_state("cli", "recovery", True)
        assert result == RuntimeState.RECOVERING

    def test_active_connection(self) -> None:
        result = classify_runtime_state("cli", "normal", True)
        assert result == RuntimeState.ACTIVE

    def test_idle_fallback(self) -> None:
        result = classify_runtime_state("env", "unknown_mode", False)
        assert result == RuntimeState.EXITED


# --------------------------------------------------------------------
# classify_recoverability tests
# --------------------------------------------------------------------


class TestClassifyRecoverability:
    def test_unknown_client_kind(self) -> None:
        result = classify_recoverability("unknown", RuntimeState.ACTIVE, 30.0)
        assert result == Recoverability.UNKNOWN

    def test_exited_not_recoverable(self) -> None:
        result = classify_recoverability("cli", RuntimeState.EXITED, 30.0)
        assert result == Recoverability.NOT_RECOVERABLE

    def test_unknown_runtime_state(self) -> None:
        result = classify_recoverability("cli", RuntimeState.UNKNOWN, 30.0)
        assert result == Recoverability.UNKNOWN

    def test_no_heartbeat_recoverable(self) -> None:
        result = classify_recoverability("cli", RuntimeState.ACTIVE, None)
        assert result == Recoverability.RECOVERABLE

    def test_very_stale_heartbeat(self) -> None:
        result = classify_recoverability("cli", RuntimeState.ACTIVE, 120.0)
        assert result == Recoverability.STALE

    def test_moderately_stale_heartbeat(self) -> None:
        result = classify_recoverability("cli", RuntimeState.ACTIVE, 75.0)
        assert result == Recoverability.RECOVERABLE

    def test_fresh_heartbeat(self) -> None:
        result = classify_recoverability("cli", RuntimeState.ACTIVE, 30.0)
        assert result == Recoverability.RECOVERABLE


# --------------------------------------------------------------------
# Doctest extraction for classification functions
# --------------------------------------------------------------------


def test_doctest_classify_attachment_display_state() -> None:
    """Verify doctests from the function itself."""
    from tela.core.classification import classify_attachment_display_state

    result = classify_attachment_display_state(
        RuntimeState.ACTIVE,
        Recoverability.RECOVERABLE,
        False,
        False,
    )
    assert result == AttachmentDisplayState.HEALTHY


def test_doctest_classify_runtime_state() -> None:
    """Verify doctests from the function itself."""
    from tela.core.classification import classify_runtime_state

    result = classify_runtime_state("cli", "normal", True)
    assert result == RuntimeState.ACTIVE


def test_doctest_classify_recoverability() -> None:
    """Verify doctests from the function itself."""
    from tela.core.classification import classify_recoverability

    result = classify_recoverability("cli", RuntimeState.EXITED, 30.0)
    assert result == Recoverability.NOT_RECOVERABLE