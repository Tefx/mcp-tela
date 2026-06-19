"""MCP stdio fixture that initializes but never answers tools/list."""

from __future__ import annotations

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("tools-list-hang", version="0.1.0")


@server.list_tools()
async def list_tools():
    await anyio.sleep_forever()


if __name__ == "__main__":
    async def main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    anyio.run(main)
