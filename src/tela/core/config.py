"""Core parsing and validation for tela configuration authority.

The local config file remains runtime source of truth. Core functions transform
already-provided data and enforce deterministic validation contracts.
"""

from __future__ import annotations

from typing import Mapping

from tela.core.contracts import pre, post
from pydantic import ValidationError

from tela.core.models import AuthMode, ProfileConfig, TelaConfig
from tela.core.catalog import merge_with_builtins


# Re-export for backward compatibility
from tela.core.errors import ConfigContractError, NESTED_TELA_PREFIX_REQUIRED  # noqa: F401


@pre(
    lambda value, env_vars: (
        isinstance(value, str) and all(isinstance(key, str) for key in env_vars.keys())
    )
)
@post(lambda result: "$\x00" not in result)
def _expand_env_token(value: str, env_vars: Mapping[str, str]) -> str:
    """Expand `$VAR` and `${VAR}` tokens using supplied environment map.

    Unknown variables are left unchanged for deterministic parse behavior.
    """

    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "$":
            result.append(char)
            index += 1
            continue

        if index + 1 < len(value) and value[index + 1] == "{":
            closing = value.find("}", index + 2)
            if closing == -1:
                result.append(char)
                index += 1
                continue
            var_name = value[index + 2 : closing]
            replacement = env_vars.get(var_name)
            if replacement is None:
                raise ConfigContractError(
                    code="CONFIG_ENV_UNSET",
                    message=f"Environment variable '{var_name}' is not set. Unset variables cause a startup error.",
                )
            result.append(replacement)
            index = closing + 1
            continue

        end = index + 1
        while end < len(value) and (value[end].isalnum() or value[end] == "_"):
            end += 1
        if end == index + 1:
            result.append(char)
            index += 1
            continue
        var_name = value[index + 1 : end]
        replacement = env_vars.get(var_name)
        if replacement is None:
            raise ConfigContractError(
                code="CONFIG_ENV_UNSET",
                message=f"Environment variable '{var_name}' is not set. Unset variables cause a startup error.",
            )
        result.append(replacement)
        index = end

    return "".join(result)


# Public alias per DESIGN.md: expand_env_vars(value: str, env_vars: dict[str, str]) -> str
expand_env_vars = _expand_env_token


@pre(
    lambda value, env_vars: (
        isinstance(value, (str, int, float, bool, list, dict, type(None)))
        and all(isinstance(key, str) for key in env_vars.keys())
    )
)
@post(
    lambda result: isinstance(result, (str, int, float, bool, list, dict, type(None)))
)
def _expand_env_in_object(value: object, env_vars: Mapping[str, str]) -> object:
    """Recursively expand env tokens in string leaves."""

    if isinstance(value, str):
        return _expand_env_token(value, env_vars)
    if isinstance(value, list):
        return [_expand_env_in_object(item, env_vars) for item in value]
    if isinstance(value, dict):
        return {
            key: _expand_env_in_object(item, env_vars) for key, item in value.items()
        }
    return value


@pre(
    lambda raw, env_vars: (
        all(isinstance(key, str) for key in raw.keys())
        and all(isinstance(key, str) for key in env_vars.keys())
    )
)
@post(lambda result: result.auth.mode in (AuthMode.OPEN, AuthMode.TOKEN))
def parse_config(raw: Mapping[str, object], env_vars: Mapping[str, str]) -> TelaConfig:
    """Parse raw configuration into `TelaConfig`.

    Server env contract:
    - `servers.<name>.env` accepts `dict[str, str]`.
    - Omitted `env` defaults to `{}`.
    - `${VAR}` placeholders in env values are expanded using `env_vars`.
    - Unresolved placeholders raise `ConfigContractError(code="CONFIG_ENV_UNSET", ...)`.

    Args:
        raw: Parsed YAML object graph for local runtime config.
        env_vars: Environment mapping used for `${VAR}` expansion.

    Returns:
        A `TelaConfig` model representing the contract surface for downstream
        validation and startup orchestration.

    Raises:
        ConfigContractError: When raw data cannot be parsed into contract model.

    Examples:
        >>> cfg = parse_config({"profiles": {}, "auth": {"mode": "token", "secrets": ["$K"]}}, {"K": "abc"})
        >>> cfg.auth.secrets
        ['abc']
    """

    try:
        expanded = _expand_env_in_object(dict(raw), env_vars)
        if not isinstance(expanded, dict):
            raise ConfigContractError(
                code="CONFIG_PARSE_ERROR",
                message="Top-level configuration must be a mapping.",
            )
        # Inject name from dict keys for servers and profiles sections.
        # INTERFACES.md specifies that the YAML key IS the server/profile name.
        for section in ("servers", "profiles"):
            if section in expanded and isinstance(expanded[section], dict):
                for key, value in expanded[section].items():
                    if isinstance(value, dict):
                        if "name" not in value:
                            value["name"] = key
        config = TelaConfig.model_validate(expanded)
        # Wire in builtin profiles when user provides none
        if not config.profiles:
            config = config.model_copy(
                update={"profiles": merge_with_builtins(config.profiles)}
            )
        return config
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(item) for item in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigContractError(
            code="CONFIG_PARSE_ERROR",
            message=f"Configuration parse failed: {details}",
        ) from exc


@pre(
    lambda config, cli_default_profile=None: (
        isinstance(config, TelaConfig)
        and (cli_default_profile is None or len(cli_default_profile) > 0)
    )
)
@post(
    lambda result: (
        isinstance(result, list)
        and all(isinstance(e, str) and len(e) > 0 for e in result)
    )
)
def validate_config(
    config: TelaConfig, cli_default_profile: str | None = None
) -> list[str]:
    """Validate cross-field config constraints.

    Contract includes open-mode default-profile rules:
    - CLI `--default-profile` takes precedence over config `default: true`.
    - Open mode rejects missing default profile selection.
    - Open mode rejects ambiguous `default: true` declarations.

    Args:
        config: Parsed config model.
        cli_default_profile: Optional CLI override from `--default-profile`.

    Returns:
        List of validation error codes/messages. Empty list means valid.

    Examples:
        >>> cfg = TelaConfig(profiles={"dev": ProfileConfig(name="dev", default=True)}, auth={"mode": "open"})
        >>> validate_config(cfg)
        []
    """

    errors: list[str] = []

    if requires_open_mode_default_resolution(config.auth.mode):
        try:
            resolve_open_mode_default_profile(
                config.profiles,
                cli_default_profile=cli_default_profile,
            )
        except ConfigContractError as exc:
            errors.append(f"{exc.code}: {exc.message}")

    if config.auth.mode == AuthMode.TOKEN and len(config.auth.secrets) == 0:
        errors.append(
            "TOKEN_MODE_SECRETS_MISSING: token mode requires at least one secret."
        )

    for name, server in config.servers.items():
        has_command = server.command is not None
        has_url = server.url is not None
        if not has_command and not has_url:
            errors.append(
                f"SERVER_MISSING_TRANSPORT: server '{name}' must define either 'command' or 'url'."
            )
        elif has_command and has_url:
            errors.append(
                f"SERVER_AMBIGUOUS_TRANSPORT: server '{name}' must define either 'command' or 'url', not both."
            )
        elif has_command and server.headers:
            errors.append(
                f"SERVER_HEADERS_WITH_STDIO: server '{name}' uses 'command' transport, but 'headers' are configured. Headers are only supported for 'url' HTTP/SSE transports."
            )

        if server.tool_prefix is not None:
            if server.tool_prefix.startswith("tela_") or server.tool_prefix.startswith("tela."):
                errors.append(
                    f"SERVER_RESERVED_PREFIX: server '{name}' tool_prefix '{server.tool_prefix}' "
                    "uses reserved 'tela_' or 'tela.' namespace."
                )
            if "." in server.tool_prefix:
                errors.append(
                    f"SERVER_INVALID_PREFIX: server '{name}' tool_prefix '{server.tool_prefix}' "
                    "contains invalid dotted syntax; use snake_case."
                )

        if server.nested_gateway and not server.tool_prefix:
            errors.append(
                f"{NESTED_TELA_PREFIX_REQUIRED}: server '{name}' explicitly set "
                "nested_gateway but omitted tool_prefix."
            )

    return errors


@pre(
    lambda profiles, cli_default_profile=None: (
        all(isinstance(name, str) for name in profiles.keys())
        and (cli_default_profile is None or len(cli_default_profile) > 0)
    )
)
@post(lambda result: len(result) > 0)
def resolve_open_mode_default_profile(
    profiles: Mapping[str, ProfileConfig],
    cli_default_profile: str | None = None,
) -> str:
    """Resolve profile binding for open mode.

    Precedence contract:
    1. CLI `--default-profile` if provided.
    2. Else exactly one profile with `default=True`.

    Rejection contract:
    - raise `ConfigContractError(code="PROFILE_NOT_FOUND", ...)` when CLI
      profile name is not present in `profiles`.
    - raise `ConfigContractError(code="OPEN_MODE_DEFAULT_PROFILE_MISSING", ...)`
      when neither source yields a default profile.
    - raise `ConfigContractError(code="OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS", ...)`
      when multiple profiles are marked `default=True`.

    Args:
        profiles: Name-to-profile map from local config.
        cli_default_profile: Optional CLI override.

    Returns:
        Resolved profile name for open-mode connection binding.

    Raises:
        ConfigContractError: For missing, unknown, or ambiguous defaults.

    Examples:
        >>> resolve_open_mode_default_profile({"dev": ProfileConfig(name="dev", default=True)})
        'dev'
    """

    if cli_default_profile is not None:
        if cli_default_profile not in profiles:
            raise ConfigContractError(
                code="PROFILE_NOT_FOUND",
                message=(
                    f"CLI default profile '{cli_default_profile}' was not found "
                    "in local configuration profiles."
                ),
            )
        return cli_default_profile

    defaults = [name for name, profile in profiles.items() if profile.default]
    if len(defaults) == 1:
        return defaults[0]
    if len(defaults) == 0:
        raise ConfigContractError(
            code="OPEN_MODE_DEFAULT_PROFILE_MISSING",
            message=(
                "Open mode requires either CLI --default-profile or exactly one "
                "profile with default=true."
            ),
        )

    conflicts = ", ".join(sorted(defaults))
    raise ConfigContractError(
        code="OPEN_MODE_DEFAULT_PROFILE_AMBIGUOUS",
        message=(f"Open mode has multiple default profiles marked true: {conflicts}."),
    )


@pre(lambda auth_mode: auth_mode in (AuthMode.OPEN, AuthMode.TOKEN))
@post(lambda result: isinstance(result, bool))
def requires_open_mode_default_resolution(auth_mode: AuthMode) -> bool:
    """Declare whether open-mode default resolution must execute.

    Args:
        auth_mode: Parsed authentication mode.

    Returns:
        `True` only when mode is `AuthMode.OPEN`.

    Examples:
        >>> requires_open_mode_default_resolution(AuthMode.OPEN)
        True
        >>> requires_open_mode_default_resolution(AuthMode.TOKEN)
        False
    """

    return auth_mode == AuthMode.OPEN
