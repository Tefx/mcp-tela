"""Verification tests for status truth boundaries and consumer agreement.

This module verifies the contract-mandated separation between:

1. **Discovery Truth** (LOCKFILE_DISCOVERY_CONTRACT):
   - Source: ~/.tela/gateway.lock
   - Authoritative for: pid, host, port, token, config_path, started_at, version
   - NOT authoritative for: lifecycle_readiness, downstream_convergence
   - Consumer rule: Use lockfile data only to discover process identity,
     bind target, auth bootstrap. Do NOT infer readiness or downstream sync.

2. **Readiness Truth** (STATUS_SNAPSHOT_CONTRACT):
   - Source: RuntimeStatusSnapshot / GET /status (via get_lifecycle_status_facts)
   - Authoritative for: running, start_time, connections, total_tool_calls, config,
     connected_servers, state, degraded_reason
   - NOT authoritative for: discovery
   - Consumer rule: Use runtime status snapshots to answer lifecycle/readiness questions.

This test module proves that:
- One shared state fixture exercises all three surfaces
- Lockfile provides discovery truth only
- gateway_status() and handle_status() derive identical readiness facts
- The two truths remain distinct and are never collapsed
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from tela.core.models import (
    ConnectionContext,
    GatewayStatus,
    LockfileData,
    ProfileConfig,
    ResolvedTool,
    ServerConfig,
    StatusResponse,
    TelaConfig,
)
from tela.shell.config_loader import Result
from tela.shell.gateway import gateway_status
from tela.shell.gateway_runtime import (
    LOCKFILE_DISCOVERY_CONTRACT,
    STATUS_SNAPSHOT_CONTRACT,
    add_runtime_connection,
    clear_runtime_connections,
    set_runtime_config,
    set_runtime_running,
    set_runtime_total_tool_calls,
)
from tela.shell.http_routes import handle_status
from tela.shell import lockfile


# =============================================================================
# Section 1: Truth Contract Verification
# =============================================================================


def test_lockfile_discovery_contract_fields_match_lockfile_data() -> None:
    """LOCKFILE_DISCOVERY_CONTRACT authoritative_fields match LockfileData fields."""
    lockfile_fields = {
        "pid",
        "host",
        "port",
        "token",
        "config_path",
        "started_at",
        "version",
    }
    assert set(LOCKFILE_DISCOVERY_CONTRACT.authoritative_fields) == lockfile_fields


def test_status_snapshot_contract_fields_match_runtime_snapshot() -> None:
    """STATUS_SNAPSHOT_CONTRACT authoritative_fields match RuntimeStatusSnapshot fields."""
    # RuntimeStatusSnapshot has: config, running, start_time, total_tool_calls, connections
    # Also includes lifecycle_facts from gateway_status: connected_servers, state, degraded_reason
    snapshot_fields = {
        "running",
        "start_time",
        "connections",
        "total_tool_calls",
        "config",
    }
    assert set(STATUS_SNAPSHOT_CONTRACT.authoritative_fields) == snapshot_fields


def test_discovery_contract_excludes_readiness() -> None:
    """LOCKFILE_DISCOVERY_CONTRACT explicitly disclaims readiness authority."""
    assert "lifecycle_readiness" in LOCKFILE_DISCOVERY_CONTRACT.not_authoritative_for
    assert "downstream_convergence" in LOCKFILE_DISCOVERY_CONTRACT.not_authoritative_for


def test_readiness_contract_excludes_discovery() -> None:
    """STATUS_SNAPSHOT_CONTRACT explicitly disclaims discovery authority."""
    assert "discovery" in STATUS_SNAPSHOT_CONTRACT.not_authoritative_for


# =============================================================================
# Section 2: Lockfile Discovery Truth - Does NOT Provide Readiness
# =============================================================================


def test_lockfile_lacks_readiness_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile discovery truth does NOT provide readiness facts.

    This test proves the two truths are DISTINCT:
    - Lockfile provides discovery (process identity, endpoint)
    - Lockfile does NOT provide readiness (connected_servers, running state)
    """
    # Create a valid lockfile
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", lockfile_path)

    lockfile_data = LockfileData(
        pid=os.getpid(),  # Use live PID so staleness check passes
        host="127.0.0.1",
        port=49152,
        token="test-token-for-discovery",
        started_at="2026-03-29T10:00:00Z",
        config_path="/path/to/tela.yaml",
        version="0.1.0",
    )
    lockfile_path.parent.mkdir(parents=True, exist_ok=True)
    lockfile_path.write_text(lockfile_data.model_dump_json(), encoding="utf-8")

    # Read lockfile - this is discovery truth
    result = lockfile.read_lockfile()
    assert result.is_ok
    data = result.value

    # Discovery truth is present
    assert data.pid == os.getpid()
    assert data.host == "127.0.0.1"
    assert data.port == 49152
    assert data.token == "test-token-for-discovery"
    assert data.config_path == "/path/to/tela.yaml"

    # Readiness truth is ABSENT - these fields do not exist on LockfileData
    assert not hasattr(data, "running")
    assert not hasattr(data, "connected_servers")
    assert not hasattr(data, "state")
    assert not hasattr(data, "degraded_reason")

    # This proves the contract: lockfile provides discovery truth ONLY
    # Readiness must come from GET /status or gateway_status()


def test_lockfile_cannot_answer_readiness_questions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reading a valid lockfile cannot answer readiness questions.

    Per DESIGN.md:
    - lockfile is discovery truth only
    - downstream readiness requires runtime status snapshot via GET /status
    - Consumers MUST NOT infer readiness from discovery artifacts
    """
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", lockfile_path)

    # Lockfile says "process exists, endpoint available"
    lockfile_data = LockfileData(
        pid=os.getpid(),  # Live PID
        host="127.0.0.1",
        port=49152,
        token="test-token",
        started_at="2026-03-29T10:00:00Z",
        config_path="/path/to/tela.yaml",
        version="0.1.0",
    )
    lockfile_path.parent.mkdir(parents=True, exist_ok=True)
    lockfile_path.write_text(lockfile_data.model_dump_json(), encoding="utf-8")

    # Discovery succeeds - we can find the process
    result = lockfile.read_lockfile()
    assert result.is_ok, "Discovery should succeed"

    # But the CRITICAL POINT: we CANNOT answer these readiness questions:
    # - Are downstream servers connected?
    # - Is the gateway in "ready" state?
    # - What is the current state (warming/ready/degraded)?
    # - How many tool calls have been made?

    # These questions require get_lifecycle_status_facts() or GET /status
    # The lockfile CANNOT provide this data because these fields don't exist
    data = result.value
    readiness_fields = [
        "running",
        "connected_servers",
        "state",
        "degraded_reason",
        "total_tool_calls",
    ]
    for field in readiness_fields:
        assert not hasattr(data, field), (
            f"LockfileData must not have readiness field '{field}'"
        )


# =============================================================================
# Section 3: Readiness Truth - gateway_status() and handle_status() Agreement
# =============================================================================


@pytest.fixture
def shared_readiness_state(monkeypatch: pytest.MonkeyPatch) -> str:
    """Fixture that configures identical runtime state for both status surfaces.

    This is the SHARED STATE FIXTURE proving one source drives both consumers.
    """

    config = TelaConfig(
        servers={
            "alpha": ServerConfig(name="alpha", command="cmd-alpha"),
            "beta": ServerConfig(name="beta", command="cmd-beta"),
            "gamma": ServerConfig(name="gamma", command="cmd-gamma"),
        },
        profiles={
            "dev": ProfileConfig(name="dev"),
            "prod": ProfileConfig(name="prod"),
        },
    )
    set_runtime_config(config)
    set_runtime_running(True)
    clear_runtime_connections()
    set_runtime_total_tool_calls(42)
    add_runtime_connection(
        ConnectionContext(
            connection_id="conn-1",
            profile_name="dev",
            connected_at="2026-03-29T12:00:00Z",
        )
    )
    add_runtime_connection(
        ConnectionContext(
            connection_id="conn-2",
            profile_name="prod",
            connected_at="2026-03-29T12:01:00Z",
        )
    )

    # Mock get_all_tools for downstream convergence state
    def _mock_get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]:
        return Result(
            value={
                "alpha": [
                    ResolvedTool(
                        name="alpha_tool",
                        server_name="alpha",
                        family="alpha",
                        schema_={},
                    )
                ],
                "beta": [
                    ResolvedTool(
                        name="beta_tool", server_name="beta", family="beta", schema_={}
                    )
                ],
                # gamma is NOT connected - partial convergence
            }
        )

    import tela.shell.gateway_lifecycle as gateway_lifecycle

    monkeypatch.setattr(gateway_lifecycle, "get_all_tools", _mock_get_all_tools)

    try:
        yield "shared_readiness_state"
    finally:
        clear_runtime_connections()
        set_runtime_running(False)
        set_runtime_config(None)


def test_both_status_surfaces_report_identical_readiness_facts(
    shared_readiness_state: str,
) -> None:
    """gateway_status() and handle_status() MUST report identical readiness facts.

    This proves both consumers delegate to the SAME authority:
    get_lifecycle_status_facts().

    The shared fixture configures ONE runtime state, and both surfaces must
    derive facts from that single source.
    """

    # Exercise both surfaces through the shared state fixture
    gateway_result = asyncio.run(gateway_status())
    http_result = handle_status("valid-token", "valid-token")

    assert shared_readiness_state == "shared_readiness_state"
    assert gateway_result.is_ok, f"gateway_status() failed: {gateway_result.error}"
    assert http_result.is_ok, f"handle_status() failed: {http_result.error}"

    gateway_status_obj: GatewayStatus = gateway_result.value
    http_status_obj: StatusResponse = http_result.value

    # ========================================
    # READINESS FACT AGREEMENT
    # Both surfaces MUST report identical facts
    # ========================================

    # Configured servers count
    assert gateway_status_obj.server_count == http_status_obj.server_count == 3

    # Connected servers list (from downstream convergence)
    assert gateway_status_obj.connected_servers == http_status_obj.connected_servers
    assert set(http_status_obj.connected_servers) == {"alpha", "beta"}

    # Active connections count
    assert (
        gateway_status_obj.active_connections == http_status_obj.active_connections == 2
    )

    # Profile count
    assert gateway_status_obj.profile_count == http_status_obj.profile_count == 2

    # Tool call count
    assert gateway_status_obj.total_tool_calls == http_status_obj.total_tool_calls == 42

    # State (partial convergence = degraded)
    assert gateway_status_obj.state == http_status_obj.state == "degraded"

    # Degraded reason
    assert (
        gateway_status_obj.degraded_reason
        == http_status_obj.degraded_reason
        == "downstream_not_fully_converged"
    )


def test_status_surfaces_derive_from_lifecycle_authority(
    shared_readiness_state: str,
) -> None:
    """Both status surfaces derive from get_lifecycle_status_facts(), NOT independently.

    This verifies that the authority surface is the single source of truth
    for readiness facts, and neither surface re-derives facts independently.
    """

    # Import the authority
    from tela.shell.gateway_lifecycle import get_lifecycle_status_facts

    # Get facts from the authority directly
    authority_result = get_lifecycle_status_facts()
    assert authority_result.is_ok
    facts = authority_result.value

    # Get status from both consumers
    gateway_result = asyncio.run(gateway_status())
    http_result = handle_status("valid-token", "valid-token")

    assert gateway_result.is_ok
    assert http_result.is_ok

    gateway_status_obj = gateway_result.value
    http_status_obj = http_result.value

    # ========================================
    # AUTHORITATIVE SOURCE AGREEMENT
    # Both consumers derive from the SAME authority
    # ========================================

    # Server count from authority
    assert gateway_status_obj.server_count == facts.server_count
    assert http_status_obj.server_count == facts.server_count

    # Connected servers from authority
    assert set(gateway_status_obj.connected_servers) == set(facts.connected_servers)
    assert set(http_status_obj.connected_servers) == set(facts.connected_servers)

    # Active connections from authority
    assert gateway_status_obj.active_connections == facts.active_connections
    assert http_status_obj.active_connections == facts.active_connections

    # Profile count from authority
    assert gateway_status_obj.profile_count == facts.profile_count
    assert http_status_obj.profile_count == facts.profile_count

    # Tool calls from authority
    assert gateway_status_obj.total_tool_calls == facts.total_tool_calls
    assert http_status_obj.total_tool_calls == facts.total_tool_calls

    # State from authority
    assert gateway_status_obj.state == facts.state
    assert http_status_obj.state == facts.state

    # Degraded reason from authority
    assert gateway_status_obj.degraded_reason == facts.degraded_reason
    assert http_status_obj.degraded_reason == facts.degraded_reason


# =============================================================================
# Section 4: Truths Remain Distinct - No Collapse
# =============================================================================


def test_truths_distinct_lockfile_vs_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Discovery truth and readiness truth are DISTINCT and NEVER collapsed.

    This test proves:
    - Lockfile provides discovery fields ONLY
    - Status provides readiness fields ONLY
    - No single surface mixes the two truths
    """

    # Setup: lockfile for discovery
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", lockfile_path)

    lockfile_data = LockfileData(
        pid=os.getpid(),
        host="127.0.0.1",
        port=49152,
        token="token-for-discovery",
        started_at="2026-03-29T10:00:00Z",
        config_path="/path/to/tela.yaml",
        version="0.1.0",
    )
    lockfile_path.parent.mkdir(parents=True, exist_ok=True)
    lockfile_path.write_text(lockfile_data.model_dump_json(), encoding="utf-8")

    # Setup: runtime for readiness
    config = TelaConfig(
        servers={"srv": ServerConfig(name="srv", command="cmd")},
        profiles={"dev": ProfileConfig(name="dev")},
    )
    set_runtime_config(config)
    set_runtime_running(True)
    clear_runtime_connections()

    def _mock_get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]:
        return Result(
            value={
                "srv": [
                    ResolvedTool(
                        name="tool", server_name="srv", family="srv", schema_={}
                    )
                ]
            }
        )

    import tela.shell.gateway_lifecycle as gateway_lifecycle

    monkeypatch.setattr(gateway_lifecycle, "get_all_tools", _mock_get_all_tools)

    try:
        # DISCOVERY TRUTH
        lockfile_result = lockfile.read_lockfile()
        assert lockfile_result.is_ok
        discovery = lockfile_result.value

        # READINESS TRUTH
        http_result = handle_status("valid-token", "valid-token")
        assert http_result.is_ok
        readiness = http_result.value

        # ========================================
        # TRUTH SEPARATION VERIFICATION
        # ========================================

        # Discovery truth has process identity fields
        discovery_fields_present = {
            "pid",
            "host",
            "port",
            "token",
            "config_path",
            "version",
        }
        for field in discovery_fields_present:
            assert hasattr(discovery, field), (
                f"LockfileData must have discovery field '{field}'"
            )

        # Readiness truth has lifecycle/convergence fields
        readiness_fields_present = {
            "server_count",
            "connected_servers",
            "active_connections",
            "state",
            "degraded_reason",
            "total_tool_calls",
        }
        for field in readiness_fields_present:
            assert hasattr(readiness, field), (
                f"StatusResponse must have readiness field '{field}'"
            )

        # Discovery truth does NOT have readiness fields
        for field in readiness_fields_present:
            assert not hasattr(discovery, field), (
                f"LockfileData must NOT have readiness field '{field}' (truth collapse detected)"
            )

        # Readiness truth does NOT have discovery fields
        discovery_only_fields = {"pid", "host", "port", "started_at"}
        for field in discovery_only_fields:
            assert not hasattr(readiness, field), (
                f"StatusResponse must NOT have discovery-only field '{field}' (truth collapse detected)"
            )

        # This proves the two truths are separate concerns:
        # - Lockfile: "Where is the process? What endpoint? What auth?"
        # - Status: "Is it ready? What's connected? What state?"

    finally:
        clear_runtime_connections()
        set_runtime_running(False)
        set_runtime_config(None)


def test_status_schema_preserved_authority_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /status schema is unchanged; internal delegation to authority is transparent."""

    config = TelaConfig(
        servers={"srv": ServerConfig(name="srv", command="cmd")},
        profiles={"dev": ProfileConfig(name="dev")},
    )
    set_runtime_config(config)
    set_runtime_running(True)
    clear_runtime_connections()

    def _mock_get_all_tools() -> Result[dict[str, list[ResolvedTool]], str]:
        return Result(
            value={
                "srv": [
                    ResolvedTool(
                        name="tool", server_name="srv", family="srv", schema_={}
                    )
                ]
            }
        )

    import tela.shell.gateway_lifecycle as gateway_lifecycle

    monkeypatch.setattr(gateway_lifecycle, "get_all_tools", _mock_get_all_tools)

    try:
        result = handle_status("valid-token", "valid-token")
        assert result.is_ok
        status = result.value

        # Schema must include all GatewayStatus fields
        expected_status_fields = {
            "uptime_seconds",
            "server_count",
            "connected_servers",
            "active_connections",
            "profile_count",
            "total_tool_calls",
            "state",
            "discovery_source",
            "config_path",
            "requested_config_path",
            "config_mismatch",
            "degraded_reason",
            "connections",
            "audit_entries",
        }
        assert set(status.model_dump().keys()) == expected_status_fields

        # Facts from authority are correct
        assert status.server_count == 1
        assert status.connected_servers == ["srv"]
        assert status.state == "ready"
        assert status.degraded_reason is None  # All servers connected = ready

    finally:
        clear_runtime_connections()
        set_runtime_running(False)
        set_runtime_config(None)


# =============================================================================
# Summary Table Evidence
# =============================================================================


def test_status_truth_verification_summary(
    shared_readiness_state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Summary verification proving the truth separation table.

    ## Status Truth Verification
    | Surface          | Discovery Truth | Readiness Truth | Matches Authority |
    | ---------------- | --------------- | --------------- | ----------------- |
    | lockfile         | YES (pid,port...) | n/a (not present) | YES (discovery) |
    | gateway_status() | n/a (not source) | YES (derived)   | YES (via get_lifecycle_status_facts) |
    | GET /status      | n/a (not source) | YES (derived)   | YES (via get_lifecycle_status_facts) |
    """

    assert shared_readiness_state == "shared_readiness_state"

    # Lockfile provides discovery truth ONLY
    lockfile_path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", lockfile_path)
    lockfile_data = LockfileData(
        pid=os.getpid(),
        host="127.0.0.1",
        port=49152,
        token="discovery-token",
        started_at="2026-03-29T10:00:00Z",
        config_path="/path/to/tela.yaml",
        version="0.1.0",
    )
    lockfile_path.parent.mkdir(parents=True, exist_ok=True)
    lockfile_path.write_text(lockfile_data.model_dump_json(), encoding="utf-8")

    lockfile_result = lockfile.read_lockfile()
    assert lockfile_result.is_ok

    # Both status surfaces derive readiness truth from authority
    gateway_result = asyncio.run(gateway_status())
    http_result = handle_status("valid-token", "valid-token")

    assert gateway_result.is_ok
    assert http_result.is_ok

    # DISCOVERY TRUTH: lockfile has these, status surfaces don't (config_path is optional in status)
    assert lockfile_result.value.pid == os.getpid()
    assert lockfile_result.value.host == "127.0.0.1"
    assert lockfile_result.value.port == 49152
    assert lockfile_result.value.token == "discovery-token"
    # Status surfaces don't have pid/host/port fields
    assert not hasattr(gateway_result.value, "pid")
    assert not hasattr(gateway_result.value, "host")
    assert not hasattr(gateway_result.value, "port")

    # READINESS TRUTH: status surfaces have these, lockfile doesn't
    assert gateway_result.value.connected_servers == http_result.value.connected_servers
    assert gateway_result.value.state == http_result.value.state == "degraded"
    assert gateway_result.value.degraded_reason == http_result.value.degraded_reason
    # Lockfile doesn't have readiness fields
    assert not hasattr(lockfile_result.value, "connected_servers")
    assert not hasattr(lockfile_result.value, "state")
    assert not hasattr(lockfile_result.value, "degraded_reason")

    # MATCHES AUTHORITY: both status surfaces agree with get_lifecycle_status_facts
    from tela.shell.gateway_lifecycle import get_lifecycle_status_facts

    auth_result = get_lifecycle_status_facts()
    assert auth_result.is_ok
    facts = auth_result.value

    assert gateway_result.value.server_count == facts.server_count
    assert http_result.value.server_count == facts.server_count
    assert set(gateway_result.value.connected_servers) == set(facts.connected_servers)
    assert set(http_result.value.connected_servers) == set(facts.connected_servers)
    assert gateway_result.value.state == facts.state
    assert http_result.value.state == facts.state
