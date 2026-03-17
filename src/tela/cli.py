"""CLI entrypoint for tela.

Wires all five subcommands (start, status, profiles, connections, audit)
into the argparse-based CLI dispatcher per INTERFACES.md.
"""

from __future__ import annotations

import argparse
import sys

from tela.commands.audit_cmd import audit_command
from tela.commands.connections_cmd import connections_command
from tela.commands.profiles_cmd import profiles_command
from tela.commands.start import start_command
from tela.commands.status_cmd import status_command
from tela.shell.gateway import bind_gateway_startup


# @invar:allow dead_export: CLI entrypoint is invoked by the command framework via pyproject.toml.
# @invar:allow shell_result: CLI entrypoint returns int exit code per POSIX convention.
# @shell_orchestration: CLI entrypoint orchestrates argparse and command dispatch.
def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint for tela.

    Parses CLI arguments and dispatches to the appropriate command handler.

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

    # --- start ---
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

    # --- status ---
    status_parser = subparsers.add_parser("status", help="Show gateway status")
    status_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Output in JSON format",
    )

    # --- profiles ---
    profiles_parser = subparsers.add_parser("profiles", help="List configured profiles")
    profiles_parser.add_argument(
        "--config",
        default="tela.yaml",
        help="Path to configuration file (default: tela.yaml)",
    )
    profiles_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Output in JSON format",
    )

    # --- connections ---
    connections_parser = subparsers.add_parser(
        "connections", help="List active upstream connections"
    )
    connections_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Output in JSON format",
    )

    # --- audit ---
    audit_parser = subparsers.add_parser("audit", help="Query audit log")
    audit_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Output in JSON format",
    )
    audit_parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 timestamp or relative duration filter",
    )
    audit_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum entries to return (default: 100)",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "start":
        return _handle_start(args)
    if args.command == "status":
        return status_command(json_output=args.json_output)
    if args.command == "profiles":
        return profiles_command(
            config_path=args.config, json_output=args.json_output
        )
    if args.command == "connections":
        return connections_command(json_output=args.json_output)
    if args.command == "audit":
        return audit_command(
            since=args.since, limit=args.limit, json_output=args.json_output
        )

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
