"""Black-Box Verification: ADR-006 Ambiguous Failure Fail-Closed

Expected: Ambiguous failures (TimeoutError, BrokenPipeError) do NOT trigger recovery retry.
Actual:   These failures return DOWNSTREAM_UNAVAILABLE immediately without retry.

Test-step semantic reporting:
- step_intent: Verify ambiguous failures fail closed (no retry)
- expected_result: DOWNSTREAM_UNAVAILABLE error with recovery_attempted=False
- observed_result: [from test execution]
- failure_alignment: [after test execution]
- product_file_modifications: None (verification only)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Public API surface imports ONLY
from tela.shell import downstream


@pytest.fixture(autouse=True)
def clean_clients() -> None:
    """Reset downstream state."""
    downstream._clients.clear()
    downstream._server_instructions.clear()
    downstream._attempted_servers.clear()
    downstream._successful_servers.clear()
    downstream._recovery_locks.clear()
    yield
    downstream._clients.clear()
    downstream._server_instructions.clear()
    downstream._attempted_servers.clear()
    downstream._successful_servers.clear()
    downstream._recovery_locks.clear()


def test_black_box_timeout_error_fails_closed_retries_nowhere() -> None:
    """ADR-006 §Recovery Eligibility: TimeoutError is NOT recovery-eligible.

    Black-box verification:
    - Surface tested: call_tool return type on TimeoutError
    - Expected: DOWNSTREAM_UNAVAILABLE with recovery_eligible=False, recovery_attempted=False
    - NO retry attempt should occur

    Per ADR-006:
    "TimeoutError / asyncio.TimeoutError ... mid-flight ambiguity; retry safety
    is not provable locally."
    """
    call_count = 0

    async def timeout_call(tool_name: str, arguments: dict) -> Any:
        nonlocal call_count
        call_count += 1
        raise asyncio.TimeoutError("Operation timed out after 30 seconds")

    # Create client handle
    session = MagicMock()
    session.call_tool = timeout_call
    stack = MagicMock()
    stack.aclose = AsyncMock()
    client_handle = downstream._ClientHandle(session=session, stack=stack)

    downstream._clients["test_server"] = client_handle

    # Act: call_tool with timeout error
    result = asyncio.run(
        downstream.call_tool("test_server", "test_tool", {"arg": "value"})
    )

    # Assert: Single call attempt (no retry)
    assert call_count == 1, (
        f"ADR-006 VIOLATION: TimeoutError MUST NOT trigger retry. "
        f"Expected exactly 1 call, got {call_count}. "
        f"Ambiguous failures MUST fail closed."
    )

    # Assert: Error return
    assert result.is_err, (
        "ADR-006 VIOLATION: TimeoutError MUST return error (not success)."
    )

    # Assert: Error shape
    assert result.error is not None, "Error MUST have error field"
    assert result.error.code == "DOWNSTREAM_UNAVAILABLE", (
        f"ADR-006 VIOLATION: Exhausted recovery MUST return DOWNSTREAM_UNAVAILABLE. "
        f"Got code: {result.error.code}"
    )

    # Assert: recovery_eligible=False
    details = result.error.details or {}
    assert details.get("recovery_eligible") is False, (
        f"ADR-006 VIOLATION: TimeoutError MUST have recovery_eligible=False. "
        f"Got: {details.get('recovery_eligible')}"
    )

    # Assert: recovery_attempted=False
    assert details.get("recovery_attempted") is False, (
        f"ADR-006 VIOLATION: TimeoutError MUST have recovery_attempted=False. "
        f"Got: {details.get('recovery_attempted')}"
    )

    print(
        f"PASS: TimeoutError failed closed with DOWNSTREAM_UNAVAILABLE. Details: {details}"
    )


def test_black_box_broken_pipe_fails_closed_retries_nowhere() -> None:
    """ADR-006 §Recovery Eligibility: BrokenPipeError is NOT recovery-eligible.

    Black-box verification:
    - Surface tested: call_tool return type on BrokenPipeError
    - Expected: DOWNSTREAM_UNAVAILABLE with recovery_eligible=False, recovery_attempted=False
    - NO retry attempt should occur

    Per ADR-006:
    "BrokenPipeError, ConnectionResetError, EOF-like transport interruption after
    call submission ... mid-flight ambiguity; retry safety is not provable locally."
    """
    call_count = 0

    async def broken_pipe_call(tool_name: str, arguments: dict) -> Any:
        nonlocal call_count
        call_count += 1
        raise BrokenPipeError("[Errno 32] Broken pipe")

    # Create client handle
    session = MagicMock()
    session.call_tool = broken_pipe_call
    stack = MagicMock()
    stack.aclose = AsyncMock()
    client_handle = downstream._ClientHandle(session=session, stack=stack)

    downstream._clients["test_server"] = client_handle

    # Act: call_tool with broken pipe error
    result = asyncio.run(
        downstream.call_tool("test_server", "test_tool", {"arg": "value"})
    )

    # Assert: Single call attempt (no retry)
    assert call_count == 1, (
        f"ADR-006 VIOLATION: BrokenPipeError MUST NOT trigger retry. "
        f"Expected exactly 1 call, got {call_count}. "
        f"Ambiguous failures MUST fail closed."
    )

    # Assert: Error return
    assert result.is_err, (
        "ADR-006 VIOLATION: BrokenPipeError MUST return error (not success)."
    )

    # Assert: Error shape
    assert result.error is not None, "Error MUST have error field"
    assert result.error.code == "DOWNSTREAM_UNAVAILABLE", (
        f"ADR-006 VIOLATION: Exhausted recovery MUST return DOWNSTREAM_UNAVAILABLE. "
        f"Got code: {result.error.code}"
    )

    # Assert: recovery_eligible=False
    details = result.error.details or {}
    assert details.get("recovery_eligible") is False, (
        f"ADR-006 VIOLATION: BrokenPipeError MUST have recovery_eligible=False. "
        f"Got: {details.get('recovery_eligible')}"
    )

    # Assert: recovery_attempted=False
    assert details.get("recovery_attempted") is False, (
        f"ADR-006 VIOLATION: BrokenPipeError MUST have recovery_attempted=False. "
        f"Got: {details.get('recovery_attempted')}"
    )

    print(
        f"PASS: BrokenPipeError failed closed with DOWNSTREAM_UNAVAILABLE. Details: {details}"
    )


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
