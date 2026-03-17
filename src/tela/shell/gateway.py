"""Gateway lifecycle and startup binding contracts.

This module defines the gateway lifecycle interface (start, shutdown, status,
connections) and implements the CLI-to-gateway startup binding. Actual transport
startup (stdio/SSE server) and runtime lifecycle management are deferred to the
gateway.runtime phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tela.core.models import (
    AuthMode,
    ConnectionContext,
    GatewayStatus,
    GatewayTransport,
    RuntimeBindingContract,
)
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


# --- Gateway Lifecycle Contracts (stubs) ---


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
# @invar:allow dead_param: contract stub preserves parameter signatures.
async def gateway_start(config: GatewayStartupConfig) -> Result[None, str]:
    """Start the gateway: load config, connect downstreams, start MCP server.

    Fails fast on config errors or tool conflicts at startup.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(gateway_start(
        ...     GatewayStartupConfig(
        ...         transport=GatewayTransport.STDIO,
        ...         port=None,
        ...         auth_mode=AuthMode.OPEN,
        ...         default_profile="dev",
        ...     )
        ... ))
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: gateway_start pending

    Args:
        config: Resolved gateway startup configuration.

    Returns:
        ``Result[None, str]`` once implemented.
    """

    raise NotImplementedError("Contract stub: gateway_start pending")


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
async def gateway_shutdown() -> Result[None, str]:
    """Graceful shutdown: stop accepting connections, close downstreams.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> import asyncio
        >>> asyncio.run(gateway_shutdown())
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: gateway_shutdown pending

    Returns:
        ``Result[None, str]`` once implemented.
    """

    raise NotImplementedError("Contract stub: gateway_shutdown pending")


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
# @invar:allow shell_result: returns GatewayStatus per DESIGN.md spec, not a failable I/O boundary.
def gateway_status() -> GatewayStatus:
    """Return current gateway runtime status.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> gateway_status()
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: gateway_status pending

    Returns:
        ``GatewayStatus`` once implemented.
    """

    raise NotImplementedError("Contract stub: gateway_status pending")


# @invar:allow dead_export: gateway lifecycle is connected in gateway.runtime step.
# @invar:allow shell_result: returns list[ConnectionContext] per DESIGN.md spec, not a failable I/O boundary.
def gateway_connections() -> list[ConnectionContext]:
    """Return list of active upstream connections.

    Contract stub: raises NotImplementedError.

    Examples:
        >>> gateway_connections()
        Traceback (most recent call last):
        ...
        NotImplementedError: Contract stub: gateway_connections pending

    Returns:
        List of ``ConnectionContext`` once implemented.
    """

    raise NotImplementedError("Contract stub: gateway_connections pending")
