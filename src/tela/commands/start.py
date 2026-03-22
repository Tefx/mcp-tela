"""Start command surface for open-mode runtime binding.

.. deprecated::
    The ``tela start`` CLI command has been replaced by ``tela connect`` and
    ``tela serve`` per INTERFACES.md §2.

    This module is retained for internal testing of ``bind_gateway_startup``
    integration but should not be used in production code paths.

    Production code should use:
    - ``tela serve`` for HTTP gateway (via ``serve_cmd.serve_command``)
    - ``tela connect`` for stdio bridge (via ``connect_cmd.connect_command``)
"""

from __future__ import annotations

from pathlib import Path

from tela.core.models import GatewayTransport, RuntimeBindingContract
from tela.shell.config_loader import Result, load_config


def start_command(
    config_path: str = "tela.yaml",
    port: int | None = None,
    default_profile: str | None = None,
    transport: str | None = None,
) -> Result[RuntimeBindingContract, str]:
    """Start command entrypoint for CLI runtime binding.

    Open-mode profile precedence contract:
    - ``--default-profile`` wins over config ``default: true``.
    - Missing/ambiguous open-mode defaults are rejected.
    - Clients cannot select profile through connection metadata.

    Transport contract:
    - Default transport is stdio when ``--port`` is omitted.
    - HTTP (Streamable HTTP) is the default when ``--port`` is provided.
    - SSE is selected only when ``--transport sse`` is explicitly given.

    This step resolves the default profile via the shared config authority
    helper (``load_config`` -> ``resolve_open_mode_default_profile``).

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
        port: Optional remote transport port.
        default_profile: Optional CLI default profile override.
        transport: Explicit transport override (``stdio``, ``sse``, ``http``).

    Returns:
        ``Result[RuntimeBindingContract, str]`` with the resolved runtime
        binding contract on success, or an error string on failure.
    """

    config_result = load_config(path=Path(config_path), default_profile=default_profile)

    if config_result.is_err:
        return Result(error=config_result.error)

    config = config_result.value
    assert config is not None  # guaranteed by is_ok

    if transport is not None:
        resolved_transport = GatewayTransport(transport)
    elif port is not None:
        resolved_transport = GatewayTransport.HTTP
    else:
        resolved_transport = GatewayTransport.STDIO

    # Use the resolved default profile from the shared authority helper.
    # In open mode, load_config already called resolve_open_mode_default_profile
    # and stored the result in config.resolved_default_profile.
    resolved_profile = default_profile or config.resolved_default_profile

    return Result(
        value=RuntimeBindingContract(
            config_path=config_path,
            transport=resolved_transport,
            port=port,
            cli_default_profile=resolved_profile,
        )
    )
