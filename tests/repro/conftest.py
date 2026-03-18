"""Shared fixtures for black-box repro tests."""

from __future__ import annotations

import pytest

from tela.core.models import (
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
)


@pytest.fixture
def sample_connection() -> ConnectionContext:
    return ConnectionContext(
        connection_id="c-test",
        profile_name="dev",
        connected_at="2026-01-01T00:00:00Z",
    )


@pytest.fixture
def allow_result() -> EnforcementResult:
    return EnforcementResult(verdict=EnforcementVerdict.ALLOW)


@pytest.fixture
def deny_result() -> EnforcementResult:
    return EnforcementResult(
        verdict=EnforcementVerdict.DENY,
        denied_by="test",
        error_code="TEST_DENY",
    )
