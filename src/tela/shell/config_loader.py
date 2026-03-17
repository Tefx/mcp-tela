"""Shell loader for local runtime configuration.

Shell handles file and environment I/O, then delegates parse/validation rules
to Core to produce a runtime-ready `TelaConfig` authority surface.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from tela.core.config import (
    ConfigContractError,
    parse_config,
    requires_open_mode_default_resolution,
    resolve_open_mode_default_profile,
    validate_config,
)
from tela.core.models import TelaConfig

# Re-export for backward compatibility
from tela.shell.result import Result  # noqa: F401


# @invar:allow dead_export: CLI/runtime wiring is implemented in later step.
# @invar:allow shell_complexity: loader orchestrates parse/validate/authority handoff.
def load_config(
    path: Path | None = None, default_profile: str | None = None
) -> Result[TelaConfig, str]:
    """Load local config from disk and environment, then delegate to Core.

    The local config file remains runtime source of truth.

    Args:
        path: Optional file path override. Defaults to local `tela.yaml`.
        default_profile: Optional CLI override for open-mode default profile.

    Returns:
        `Result[TelaConfig, str]` once implemented.

    Raises:
        None. All failures are represented as `Result(..., error=...)`.
    """

    config_path = path if path is not None else Path("tela.yaml")
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Result(
            error=(
                f"CONFIG_FILE_MISSING: configuration file not found at {config_path}"
            )
        )
    except OSError as exc:
        return Result(error=f"CONFIG_FILE_READ_ERROR: {exc}")

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        return Result(error=f"CONFIG_PARSE_ERROR: invalid YAML: {exc}")

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return Result(
            error="CONFIG_PARSE_ERROR: top-level YAML document must be a mapping"
        )

    try:
        config = parse_config(loaded, os.environ)
    except ConfigContractError as exc:
        return Result(error=f"{exc.code}: {exc.message}")

    errors = validate_config(config, cli_default_profile=default_profile)
    if len(errors) > 0:
        return Result(error="; ".join(errors))

    resolved_default_profile = config.resolved_default_profile
    if requires_open_mode_default_resolution(config.auth.mode):
        try:
            resolved_default_profile = resolve_open_mode_default_profile(
                config.profiles,
                cli_default_profile=default_profile,
            )
        except ConfigContractError as exc:
            return Result(error=f"{exc.code}: {exc.message}")

    return Result(
        value=config.model_copy(
            update={"resolved_default_profile": resolved_default_profile}
        )
    )
