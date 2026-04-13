"""Profiles CLI command surface.

Provides the ``tela profiles`` command for listing configured profiles.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tela.shell.config_loader import load_config
from tela.shell.result import Result


# @shell_complexity: command provides dual-format output plus error reporting branches.
def profiles_command(
    config_path: str = "tela.yaml", json_output: bool = False
) -> Result[int, str]:
    """List configured profiles.

    Examples:
        >>> profiles_command(config_path="/nonexistent/path").value
        1

    Args:
        config_path: Path to configuration file.
        json_output: Whether to output JSON.

    Returns:
        Result containing process exit code.
    """
    config_result = load_config(path=Path(config_path))
    if config_result.is_err:
        print(f"error: {config_result.error}", file=sys.stderr)
        return Result(value=1)

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

    return Result(value=0)
