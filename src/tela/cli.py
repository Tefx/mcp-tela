"""CLI entrypoint for tela.

Wires subcommands (connect, serve, status, profiles, connections, audit)
into the argparse-based CLI dispatcher per INTERFACES.md.
"""

from __future__ import annotations

import argparse
import sys

from tela.commands.audit_cmd import audit_command
from tela.commands.connect_cmd import connect_command
from tela.commands.connections_cmd import connections_command
from tela.commands.doctor_cmd import doctor_command
from tela.commands.profiles_cmd import profiles_command
from tela.commands.serve_cmd import serve_command
from tela.commands.status_cmd import status_command
from tela.commands.stop_cmd import stop_command


# @invar:allow shell_result: CLI entrypoint returns int exit code per POSIX convention.
# @shell_orchestration: CLI entrypoint orchestrates argparse and command dispatch.
# @shell_complexity: CLI dispatch intentionally branches across command entrypoints.
def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint for tela.

    Parses CLI arguments and dispatches to the appropriate command handler.

    Examples:
        >>> # The main function returns int exit code per POSIX convention.
        >>> # Note: argparse --help triggers SystemExit(0), argv=[] prints help and returns 1.
        >>> # Comprehensive CLI testing is in tests/ via pytest.
        >>> callable(main)
        True
        >>> main.__name__
        'main'

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

    # --- serve ---
    serve_parser = subparsers.add_parser("serve", help="Start the tela HTTP gateway")
    serve_parser.add_argument(
        "--config",
        default="tela.yaml",
        help="Path to configuration file (default: tela.yaml)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="HTTP bind port (default: 0 for ephemeral)",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--default-profile",
        default=None,
        help="Open-mode default profile override",
    )
    serve_parser.add_argument(
        "--idle-timeout",
        type=int,
        default=300,
        help="Idle timeout seconds before shutdown (default: 300, 0 to disable)",
    )
    serve_parser.add_argument(
        "--reaper-sweep-interval",
        type=float,
        default=None,
        help="Override reaper sweep interval seconds (CLI wins over config)",
    )
    serve_parser.add_argument(
        "--reaper-native-ttl",
        type=float,
        default=None,
        help="Override native idle TTL seconds (0 disables native reaping)",
    )
    serve_parser.add_argument(
        "--reaper-bridge-ttl",
        type=float,
        default=None,
        help="Override bridge idle TTL seconds (0 disables bridge reaping)",
    )
    serve_parser.add_argument(
        "--token",
        default=None,
        help="Bearer token override (default: TELA_BEARER_TOKEN or generated)",
    )

    # --- connect ---
    connect_parser = subparsers.add_parser(
        "connect", help="Start stdio bridge to tela HTTP gateway"
    )
    connect_parser.add_argument(
        "--config",
        default="tela.yaml",
        help="Path to configuration file (default: tela.yaml)",
    )
    connect_parser.add_argument(
        "--default-profile",
        default=None,
        help="Open-mode default profile override for auto-started serve",
    )
    connect_parser.add_argument(
        "--server",
        default=None,
        help="Explicit gateway endpoint as host:port",
    )
    connect_parser.add_argument(
        "--token",
        default=None,
        help="Bearer token override (default: env, then lockfile)",
    )
    connect_parser.add_argument(
        "--max-recovery-attempts",
        type=int,
        default=3,
        help="Maximum transient error recovery retries (default: 3)",
    )
    connect_parser.add_argument(
        "--client-kind",
        default=None,
        help="ADR-008 client kind label (default: TELA_CLIENT_KIND, then unknown)",
    )

    # --- stop ---
    subparsers.add_parser("stop", help="Stop the running tela HTTP gateway")

    # --- status ---
    status_parser = subparsers.add_parser("status", help="Show gateway status")
    status_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Output in JSON format",
    )
    status_parser.add_argument(
        "--probe",
        dest="probe",
        action="store_true",
        default=False,
        help="Actively probe runtime endpoint (mutually exclusive with --clients)",
    )
    status_parser.add_argument(
        "--clients",
        dest="clients",
        action="store_true",
        default=False,
        help="List client attachments from registry (mutually exclusive with --probe)",
    )
    status_parser.add_argument(
        "--probe-timeout",
        dest="probe_timeout",
        type=float,
        default=None,
        help="Timeout for --probe in seconds (default: 5.0, requires --probe)",
    )

    # --- doctor ---
    doctor_parser = subparsers.add_parser(
        "doctor", help="Diagnose gateway state and explicitly recover with --recover"
    )
    doctor_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Output in JSON format",
    )
    doctor_parser.add_argument(
        "--recover",
        dest="recover",
        action="store_true",
        default=False,
        help="Explicitly probe and attempt recovery mutations",
    )
    doctor_parser.add_argument(
        "--probe-timeout",
        dest="probe_timeout",
        type=float,
        default=None,
        help="Timeout for recovery probe in seconds (requires --recover)",
    )
    doctor_parser.add_argument(
        "--recover-timeout",
        dest="recover_timeout",
        type=float,
        default=None,
        help="Timeout for cold-start recovery in seconds",
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

    explicit_argv = argv is not None
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if explicit_argv:
            raise
        if isinstance(exc.code, int):
            return exc.code
        return 1

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "status":
        status_result = status_command(
            json_output=args.json_output,
            probe=args.probe,
            clients=args.clients,
            probe_timeout=args.probe_timeout,
        )
        if status_result.is_err:
            print(f"error: {status_result.error}", file=sys.stderr)
            return 1
        assert status_result.value is not None
        return status_result.value
    if args.command == "doctor":
        doctor_result = doctor_command(
            json_output=args.json_output,
            recover=args.recover,
            probe_timeout=args.probe_timeout,
            recover_timeout=args.recover_timeout,
        )
        if doctor_result.is_err:
            print(f"error: {doctor_result.error}", file=sys.stderr)
            return 1
        assert doctor_result.value is not None
        return doctor_result.value
    if args.command == "serve":
        serve_result = serve_command(
            config_path=args.config,
            port=args.port,
            host=args.host,
            default_profile=args.default_profile,
            idle_timeout=args.idle_timeout,
            reaper_sweep_interval=args.reaper_sweep_interval,
            reaper_native_ttl=args.reaper_native_ttl,
            reaper_bridge_ttl=args.reaper_bridge_ttl,
            token=args.token,
        )
        if serve_result.is_err:
            print(f"error: {serve_result.error}", file=sys.stderr)
            return 1
        assert serve_result.value is not None
        return serve_result.value
    if args.command == "connect":
        connect_result = connect_command(
            config_path=args.config,
            default_profile=args.default_profile,
            server=args.server,
            token=args.token,
            max_recovery_attempts=args.max_recovery_attempts,
            client_kind=args.client_kind,
        )
        if connect_result.is_err:
            print(f"error: {connect_result.error}", file=sys.stderr)
            return 1
        assert connect_result.value is not None
        return connect_result.value
    if args.command == "stop":
        stop_result = stop_command()
        if stop_result.is_err:
            print(f"error: {stop_result.error}", file=sys.stderr)
            return 1
        assert stop_result.value is not None
        return stop_result.value
    if args.command == "profiles":
        profiles_result = profiles_command(
            config_path=args.config,
            json_output=args.json_output,
        )
        if profiles_result.is_err:
            print(f"error: {profiles_result.error}", file=sys.stderr)
            return 1
        assert profiles_result.value is not None
        return profiles_result.value
    if args.command == "connections":
        connections_result = connections_command(json_output=args.json_output)
        if connections_result.is_err:
            print(f"error: {connections_result.error}", file=sys.stderr)
            return 1
        assert connections_result.value is not None
        return connections_result.value
    if args.command == "audit":
        audit_result = audit_command(
            since=args.since, limit=args.limit, json_output=args.json_output
        )
        if audit_result.is_err:
            print(f"error: {audit_result.error}", file=sys.stderr)
            return 1
        assert audit_result.value is not None
        return audit_result.value

    parser.print_help()
    return 1
