"""Contract-shaped tests for ServerConfig.env parsing behavior.

This module tests the env field contract defined in INTERFACES.md:

env contract:
- type is `dict[str, str]` (`VAR_NAME -> value`)
- omitted `env` defaults to `{}`
- explicit `env: {}` is equivalent to omitting `env`
- parser accepts `${VAR}` placeholders in env values and resolves them from parse-time environment input
- unresolved `${VAR}` placeholders are rejected during parse as configuration errors

Out of scope: runtime implementation behavior (spawning, subprocess, etc.)
"""

from __future__ import annotations

import pytest

from tela.core.config import ConfigContractError, parse_config
from tela.core.models import ServerConfig


# --- Default behavior tests ---


class TestServerConfigEnvDefaults:
    """Tests for env field default behavior on model and parse."""

    def test_model_envdefaults_to_empty_dict(self) -> None:
        """ServerConfig.env defaults to {} at model level."""
        s = ServerConfig(name="fs", command="cmd")
        assert s.env == {}

    def test_model_explicit_empty_env_is_allowed(self) -> None:
        """Explicit `env: {}` is valid and equivalent to omitted env."""
        s = ServerConfig(name="fs", command="cmd", env={})
        assert s.env == {}

    def test_parse_no_env_field_yields_empty_dict(self) -> None:
        """Parse-time omission of env field results in {}."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {"fs": {"command": "cmd"}},
                "auth": {"mode": "open"},
            },
            {},
        )
        assert config.servers["fs"].env == {}

    def test_parse_explicit_empty_env_yields_empty_dict(self) -> None:
        """Parse-time explicit `env: {}` matches omitted env."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {"fs": {"command": "cmd", "env": {}}},
                "auth": {"mode": "open"},
            },
            {},
        )
        assert config.servers["fs"].env == {}


# --- Explicit env mapping tests ---


class TestServerConfigEnvExplicitMapping:
    """Tests for explicit env mapping preservation."""

    def test_parse_preserves_static_env_values(self) -> None:
        """Static env values pass through unchanged."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "fs": {
                        "command": "cmd",
                        "env": {"TOKEN": "abc123", "PORT": "8080"},
                    }
                },
                "auth": {"mode": "open"},
            },
            {},
        )
        assert config.servers["fs"].env == {"TOKEN": "abc123", "PORT": "8080"}

    def test_parse_multiple_servers_with_distinct_env(self) -> None:
        """Each server gets its own env dict."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "fs": {"command": "cmd1", "env": {"A": "1"}},
                    "git": {"command": "cmd2", "env": {"B": "2"}},
                },
                "auth": {"mode": "open"},
            },
            {},
        )
        assert config.servers["fs"].env == {"A": "1"}
        assert config.servers["git"].env == {"B": "2"}


# --- ${VAR} expansion tests ---


class TestServerConfigEnvExpansion:
    """Tests for `${VAR}` expansion behavior."""

    def test_simple_brace_expansion(self) -> None:
        """`${VAR}` expands to env value."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {"srv": {"command": "cmd", "env": {"API_KEY": "${MY_KEY}"}}},
                "auth": {"mode": "open"},
            },
            {"MY_KEY": "secret123"},
        )
        assert config.servers["srv"].env == {"API_KEY": "secret123"}

    def test_simple_dollar_expansion(self) -> None:
        """`$VAR` (no braces) expands to env value."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {"srv": {"command": "cmd", "env": {"HOST": "$HOSTNAME"}}},
                "auth": {"mode": "open"},
            },
            {"HOSTNAME": "localhost"},
        )
        assert config.servers["srv"].env == {"HOST": "localhost"}

    def test_prefix_expansion(self) -> None:
        """`${VAR}` expands when embedded in larger string."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "srv": {
                        "command": "cmd",
                        "env": {"URL": "https://${HOST}:8080/api"},
                    }
                },
                "auth": {"mode": "open"},
            },
            {"HOST": "example.com"},
        )
        assert config.servers["srv"].env == {"URL": "https://example.com:8080/api"}

    def test_multiple_placeholders_in_one_value(self) -> None:
        """Multiple `${VAR}` in one value all expand."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "srv": {
                        "command": "cmd",
                        "env": {"CONN": "${HOST}:${PORT}"},
                    }
                },
                "auth": {"mode": "open"},
            },
            {"HOST": "db", "PORT": "5432"},
        )
        assert config.servers["srv"].env == {"CONN": "db:5432"}

    def test_env_values_in_args_also_expand(self) -> None:
        """Expansion works in all string fields including args."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "srv": {
                        "command": "node",
                        "args": ["--config", "${CFG_PATH}"],
                        "env": {"NODE_ENV": "production"},
                    }
                },
                "auth": {"mode": "open"},
            },
            {"CFG_PATH": "/etc/app.yaml"},
        )
        assert config.servers["srv"].args == ["--config", "/etc/app.yaml"]

    def test_multiple_keys_in_env_dict(self) -> None:
        """All env dict keys can use expansion."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "srv": {
                        "command": "cmd",
                        "env": {
                            "DB_HOST": "${DB_HOST}",
                            "DB_PORT": "${DB_PORT}",
                            "DB_PASS": "${DB_PASS}",
                        },
                    }
                },
                "auth": {"mode": "open"},
            },
            {"DB_HOST": "localhost", "DB_PORT": "5432", "DB_PASS": "secret"},
        )
        assert config.servers["srv"].env == {
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "DB_PASS": "secret",
        }


# --- Failure path tests ---


class TestServerConfigEnvExpansionFailures:
    """Tests for error behavior on unresolved / invalid vars."""

    def test_unresolved_brace_var_raises_config_error(self) -> None:
        """`${UNDEFINED}` raises ConfigContractError with CONFIG_ENV_UNSET."""
        with pytest.raises(ConfigContractError) as exc_info:
            parse_config(
                {
                    "profiles": {"dev": {"default": True}},
                    "servers": {
                        "srv": {"command": "cmd", "env": {"KEY": "${MISSING}"}}
                    },
                    "auth": {"mode": "open"},
                },
                {},
            )
        assert exc_info.value.code == "CONFIG_ENV_UNSET"
        assert "MISSING" in exc_info.value.message

    def test_unresolved_dollar_var_raises_config_error(self) -> None:
        """`$UNDEFINED` raises ConfigContractError with CONFIG_ENV_UNSET."""
        with pytest.raises(ConfigContractError) as exc_info:
            parse_config(
                {
                    "profiles": {"dev": {"default": True}},
                    "servers": {"srv": {"command": "cmd", "env": {"KEY": "$MISSING"}}},
                    "auth": {"mode": "open"},
                },
                {},
            )
        assert exc_info.value.code == "CONFIG_ENV_UNSET"
        assert "MISSING" in exc_info.value.message

    def test_partial_expansion_failure_reports_first_missing_var(self) -> None:
        """When expansion fails mid-string, error identifies the missing var."""
        with pytest.raises(ConfigContractError) as exc_info:
            parse_config(
                {
                    "profiles": {"dev": {"default": True}},
                    "servers": {
                        "srv": {
                            "command": "cmd",
                            "env": {"URL": "${KNOWN}/${UNKNOWN}"},
                        }
                    },
                    "auth": {"mode": "open"},
                },
                {"KNOWN": "valid"},
            )
        assert exc_info.value.code == "CONFIG_ENV_UNSET"
        assert "UNKNOWN" in exc_info.value.message


# --- Documented shape fixture tests ---


class TestServerConfigEnvDocumentedShape:
    """Tests using documented YAML shapes from INTERFACES.md."""

    def test_documented_server_env_example(self) -> None:
        """Exact documented server+env shape from INTERFACES.md contract."""
        # This fixture matches the documented shape:
        # servers:
        #   myserver:
        #     command: some-command
        #     env:
        #       API_KEY: ${API_KEY}
        #       PORT: "8080"
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "myserver": {
                        "command": "some-command",
                        "env": {
                            "API_KEY": "${API_KEY}",
                            "PORT": "8080",
                        },
                    }
                },
                "auth": {"mode": "open"},
            },
            {"API_KEY": "sk_live_abc123"},
        )
        assert config.servers["myserver"].name == "myserver"
        assert config.servers["myserver"].command == "some-command"
        assert config.servers["myserver"].env == {
            "API_KEY": "sk_live_abc123",
            "PORT": "8080",
        }

    def test_documented_server_env_with_all_optional_fields(self) -> None:
        """Full documented server shape with env plus all optional fields."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "fs": {
                        "command": "mcp-filesystem",
                        "args": ["--root", "${ROOT_PATH}"],
                        "env": {"LOG_LEVEL": "${LOG_LEVEL}"},
                        "family": "filesystem",
                        "default_posture": "read_only",
                    }
                },
                "auth": {"mode": "open"},
            },
            {"ROOT_PATH": "/data", "LOG_LEVEL": "info"},
        )
        assert config.servers["fs"].name == "fs"
        assert config.servers["fs"].command == "mcp-filesystem"
        assert config.servers["fs"].args == ["--root", "/data"]
        assert config.servers["fs"].env == {"LOG_LEVEL": "info"}
        assert config.servers["fs"].family == "filesystem"

    def test_documented_server_with_url_transport_and_env(self) -> None:
        """SSE server with url and env."""
        config = parse_config(
            {
                "profiles": {"dev": {"default": True}},
                "servers": {
                    "remote": {
                        "url": "http://${HOST}:8080/sse",
                        "env": {"AUTH_TOKEN": "${TOKEN}"},
                    }
                },
                "auth": {"mode": "open"},
            },
            {"HOST": "internal.example.com", "TOKEN": "tok123"},
        )
        assert config.servers["remote"].url == "http://internal.example.com:8080/sse"
        assert config.servers["remote"].env == {"AUTH_TOKEN": "tok123"}
