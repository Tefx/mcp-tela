"""Contract-only start command surface.

No runtime wiring is implemented in this step.
"""

from __future__ import annotations


def start_command(
    config_path: str = "tela.yaml",
    port: int | None = None,
    default_profile: str | None = None,
) -> int:
    """Start command contract for CLI entrypoint.

    Open-mode profile precedence contract:
    - `--default-profile` wins over config `default: true`.
    - Missing/ambiguous open-mode defaults are rejected.

    Args:
        config_path: Local runtime config path.
        port: Optional SSE port.
        default_profile: Optional CLI default profile override.

    Returns:
        Process exit code once implementation exists.

    Raises:
        NotImplementedError: This step defines signatures/contracts only.
    """

    _ = (config_path, port, default_profile)
    raise NotImplementedError("Contract stub: start_command")
