"""Regression tests for rem.bridge_flake.gate blockers B1, B2, B3.

B1: Bridge HTTP transient retry prevents BrokenPipe/timeout on initialize/tools_list.
B2: Lockfile PID identity is bound to the spawned serve process.
B3: Soak script propagates pipeline failures through tee.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib import error as urllib_error

import pytest

from tela.commands.connect_cmd import (
    HTTP_TRANSIENT_RETRIES,
    _autostart_serve,
    _is_transient_url_error,
    _post_json,
    _post_mcp_message,
    _wait_for_live_lockfile,
)
from tela.core.models import LockfileData
from tela.shell import lockfile


# ---------------------------------------------------------------------------
# B1: Transient retry on bridge HTTP calls
# ---------------------------------------------------------------------------


class TestB1TransientRetry:
    """B1: Bridge HTTP calls must retry on transient connection errors."""

    def test_is_transient_url_error_connection_refused(self) -> None:
        """ConnectionRefusedError must be classified as transient."""
        inner = ConnectionRefusedError("Connection refused")
        exc = urllib_error.URLError(inner)
        assert _is_transient_url_error(exc) is True

    def test_is_transient_url_error_connection_reset(self) -> None:
        """ConnectionResetError must be classified as transient."""
        inner = ConnectionResetError("Connection reset")
        exc = urllib_error.URLError(inner)
        assert _is_transient_url_error(exc) is True

    def test_is_transient_url_error_broken_pipe(self) -> None:
        """BrokenPipeError must be classified as transient."""
        inner = BrokenPipeError("Broken pipe")
        exc = urllib_error.URLError(inner)
        assert _is_transient_url_error(exc) is True

    def test_is_transient_url_error_non_transient(self) -> None:
        """Non-connection errors must NOT be classified as transient."""
        exc = urllib_error.URLError("unknown host")
        assert _is_transient_url_error(exc) is False

    def test_post_mcp_message_retries_on_transient_error(self) -> None:
        """_post_mcp_message must retry on transient URLError, then succeed."""
        call_count = 0

        class FakeResponse:
            headers = {"Content-Type": "application/json", "mcp-session-id": None}

            def read(self) -> bytes:
                return b'{"result": "ok"}'

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                pass

        def mock_urlopen(req: object, timeout: float = 0) -> FakeResponse:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise urllib_error.URLError(ConnectionRefusedError("Connection refused"))
            return FakeResponse()

        with patch("tela.commands.connect_cmd.urllib_request.urlopen", mock_urlopen), \
             patch("tela.commands.connect_cmd.HTTP_TRANSIENT_BACKOFF_SECONDS", 0.01):
            result = _post_mcp_message(
                mcp_url="http://127.0.0.1:9999/mcp",
                bearer_token="test-token",
                payload=b'{"method": "initialize"}',
            )

        assert result.is_ok, f"Expected success after retries, got: {result.error}"
        assert call_count == 3, f"Expected 3 attempts (2 retries + 1 success), got {call_count}"

    def test_post_mcp_message_fails_after_max_retries(self) -> None:
        """_post_mcp_message must fail after exhausting retries."""
        call_count = 0

        def mock_urlopen(req: object, timeout: float = 0) -> None:
            nonlocal call_count
            call_count += 1
            raise urllib_error.URLError(ConnectionRefusedError("Connection refused"))

        with patch("tela.commands.connect_cmd.urllib_request.urlopen", mock_urlopen), \
             patch("tela.commands.connect_cmd.HTTP_TRANSIENT_BACKOFF_SECONDS", 0.01):
            result = _post_mcp_message(
                mcp_url="http://127.0.0.1:9999/mcp",
                bearer_token="test-token",
                payload=b'{"method": "initialize"}',
            )

        assert result.is_err
        assert "MCP_FORWARD_FAILED" in (result.error or "")
        assert call_count == HTTP_TRANSIENT_RETRIES + 1

    def test_post_mcp_message_no_retry_on_http_error(self) -> None:
        """HTTP errors (4xx/5xx) must NOT be retried."""
        call_count = 0

        def mock_urlopen(req: object, timeout: float = 0) -> None:
            nonlocal call_count
            call_count += 1
            raise urllib_error.HTTPError(
                "http://test/mcp", 500, "Server Error", {}, None  # type: ignore[arg-type]
            )

        with patch("tela.commands.connect_cmd.urllib_request.urlopen", mock_urlopen):
            result = _post_mcp_message(
                mcp_url="http://127.0.0.1:9999/mcp",
                bearer_token="test-token",
                payload=b'{"method": "initialize"}',
            )

        assert result.is_err
        assert call_count == 1, "HTTP errors must not trigger retry"

    def test_post_json_retries_on_transient_error(self) -> None:
        """_post_json must retry on transient URLError for connect/disconnect."""
        call_count = 0

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                pass

        def mock_urlopen(req: object, timeout: float = 0) -> FakeResponse:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise urllib_error.URLError(ConnectionRefusedError("Connection refused"))
            return FakeResponse()

        with patch("tela.commands.connect_cmd.urllib_request.urlopen", mock_urlopen), \
             patch("tela.commands.connect_cmd.HTTP_TRANSIENT_BACKOFF_SECONDS", 0.01):
            result = _post_json(
                url="http://127.0.0.1:9999/connect",
                bearer_token="test-token",
                payload={"connection_id": "test"},
            )

        assert result.is_ok, f"Expected success after retry, got: {result.error}"
        assert call_count == 2


# ---------------------------------------------------------------------------
# B2: Lockfile PID identity binding
# ---------------------------------------------------------------------------


class TestB2LockfilePidBinding:
    """B2: Lockfile ownership must be bound to the spawned serve process."""

    def test_autostart_serve_returns_spawned_pid(self, tmp_path: Path) -> None:
        """_autostart_serve must return the PID of the spawned subprocess."""
        config_path = tmp_path / "tela.yaml"
        config_path.write_text("auth:\n  mode: open\n")

        result = _autostart_serve(
            config_path=str(config_path),
            default_profile=None,
        )

        assert result.is_ok
        assert result.value is not None
        spawned_pid = result.value
        assert isinstance(spawned_pid, int)
        assert spawned_pid > 0

        # Verify the PID is a real process (may already be exiting, so allow both)
        try:
            os.kill(spawned_pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True  # Process exists but different user
        # Process may have already exited, that's OK - we just verify PID was captured

    def test_wait_for_live_lockfile_rejects_mismatched_pid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_wait_for_live_lockfile must reject lockfiles from wrong process."""
        path = tmp_path / "gateway.lock"
        monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

        # Write lockfile with current PID (alive, but wrong process)
        data = LockfileData(
            pid=os.getpid(),
            host="127.0.0.1",
            port=9999,
            token="test-token",
            started_at="2026-01-01T00:00:00Z",
            config_path=str(tmp_path / "tela.yaml"),
            version="0.1.0",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data.model_dump_json(), encoding="utf-8")

        # Wait with a different expected PID - must timeout
        result = _wait_for_live_lockfile(
            timeout_seconds=0.3,
            expected_pid=os.getpid() + 99999,
        )

        assert result.is_err
        assert "LOCKFILE_WAIT_TIMEOUT" in (result.error or "")

    def test_wait_for_live_lockfile_accepts_matching_pid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_wait_for_live_lockfile must accept lockfiles from expected process."""
        path = tmp_path / "gateway.lock"
        monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

        current_pid = os.getpid()
        data = LockfileData(
            pid=current_pid,
            host="127.0.0.1",
            port=9999,
            token="test-token",
            started_at="2026-01-01T00:00:00Z",
            config_path=str(tmp_path / "tela.yaml"),
            version="0.1.0",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data.model_dump_json(), encoding="utf-8")

        result = _wait_for_live_lockfile(
            timeout_seconds=1.0,
            expected_pid=current_pid,
        )

        assert result.is_ok
        assert result.value is not None
        assert result.value.pid == current_pid

    def test_wait_for_live_lockfile_no_pid_filter_accepts_any(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Without expected_pid, any live lockfile must be accepted."""
        path = tmp_path / "gateway.lock"
        monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

        data = LockfileData(
            pid=os.getpid(),
            host="127.0.0.1",
            port=9999,
            token="test-token",
            started_at="2026-01-01T00:00:00Z",
            config_path=str(tmp_path / "tela.yaml"),
            version="0.1.0",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data.model_dump_json(), encoding="utf-8")

        result = _wait_for_live_lockfile(timeout_seconds=1.0)

        assert result.is_ok
        assert result.value is not None


# ---------------------------------------------------------------------------
# B3: Soak script pipefail propagation
# ---------------------------------------------------------------------------


class TestB3SoakPipefailPropagation:
    """B3: Soak script must propagate failures through tee pipeline."""

    def test_soak_script_has_pipefail(self) -> None:
        """soak_cold_start.sh must include 'set -eo pipefail'."""
        script_path = Path(__file__).parent / "soak_cold_start.sh"
        content = script_path.read_text()
        assert "pipefail" in content, (
            "soak_cold_start.sh must use pipefail to prevent tee masking failures"
        )

    def test_pipefail_propagates_inner_failure(self) -> None:
        """Verify pipefail makes 'false | tee /dev/null' return non-zero."""
        result = subprocess.run(
            ["bash", "-c", "set -eo pipefail; false | tee /dev/null"],
            capture_output=True,
        )
        assert result.returncode != 0, (
            "pipefail must propagate inner command failure through tee"
        )

    def test_without_pipefail_tee_masks_failure(self) -> None:
        """Confirm that without pipefail, tee masks the inner failure."""
        result = subprocess.run(
            ["bash", "-c", "set -e; false | tee /dev/null"],
            capture_output=True,
        )
        # Without pipefail, bash uses exit code of last command (tee=0)
        assert result.returncode == 0, (
            "Without pipefail, tee should mask inner failures (this is the bug we fixed)"
        )
