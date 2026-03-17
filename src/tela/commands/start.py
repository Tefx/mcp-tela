"""Contract-only start command surface.

This step defines only CLI-to-runtime binding contracts for open mode.
No runtime wiring is implemented in this step.
"""

from __future__ import annotations

from tela.core.models import GatewayTransport, RuntimeBindingContract
from tela.shell.config_loader import Result


# @invar:allow dead_export: CLI entrypoint is wired by the command framework.
def start_command(
    config_path: str = "tela.yaml",
    port: int | None = None,
    default_profile: str | None = None,
) -> Result[int, str]:
    """Start command contract for CLI entrypoint.

    Open-mode profile precedence contract:
    - `--default-profile` wins over config `default: true`.
    - Missing/ambiguous open-mode defaults are rejected.
    - Clients cannot select profile through connection metadata.

    Transport contract:
    - Default transport is stdio when `--port` is omitted.
    - SSE transport is selected only when `--port` is provided.

    Examples:
        >>> start_command("tela.yaml", None, None)
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: start_command runtime wiring pending

    Args:
        config_path: Local runtime config path.
        port: Optional SSE port.
        default_profile: Optional CLI default profile override.

    Returns:
        `Result[int, str]` once implementation exists.

    Raises:
        NotImplementedError: This step defines signatures/contracts only.
    """

    _ = RuntimeBindingContract(
        config_path=config_path,
        transport=(
            GatewayTransport.SSE if port is not None else GatewayTransport.STDIO
        ),
        port=port,
        cli_default_profile=default_profile,
    )
    raise NotImplementedError("Contract stub: start_command runtime wiring pending")
