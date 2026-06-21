"""Connect command entrypoint for stdio-to-HTTP bridge lifecycle.

Implements ``tela connect`` service discovery, optional auto-start of
``tela serve``, and bridge lifecycle management:

1. Discover endpoint from lockfile (unless ``--server`` is given)
2. Resolve bearer token precedence
3. Delegate bridge runtime to ``connect_bridge``

This module is the **CLI facade**: it resolves endpoint, token, and autostart
decisions, then delegates bridge runtime (forwarding, recovery, readiness
polling) to ``connect_bridge``.

Interrupt semantics and host-facing message-state contracts are declared in
``tela.commands.remote_state``.
"""

# @invar:allow file_size: Connect facade coordinates discovery/autostart/wiring
# and delegates bridge runtime to connect_bridge.

from __future__ import annotations

import subprocess
import sys
import time
import os
import uuid

from tela.core.models import LockfileData
from tela.commands.connect_bridge import (
    BRIDGE_READINESS_MAX_POLLS,
    BridgeMessage,
    HTTP_TIMEOUT_SECONDS,
    HTTP_TRANSIENT_BACKOFF_SECONDS,
    HTTP_TRANSIENT_RETRIES,
    TEARDOWN_RESUME_TIMEOUT_SECONDS,
    _emit_bridge_diagnostic,
    _get_gateway_status,
    _wait_for_gateway_readiness,
    extract_jsonrpc_method,
    forward_stdio_http,
    is_mcp_transient_warming_error,
    is_recoverable_error,
    post_json,
    post_json_once,
    post_mcp_message,
    read_framed_message,
    recover_gateway,
    run_bridge,
    write_framed_message,
)
from tela.commands.serve_cmd import _resolve_bearer_token_cli_or_env
from tela.shell.result import Result
from tela.shell.lockfile import delete_lockfile_if_stale, read_lockfile
from tela.shell.startup_coordinator import (
    discover_or_autostart as _coordinator_discover_or_autostart,
)

# Backward-compatible aliases — tests reference these underscore-prefixed names
# from connect_cmd. They now delegate to connect_bridge.
_BridgeMessage = BridgeMessage
_read_framed_message = read_framed_message
_write_framed_message = write_framed_message
_extract_jsonrpc_method = extract_jsonrpc_method
_is_recoverable_error = is_recoverable_error
_emit_bridge_diagnostic = _emit_bridge_diagnostic
_get_gateway_status = _get_gateway_status
_wait_for_gateway_readiness = _wait_for_gateway_readiness
_forward_stdio_http = forward_stdio_http
_recover_gateway = recover_gateway
_post_json = post_json
_post_json_once = post_json_once
_post_mcp_message = post_mcp_message
_run_bridge = run_bridge
_is_mcp_transient_warming_error = is_mcp_transient_warming_error
BRIDGE_READINESS_MAX_POLLS = BRIDGE_READINESS_MAX_POLLS
HTTP_TIMEOUT_SECONDS = HTTP_TIMEOUT_SECONDS
HTTP_TRANSIENT_BACKOFF_SECONDS = HTTP_TRANSIENT_BACKOFF_SECONDS
HTTP_TRANSIENT_RETRIES = HTTP_TRANSIENT_RETRIES
TEARDOWN_RESUME_TIMEOUT_SECONDS = TEARDOWN_RESUME_TIMEOUT_SECONDS


LOCKFILE_WAIT_TIMEOUT_SECONDS = 5.0
LOCKFILE_WAIT_POLL_SECONDS = 0.1
LOCKFILE_START_RACE_RETRIES = 3

# Re-export constants for backward compatibility with test modules
# that import from connect_cmd.
# These are now defined in connect_bridge but re-exported here.


class ConnectEndpoint:
    """Resolved server endpoint for bridge transport."""

    __slots__ = ("host", "port", "lockfile_token")

    def __init__(self, host: str, port: int, lockfile_token: str | None) -> None:
        self.host = host
        self.port = port
        self.lockfile_token = lockfile_token

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ConnectEndpoint):
            return NotImplemented
        return (
            self.host == other.host
            and self.port == other.port
            and self.lockfile_token == other.lockfile_token
        )

    def __repr__(self) -> str:
        return (
            f"ConnectEndpoint(host={self.host!r}, "
            f"port={self.port}, lockfile_token={self.lockfile_token!r})"
        )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


# @shell_complexity: command orchestrates lockfile discovery/autostart and bridge lifecycle.
def connect_command(
    config_path: str = "tela.yaml",
    default_profile: str | None = None,
    server: str | None = None,
    token: str | None = None,
    max_recovery_attempts: int = 3,
    client_kind: str | None = None,
) -> Result[int, str]:
    """Run ``tela connect`` stdio bridge.

    Args:
        config_path: Config file path used when auto-starting ``tela serve``.
        default_profile: Optional open-mode default profile for auto-started server.
        server: Optional explicit ``host:port`` endpoint.
        token: Optional bearer token override.
        max_recovery_attempts: Maximum transient error recovery retries.
        client_kind: Optional ADR-008 client kind override.

    Returns:
        Result with exit code ``0`` on success.
    """

    if max_recovery_attempts < 0:
        return Result(error="INVALID_MAX_RECOVERY_ATTEMPTS: must be >= 0")

    client_kind_result = _resolve_client_kind(cli_client_kind=client_kind)
    if client_kind_result.is_err or client_kind_result.value is None:
        return Result(error=client_kind_result.error or "INVALID_CLIENT_KIND")
    resolved_client_kind = client_kind_result.value
    client_id = f"client_{uuid.uuid4().hex}"

    endpoint_result = _resolve_endpoint(
        config_path=config_path,
        default_profile=default_profile,
        server=server,
    )
    if endpoint_result.is_err:
        return Result(error=endpoint_result.error)
    assert endpoint_result.value is not None

    token_result = _resolve_connect_token(
        cli_token=token,
        lockfile_token=endpoint_result.value.lockfile_token,
    )
    if token_result.is_err:
        return Result(error=token_result.error)
    assert token_result.value is not None

    bridge_result = _run_bridge(
        host=endpoint_result.value.host,
        port=endpoint_result.value.port,
        bearer_token=token_result.value,
        max_recovery_attempts=max_recovery_attempts,
        client_id=client_id,
        client_kind=resolved_client_kind,
        recovery_config_path=config_path if server is None else None,
        recovery_default_profile=default_profile if server is None else None,
        discover_or_autostart=_discover_or_autostart_bridge_adapter,
    )
    if bridge_result.is_err:
        return Result(error=bridge_result.error)
    return Result(value=0)


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


def _resolve_endpoint(
    *,
    config_path: str,
    default_profile: str | None,
    server: str | None,
) -> Result[ConnectEndpoint, str]:
    """Resolve endpoint either from ``--server`` or lockfile discovery."""

    if server is not None:
        server_result = _parse_server(server)
        if server_result.is_err:
            return Result(error=server_result.error)
        assert server_result.value is not None
        host, port = server_result.value
        return Result(value=ConnectEndpoint(host=host, port=port, lockfile_token=None))

    discovery_result = _discover_or_autostart(
        config_path=config_path,
        default_profile=default_profile,
    )
    if discovery_result.is_err:
        return Result(error=discovery_result.error)
    assert discovery_result.value is not None
    lockfile = discovery_result.value
    return Result(
        value=ConnectEndpoint(
            host=lockfile.host,
            port=lockfile.port,
            lockfile_token=lockfile.token,
        )
    )


def _parse_server(raw_server: str) -> Result[tuple[str, int], str]:
    """Parse explicit ``--server`` value as ``host:port``."""

    host, sep, port_text = raw_server.rpartition(":")
    if sep == "" or host == "" or port_text == "":
        return Result(error="INVALID_SERVER: expected --server host:port")
    try:
        port = int(port_text)
    except ValueError:
        return Result(error="INVALID_SERVER: expected --server host:port")

    if port < 1 or port > 65535:
        return Result(error="INVALID_SERVER: port must be in range 1..65535")
    return Result(value=(host, port))


def _resolve_connect_token(
    *,
    cli_token: str | None,
    lockfile_token: str | None,
) -> Result[str, str]:
    """Resolve bearer token precedence for ``tela connect``.

    Precedence order:
    1. ``--token``
    2. ``TELA_BEARER_TOKEN``
    3. lockfile ``token`` field
    """

    cli_env_result = _resolve_bearer_token_cli_or_env(cli_token)
    if cli_env_result.is_ok:
        return cli_env_result

    # Command-specific fallback: try lockfile token
    if lockfile_token is not None:
        return Result(value=lockfile_token)

    return Result(
        error=(
            "MISSING_TOKEN: --server requires --token or TELA_BEARER_TOKEN "
            "because lockfile discovery is disabled"
        )
    )


def _resolve_client_kind(*, cli_client_kind: str | None) -> Result[str, str]:
    """Resolve ADR-008 client kind using CLI, environment, then unknown.

    Args:
        cli_client_kind: Optional ``--client-kind`` value.

    Returns:
        Client kind string for diagnostic attachment records.
    """

    if cli_client_kind is not None and cli_client_kind.strip() != "":
        return Result(value=cli_client_kind)
    env_client_kind = os.environ.get("TELA_CLIENT_KIND")
    if env_client_kind is not None and env_client_kind.strip() != "":
        return Result(value=env_client_kind)
    return Result(value="unknown")


# ---------------------------------------------------------------------------
# Discovery / autostart
# ---------------------------------------------------------------------------


# @shell_complexity: discovery flow delegates to startup coordinator for leader/follower arbitration.
def _autostart_serve_adapter(
    config_path: str,
    default_profile: str | None,
) -> Result[int, str]:
    """Adapter for startup coordinator: wraps _autostart_serve with positional args."""

    return _autostart_serve(
        config_path=config_path,
        default_profile=default_profile,
    )


def _discover_or_autostart_bridge_adapter(
    config_path: str,
    default_profile: str | None,
) -> Result[LockfileData, str]:
    """Adapter linking facade discovery to bridge recovery discovery."""

    return _discover_or_autostart(
        config_path=config_path,
        default_profile=default_profile,
    )


def _discover_or_autostart(
    *,
    config_path: str,
    default_profile: str | None,
) -> Result[LockfileData, str]:
    """Discover running server via lockfile or coordinate autostart leadership.

    Delegates to the startup coordinator for single-leader election and
    config-path ownership validation during concurrent connect invocations.
    """

    first_result = _coordinator_discover_or_autostart(
        config_path=config_path,
        default_profile=default_profile,
        read_lockfile=read_lockfile,
        wait_for_live_lockfile=_wait_for_live_lockfile,
        autostart_serve=_autostart_serve_adapter,
        lockfile_wait_timeout_seconds=LOCKFILE_WAIT_TIMEOUT_SECONDS,
    )
    if first_result.is_ok:
        return first_result

    # Bounded second-chance discovery for startup races where the first
    # coordinator run fails while the gateway is still converging.
    retryable_error = first_result.error or ""
    if not retryable_error.startswith("DISCOVERY_FAILED"):
        return first_result

    return _coordinator_discover_or_autostart(
        config_path=config_path,
        default_profile=default_profile,
        read_lockfile=read_lockfile,
        wait_for_live_lockfile=_wait_for_live_lockfile,
        autostart_serve=_autostart_serve_adapter,
        lockfile_wait_timeout_seconds=LOCKFILE_WAIT_TIMEOUT_SECONDS,
    )


# @shell_complexity: polling loop branches on deadline, stale state, and PID identity filter.
def _wait_for_live_lockfile(
    timeout_seconds: float,
    expected_pid: int | None = None,
) -> Result[LockfileData, str]:
    """Wait for a non-stale lockfile to become available.

    Args:
        timeout_seconds: Maximum time to wait for lockfile.
        expected_pid: If set, only accept a lockfile whose ``pid`` matches this
            value. This prevents cross-contamination from concurrent or stale
            serve processes by binding lockfile identity to the specific process
            that was spawned.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        lockfile_result = read_lockfile()
        if lockfile_result.is_ok:
            assert lockfile_result.value is not None
            if expected_pid is not None and lockfile_result.value.pid != expected_pid:
                time.sleep(LOCKFILE_WAIT_POLL_SECONDS)
                continue
            return Result(value=lockfile_result.value)

        if lockfile_result.error is not None and lockfile_result.error.startswith(
            "LOCKFILE_STALE"
        ):
            _ = delete_lockfile_if_stale()

        time.sleep(LOCKFILE_WAIT_POLL_SECONDS)

    return Result(error="LOCKFILE_WAIT_TIMEOUT: timed out waiting for gateway.lock")


def _autostart_serve(
    *,
    config_path: str,
    default_profile: str | None,
) -> Result[int, str]:
    """Auto-start ``tela serve`` as detached subprocess.

    Returns:
        Result with the spawned process PID on success, enabling callers to
        validate that the lockfile belongs to the exact process that was started.
    """

    command: list[str] = [
        sys.executable,
        "-m",
        "tela",
        "serve",
        "--config",
        config_path,
        "--idle-timeout",
        "300",
    ]
    if default_profile is not None:
        command.extend(["--default-profile", default_profile])

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return Result(error=f"AUTOSTART_FAILED: {exc}")

    return Result(value=proc.pid)
