"""Core parsing and validation contracts for tela configuration.

This module is acceptance-only in this step: signatures, type contracts,
planned @pre/@post, and doctest placeholders. No business logic is included.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

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


@pre(lambda raw, env_vars: isinstance(raw, Mapping) and isinstance(env_vars, Mapping))
@post(lambda result: isinstance(result, TelaConfig))
def parse_config(raw: Mapping[str, object], env_vars: Mapping[str, str]) -> TelaConfig:
    """Parse raw configuration into `TelaConfig`.

    Args:
        raw: Parsed YAML object graph for local runtime config.
        env_vars: Environment mapping used for `${VAR}` expansion.

    Returns:
        A `TelaConfig` model representing the contract surface for downstream
        validation and startup orchestration.

    Raises:
        ConfigContractError: Planned for parse-time shape rejections.

    Examples:
        >>> parse_config({"profiles": {}, "auth": {"mode": "token"}}, {})  # doctest: +SKIP
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: parse_config
    """

    raise NotImplementedError("Contract stub: parse_config")


@pre(
    lambda config, cli_default_profile=None: (
        isinstance(config, TelaConfig)
        and (cli_default_profile is None or isinstance(cli_default_profile, str))
    )
)
@post(lambda result: isinstance(result, list))
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

    Raises:
        ConfigContractError: Planned for fatal validation contracts.

    Examples:
        >>> cfg = TelaConfig()  # doctest: +SKIP
        >>> validate_config(cfg, cli_default_profile=None)  # doctest: +SKIP
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: validate_config
    """

    raise NotImplementedError("Contract stub: validate_config")


@pre(
    lambda profiles, cli_default_profile=None: (
        isinstance(profiles, Mapping)
        and (cli_default_profile is None or isinstance(cli_default_profile, str))
    )
)
@post(lambda result: isinstance(result, str))
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
        >>> resolve_open_mode_default_profile({}, None)  # doctest: +SKIP
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: resolve_open_mode_default_profile
    """

    raise NotImplementedError("Contract stub: resolve_open_mode_default_profile")


@pre(lambda auth_mode: isinstance(auth_mode, AuthMode))
@post(lambda result: isinstance(result, bool))
def requires_open_mode_default_resolution(auth_mode: AuthMode) -> bool:
    """Declare whether open-mode default resolution must execute.

    Args:
        auth_mode: Parsed authentication mode.

    Returns:
        `True` only when mode is `AuthMode.OPEN`.

    Examples:
        >>> requires_open_mode_default_resolution(AuthMode.OPEN)  # doctest: +SKIP
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: requires_open_mode_default_resolution
    """

    raise NotImplementedError("Contract stub: requires_open_mode_default_resolution")
