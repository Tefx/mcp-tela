"""Start command surface for open-mode runtime binding.

Implements CLI-to-runtime binding for default-profile resolution using the
shared config authority helper. Gateway startup orchestration and upstream
initialize handling are deferred to sibling runtime steps.
"""

from __future__ import annotations

from pathlib import Path

from tela.core.models import GatewayTransport, RuntimeBindingContract
from tela.shell.config_loader import Result, load_config


# @invar:allow dead_export: CLI entrypoint is wired by the command framework.
def start_command(
    config_path: str = "tela.yaml",
    port: int | None = None,
    default_profile: str | None = None,
) -> Result[RuntimeBindingContract, str]:
    """Start command entrypoint for CLI runtime binding.

    Open-mode profile precedence contract:
    - ``--default-profile`` wins over config ``default: true``.
    - Missing/ambiguous open-mode defaults are rejected.
    - Clients cannot select profile through connection metadata.

    Transport contract:
    - Default transport is stdio when ``--port`` is omitted.
    - SSE transport is selected only when `--port` is provided.

    This step resolves the default profile via the shared config authority
    helper (``load_config`` -> ``resolve_open_mode_default_profile``).
    Gateway startup orchestration is deferred.

    Examples:
        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> p = os.path.join(d, "tela.yaml")
        >>> with open(p, "w") as f:
        ...     _ = f.write("profiles:\\n  dev:\\n    name: dev\\n    default: true\\nauth:\\n  mode: open\\n")
        >>> r = start_command(config_path=p)
        >>> r.is_ok
        True
        >>> r.value.cli_default_profile
        'dev'

    Args:
        config_path: Local runtime config path.
        port: Optional SSE port.
        default_profile: Optional CLI default profile override.

    Returns:
        ``Result[RuntimeBindingContract, str]`` with the resolved runtime
        binding contract on success, or an error string on failure.
    """

    config_result = load_config(
        path=Path(config_path), default_profile=default_profile
    )

    if config_result.is_err:
        return Result(error=config_result.error)

    config = config_result.value
    assert config is not None  # guaranteed by is_ok

    transport = GatewayTransport.SSE if port is not None else GatewayTransport.STDIO

    # Use the resolved default profile from the shared authority helper.
    # In open mode, load_config already called resolve_open_mode_default_profile
    # and stored the result in config.resolved_default_profile.
    resolved_profile = default_profile or config.resolved_default_profile

    return Result(
        value=RuntimeBindingContract(
            config_path=config_path,
            transport=transport,
            port=port,
            cli_default_profile=resolved_profile,
        )
    )
