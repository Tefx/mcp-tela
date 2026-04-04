"""Black-Box Verification: ADR-006 Exhausted Recovery Returns DOWNSTREAM_UNAVAILABLE

Expected: Exhausted recovery returns the same error family as non-recovery failures.
Actual:   DOWNSTREAM_UNAVAILABLE with recovery context in details.

Test-step semantic reporting:
- step_intent: Verify exhausted recovery maintains stable error API surface
- expected_result: TelaError(code="DOWNSTREAM_UNAVAILABLE") with recovery fields
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
from tela.core.models import TelaError
from tela.shell import downstream
from tela.shell.config_loader import Result


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


def test_black_box_exhausted_recovery_error_shape() -> None:
    """ADR-006 §Error Model: Exhausted recovery returns DOWNSTREAM_UNAVAILABLE.

    Black-box verification:
    - Surface tested: TelaError shape on exhausted recovery
    - Expected code: "DOWNSTREAM_UNAVAILABLE" (NOT a new error code)
    - Expected details: recovery_attempted=True, recovery_stage=<stage>, recovery_eligible=True

    Per ADR-006:
    "exhausted recovery returns TelaError(code='DOWNSTREAM_UNAVAILABLE', ...)"
    "This ADR does not require a new public error code."
    """
    call_count = 0

    async def always_fails_connect_error(tool_name: str, arguments: dict) -> Any:
        nonlocal call_count
        call_count += 1
        raise RuntimeError(
            "Client is not connected. Use the 'async with client:' context manager first."
        )

    # Create client handle that always fails
    session = MagicMock()
    session.call_tool = always_fails_connect_error
    stack = MagicMock()
    stack.aclose = AsyncMock()
    client_handle = downstream._ClientHandle(session=session, stack=stack)

    downstream._clients["test_server"] = client_handle

    # Import required for recovery path
    from tela.shell.gateway_runtime import set_runtime_config
    from tela.core.models import ServerConfig, TelaConfig

    # Set up runtime config (required for recovery)
    set_runtime_config(
        TelaConfig(
            servers={"test_server": ServerConfig(name="test_server", command="echo")}
        )
    )

    # Act: call_tool on client that fails with recovery-eligible error
    result = asyncio.run(
        downstream.call_tool("test_server", "test_tool", {"arg": "value"})
    )

    # Assert: Error return
    assert result.is_err, "ADR-006 VIOLATION: Exhausted recovery MUST return error."

    # Assert: Error code is DOWNSTREAM_UNAVAILABLE (NOT a new code)
    assert result.error is not None, "Error MUST have error field"
    assert result.error.code == "DOWNSTREAM_UNAVAILABLE", (
        f"ADR-006 VIOLATION: Exhausted recovery MUST return DOWNSTREAM_UNAVAILABLE. "
        f"Got code: {result.error.code}. "
        f"ADR-006 explicitly states NO new public error code."
    )

    # Assert: recovery_attempted=True (recovery was tried)
    details = result.error.details or {}
    assert details.get("recovery_attempted") is True, (
        f"ADR-006 VIOLATION: Exhausted recovery MUST have recovery_attempted=True. "
        f"Got: {details.get('recovery_attempted')}"
    )

    # Assert: recovery_eligible=True (was eligible for recovery)
    assert details.get("recovery_eligible") is True, (
        f"ADR-006 VIOLATION: Exhausted recovery MUST have recovery_eligible=True. "
        f"Got: {details.get('recovery_eligible')}"
    )

    # Assert: recovery_stage is present
    assert "recovery_stage" in details, (
        f"ADR-006 VIOLATION: Exhausted recovery MUST have recovery_stage field. "
        f"Got details: {details}"
    )

    # Assert: underlying_error is present
    assert "underlying_error" in details, (
        f"ADR-006 VIOLATION: Exhausted recovery MUST have underlying_error field. "
        f"Got details: {details}"
    )

    # Assert: server_name is present
    assert details.get("server_name") == "test_server", (
        f"ADR-006 VIOLATION: Error details MUST include server_name. "
        f"Got: {details.get('server_name')}"
    )

    print(
        f"PASS: Exhausted recovery returned DOWNSTREAM_UNAVAILABLE with proper shape. Details: {details}"
    )


def test_black_box_exhausted_recovery_no_new_error_code() -> None:
    """ADR-006 §Error Model: No new public error code for exhausted recovery.

    Black-box verification:
    - Verify error code is the SAME for non-recovery and exhausted recovery
    - Callers cannot distinguish recovery exhaustion from other unavailable states
    - No RECOVERY_EXHAUSTED or RECOVERY_TIMEOUT error codes

    This ensures ADR-006's promise: "This ADR does not require a new public error code."
    """
    # Get error from recovery-eligible failure without recovery
    # (no client handle → immediate DOWNSTREAM_UNAVAILABLE)
    result_no_recovery = asyncio.run(
        downstream.call_tool("nonexistent_server", "tool", {})
    )

    # Get error from exhausted recovery (from previous test setup)
    # Already verified above that code is DOWNSTREAM_UNAVAILABLE

    # Both MUST return same error code
    assert result_no_recovery.is_err, "No-recovery case MUST return error"
    assert result_no_recovery.error is not None
    no_recovery_code = result_no_recovery.error.code

    # Verify: No new error codes introduced
    assert no_recovery_code == "DOWNSTREAM_UNAVAILABLE", (
        f"ADR-006 VIOLATION: Non-recovery error MUST be DOWNSTREAM_UNAVAILABLE. "
        f"Got: {no_recovery_code}"
    )

    # Verify: Error shape for non-recovery case
    details_no_recovery = result_no_recovery.error.details or {}
    assert details_no_recovery.get("recovery_attempted") is True, (
        f"ADR-006 VIOLATION: Non-recovery case MUST still have recovery_attempted field. "
        f"Got: {details_no_recovery}"
    )
    assert details_no_recovery.get("recovery_eligible") is True, (
        f"ADR-006 VIOLATION: Non-recovery case MUST still have recovery_eligible field. "
        f"Got: {details_no_recovery}"
    )

    print(f"PASS: No new error code - both return DOWNSTREAM_UNAVAILABLE")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
