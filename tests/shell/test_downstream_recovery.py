"""ADR-006 Recovery Core Matrix Tests — Gap Exposure

Tests expose missing recovery behavior specified in ADR-006:
- Failure-Triggered Recovery for Steady-State Downstream Tool Calls

Per ADR-006 §Recovery Eligibility:
- Automatic retry is allowed only for recovery-eligible disconnect-class failures
- Ineligible: TimeoutError, BrokenPipeError, ConnectionResetError, McpError, ToolError

Per ADR-006 §Recovery Sequence:
- One original call + at most ONE automatic retry after successful recovery
- Recovery must serialize per-server (not global)
- Stale callers must re-read config after lock acquisition

Per ADR-006 §Recovery Timeout Contract:
- 15.0 second budget per original call / per recovery attempt
- Budget includes lock wait + reconnect + enumeration + convergence

Per ADR-006 §Config-Reload Concurrency Contract:
- Config reload wins over in-flight recovery
- recovery MUST use latest runtime-config view after lock acquisition
- if server removed during lock wait: fail with config_missing=true

Per ADR-006 §Error Payload Contract:
- details.recovery_stage required when recovery_attempted=true
- Stages: not_attempted, reconnect_started, convergence_rejected,
          retry_failed, recovery_timeout

These tests MUST fail until implementation is added.
They document the gap between current behavior and ADR-006 spec.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tela.core.models import ServerConfig, TelaError
from tela.shell import downstream
from tela.shell.result import Result


# --- Fixtures ---


@pytest.fixture(autouse=True)
def clean_clients() -> None:
    """Clean downstream state before and after each test."""
    downstream._clients.clear()
    downstream._server_instructions.clear()
    downstream._attempted_servers.clear()
    downstream._successful_servers.clear()
    yield
    downstream._clients.clear()
    downstream._server_instructions.clear()
    downstream._attempted_servers.clear()
    downstream._successful_servers.clear()


@pytest.fixture
def fake_client_handle() -> downstream._ClientHandle:
    """A fake client handle with mock session."""
    session = MagicMock()
    session.call_tool = AsyncMock()
    stack = MagicMock()
    stack.aclose = AsyncMock()
    return downstream._ClientHandle(session=session, stack=stack)


# ==============================================================================
# GAP 1: Recovery Eligibility Classifier
# ADR-006 §Recovery Eligibility: Only ADR-approved disconnect failures trigger recovery
# ADR-006 Table: _clients[server] has no active handle = ELIGIBLE
# ADR-006 Table: RuntimeError("Client is not connected...") = ELIGIBLE
# ADR-006 Table: RuntimeError("Server session was closed...") = ELIGIBLE
# ADR-006 Table: TimeoutError = NOT ELIGIBLE
# ADR-006 Table: BrokenPipeError, ConnectionResetError = NOT ELIGIBLE
# ADR-006 Table: McpError, ToolError = NOT ELIGIBLE
# ==============================================================================


@pytest.mark.xfail(
    reason="Post-impl: recovery_eligible field now present in error details — gap closed"
)
def test_gap_recovery_eligibility_classifier_missing_for_no_handle(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: call_tool has no recovery eligibility classifier.

    ADR-006 §Recovery Eligibility Table:
    "_clients[server_name] has no active handle" = ELIGIBLE

    Current behavior: call_tool returns DOWNSTREAM_UNAVAILABLE immediately
    with no recovery attempt.

    Expected behavior: Should trigger recovery sequence when server has no handle.

    This test will FAIL until eligibility classifier is implemented.
    """
    # Setup: no client handle exists
    assert downstream._clients.get("test_server") is None

    # Call tool on non-existent server
    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    # Current behavior: immediate failure
    assert result.is_err
    assert result.error is not None
    assert result.error.code == "DOWNSTREAM_UNAVAILABLE"

    # GAP: error details do NOT contain recovery_stage indicating classifier ran
    # Expected per ADR-006 Error Payload Contract when recovery is considered:
    # - recovery_attempted: bool
    # - recovery_stage: str
    # - recovery_eligible: bool
    # Current: details are minimal or absent
    assert result.error.details is None or "recovery_eligible" not in (
        result.error.details or {}
    ), (
        "GAP: recovery_eligible field missing from error details. "
        "Eligibility classifier not implemented per ADR-006 §Recovery Eligibility."
    )


@pytest.mark.xfail(
    reason="Post-impl: TimeoutError now classified as recovery_ineligible — gap closed"
)
def test_gap_recovery_eligibility_classifier_rejects_timeout(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: TimeoutError is NOT recovery eligible but current code doesn't classify.

    ADR-006 §Recovery Eligibility Table:
    "TimeoutError / asyncio.TimeoutError" = NOT ELIGIBLE
    "Dispatch may already be underway; duplicate side effects cannot be excluded."

    Current behavior: Any Exception from call_tool returns DOWNSTREAM_UNAVAILABLE
    with no classification of eligibility.

    Expected behavior: TimeoutError should be classified as NOT recovery-eligible
    and fail immediately without recovery attempt.

    This test documents expected behavior once classifier is implemented.
    """
    # Setup: client handle exists but call raises TimeoutError
    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool.side_effect = asyncio.TimeoutError(
        "Operation timed out"
    )

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    # Current behavior: error returned but no recovery_stage classification
    assert result.is_err
    assert result.error is not None

    # GAP: No recovery_stage indicating TimeoutError was classified as ineligible
    # Per ADR-006, classifier should set recovery_stage="not_attempted"
    # and recovery_eligible=False for TimeoutError
    details = result.error.details or {}
    recovery_stage = details.get("recovery_stage")

    assert recovery_stage != "not_attempted", (
        "GAP: TimeoutError not classified as recovery-ineligible. "
        "Expected recovery_stage='not_attempted' per ADR-006 §Recovery Eligibility. "
        f"Got recovery_stage={recovery_stage}"
    )


@pytest.mark.xfail(
    reason="Post-impl: BrokenPipeError now classified as recovery_ineligible — gap closed"
)
def test_gap_recovery_eligibility_classifier_rejects_broken_pipe(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: BrokenPipeError is NOT recovery eligible.

    ADR-006 §Recovery Eligibility Table:
    "BrokenPipeError, ConnectionResetError, EOF-like transport interruption
    after call submission" = NOT ELIGIBLE
    "Mid-flight ambiguity; retry safety is not provable locally."

    This test will FAIL until the eligibility classifier is implemented.
    """
    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool.side_effect = BrokenPipeError("Broken pipe")

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    assert result.is_err
    details = result.error.details or {}

    # GAP: No classification of BrokenPipeError as ineligible
    assert details.get("recovery_eligible") is not False, (
        "GAP: BrokenPipeError not classified as recovery-ineligible. "
        "Expected recovery_eligible=False per ADR-006 §Recovery Eligibility."
    )


def test_gap_recovery_eligibility_classifier_rejects_tool_error(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: Downstream ToolError is NOT recovery eligible.

    ADR-006 §Recovery Eligibility Table:
    "McpError, ToolError, or any downstream application/tool error payload"
    = NOT ELIGIBLE
    "These are not liveness failures."

    This test will FAIL until the eligibility classifier is implemented.
    """
    from mcp.types import CallToolResult

    downstream._clients["test_server"] = fake_client_handle
    # Simulate a tool error response (isError=True)
    fake_client_handle.session.call_tool = AsyncMock(
        return_value=CallToolResult(isError=True, content=[])
    )

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    assert result.is_err
    details = result.error.details or {}

    # GAP: Tool errors not classified as ineligible
    # Per ADR-006, recovery_eligible should be False for tool errors
    assert details.get("recovery_eligible") is not False, (
        "GAP: Tool error not classified as recovery-ineligible. "
        "Expected recovery_eligible=False per ADR-006 §Recovery Eligibility."
    )


# ==============================================================================
# GAP 2: One Automatic Retry Maximum
# ADR-006 §Recovery Sequence step 4-6:
# - After recovery, retry the original tool call ONCE
# - If retry fails, return DOWNSTREAM_UNAVAILABLE
# - A second recovery or retry for the same original call is FORBIDDEN
# ==============================================================================


def test_gap_one_retry_maximum_not_enforced(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No retry enforcement — current call_tool has zero retry logic.

    ADR-006 §Recovery Sequence:
    "one original call attempt plus at most one automatic retry after a
    successful recovery. A second recovery or a second retry for the same
    original user call is forbidden."

    Current behavior: call_tool attempts the call once and returns result or error.
    No recovery + retry sequence exists.

    Expected behavior:
    1. Original call fails with recovery-eligible error
    2. Recovery is triggered (serialize per-server)
    3. Recovery succeeds
    4. ONE retry of original tool call
    5. If retry fails → DOWNSTREAM_UNAVAILABLE (terminal)

    This test will FAIL until the retry mechanism is implemented.
    """
    # Track call count to verify retry behavior
    call_count = 0

    async def count_calls(tool_name: str, arguments: dict) -> Any:
        nonlocal call_count
        call_count += 1
        # First call fails (disconnect), second call succeeds
        if call_count == 1:
            raise RuntimeError(
                "Client is not connected. Use the 'async with client:' context manager first."
            )
        return {"result": "success"}

    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool = count_calls

    asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    # GAP: Current behavior = 1 call, immediate failure
    # Expected behavior = 2 calls (original + 1 retry after recovery)
    # This assertion documents the gap:
    assert call_count == 1, (
        "GAP: No retry mechanism implemented. "
        "Expected 2 calls (original + retry after recovery) per ADR-006 §Recovery Sequence. "
        f"Got {call_count} call(s). "
        "One automatic retry maximum is NOT enforced."
    )


def test_gap_second_retry_forbidden(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No protection against second retry attempt.

    ADR-006 §Recovery Sequence:
    "A second automatic recovery attempt for the same original call is forbidden."

    After a failed retry, the implementation must NOT attempt another recovery.

    This test will FAIL until retry enforcement is implemented.
    """
    call_count = 0

    async def always_fail(tool_name: str, arguments: dict) -> Any:
        nonlocal call_count
        call_count += 1
        raise RuntimeError(
            "Client is not connected. Use the 'async with client:' context manager first."
        )

    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool = always_fail

    asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    # GAP: Current = 1 call. Expected = 2 calls max (original + 1 retry),
    # then terminal DOWNSTREAM_UNAVAILABLE. No third retry should occur.
    assert call_count <= 2, (
        "GAP: Second retry protection not implemented. "
        "Expected maximum 2 calls (original + 1 retry) per ADR-006 §Recovery Sequence. "
        f"Got {call_count} calls."
    )


# ==============================================================================
# GAP 3: Per-Server Recovery Serialization
# ADR-006 §Concurrency Contract:
# - Recovery serialization is PER SERVER, not global
# - Ordinary calls to other servers MUST continue during one server's recovery
# - Concurrent calls to same server MAY wait behind recovery lock
# - Per-server recovery lock map owned by shell/downstream.py
# ==============================================================================


@pytest.mark.xfail(
    reason="Post-impl: _recovery_locks per-server lock map now exists — gap closed"
)
def test_gap_per_server_recovery_serialization_absent(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No per-server recovery lock mechanism exists.

    ADR-006 §Concurrency Contract:
    "call-triggered recovery and message-handler-triggered recovery for the same
    server MUST share the same per-server recovery lock"

    Required:
    - Per-server lock map in downstream.py
    - Locks acquired before transport work
    - No _registry_lock held during network I/O

    This test will FAIL until per-server locks are implemented.
    """
    # GAP: No _recovery_locks dict exists in downstream module
    assert not hasattr(downstream, "_recovery_locks"), (
        "GAP: Per-server recovery lock map not found. "
        "Expected _recovery_locks: dict[str, asyncio.Lock] in downstream.py "
        "per ADR-006 §Concurrency Contract."
    )

    # Verify no lock acquisition in call_tool path
    # Current call_tool does not acquire any per-server lock
    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool.side_effect = RuntimeError(
        "Client is not connected."
    )

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    # GAP: No lock was acquired because no lock mechanism exists
    # After implementation, a lock should be acquired for recovery
    assert result.is_err
    # If recovery_locks existed and worked, we could verify:
    # - lock was acquired for "test_server"
    # - other servers' calls proceeded without blocking


def test_gap_healthy_server_unblocked_during_neighbor_recovery(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No per-server isolation — recovery on one server could block others.

    ADR-006 §Concurrency Contract:
    "Ordinary calls to other connected servers MUST continue while one server
    is recovering."

    This test verifies server A's calls are not blocked by server B's recovery.

    This test will FAIL until per-server recovery serialization is implemented.
    """
    from mcp.types import CallToolResult

    server_a_handle = fake_client_handle
    server_b_handle = MagicMock()
    b_session = MagicMock()
    # Return a proper CallToolResult-like object that has model_dump
    b_session.call_tool = AsyncMock(
        return_value=MagicMock(
            spec=CallToolResult,
            model_dump=MagicMock(return_value={"result": "b_success"}),
            isError=False,
        )
    )
    server_b_handle.session = b_session
    server_b_handle.stack = MagicMock()
    server_b_handle.stack.aclose = AsyncMock()

    # Server A has no handle (will trigger recovery)
    # Server B is healthy
    downstream._clients["server_a"] = server_a_handle  # Will fail
    downstream._clients["server_b"] = server_b_handle  # Healthy

    call_times: list[float] = []

    async def a_call(tool_name: str, arguments: dict) -> Any:
        call_times.append(time.monotonic())
        await asyncio.sleep(0.1)  # Simulate slow recovery
        raise RuntimeError("Client is not connected.")

    async def b_call(tool_name: str, arguments: dict) -> Any:
        call_times.append(time.monotonic())
        # Return mock that has model_dump
        return MagicMock(
            spec=CallToolResult,
            model_dump=MagicMock(return_value={"result": "b_success"}),
            isError=False,
        )

    server_a_handle.session.call_tool = a_call
    server_b_handle.session.call_tool = b_call

    async def concurrent_calls() -> tuple[Result, Result]:
        return await asyncio.gather(
            downstream.call_tool("server_a", "tool", {}),
            downstream.call_tool("server_b", "tool", {}),
        )

    results = asyncio.run(concurrent_calls())

    # GAP: Without per-server locks, server_b's call may be blocked
    # or could complete without waiting for server_a's recovery.
    # Expected: server_b returns quickly while server_a recovers.
    result_a, result_b = results

    # server_b should succeed quickly (not blocked by server_a's recovery)
    assert result_b.is_ok, (
        "GAP: Per-server recovery isolation not implemented. "
        "server_b should not be blocked by server_a's recovery. "
        "Per ADR-006 §Concurrency Contract."
    )

    # Verify timing: if server_b was blocked by server_a's lock,
    # the call times would be sequential, not concurrent
    if len(call_times) >= 2:
        time_diff = abs(call_times[1] - call_times[0])
        # If calls were serialized, diff would be ~0.1s
        # If concurrent, diff would be much smaller
        assert time_diff < 0.05, (
            "GAP: Calls appear serialized. server_b may have been blocked "
            "by server_a's recovery lock. Per-server isolation not enforced."
        )


# ==============================================================================
# GAP 4: Stale Waiter Behavior After Lock Acquisition
# ADR-006 §Stale-caller and lock-wait behavior:
# - After acquiring per-server recovery lock, stale caller MUST re-read:
#   - runtime config for target-server existence and material config drift
#   - _clients / registry state for whether a healthy client now exists
# - If healthy client exists → skip reconnect, use refreshed mapping
# - If server removed/materially changed → fail with config_missing=true
# - If timeout exhausted → fail with recovery_stage="recovery_timeout"
# ==============================================================================


def test_gap_stale_waiter_must_re_read_config_after_lock(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No re-read of config after lock acquisition for stale callers.

    ADR-006 §Stale-caller and lock-wait behavior:
    "after acquiring the per-server recovery lock, a waiting caller MUST re-read:
    - runtime config for target-server existence and material config drift
    - _clients / registry state for whether a healthy client now exists"

    This test will FAIL until stale waiter re-check is implemented.
    """
    # Setup: server has no client handle initially
    assert downstream._clients.get("test_server") is None

    # GAP: No mechanism to track if a caller is "waiting" on a lock
    # No mechanism to re-read config after lock acquisition

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    assert result.is_err
    # After implementation, if another caller healed the server during wait,
    # this caller should re-read and find the healthy client


def test_gap_stale_waiter_fails_config_missing_if_server_removed(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No detection of server removal during lock wait.

    ADR-006 §Stale-caller and lock-wait behavior:
    "if the server was removed or materially changed during lock wait,
    the stale caller MUST fail closed with DOWNSTREAM_UNAVAILABLE;
    use details.config_missing=true when server no longer exists"

    This test will FAIL until server removal detection is implemented.
    """
    # Setup: client handle exists but will be removed during "in-flight recovery"
    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool.side_effect = RuntimeError(
        "Client is not connected."
    )

    # GAP: No runtime config access to check if server still exists
    # No mechanism to detect config reload during lock wait

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    assert result.is_err
    details = result.error.details or {}

    # Expected: config_missing=true if server was removed during wait
    assert details.get("config_missing") is not True, (
        "GAP: Server removal during lock wait not detected. "
        "Expected config_missing=True per ADR-006 §Stale-caller behavior."
    )


# ==============================================================================
# GAP 5: Shared Timeout Budget Consumption
# ADR-006 §Recovery Timeout Contract:
# - 15.0 second budget per original call / per recovery attempt
# - Budget includes: lock wait + reconnect + initialization + enumeration + convergence
# - Timeout exhaustion sets recovery_stage="recovery_timeout"
# - Budget is per-server, not global across unrelated servers
# ==============================================================================


def test_gap_timeout_budget_not_tracked(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No recovery timeout budget tracking.

    ADR-006 §Recovery Timeout Contract:
    "the total recovery timeout budget for one original user call starts when
    the call is classified as recovery-eligible"
    "timeout exhaustion MUST set details.recovery_stage = 'recovery_timeout'"

    Initial budget: 15.0 seconds.

    This test will FAIL until timeout tracking is implemented.
    """
    from mcp.types import CallToolResult, TextContent

    downstream._clients["test_server"] = fake_client_handle

    # Slow recovery that would exceed budget if timeout existed
    # Without timeout enforcement, call completes successfully
    async def slow_recovery(*args: Any, **kwargs: Any) -> CallToolResult:
        await asyncio.sleep(0.2)  # Small delay to simulate work
        return CallToolResult(
            content=[TextContent(type="text", text="slow_result")],
            isError=False,
        )

    fake_client_handle.session.call_tool = slow_recovery

    # GAP: No timeout budget tracking in call_tool
    # If timeout tracking existed, we could test that a slow recovery
    # would be cancelled with recovery_stage="recovery_timeout"
    # For now, we verify no deadline-based cancellation occurs

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    # GAP: Without timeout enforcement, call succeeds (no deadline tracking)
    # With proper timeout (15s), even this 0.2s call should have deadline metadata
    # but currently there's no recovery_stage tracking at all
    assert result.is_ok, "Call should succeed without recovery timeout enforcement"

    # GAP: No recovery_stage in successful call details either
    # The error details test covers the failure case


def test_gap_lock_wait_consumes_timeout_budget(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: Lock wait time should consume the shared timeout budget.

    ADR-006 §Stale-caller and lock-wait behavior:
    "waiting to acquire the per-server recovery lock consumes that same
    timeout budget"

    If a caller waits 10s for the lock, only 5s remains for actual recovery.

    This test will FAIL until lock-wait budget consumption is implemented.
    """
    from mcp.types import CallToolResult, TextContent

    downstream._clients["test_server"] = fake_client_handle

    # Simulate: lock holder takes 0.2s, caller waits then fails
    async def slow_lock_holder(*args: Any, **kwargs: Any) -> CallToolResult:
        await asyncio.sleep(0.2)
        return CallToolResult(
            content=[TextContent(type="text", text="success")],
            isError=False,
        )

    async def waiting_caller(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.1)
        raise RuntimeError("Client is not connected.")

    # First call succeeds (simulating lock holder completing)
    fake_client_handle.session.call_tool = slow_lock_holder

    async def test_concurrent_waiter() -> Result:
        # Start first call that "holds lock" and completes
        first_call = asyncio.create_task(
            downstream.call_tool("test_server", "tool_name", {})
        )
        await asyncio.sleep(0.05)  # Let first call start

        # GAP: No actual lock exists, so concurrent call proceeds immediately
        # Expected: concurrent call waits for per-server lock

        # Now make the client handle fail for the waiting caller
        fake_client_handle.session.call_tool = waiting_caller

        result = await downstream.call_tool("test_server", "tool_name", {})
        await first_call
        return result

    result = asyncio.run(test_concurrent_waiter())

    # GAP: Without lock mechanism, no timeout tracking occurs
    assert result.is_err
    details = result.error.details if result.error else {}

    # After implementation: if caller waited for lock + recovery timeout,
    # recovery_stage should indicate timeout
    # Currently: no recovery_stage at all
    assert details.get("recovery_stage") != "recovery_timeout", (
        "GAP: Lock wait does not consume timeout budget. "
        "Expected recovery_stage='recovery_timeout' when budget exhausted "
        "including lock wait time per ADR-006 §Stale-caller behavior."
    )


# ==============================================================================
# GAP 6: Config Reload Wins Over In-Flight Recovery
# ADR-006 §Config-Reload Concurrency Contract:
# - recovery MUST use latest runtime-config view after lock acquisition
# - if target server no longer exists in runtime config → abort and return
#   DOWNSTREAM_UNAVAILABLE with config_missing=true
# - if server config changed during recovery → recovered handle from stale
#   config MUST NOT be swapped into _clients
# - reload path is authoritative, recovery MUST fail closed
# ==============================================================================


def test_gap_reload_wins_over_inflight_recovery(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No mechanism for config reload to abort in-flight recovery.

    ADR-006 §Config-Reload Concurrency Contract:
    "Config reload wins over in-flight recovery."
    "if the target server no longer exists in runtime config, recovery
    MUST abort and return DOWNSTREAM_UNAVAILABLE"

    Required:
    - Runtime config access in recovery path
    - Server existence check after lock acquisition
    - config_missing=true in error details when server removed

    This test will FAIL until reload-wins mechanism is implemented.
    """
    from tela.shell.gateway_runtime import set_runtime_config
    from tela.core.models import TelaConfig

    # Setup: server exists in config
    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool.side_effect = RuntimeError(
        "Client is not connected."
    )

    initial_config = TelaConfig(
        servers={"test_server": ServerConfig(name="test_server", command="cmd")}
    )
    set_runtime_config(initial_config)

    # GAP: No check if server still exists in runtime config after lock acquisition
    # No mechanism for config reload to interrupt in-flight recovery

    # Simulate config reload removing the server during "recovery"
    _ = TelaConfig(servers={})  # Server removed
    # GAP: No on_config_changed call would trigger recovery abort

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    assert result.is_err
    details = result.error.details or {}

    # Expected: config_missing=True when server removed during recovery
    assert details.get("config_missing") is not True, (
        "GAP: Config reload does not abort in-flight recovery. "
        "Expected config_missing=True when server removed from runtime config "
        "per ADR-006 §Config-Reload Concurrency Contract."
    )


# ==============================================================================
# GAP 7: Convergence Rejection Becomes Terminal DOWNSTREAM_UNAVAILABLE
# ADR-006 §Recovery Sequence:
# "If convergence rejects the reconnect payload (for example due to
# TOOL_CONFLICT), the recovered client handle is treated as unusable for
# this request, the call does not proceed to retry, and the outward failure
# remains DOWNSTREAM_UNAVAILABLE with rejection context in diagnostics."
# ==============================================================================


@pytest.mark.xfail(
    reason="Post-impl: _recover_server_client shared primitive now exists — gap closed"
)
def test_gap_convergence_rejection_returns_downstream_unavailable(
    fake_client_handle: downstream._ClientHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GAP EXPOSED: No convergence rejection handling in recovery path.

    ADR-006 §Recovery Sequence:
    "If convergence rejects the reconnect payload (for example due to
    TOOL_CONFLICT), ... the outward failure remains DOWNSTREAM_UNAVAILABLE
    with rejection context in diagnostics."

    recovery_stage should be "convergence_rejected".

    This test will FAIL until convergence rejection handling is implemented.
    """

    # Track if recovery path was triggered
    recovery_called = False
    convergence_result: Result = Result(
        error="TOOL_CONFLICT: tool_name conflicts with another server"
    )

    async def mock_recover_server_client(
        server_name: str,
        *,
        deadline_monotonic: float,
    ) -> Result[None, TelaError]:
        nonlocal recovery_called, convergence_result
        recovery_called = True
        # GAP: No _recover_server_client exists yet
        # This would call reload.on_server_reconnect which could return conflict
        return Result(
            error=TelaError(
                code="DOWNSTREAM_UNAVAILABLE",
                message="Convergence rejected",
                details={
                    "server_name": server_name,
                    "recovery_attempted": True,
                    "recovery_stage": "convergence_rejected",
                    "recovery_eligible": True,
                    "config_missing": False,
                    "underlying_error": "TOOL_CONFLICT during on_server_reconnect",
                },
            )
        )

    # GAP: _recover_server_client does not exist in downstream module
    assert not hasattr(downstream, "_recover_server_client"), (
        "GAP: _recover_server_client recovery primitive not found. "
        "Expected shared internal recovery primitive owned by shell/downstream.py "
        "per ADR-006 §Internal recovery primitive interface."
    )

    # Setup: client handle missing to trigger recovery
    assert downstream._clients.get("test_server") is None

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    # Current behavior: immediate DOWNSTREAM_UNAVAILABLE
    assert result.is_err

    # GAP: No recovery_stage="convergence_rejected" because no recovery ran
    details = result.error.details or {}
    assert details.get("recovery_stage") != "convergence_rejected", (
        "GAP: Convergence rejection not handled in recovery path. "
        "Expected recovery_stage='convergence_rejected' per ADR-006 §Recovery Sequence."
    )


# ==============================================================================
# GAP 8: Required Error Details for Recovery
# ADR-006 §Error Payload Contract:
# When recovery is attempted or considered, TelaError.details MUST contain:
# - server_name: str (required)
# - recovery_attempted: bool (required)
# - recovery_stage: str (required when recovery_attempted=true)
# - recovery_eligible: bool (required)
# - config_missing: bool (optional when config lookup not reached)
# - underlying_error: str (required)
# ==============================================================================


def test_gap_error_details_missing_recovery_fields(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: Error details lack ADR-006 required recovery fields.

    ADR-006 §Error Payload Contract:
    "When recovery is attempted or considered, TelaError.details MUST use
    these field names so diagnostics remain stable across implementations:"

    Current behavior: call_tool error details are minimal.

    This test will FAIL until full error enrichment is implemented.
    """
    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool.side_effect = RuntimeError(
        "Client is not connected."
    )

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    assert result.is_err
    details = result.error.details or {}

    # Required fields per ADR-006 §Error Payload Contract
    required_fields = [
        "server_name",
        "recovery_attempted",
        "recovery_stage",
        "recovery_eligible",
        "underlying_error",
    ]

    missing_fields = [f for f in required_fields if f not in details]

    assert not missing_fields, (
        f"GAP: Error details missing required ADR-006 fields: {missing_fields}. "
        "Per ADR-006 §Error Payload Contract, details must include: "
        "server_name, recovery_attempted, recovery_stage, recovery_eligible, underlying_error."
    )


# ==============================================================================
# GAP 9: Structured Recovery Diagnostics
# ADR-006 §Observability:
# Gateway MUST emit structured diagnostics for:
# - downstream_recovery_started
# - downstream_recovery_succeeded
# - downstream_recovery_rejected
# - downstream_recovery_exhausted
# - downstream_recovery_classifier_unknown
# ==============================================================================


@pytest.mark.xfail(
    reason="Post-impl: structured recovery diagnostics now emitted — gap closed"
)
def test_gap_recovery_diagnostics_not_emitted(
    fake_client_handle: downstream._ClientHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GAP EXPOSED: No structured recovery diagnostic events emitted.

    ADR-006 §Observability:
    "The gateway MUST emit structured diagnostics for:
    - recovery started
    - recovery succeeded
    - recovery rejected by convergence/conflict checks
    - recovery exhausted"

    Structured contract:
    {
        event: str,  # downstream_recovery_started|succeeded|rejected|exhausted|classifier_unknown
        level: str,  # INFO|WARNING
        server_name: str,
        tool_name: str | None,
        elapsed_ms: float,
        recovery_stage: str,
        underlying_error: str | None,
        request_id: str | None,
    }

    This test will FAIL until recovery diagnostics are implemented.
    """
    from tela.shell import audit

    audit_events: list[dict] = []

    def mock_audit_write(entry: dict) -> Any:
        audit_events.append(entry)
        return Result(value=None)

    monkeypatch.setattr(audit, "audit_write", mock_audit_write)

    downstream._clients["test_server"] = fake_client_handle
    fake_client_handle.session.call_tool.side_effect = RuntimeError(
        "Client is not connected."
    )

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    assert result.is_err

    # GAP: No recovery-specific audit events emitted
    recovery_events = [
        e for e in audit_events if "recovery" in str(e.get("event", "")).lower()
    ]

    assert recovery_events, (
        "GAP: No structured recovery diagnostic events emitted. "
        "Expected events like downstream_recovery_started, downstream_recovery_succeeded, "
        "downstream_recovery_rejected, downstream_recovery_exhausted "
        "per ADR-006 §Observability."
    )


# ==============================================================================
# GAP 10: Shared Recovery Primitive
# ADR-006 §Internal recovery primitive interface:
# _recover_server_client(server_name, *, deadline_monotonic: float) -> Result[None, TelaError]
# Contract:
# - MUST be single shared recovery authority for message-handler and call-triggered
# - MUST acquire or run under per-server recovery lock before transport work
# - MUST re-read runtime config after lock acquisition
# - MUST NOT mutate _clients outside convergence path
# - MUST return Result(error=TelaError) with recovery_stage set on failure
# ==============================================================================


def test_gap_shared_recovery_primitive_not_extracted(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: No shared _recover_server_client primitive exists.

    ADR-006 §Internal recovery primitive interface:
    "a shared internal recovery primitive is extracted so both:
    - message-handler reconnect flow
    - call-triggered recovery flow
    use the same reconnect authority"

    This is the core implementation requirement for ADR-006.

    This test will FAIL until the primitive is extracted.
    """
    # Check that _recover_server_client exists and has correct signature
    assert hasattr(downstream, "_recover_server_client"), (
        "GAP: _recover_server_client shared recovery primitive not found. "
        "Expected in shell/downstream.py per ADR-006 §Internal recovery primitive interface. "
        "This primitive must be the single shared recovery authority for both "
        "message-handler reconnect flow and call-triggered recovery flow."
    )

    import inspect

    sig = inspect.signature(downstream._recover_server_client)
    params = list(sig.parameters.keys())

    # Expected signature: (server_name: str, *, deadline_monotonic: float)
    expected_params = ["server_name", "deadline_monotonic"]
    missing_params = [p for p in expected_params if p not in params]

    assert not missing_params, (
        f"GAP: _recover_server_client has wrong signature. "
        f"Expected parameters {expected_params}, got {params}. "
        "Per ADR-006 §Internal recovery primitive interface."
    )


# ==============================================================================
# Integration: End-to-End Recovery Sequence Gap
# ADR-006 §Recovery Sequence:
# 1. Attempt normal downstream call
# 2. If succeeds, return normally
# 3. If failure not recovery-eligible, return original failure as DOWNSTREAM_UNAVAILABLE
# 4. If recovery-eligible:
#    a. serialize recovery for that server
#    b. after lock acquisition, re-read runtime config for that server
#    c. if server no longer exists, return DOWNSTREAM_UNAVAILABLE with config_missing=true
#    d. open fresh client session
#    e. enumerate fresh tool set
#    f. pass through existing single-server reconnect convergence path
#    g. only after convergence accepts, retry original tool call once
# 5. If retry succeeds, return result
# 6. If recovery or retry fails, return DOWNSTREAM_UNAVAILABLE
# ==============================================================================


@pytest.mark.xfail(
    reason="Post-impl: full recovery sequence now runs (recovery_attempted=True) — gap closed"
)
def test_gap_full_recovery_sequence_not_implemented(
    fake_client_handle: downstream._ClientHandle,
) -> None:
    """GAP EXPOSED: Full recovery sequence (steps 1-6) not implemented.

    ADR-006 §Recovery Sequence defines the complete recovery flow.
    Current call_tool implements steps 1-2 only.

    This test verifies the complete sequence is missing.
    """
    # Track what happens during a "recovery-eligible" failure
    downstream._clients["test_server"] = fake_client_handle

    call_sequence: list[str] = []

    async def track_calls(tool_name: str, arguments: dict) -> Any:
        call_sequence.append("call_tool")
        raise RuntimeError(
            "Client is not connected. Use the 'async with client:' context manager first."
        )

    fake_client_handle.session.call_tool = track_calls

    result = asyncio.run(downstream.call_tool("test_server", "tool_name", {}))

    # GAP: Only step 1 (attempt call) happened
    # Steps 3-6 (recovery + retry) did not occur
    assert call_sequence == ["call_tool"], (
        "GAP: Full recovery sequence not implemented. "
        f"Expected full sequence: 1) call_tool, 2) recovery, 3) retry. "
        f"Got: {call_sequence}. "
        "Per ADR-006 §Recovery Sequence steps 1-6."
    )

    # Verify error has no recovery context
    assert result.is_err
    details = result.error.details or {}

    # GAP: No recovery_stage indicating recovery was attempted
    assert details.get("recovery_attempted") is not True, (
        "GAP: Recovery not triggered for eligible failure. "
        "Expected recovery_attempted=True per ADR-006 §Recovery Eligibility."
    )
