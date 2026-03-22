"""CLI entrypoint for tela.

Wires subcommands (start, connect, serve, status, profiles, connections, audit)
into the argparse-based CLI dispatcher per INTERFACES.md.
"""

from __future__ import annotations

import asyncio
import argparse
import sys
from pathlib import Path

from tela.commands.audit_cmd import audit_command
from tela.commands.connect_cmd import connect_command
from tela.commands.connections_cmd import connections_command
from tela.commands.profiles_cmd import profiles_command
from tela.commands.serve_cmd import serve_command
from tela.commands.start import start_command
from tela.commands.status_cmd import status_command
from tela.shell.config_loader import Result, load_config
from tela.shell.gateway import (
    GatewayStartupConfig,
    bind_gateway_startup,
    gateway_shutdown,
    gateway_reload_config_from_disk,
    gateway_start,
    get_runtime,
)
from tela.core.models import TelaConfig


CONFIG_WATCH_POLL_SECONDS = 0.5


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
        help="Remote transport port (default: Streamable HTTP; omit for stdio)",
    )
    start_parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default=None,
        help="Transport protocol (default: http when --port given, else stdio)",
    )
    start_parser.add_argument(
        "--default-profile",
        default=None,
        help="Open-mode default profile override",
    )

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
        start_result = _handle_start(args)
        if start_result.is_err:
            print(f"error: {start_result.error}", file=sys.stderr)
            return 1
        assert start_result.value is not None
        return start_result.value
    if args.command == "status":
        status_result = status_command(json_output=args.json_output)
        if status_result.is_err:
            print(f"error: {status_result.error}", file=sys.stderr)
            return 1
        assert status_result.value is not None
        return status_result.value
    if args.command == "serve":
        serve_result = serve_command(
            config_path=args.config,
            port=args.port,
            host=args.host,
            default_profile=args.default_profile,
            idle_timeout=args.idle_timeout,
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
        )
        if connect_result.is_err:
            print(f"error: {connect_result.error}", file=sys.stderr)
            return 1
        assert connect_result.value is not None
        return connect_result.value
    if args.command == "profiles":
        return profiles_command(config_path=args.config, json_output=args.json_output)
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


# @shell_complexity: start path handles multiple startup/config/runtime branches.
def _handle_start(args: argparse.Namespace) -> Result[int, str]:
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
        transport=args.transport,
    )

    if runtime_result.is_err:
        return Result(error=runtime_result.error)

    assert runtime_result.value is not None

    config_result = load_config(
        path=Path(args.config), default_profile=args.default_profile
    )
    if config_result.is_err:
        return Result(error=config_result.error)

    assert config_result.value is not None

    # Step 2: Bind runtime contract into gateway startup config
    gateway_result = bind_gateway_startup(
        runtime_result.value, config=config_result.value
    )

    if gateway_result.is_err:
        return Result(error=gateway_result.error)

    assert gateway_result.value is not None

    if gateway_result.value.transport.value == "stdio":
        run_result = asyncio.run(
            _run_stdio_gateway(
                startup_config=gateway_result.value,
                tela_config=config_result.value,
                config_path=Path(args.config),
            )
        )
        if run_result.is_err:
            return Result(error=run_result.error)
        assert run_result.value is not None
        return Result(value=run_result.value)

    run_result = asyncio.run(
        _serve_remote_gateway(
            startup_config=gateway_result.value,
            tela_config=config_result.value,
            config_path=Path(args.config),
        )
    )
    if run_result.is_err:
        return Result(error=run_result.error)
    assert run_result.value is not None
    return Result(value=run_result.value)


async def _run_stdio_gateway(
    startup_config: GatewayStartupConfig,
    tela_config: TelaConfig,
    config_path: Path,
) -> Result[int, str]:
    """Start gateway and run FastMCP stdio transport in one loop."""

    startup_result = await gateway_start(startup_config, tela_config=tela_config)
    if startup_result.is_err:
        return Result(error=startup_result.error)

    print(
        f"tela: ready (transport={startup_config.transport.value}, "
        f"profile={startup_config.default_profile})",
        file=sys.stderr,
    )

    runtime = get_runtime()
    if runtime.upstream_server is None:
        return Result(error="STARTUP_FAILED: upstream MCP server not initialized")

    stop_watcher = asyncio.Event()
    watcher_task = asyncio.create_task(
        _watch_config_changes(
            config_path=config_path,
            default_profile=startup_config.default_profile,
            stop_event=stop_watcher,
        )
    )

    gateway_exit = 0
    try:
        await runtime.upstream_server.run_stdio_async()
    except Exception as exc:
        gateway_exit = 1
        return Result(error=f"STDIO_RUN_FAILED: {exc}")
    finally:
        stop_watcher.set()
        await _await_task(watcher_task)
        await gateway_shutdown()

    return Result(value=gateway_exit)


# @shell_complexity: remote startup handles transport selection and shutdown branches.
async def _serve_remote_gateway(
    startup_config: GatewayStartupConfig,
    tela_config: TelaConfig,
    config_path: Path,
) -> Result[int, str]:
    """Start gateway and run FastMCP remote transport (SSE or HTTP) in one loop."""

    startup_result = await gateway_start(startup_config, tela_config=tela_config)
    if startup_result.is_err:
        return Result(error=startup_result.error)

    transport_value = startup_config.transport.value
    print(
        f"tela: ready (transport={transport_value}, "
        f"profile={startup_config.default_profile})",
        file=sys.stderr,
    )

    runtime = get_runtime()
    if runtime.upstream_server is None:
        return Result(error="STARTUP_FAILED: upstream MCP server not initialized")

    stop_watcher = asyncio.Event()
    watcher_task = asyncio.create_task(
        _watch_config_changes(
            config_path=config_path,
            default_profile=startup_config.default_profile,
            stop_event=stop_watcher,
        )
    )

    gateway_exit = 0
    try:
        if transport_value == "http":
            await asyncio.to_thread(runtime.upstream_server.run, "streamable-http")
        else:
            await asyncio.to_thread(runtime.upstream_server.run, "sse")
    except Exception as exc:
        gateway_exit = 1
        return Result(error=f"{transport_value.upper()}_RUN_FAILED: {exc}")
    finally:
        stop_watcher.set()
        await _await_task(watcher_task)
        await gateway_shutdown()

    return Result(value=gateway_exit)


# @shell_complexity: watcher handles multiple change/error control-flow branches.
async def _watch_config_changes(
    *,
    config_path: Path,
    default_profile: str | None,
    stop_event: asyncio.Event,
) -> None:
    """Poll config mtime and run hot-reload callback when it changes."""

    last_mtime_result = _config_mtime_ns(config_path)
    if last_mtime_result.is_err:
        return
    last_mtime_ns = last_mtime_result.value

    while not stop_event.is_set():
        await asyncio.sleep(CONFIG_WATCH_POLL_SECONDS)
        current_mtime_result = _config_mtime_ns(config_path)
        if current_mtime_result.is_err:
            continue
        current_mtime_ns = current_mtime_result.value

        if current_mtime_ns is None:
            continue

        if last_mtime_ns is not None and current_mtime_ns <= last_mtime_ns:
            continue

        reload_result = await gateway_reload_config_from_disk(
            config_path=config_path,
            default_profile=default_profile,
        )
        if reload_result.is_err:
            print(
                f"warning: config reload failed: {reload_result.error}", file=sys.stderr
            )
        last_mtime_ns = current_mtime_ns


def _config_mtime_ns(config_path: Path) -> Result[int | None, str]:
    """Return file mtime (ns) for config watcher, or None if unreadable."""

    try:
        return Result(value=config_path.stat().st_mtime_ns)
    except OSError:
        return Result(value=None)


# @shell_orchestration: async task cancellation is lifecycle cleanup plumbing.
async def _await_task(task: asyncio.Task[None]) -> None:
    """Await or cancel watcher task during shutdown."""

    if task.done():
        await task
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
