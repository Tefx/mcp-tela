"""Gateway startup contracts for runtime binding.

This module defines acceptance-only startup interfaces for wiring CLI start
arguments into gateway runtime configuration. Runtime startup behavior is out of
scope for this step.
"""

from __future__ import annotations

from dataclasses import dataclass

from tela.core.models import AuthMode, GatewayTransport, RuntimeBindingContract
from tela.shell.config_loader import Result


@dataclass(frozen=True)
class GatewayStartupConfig:
    """Resolved gateway startup contract consumed by runtime shell.

    Semantics:
    - stdio is the default transport.
    - SSE is optional and enabled only when an explicit port is provided.
    - open mode requires no token and must carry an explicit default profile.
    """

    transport: GatewayTransport
    port: int | None
    auth_mode: AuthMode
    default_profile: str | None


# @invar:allow dead_export: startup wiring is connected in a later runtime step.
def bind_gateway_startup(
    runtime: RuntimeBindingContract,
) -> Result[GatewayStartupConfig, str]:
    """Bind CLI runtime contract into gateway startup contract.

    This is acceptance-only for now; no runtime startup logic is implemented.

    Examples:
        >>> from tela.core.models import GatewayTransport
        >>> bind_gateway_startup(
        ...     RuntimeBindingContract(
        ...         config_path="tela.yaml",
        ...         transport=GatewayTransport.STDIO,
        ...         port=None,
        ...         cli_default_profile=None,
        ...     )
        ... )
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: bind_gateway_startup runtime wiring pending

    Args:
        runtime: CLI runtime binding contract from `tela start`.

    Returns:
        `Result[GatewayStartupConfig, str]` once runtime wiring is implemented.

    Raises:
        NotImplementedError: This step is contract-only.
    """

    _ = runtime
    raise NotImplementedError(
        "Contract stub: bind_gateway_startup runtime wiring pending"
    )
