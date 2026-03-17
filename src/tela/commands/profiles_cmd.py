"""Profiles CLI command surface.

Provides the ``tela profiles`` command for listing configured profiles.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tela.shell.config_loader import load_config


# @invar:allow dead_export: CLI entrypoint is wired by the command framework.
# @invar:allow shell_result: CLI handler returns int exit code per POSIX convention.
# @invar:allow shell_complexity: command supports both JSON and human-readable listing flows.
# @shell_complexity: command provides dual-format output plus error reporting branches.
def profiles_command(config_path: str = "tela.yaml", json_output: bool = False) -> int:
    """List configured profiles.

    Examples:
        >>> profiles_command(config_path="/nonexistent/path")
        1

    Args:
        config_path: Path to configuration file.
        json_output: Whether to output JSON.

    Returns:
        Process exit code.
    """
    config_result = load_config(path=Path(config_path))
    if config_result.is_err:
        print(f"error: {config_result.error}", file=sys.stderr)
        return 1

    assert config_result.value is not None
    profiles = config_result.value.profiles

    if json_output:
        out = {name: p.model_dump() for name, p in profiles.items()}
        print(json.dumps(out, indent=2))
    else:
        if not profiles:
            print("No profiles configured.")
        else:
            for name, profile in profiles.items():
                default_marker = " (default)" if profile.default else ""
                print(f"  {name}{default_marker}")

    return 0
