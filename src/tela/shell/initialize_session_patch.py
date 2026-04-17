"""Install canonical initialize admission on MCP sessions.

FastMCP's default session handling accepts MCP initialize requests without
calling tela's runtime admission logic. This module patches the session-level
initialize hook so that every transport (stdio and streamable HTTP) runs through
``handle_initialize`` before the protocol initialize response is emitted.
"""

from __future__ import annotations

from typing import Any, cast

from mcp import types as mcp_types
from mcp.server.session import ServerSession

from tela.shell.connection_lifecycle import cleanup_connection_by_id
from tela.shell.gateway_runtime import capture_session
from tela.shell.upstream import handle_initialize

_ORIGINAL_RECEIVED_REQUEST = ServerSession._received_request
_PATCH_INSTALLED = False


# @shell_complexity: initialize gate must branch across admission failure, session capture failure, and cleanup paths before deferring to the protocol handler.
async def _patched_received_request(
    self: ServerSession,
    responder: Any,
) -> None:
    """Run tela initialize admission before the default MCP initialize response."""

    root = responder.request.root
    if isinstance(root, mcp_types.InitializeRequest):
        dumped = root.params.clientInfo.model_dump(exclude_none=True)
        client_info = {str(key): value for key, value in dumped.items()}
        initialize_result = await handle_initialize(client_info)
        if initialize_result.is_err:
            with responder:
                await responder.respond(
                    mcp_types.ErrorData(
                        code=0,
                        message=initialize_result.error or "INITIALIZE_REJECTED",
                    )
                )
            return

        assert initialize_result.value is not None
        capture_result = capture_session(initialize_result.value.connection_id, self)
        if capture_result.is_err:
            _ = cleanup_connection_by_id(initialize_result.value.connection_id)
            with responder:
                await responder.respond(
                    mcp_types.ErrorData(
                        code=0,
                        message=capture_result.error or "INITIALIZE_REJECTED",
                    )
                )
            return

        try:
            await _ORIGINAL_RECEIVED_REQUEST(self, responder)
        except Exception:
            _ = cleanup_connection_by_id(initialize_result.value.connection_id)
            raise
        return

    await _ORIGINAL_RECEIVED_REQUEST(self, responder)


def install_initialize_session_patch() -> None:
    """Install the patched ServerSession initialize hook once per process."""

    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return
    ServerSession._received_request = cast(Any, _patched_received_request)
    _PATCH_INSTALLED = True
