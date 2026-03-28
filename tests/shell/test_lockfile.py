"""Tests for lockfile read/write/delete contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tela.core.models import LockfileData
from tela.shell import lockfile
from tela.shell.gateway_runtime import LOCKFILE_DISCOVERY_CONTRACT


def _sample_lockfile_data(config_path: str = "/tmp/tela.yaml") -> LockfileData:
    return LockfileData(
        pid=os.getpid(),
        host="127.0.0.1",
        port=39123,
        token="token-example",
        started_at="2026-01-01T00:00:00Z",
        config_path=config_path,
        version="0.1.0",
    )


def test_write_lockfile_persists_json_with_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    data = _sample_lockfile_data(config_path=str(tmp_path / "tela.yaml"))
    result = lockfile.write_lockfile(data)

    assert result.is_ok
    assert path.exists()
    assert path.parent.stat().st_mode & 0o777 == lockfile.LOCKFILE_DIRECTORY_MODE
    assert path.stat().st_mode & 0o777 == lockfile.LOCKFILE_FILE_MODE

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == data.model_dump()

    # temp files should not leak after successful write
    assert not any(
        item.is_file() and str(item).endswith(lockfile.LOCKFILE_TMP_SUFFIX)
        for item in path.parent.iterdir()
    )


def test_read_lockfile_returns_stored_data_when_pid_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    data = _sample_lockfile_data()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data.model_dump_json(), encoding="utf-8")

    result = lockfile.read_lockfile()
    assert result.is_ok
    assert result.value == data


def test_read_lockfile_detects_stale_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    stale_pid_data = LockfileData(
        pid=2**31,
        host="127.0.0.1",
        port=39124,
        token="token-stale",
        started_at="2026-01-01T00:00:00Z",
        config_path=str(tmp_path / "tela.yaml"),
        version="0.1.0",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stale_pid_data.model_dump_json(), encoding="utf-8")

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None


def test_read_lockfile_rejects_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None


def test_delete_lockfile_removes_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    result = lockfile.delete_lockfile()

    assert result.is_ok
    assert not path.exists()


def test_delete_lockfile_succeeds_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)

    result = lockfile.delete_lockfile()

    assert result.is_ok
    assert not path.exists()


# -- Schema Validation Regression (INTERFACES.md §7.3 Lockfile Contract) ---


def test_read_lockfile_rejects_malformed_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile with invalid JSON must be rejected with LOCKFILE_PARSE_ERROR.

    Ref: INTERFACES.md §7.3 - lockfile must be valid JSON matching LockfileData.
    Minimal fixture: any malformed content (e.g., truncated JSON).
    """
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Malformed JSON - truncated object
    path.write_text('{"pid": 12345, "host": "127.0.0.1"', encoding="utf-8")

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None
    assert "LOCKFILE_PARSE_ERROR" in result.error


def test_read_lockfile_rejects_missing_required_field_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile missing required 'pid' field must be rejected with LOCKFILE_PARSE_ERROR.

    Ref: INTERFACES.md §7.3 - All fields (pid, host, port, token, started_at, config_path, version) required.
    Minimal fixture: valid JSON with all fields except 'pid'.
    """
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Missing 'pid' field
    path.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 49152,
                "token": "bearer-token-here",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/path/to/tela.yaml",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None
    assert "LOCKFILE_PARSE_ERROR" in result.error


def test_read_lockfile_rejects_missing_required_field_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile missing required 'token' field must be rejected with LOCKFILE_PARSE_ERROR.

    Ref: INTERFACES.md §7.3 - All fields (pid, host, port, token, started_at, config_path, version) required.
    Minimal fixture: valid JSON with all fields except 'token'.
    """
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Missing 'token' field
    path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "host": "127.0.0.1",
                "port": 49152,
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/path/to/tela.yaml",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None
    assert "LOCKFILE_PARSE_ERROR" in result.error


def test_read_lockfile_rejects_wrong_type_for_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile with wrong type for 'pid' field must be rejected with LOCKFILE_PARSE_ERROR.

    Ref: INTERFACES.md §7.3 - 'pid' must be integer (process id).
    Minimal fixture: valid JSON with 'pid' as string instead of int.
    """
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 'pid' as string instead of int
    path.write_text(
        json.dumps(
            {
                "pid": "not-a-number",
                "host": "127.0.0.1",
                "port": 49152,
                "token": "bearer-token-here",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/path/to/tela.yaml",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None
    assert "LOCKFILE_PARSE_ERROR" in result.error


def test_read_lockfile_rejects_wrong_type_for_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile with non-coercible 'port' field must be rejected with LOCKFILE_PARSE_ERROR.

    Ref: INTERFACES.md §7.3 - 'port' must be integer.
    Minimal fixture: valid JSON with 'port' as non-numeric string (coercion failure).
    Note: Pydantic coerces numeric strings like "49152" to int, so use non-numeric.
    """
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 'port' as non-numeric string (triggers pydantic int parsing error)
    path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "host": "127.0.0.1",
                "port": "not-a-port",
                "token": "bearer-token-here",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/path/to/tela.yaml",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None
    assert "LOCKFILE_PARSE_ERROR" in result.error


def test_read_lockfile_accepts_extra_fields_not_in_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile with extra fields must parse successfully; extra fields are ignored.

    Ref: INTERFACES.md §7.3 - LockfileData has 7 required fields. Extra fields
    are accepted (Pydantic's default extra="ignore" behavior) and do not cause
    parse errors. Only the 7 required fields are guaranteed present in the model.
    """
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Valid schema plus extra field (pydantic accepts this by default)
    path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "host": "127.0.0.1",
                "port": 49152,
                "token": "bearer-token-here",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/path/to/tela.yaml",
                "version": "0.1.0",
                "extra_field": "should-be-ignored",
            }
        ),
        encoding="utf-8",
    )

    # Patch is_stale to return False so the live process check doesn't interfere
    monkeypatch.setattr(lockfile, "is_stale", lambda _data: False)

    result = lockfile.read_lockfile()
    # Should succeed - pydantic ignores extra fields by default
    assert result.is_ok
    assert result.value is not None
    # Extra field should not appear in model
    assert not hasattr(result.value, "extra_field")


def test_read_lockfile_rejects_null_field_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile with null required field must be rejected with LOCKFILE_PARSE_ERROR.

    Ref: INTERFACES.md §7.3 - All fields are required (non-nullable).
    Minimal fixture: valid JSON with one field set to null.
    """
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 'host' as null instead of string
    path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "host": None,
                "port": 49152,
                "token": "bearer-token-here",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/path/to/tela.yaml",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    result = lockfile.read_lockfile()
    assert result.is_err
    assert result.error is not None
    assert "LOCKFILE_PARSE_ERROR" in result.error


def test_read_lockfile_validates_minimal_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lockfile with minimal valid schema per INTERFACES.md §7.3 must parse successfully.

    Ref: INTERFACES.md §7.3 - Minimal fixture has all 7 required fields with valid values.
    This test uses the minimal documented shape without convenience-only fields.
    """
    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal valid lockfile per spec - exactly the documented shape
    path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "host": "127.0.0.1",
                "port": 49152,
                "token": "tela_tok_a1b2c3d4e5f6g7h8",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": "/home/user/.tela/tela.yaml",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    # Patch is_stale to return False so the live process check doesn't interfere
    monkeypatch.setattr(lockfile, "is_stale", lambda _data: False)

    result = lockfile.read_lockfile()
    assert result.is_ok
    assert result.value is not None
    assert result.value.pid == 12345
    assert result.value.host == "127.0.0.1"
    assert result.value.port == 49152
    assert result.value.token == "tela_tok_a1b2c3d4e5f6g7h8"
    assert result.value.started_at == "2026-03-22T10:00:00Z"
    assert result.value.config_path == "/home/user/.tela/tela.yaml"
    assert result.value.version == "0.1.0"


# --- Discovery vs Readiness split tests ---
# Ref: docs/INTERFACES.md §7.3 Lockfile Contract
# These tests prove that lockfile discovery does NOT imply downstream readiness.


def test_lockfile_discovery_does_not_prove_downstream_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Lockfile readable does NOT mean downstreams are ready.

    Ref: docs/INTERFACES.md §7.3 - Lockfile proves discovery only.
    This test validates the contract: a valid lockfile may exist while
    the gateway is still starting, warming, or in degraded state.
    """
    from tela.shell import lockfile

    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write a valid lockfile for a "starting" gateway
    starting_payload = {
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": 49152,
        "token": "starting-token",
        "started_at": "2026-03-22T10:00:00Z",
        "config_path": str(tmp_path / "tela.yaml"),
        "version": "0.1.0",
    }
    path.write_text(json.dumps(starting_payload), encoding="utf-8")

    # Lockfile IS readable (discovery succeeds)
    result = lockfile.read_lockfile()
    assert result.is_ok, "Lockfile must be readable for discovery"

    # But lockfile does NOT contain:
    # - connected_servers list
    # - running flag
    # - active_connections count
    lockfile_data = result.value
    assert not hasattr(lockfile_data, "connected_servers")
    assert not hasattr(lockfile_data, "running")
    assert not hasattr(lockfile_data, "active_connections")

    # Contract: discovery is not authoritative for readiness
    assert "lifecycle_readiness" in LOCKFILE_DISCOVERY_CONTRACT.not_authoritative_for
    assert "downstream_convergence" in LOCKFILE_DISCOVERY_CONTRACT.not_authoritative_for


def test_lockfile_config_path_ownership_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Lockfile config_path establishes ownership for query commands.

    Ref: docs/INTERFACES.md §7.3 - config_path field is used by query commands
    to verify they are querying the correct gateway instance.
    """
    from tela.shell import lockfile

    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)

    owner_config = str(tmp_path / "owned.yaml")
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": "127.0.0.1",
                "port": 49153,
                "token": "ownership-token",
                "started_at": "2026-03-22T10:00:00Z",
                "config_path": owner_config,
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    result = lockfile.read_lockfile()
    assert result.is_ok
    assert result.value.config_path == owner_config


# --- Minimal spec fixture tests ---
# Ref: docs/INTERFACES.md §7.3 - minimal lockfile fixture with exactly 7 required fields


def test_lockfile_minimal_fixture_is_valid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Minimal lockfile with exactly 7 required fields validates successfully.

    Ref: docs/INTERFACES.md §7.3 Lockfile Contract
    The minimal documented shape contains only the 7 required fields.
    """
    from tela.shell import lockfile

    path = tmp_path / "gateway.lock"
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Exactly the 7 required fields per INTERFACES.md §7.3
    minimal_payload = {
        "pid": 12345,
        "host": "127.0.0.1",
        "port": 49152,
        "token": "tela_tok_a1b2c3d4e5f6g7h8",
        "started_at": "2026-03-22T10:00:00Z",
        "config_path": "/home/user/.tela/tela.yaml",
        "version": "0.1.0",
    }
    path.write_text(json.dumps(minimal_payload), encoding="utf-8")

    # Patch is_stale to return False so the live process check doesn't interfere
    monkeypatch.setattr(lockfile, "is_stale", lambda _data: False)

    result = lockfile.read_lockfile()
    assert result.is_ok
    assert result.value.pid == 12345
    assert result.value.host == "127.0.0.1"
    assert result.value.port == 49152
    assert result.value.token == "tela_tok_a1b2c3d4e5f6g7h8"
    assert result.value.started_at == "2026-03-22T10:00:00Z"
    assert result.value.config_path == "/home/user/.tela/tela.yaml"
    assert result.value.version == "0.1.0"
