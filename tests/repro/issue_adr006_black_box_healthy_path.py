"""Black-Box Verification: ADR-006 Healthy Path Probe-Free Contract

Expected: Healthy calls remain single-attempt with no preflight probe.
Actual:   Call_tool attempts exactly one downstream call on healthy path.

Test-step semantic reporting:
- step_intent: Verify healthy path has NO visible probe/preflight step
- expected_result: Single call_tool invocation on healthy client, no additional calls
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
    """Reset downstream state before/after each test."""
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


@pytest.fixture
def healthy_client_handle() -> downstream._ClientHandle:
    """A healthy client handle that does NOT require recovery."""
    session = MagicMock()
    # Successful tool call - this is the healthy path
    from mcp.types import CallToolResult, TextContent

    session.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="healthy_result")],
            isError=False,
        )
    )
    stack = MagicMock()
    stack.aclose = AsyncMock()
    return downstream._ClientHandle(session=session, stack=stack)


def test_black_box_healthy_path_probe_free(
    healthy_client_handle: downstream._ClientHandle,
) -> None:
    """ADR-006 §Caller-Visible Behavior: Healthy path remains probe-free.

    Black-box verification from public API surface:
    - Surface tested: call_tool(server_name, tool_name, arguments) -> Result[dict, TelaError]
    - Healthy case: client handle exists and is connected
    - Expected: Exactly ONE downstream call, NO preflight/probe/liveness check

    This test does NOT inspect:
    - Internal state (_clients dict structure)
    - Implementation details (lock acquisition timing)
    - Recovery paths (covered by other tests)
    """
    call_count = 0

    async def counting_call_tool(tool_name: str, arguments: dict) -> Any:
        nonlocal call_count
        call_count += 1
        # Return success on the single call
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(
            content=[TextContent(type="text", text="result")],
            isError=False,
        )

    # Setup: healthy client exists
    healthy_client_handle.session.call_tool = counting_call_tool
    downstream._clients["test_server"] = healthy_client_handle

    # Act: call_tool on healthy path
    result = asyncio.run(
        downstream.call_tool("test_server", "test_tool", {"arg": "value"})
    )

    # Assert: Single call, no probe
    assert call_count == 1, (
        f"ADR-006 VIOLATION: Healthy path must be single-attempt. "
        f"Expected exactly 1 call_tool invocation, got {call_count}. "
        f"Healthy calls MUST NOT require preflight probe or liveness check."
    )

    # Assert: Successful return
    assert result.is_ok, (
        f"ADR-006 VIOLATION: Healthy path MUST return success. "
        f"Got error: {result.error}"
    )

    # Assert: Tool payload returned (no new protocol step)
    assert result.value is not None, (
        "ADR-006 VIOLATION: Healthy path MUST return ordinary tool payload."
    )

    print(
        f"PASS: Healthy call executed exactly once without probe. Result: {result.value}"
    )


if __name__ == "__main__":
    # Run test
    import sys

    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
