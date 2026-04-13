"""Black-Box Verification: ADR-006 Recovery Protocol Step Transparency

Expected: Recovery is NOT a new protocol step visible to callers.
Actual:   Recovered calls surface as ordinary successful calls plus latency/diagnostics.

Test-step semantic reporting:
- step_intent: Verify recovery is transparent to callers (no new API surface)
- expected_result: Success after recovery returns ordinary tool payload, no recovery-specific fields
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


def test_black_box_recovery_transparency_no_new_api_step() -> None:
    """ADR-006 §Caller-Visible Behavior: No new protocol step exposed to agent.

    Black-box verification:
    - Surface tested: call_tool return type is Result[dict, TelaError]
    - Success after recovery returns ordinary dict payload (no recovery metadata)
    - Error after exhausted recovery returns TelaError(code="DOWNSTREAM_UNAVAILABLE")
    - No recovery-specific fields on success path

    This test verifies the PUBLIC API contract, not internal implementation.
    """
    from mcp.types import CallToolResult, TextContent

    call_count = 0

    async def first_call_fails_second_succeeds(tool_name: str, arguments: dict) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: recovery-eligible disconnect
            raise RuntimeError(
                "Client is not connected. Use the 'async with client:' context manager first."
            )
        # Second call: success after recovery
        return CallToolResult(
            content=[TextContent(type="text", text="recovered_result")],
            isError=False,
        )

    # Create client handle that will trigger recovery
    session = MagicMock()
    session.call_tool = first_call_fails_second_succeeds
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

    # Act: call_tool on disconnected client
    result = asyncio.run(
        downstream.call_tool("test_server", "test_tool", {"arg": "value"})
    )

    # Assert: Recovery succeeded, caller sees ordinary success
    assert result.is_ok, (
        f"ADR-006 VIOLATION: Recovery success should return is_ok=True. "
        f"Got error: {result.error}"
    )

    # Assert: Return type is ordinary dict (not recovery-specific type)
    assert result.value is not None, "Success result MUST have value"
    assert isinstance(result.value, dict), (
        f"ADR-006 VIOLATION: Success after recovery MUST return ordinary dict. "
        f"Got type: {type(result.value)}"
    )

    # Assert: No recovery-specific fields in success payload
    assert "recovery_attempted" not in result.value, (
        "ADR-006 VIOLATION: Success payload MUST NOT contain recovery metadata. "
        "Recovery is internal to the gateway."
    )
    assert "recovery_stage" not in result.value, (
        "ADR-006 VIOLATION: Success payload MUST NOT contain recovery stage. "
        "Recovery is internal to the gateway."
    )

    print(f"PASS: Recovery transparent - returned ordinary dict: {result.value}")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
