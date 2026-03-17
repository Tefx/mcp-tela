"""Core parsing and validation for tela configuration authority.

The local config file remains runtime source of truth. Core functions transform
already-provided data and enforce deterministic validation contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from tela.core.models import AuthMode, ProfileConfig, TelaConfig

pre: Callable[[Callable[..., bool]], Callable[[Any], Any]] = lambda _predicate: (
    lambda func: func
)
post: Callable[[Callable[[Any], bool]], Callable[[Any], Any]] = lambda _predicate: (
    lambda func: func
)


@dataclass(frozen=True)
class ConfigContractError(Exception):
    """Contract-level configuration rejection.

    Attributes:
        code: Stable contract error code.
        message: Human-readable reason for rejection.
    """

    code: str
    message: str


@pre(
    lambda value, env_vars: (
        len(value) >= 0 and all(isinstance(key, str) for key in env_vars.keys())
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
                result.append(value[index : closing + 1])
            else:
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
            result.append(value[index:end])
        else:
            result.append(replacement)
        index = end

    return "".join(result)


@pre(
    lambda value, env_vars: (
        value is not Ellipsis and all(isinstance(key, str) for key in env_vars.keys())
    )
)
@post(lambda result: result is not Ellipsis)
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
        for section in ('servers', 'profiles'):
            if section in expanded and isinstance(expanded[section], dict):
                for key, value in expanded[section].items():
                    if isinstance(value, dict) and 'name' not in value:
                        value['name'] = key
        return TelaConfig.model_validate(expanded)
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
        len(config.profiles) >= 0
        and (cli_default_profile is None or len(cli_default_profile) > 0)
    )
)
@post(lambda result: all(":" in error for error in result))
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
@post(lambda result: result in (True, False))
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
