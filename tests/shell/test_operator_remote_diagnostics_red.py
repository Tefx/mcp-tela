"""Remote operator diagnostics — expected-red tests.

Defines the expected behavior for remote read-only operator diagnostics
surfaces equivalent to ``tela status --probe`` and ``tela status --clients``.

These tests are *expected-red* because the remote HTTP endpoints are absent
or incomplete (ADR-008 operator-recovery-exposure decision B: CLI-only recovery).

Probe requirements:
- active probe observes the current lockfile / runtime endpoint only
- active probe does not cold-start an absent runtime
- active probe does not invoke doctor recovery or mutate recovery state

Client diagnostics requirements:
- report attached clients / connections using current runtime / attachment state
- do not register, admit, disconnect, release sessions, or otherwise change admission state
- preserve ``active_connections`` count vs ``connections`` structural semantics

Property-based coverage varies runtime endpoint presence / staleness / timeouts
and pending / attached / stale client states.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings, strategies as st
from starlette.testclient import TestClient

from tela.core.classification import (
    AttachmentDisplayState,
    ClientAttachment,
    Recoverability,
    RuntimeState,
)
from tela.core.models import AuthMode, ConnectionContext, GatewayTransport, TelaConfig
from tela.shell import http_routes
from tela.shell.gateway import GatewayStartupConfig, gateway_shutdown, gateway_start
from tela.shell.gateway_runtime import (
    add_runtime_connection,
    clear_runtime_connections,
    get_runtime_config,
    get_runtime_connections_snapshot,
    is_runtime_running,
    set_runtime_config,
    set_runtime_running,
    with_upstream_server,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_http_client(token: str) -> TestClient:
    """Synchronously start gateway HTTP and return a ``TestClient``.

    The caller is responsible for shutting down the gateway after use.
    """
    app: TestClient | None = None

    async def _inner() -> TestClient:
        nonlocal app
        config = GatewayStartupConfig(
            transport=GatewayTransport.HTTP,
            port=0,
            auth_mode=AuthMode.OPEN,
            default_profile="dev",
        )
        start_result = await gateway_start(
            config,
            tela_config=TelaConfig(),
            expected_bearer_token=token,
        )
        assert start_result.is_ok, f"gateway_start failed: {start_result.error}"

        app_result = with_upstream_server(lambda s: s.streamable_http_app())
        assert app_result.is_ok, f"streamable_http_app failed: {app_result.error}"
        assert app_result.value is not None
        return TestClient(app_result.value)

    return asyncio.run(_inner())


# =============================================================================
# Red: Remote probe diagnostics surface is absent / incomplete
# =============================================================================


class TestRemoteProbeSurfaceIsAbsent:
    """Expected-red: no HTTP handler or route exposes active remote probe.

    These tests assert the existence of a remote probe endpoint that is
    functionally equivalent to ``tela status --probe``.
    """

    pytestmark = pytest.mark.xfail(
        reason="Remote operator diagnostics not yet implemented",
        strict=True,
    )

    def test_remote_probe_handler_is_defined(self) -> None:
        """`http_routes` must define a dedicated remote probe handler."""
        assert hasattr(http_routes, "handle_operator_probe"), (
            "Remote operator probe handler is absent. "
            "ADR-008 requires a read-only HTTP probe equivalent to ``tela status --probe``."
        )

    def test_remote_probe_handler_is_callable(self) -> None:
        """The remote probe handler must be callable."""
        handler = getattr(http_routes, "handle_operator_probe", None)
        assert handler is not None, "Remote probe handler must exist"
        assert callable(handler), "Remote probe handler must be callable"

    def test_remote_probe_route_responds(self) -> None:
        """A canonical remote probe route must respond with HTTP 200."""
        client = _make_http_client(token="probe-absent-token")
        try:
            # Several canonical paths are acceptable; at least one must be present.
            candidates = ["/probe", "/status/probe", "/operator/probe"]
            got_200 = False
            failures: list[str] = []
            for route in candidates:
                response = client.get(
                    route,
                    headers={"Authorization": "Bearer probe-absent-token"},
                )
                if response.status_code == 200:
                    got_200 = True
                    break
                failures.append(f"{route}: {response.status_code}")
            assert got_200, (
                "No canonical remote probe route responded 200. "
                f"Tried: {failures}."
            )
        finally:
            asyncio.run(gateway_shutdown())

    @settings(max_examples=20, deadline=None)
    @given(
        runtime_present=st.booleans(),
        runtime_stale=st.booleans(),
        timeout_seconds=st.floats(min_value=0.1, max_value=10.0).filter(lambda x: x == x),
    )
    def test_remote_probe_behavior_across_endpoint_states(
        self, runtime_present: bool, runtime_stale: bool, timeout_seconds: float
    ) -> None:
        """Property-based: remote probe must observe endpoint state without mutation.

        Generated state drives behavior expectations:
        - runtime_present=False -> probe reports absent / not-started
        - runtime_present=True, runtime_stale=False -> probe reports healthy
        - runtime_present=True, runtime_stale=True -> probe reports stale/degraded
        - timeout_seconds is the probe call timeout; must not truncate or default
        """
        # Setup runtime state from generated parameters
        clear_runtime_connections()
        if runtime_present:
            set_runtime_config(TelaConfig())
            set_runtime_running(True)
        else:
            set_runtime_running(False)

        # Snapshot before probe call — used to prove read-only / non-mutation
        before_config = get_runtime_config()
        before_running = is_runtime_running()
        before_connections = get_runtime_connections_snapshot()
        assert before_config.is_ok and before_running.is_ok and before_connections.is_ok

        # Red-test gate: handler must exist before behavioral assertions can run
        assert hasattr(http_routes, "handle_operator_probe"), (
            "Remote probe handler absent — cannot exercise endpoint-state matrix."
        )

        handler = getattr(http_routes, "handle_operator_probe")
        probe_result = handler(timeout_seconds=timeout_seconds)

        # Behavior-defining assertions driven by generated state
        if not runtime_present:
            # Absent runtime must be reported absent; probe must not cold-start
            assert probe_result.is_err or getattr(probe_result.value, "running", False) is False, (
                "Probe of absent runtime reported running=True (cold-start side-effect?)"
            )
        else:
            # Present runtime must be reported present
            assert probe_result.is_ok, f"Probe of present runtime failed: {probe_result.error}"
            assert probe_result.value is not None, "Probe ok but value is None"
            snapshot = getattr(probe_result.value, "snapshot", probe_result.value)
            assert getattr(snapshot, "running", False) is True, (
                "Probe of running runtime reported running=False"
            )
            # Staleness must be reflected in status / degraded_reason
            if runtime_stale:
                degraded = getattr(snapshot, "degraded_reason", None)
                status_state = getattr(snapshot, "state", None)
                assert degraded is not None or status_state in ("stale", "degraded"), (
                    "runtime_stale=True but probe returned no staleness indicator"
                )

        # Non-mutation guarantees: diagnostics must be read-only
        after_config = get_runtime_config()
        after_running = is_runtime_running()
        after_connections = get_runtime_connections_snapshot()
        assert before_config == after_config, "Probe must not mutate runtime config"
        assert before_running == after_running, "Probe must not mutate runtime running flag"
        assert before_connections == after_connections, "Probe must not mutate runtime connections"


# =============================================================================
# Red: Remote client diagnostics surface is absent / incomplete
# =============================================================================


class TestRemoteClientSurfaceIsAbsent:
    """Expected-red: no HTTP handler or route exposes remote client diagnostics.

    These tests assert the existence of a remote endpoint that is
    functionally equivalent to ``tela status --clients``.
    """

    pytestmark = pytest.mark.xfail(
        reason="Remote operator diagnostics not yet implemented",
        strict=True,
    )

    def test_remote_clients_handler_is_defined(self) -> None:
        """`http_routes` must define a dedicated remote client-diagnostics handler."""
        assert hasattr(http_routes, "handle_operator_clients"), (
            "Remote operator clients handler is absent. "
            "ADR-008 requires a read-only HTTP clients endpoint "
            "equivalent to ``tela status --clients``."
        )

    def test_remote_clients_handler_is_callable(self) -> None:
        handler = getattr(http_routes, "handle_operator_clients", None)
        assert handler is not None, "Remote clients handler must exist"
        assert callable(handler), "Remote clients handler must be callable"

    def test_remote_clients_route_responds(self) -> None:
        """A canonical remote clients route must respond with HTTP 200."""
        client = _make_http_client(token="clients-absent-token")
        try:
            candidates = ["/clients", "/status/clients", "/operator/clients"]
            got_200 = False
            failures: list[str] = []
            for route in candidates:
                response = client.get(
                    route,
                    headers={"Authorization": "Bearer clients-absent-token"},
                )
                if response.status_code == 200:
                    got_200 = True
                    break
                failures.append(f"{route}: {response.status_code}")
            assert got_200, (
                "No canonical remote clients route responded 200. "
                f"Tried: {failures}."
            )
        finally:
            asyncio.run(gateway_shutdown())

    @settings(max_examples=20, deadline=None)
    @given(
        endpoint_present=st.booleans(),
        endpoint_stale=st.booleans(),
        num_attachments=st.integers(0, 5),
        attachment_state=st.sampled_from(
            ["pending", "attached", "stale", "unknown"]
        ),
    )
    def test_remote_clients_behavior_across_states(
        self,
        endpoint_present: bool,
        endpoint_stale: bool,
        num_attachments: int,
        attachment_state: str,
    ) -> None:
        """Property-based: remote clients endpoint must reflect generated state without mutation.

        Generated state drives behavior expectations:
        - endpoint_present=False -> endpoint reports empty / absent attachments
        - endpoint_present=True, endpoint_stale=True -> at least one attachment must show stale indicator
        - attachment_state varies per generated record -> display_state / runtime_state must match that value when mapped
        - num_attachments controls list length -> returned count must equal generated count when present
        """
        # Setup endpoint and registry state from generated parameters
        from tela.shell.adr008_registry_events import upsert_client_attachment, read_attachment_registry

        if endpoint_present:
            set_runtime_config(TelaConfig())
            set_runtime_running(True)
        else:
            set_runtime_running(False)

        # Build attachments consistent with generated state
        registry_before = read_attachment_registry()
        assert registry_before.is_ok
        for i in range(num_attachments):
            if attachment_state == "pending":
                disp_state = AttachmentDisplayState.STARTED
            elif attachment_state == "attached":
                disp_state = AttachmentDisplayState.HEALTHY
            elif attachment_state == "stale":
                disp_state = AttachmentDisplayState.STALE_CANDIDATE
            else:  # "unknown"
                disp_state = AttachmentDisplayState.UNKNOWN
            runtime_state = RuntimeState.IDLE if (endpoint_stale and i == 0) else RuntimeState.ACTIVE
            if attachment_state == "unknown":
                runtime_state = RuntimeState.UNKNOWN
            elif attachment_state == "pending":
                runtime_state = RuntimeState.INITIALIZING
            elif attachment_state == "stale":
                runtime_state = RuntimeState.IDLE
            recov = Recoverability.STALE if endpoint_stale else Recoverability.RECOVERABLE
            att = ClientAttachment(
                client_id=f"c-{attachment_state}-{i}",
                client_kind="cli",
                display_state=disp_state,
                runtime_state=runtime_state,
                recoverability=recov,
                connected_at="2026-01-01T00:00:00Z",
                last_heartbeat="2026-01-01T00:02:00Z" if not endpoint_stale else "2026-01-01T00:00:00Z",
            )
            upsert_client_attachment(att)

        # Snapshot before endpoint call — used to prove read-only / non-mutation
        before_config = get_runtime_config()
        before_running = is_runtime_running()
        before_connections = get_runtime_connections_snapshot()
        before_registry = read_attachment_registry()
        assert before_config.is_ok and before_running.is_ok and before_connections.is_ok and before_registry.is_ok

        # Red-test gate: handler must exist before behavioral assertions can run
        assert hasattr(http_routes, "handle_operator_clients"), (
            "Remote clients handler absent — cannot exercise client-state matrix."
        )

        handler = getattr(http_routes, "handle_operator_clients")
        result = handler()

        # Behavior-defining assertions driven by generated state
        if not endpoint_present:
            # Absent endpoint must not fabricate clients
            if result.is_ok:
                clients = getattr(result.value, "clients", result.value)
                assert not clients or len(clients) == 0, (
                    "Clients endpoint reported non-empty list when endpoint_present=False"
                )
            # else: error is acceptable for absent endpoint (e.g., gateway not started)
        else:
            assert result.is_ok, f"Clients endpoint failed when endpoint_present=True: {getattr(result, 'error', 'unknown')}"
            clients = result.value
            if isinstance(clients, list):
                assert len(clients) == num_attachments, (
                    f"Expected {num_attachments} clients but got {len(clients)}"
                )
                # attachment_state "stale" or endpoint_stale must reflect in at least one client
                if endpoint_stale or attachment_state == "stale":
                    any_stale = any(
                        getattr(c, "stale_candidate", False)
                        or getattr(c, "display_state", "") in ("stale_candidate", "degraded")
                        or getattr(c, "recoverability", "") == "stale"
                        for c in clients
                    )
                    assert any_stale, (
                        "Stale state was generated but no client reflects staleness"
                    )
            # active_connections count vs connections structural semantics already guarded in TestExistingStatusIsReadOnly

        # Non-mutation guarantees: diagnostics must be read-only
        after_config = get_runtime_config()
        after_running = is_runtime_running()
        after_connections = get_runtime_connections_snapshot()
        after_registry = read_attachment_registry()
        if before_config.value is None:
            assert after_config.value is None, "Clients endpoint mutated runtime config from None"
        else:
            assert after_config.value is not None, (
                "Clients endpoint must not mutate runtime config"
            )
            assert (
                before_config.value.model_dump_json() == after_config.value.model_dump_json()
            ), "Clients endpoint must not mutate runtime config"
        assert before_running.value == after_running.value, "Clients endpoint must not mutate runtime running flag"
        assert before_connections == after_connections, "Clients endpoint must not mutate runtime connections"
        if before_registry.value is not None and after_registry.value is not None:
            assert (
                before_registry.value.model_dump_json() == after_registry.value.model_dump_json()
            ), "Clients endpoint must not mutate attachment registry"
        elif before_registry.value is None:
            assert after_registry.value is None or len(getattr(after_registry.value, "attachments", [])) == 0, (
                "Clients endpoint backfilled a missing registry without authorization"
            )


# =============================================================================
# Green baseline: existing passive GET /status is read-only (guard tests)
# =============================================================================


class TestExistingStatusIsReadOnly:
    """Guard tests proving the existing ``GET /status`` surface does not mutate.

    These establish the baseline read-only contract that remote probe / clients
    surfaces must also obey.
    """

    def test_get_status_does_not_cold_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passive status must never trigger cold-start."""
        called: list[bool] = []

        def _fake_autostart(*, config_path: str, default_profile: str | None) -> object:
            called.append(True)
            return MagicMock()

        monkeypatch.setattr(
            "tela.commands.connect_cmd._autostart_serve",
            _fake_autostart,
        )

        client = _make_http_client(token="status-readonly-token")
        try:
            client.get(
                "/status",
                headers={"Authorization": "Bearer status-readonly-token"},
            )
            assert not called, "GET /status must not cold-start an absent runtime"
        finally:
            asyncio.run(gateway_shutdown())

    def test_get_status_does_not_invoke_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passive status must never invoke doctor recovery."""
        called: list[bool] = []

        def _fake_recover(*args: object, **kwargs: object) -> object:
            called.append(True)
            return MagicMock()

        monkeypatch.setattr(
            "tela.commands.doctor_cmd._recover_doctor_runtime",
            _fake_recover,
        )

        client = _make_http_client(token="status-readonly-token")
        try:
            client.get(
                "/status",
                headers={"Authorization": "Bearer status-readonly-token"},
            )
            assert not called, "GET /status must not invoke recovery"
        finally:
            asyncio.run(gateway_shutdown())

    def test_get_status_does_not_register_connections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passive status must never register a new bridge connection."""
        called: list[str] = []

        original_add = add_runtime_connection

        def _tracked_add(ctx: ConnectionContext) -> object:
            called.append(ctx.connection_id)
            return original_add(ctx)

        monkeypatch.setattr(
            "tela.shell.http_routes.register_bridge_connection",
            lambda _name: (_ for _ in ()).throw(AssertionError("registration during status")),
        )
        monkeypatch.setattr(
            "tela.shell.gateway_runtime.add_runtime_connection",
            _tracked_add,
        )

        client = _make_http_client(token="status-readonly-token")
        try:
            client.get(
                "/status",
                headers={"Authorization": "Bearer status-readonly-token"},
            )
            # No new connections should be added by status reads except any test-only ones
            snapshot = get_runtime_connections_snapshot()
            assert snapshot.is_ok
            assert snapshot.value is not None
            assert all(cid.startswith("_") for cid in called), (
                "Status read added a non-test connection: " + str(called)
            )
        finally:
            asyncio.run(gateway_shutdown())

    def test_get_status_does_not_disconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passive status must never disconnect or clean up connections."""
        called: list[str] = []

        def _fake_cleanup(connection_id: str) -> object:
            called.append(connection_id)
            return MagicMock()

        monkeypatch.setattr(
            "tela.shell.http_routes.cleanup_connection_by_id",
            _fake_cleanup,
        )

        client = _make_http_client(token="status-readonly-token")
        try:
            client.get(
                "/status",
                headers={"Authorization": "Bearer status-readonly-token"},
            )
            assert not called, "GET /status must not disconnect any connections"
        finally:
            asyncio.run(gateway_shutdown())

    def test_get_status_preserves_active_connections_semantics(self) -> None:
        """GET /status must return ``active_connections`` as int and ``connections`` as struct.

        This enforces the count-vs-collection semantic split required by the
        INTERFACES.md contract §7.2.1.
        """
        client = _make_http_client(token="status-semantics-token")
        try:
            response = client.get(
                "/status",
                headers={"Authorization": "Bearer status-semantics-token"},
            )
            if response.status_code == 200:
                payload = response.json()
                assert isinstance(payload.get("active_connections"), int), (
                    "active_connections must be an int count"
                )
                assert isinstance(payload.get("connections"), list), (
                    "connections must be a structural list"
                )
                assert len(payload["connections"]) == payload.get(
                    "active_connections", -1
                ), (
                    "active_connections must equal len(connections) in steady state"
                )
        finally:
            asyncio.run(gateway_shutdown())


# =============================================================================
# Green baseline: existing attachment registry reads do not mutate
# =============================================================================


class TestExistingRegistryReadsAreReadOnly:
    """Guard tests proving ADR-008 registry reads are read-only.

    These establish the baseline for remote client diagnostics.
    """

    def test_read_attachment_registry_does_not_write(self) -> None:
        """Registry read must not persist any changes."""
        from tela.shell.adr008_registry_events import read_attachment_registry

        before = read_attachment_registry()
        after = read_attachment_registry()
        assert before.is_ok
        assert after.is_ok
        # Identical read results (no mutation between reads)
        # value may be None if file missing, but that is still consistent
        if before.value is not None and after.value is not None:
            assert (
                before.value.model_dump_json() == after.value.model_dump_json()
            )

    def test_upsert_is_not_triggered_by_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reading the attachment registry must never call upsert/write."""
        called: list[bool] = []

        def _fake_upsert(*args: object, **kwargs: object) -> object:
            called.append(True)
            return MagicMock()

        monkeypatch.setattr(
            "tela.shell.adr008_registry_events.upsert_client_attachment",
            _fake_upsert,
        )
        monkeypatch.setattr(
            "tela.shell.adr008_registry_events.write_attachment_registry",
            lambda _reg: (_ for _ in ()).throw(AssertionError("write during read")),
        )

        from tela.shell.adr008_registry_events import read_attachment_registry

        read_attachment_registry()
        assert not called, "Registry read must not trigger upsert"


# =============================================================================
# Red: CLI-equivalent behavior must be mirrored remotely (structural contracts)
# =============================================================================


class TestRemoteDiagnosticsMirrorCliContract:
    """Remote diagnostics must expose the same read-only contract as CLI surfaces."""

    pytestmark = pytest.mark.xfail(
        reason="Remote operator diagnostics not yet implemented",
        strict=True,
    )

    def test_probe_cli_flag_has_no_http_analogue(self) -> None:
        """`tela status --probe` must have a read-only HTTP analogue.

        This test fails because no HTTP query parameter or sub-route currently
        provides the active-probe semantic.
        """
        assert (
            hasattr(http_routes, "handle_operator_probe")
            or hasattr(http_routes, "handle_status")
            and "probe" in http_routes.handle_status.__code__.co_varnames
        ), (
            "HTTP status handler does not accept a probe parameter, "
            "and no dedicated probe handler exists."
        )

    def test_clients_cli_flag_has_no_http_analogue(self) -> None:
        """`tela status --clients` must have a read-only HTTP analogue.

        This test fails because no HTTP endpoint currently lists ADR-008
        client attachments.
        """
        assert (
            hasattr(http_routes, "handle_operator_clients")
            or hasattr(http_routes, "handle_status")
            and "clients" in http_routes.handle_status.__code__.co_varnames
        ), (
            "HTTP status handler does not accept a clients parameter, "
            "and no dedicated clients handler exists."
        )