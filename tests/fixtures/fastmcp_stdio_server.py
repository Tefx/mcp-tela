"""Minimal FastMCP stdio server used by downstream runtime tests."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP("tela-test-stdio")


@server.tool()
def ping() -> str:
    """Return static liveness response."""

    return "pong"


@server.tool()
def echo(value: str) -> str:
    """Echo an input string."""

    return value


if __name__ == "__main__":
    server.run(transport="stdio")
