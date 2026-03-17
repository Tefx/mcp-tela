"""Gateway startup binding for runtime configuration.

This module implements the binding from CLI runtime contract into gateway
startup configuration. Actual transport startup (stdio/SSE server) is
deferred to the gateway.runtime phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tela.core.models import AuthMode, GatewayTransport, RuntimeBindingContract
from tela.shell.config_loader import Result, load_config


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
    """Bind CLI runtime contract into gateway startup configuration.

    Resolves the gateway startup config from the CLI runtime binding contract.
    The resolved default-profile is passed through as-is from the shared
    config authority path -- this function does not re-derive profile selection.

    Examples:
        >>> import tempfile, os
        >>> from tela.core.models import GatewayTransport, RuntimeBindingContract
        >>> d = tempfile.mkdtemp()
        >>> p = os.path.join(d, "tela.yaml")
        >>> with open(p, "w") as f:
        ...     _ = f.write("profiles:\\n  dev:\\n    name: dev\\n    default: true\\nauth:\\n  mode: open\\n")
        >>> r = bind_gateway_startup(
        ...     RuntimeBindingContract(
        ...         config_path=p,
        ...         transport=GatewayTransport.STDIO,
        ...         port=None,
        ...         cli_default_profile="dev",
        ...     )
        ... )
        >>> r.is_ok
        True
        >>> r.value.transport
        <GatewayTransport.STDIO: 'stdio'>
        >>> r.value.default_profile
        'dev'

    Args:
        runtime: CLI runtime binding contract from ``tela start``.

    Returns:
        ``Result[GatewayStartupConfig, str]`` with the resolved gateway
        startup config on success, or an error string on failure.
    """

    # Determine auth mode by loading the config to inspect auth settings.
    # The config is already validated by start_command; we only need the
    # auth mode to decide open vs token gateway startup.
    config_result = load_config(
        path=Path(runtime.config_path),
        default_profile=runtime.cli_default_profile,
    )

    if config_result.is_err:
        return Result(error=config_result.error)

    assert config_result.value is not None
    auth_mode = config_result.value.auth.mode

    return Result(
        value=GatewayStartupConfig(
            transport=runtime.transport,
            port=runtime.port,
            auth_mode=AuthMode(auth_mode),
            default_profile=runtime.cli_default_profile,
        )
    )
