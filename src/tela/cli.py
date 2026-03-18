"""CLI entrypoint for tela.

Wires all five subcommands (start, status, profiles, connections, audit)
into the argparse-based CLI dispatcher per INTERFACES.md.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tela.commands.audit_cmd import audit_command
from tela.commands.connections_cmd import connections_command
from tela.commands.profiles_cmd import profiles_command
from tela.commands.start import start_command
from tela.commands.status_cmd import status_command
from tela.shell.config_loader import load_config
from tela.shell.gateway import bind_gateway_startup, gateway_shutdown, gateway_start
from tela.shell.upstream import handle_initialize


# @invar:allow dead_export: CLI entrypoint is invoked by the command framework via pyproject.toml.
# @invar:allow shell_result: CLI entrypoint returns int exit code per POSIX convention.
# @shell_orchestration: CLI entrypoint orchestrates argparse and command dispatch.
def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint for tela.

    Parses CLI arguments and dispatches to the appropriate command handler.

    Examples:
        >>> pass  # doctest: +SKIP

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

    if args.command == "start":
        return _handle_start(args)
    if args.command == "status":
        return status_command(json_output=args.json_output)
    if args.command == "profiles":
        return profiles_command(config_path=args.config, json_output=args.json_output)
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

    config_result = load_config(
        path=Path(args.config), default_profile=args.default_profile
    )
    if config_result.is_err:
        print(f"error: {config_result.error}", file=sys.stderr)
        return 1

    assert config_result.value is not None

    # Step 2: Bind runtime contract into gateway startup config
    gateway_result = bind_gateway_startup(
        runtime_result.value, config=config_result.value
    )

    if gateway_result.is_err:
        print(f"error: {gateway_result.error}", file=sys.stderr)
        return 1

    assert gateway_result.value is not None

    startup_result = asyncio.run(
        gateway_start(gateway_result.value, tela_config=config_result.value)
    )
    if startup_result.is_err:
        print(f"error: {startup_result.error}", file=sys.stderr)
        return 1

    print(
        f"tela: ready (transport={gateway_result.value.transport.value}, "
        f"profile={gateway_result.value.default_profile})",
        file=sys.stderr,
    )

    try:
        if gateway_result.value.transport.value == "stdio":
            return _serve_stdio_mcp()
        assert gateway_result.value.port is not None
        return _serve_sse_gateway(gateway_result.value.port)
    finally:
        if gateway_result.value.transport.value == "stdio":
            asyncio.run(gateway_shutdown())


def _write_jsonrpc_response(request_id: Any, payload: dict[str, Any]) -> None:
    """Write a JSON-RPC response on stdout and flush transport."""

    if request_id is None:
        return
    wire = {"jsonrpc": "2.0", "id": request_id, **payload}
    sys.stdout.write(json.dumps(wire) + "\n")
    sys.stdout.flush()


def _serve_stdio_mcp() -> int:
    """Serve minimal JSON-RPC on stdio for MCP liveness."""

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(request, dict):
            continue

        request_id = request.get("id")
        method = request.get("method")
        if method == "initialize":
            params = request.get("params", {})
            client_info_raw = (
                params.get("clientInfo", {}) if isinstance(params, dict) else {}
            )
            client_info = client_info_raw if isinstance(client_info_raw, dict) else {}
            init_result = asyncio.run(handle_initialize(client_info=client_info))
            if init_result.is_ok and init_result.value is not None:
                _write_jsonrpc_response(
                    request_id,
                    {
                        "result": {
                            "capabilities": {},
                            "connection": init_result.value.model_dump(),
                        }
                    },
                )
            else:
                _write_jsonrpc_response(
                    request_id,
                    {
                        "error": {
                            "code": -32000,
                            "message": init_result.error or "initialize failed",
                        }
                    },
                )
            continue

        _write_jsonrpc_response(
            request_id,
            {"error": {"code": -32601, "message": "Method not found"}},
        )

    return 0


class _SSEHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler to keep SSE transport process alive."""

    def do_GET(self) -> None:
        """Return readiness payload for gateway liveness probes."""

        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default access logs on stderr."""


def _serve_sse_gateway(port: int) -> int:
    """Run a long-lived HTTP server for SSE gateway liveness."""

    server = ThreadingHTTPServer(("127.0.0.1", port), _SSEHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
