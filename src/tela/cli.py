"""CLI entrypoint for tela.

Wires ``tela start`` with ``--config``, ``--port``, and ``--default-profile``
options into the runtime startup path. Other commands (status, profiles, audit)
are out of scope for this step.
"""

from __future__ import annotations

import argparse
import sys

from tela.commands.start import start_command
from tela.shell.gateway import bind_gateway_startup


# @invar:allow dead_export: CLI entrypoint is invoked by the command framework via pyproject.toml.
# @invar:allow shell_result: CLI entrypoint returns int exit code per POSIX convention.
# @shell_orchestration: CLI entrypoint orchestrates argparse and command dispatch.
def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint for tela.

    Parses CLI arguments and dispatches to the appropriate command handler.
    Currently only ``tela start`` is implemented.

    Examples:
        >>> main(["start", "--help"])  # doctest: +SKIP
        0

    Args:
        argv: Optional argument list for testing. Defaults to sys.argv[1:].

    Returns:
        Process exit code (0 for success, non-zero for failure).
    """

    parser = argparse.ArgumentParser(
        prog="tela",
        description="MCP capability gateway",
    )
    subparsers = parser.add_subparsers(dest="command")

    start_parser = subparsers.add_parser("start", help="Start the tela gateway")
    start_parser.add_argument(
        "--config",
        default="tela.yaml",
        help="Path to configuration file (default: tela.yaml)",
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="SSE port (omit for stdio transport)",
    )
    start_parser.add_argument(
        "--default-profile",
        default=None,
        help="Open-mode default profile override",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "start":
        return _handle_start(args)

    parser.print_help()
    return 1


# @invar:allow shell_result: CLI handler returns int exit code per POSIX convention.
def _handle_start(args: argparse.Namespace) -> int:
    """Handle ``tela start`` by resolving config and binding gateway startup.

    Wires CLI arguments through the shared config authority path into
    gateway startup configuration. The resolved default-profile is passed
    as a single fact, not re-derived by downstream consumers.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    # Step 1: Resolve config + default profile via shared authority
    runtime_result = start_command(
        config_path=args.config,
        port=args.port,
        default_profile=args.default_profile,
    )

    if runtime_result.is_err:
        print(f"error: {runtime_result.error}", file=sys.stderr)
        return 1

    assert runtime_result.value is not None

    # Step 2: Bind runtime contract into gateway startup config
    gateway_result = bind_gateway_startup(runtime_result.value)

    if gateway_result.is_err:
        print(f"error: {gateway_result.error}", file=sys.stderr)
        return 1

    assert gateway_result.value is not None

    # Gateway startup config is now resolved. Actual transport startup
    # (stdio server, SSE server) is deferred to the gateway.runtime phase.
    # For now, print confirmation and return success.
    print(
        f"tela: ready (transport={gateway_result.value.transport.value}, "
        f"profile={gateway_result.value.default_profile})"
    )
    return 0
