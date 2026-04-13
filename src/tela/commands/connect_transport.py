"""Transport payload helpers for ``tela connect`` bridge forwarding."""

from __future__ import annotations

import json

from tela.shell.result import Result


# @invar:allow shell_result: pure payload rewrite helper used inside bridge forwarding.
# @shell_orchestration: bridge payload adaptation is transport glue at the stdio/HTTP boundary.
# @shell_complexity: initialize payload rewrite branches on malformed/non-initialize JSON.
def inject_bridge_connection_id(payload: bytes, *, connection_id: str | None) -> bytes:
    """Attach bridge connection identity to MCP initialize clientInfo."""
    if connection_id is None:
        return payload
    try:
        message = json.loads(payload)
    except (TypeError, ValueError):
        return payload
    if not isinstance(message, dict) or message.get("method") != "initialize":
        return payload
    params = message.get("params")
    if not isinstance(params, dict):
        return payload
    client_info = params.get("clientInfo")
    if not isinstance(client_info, dict):
        return payload
    if client_info.get("tela_bridge_connection_id") == connection_id:
        return payload
    enriched_message = dict(message)
    enriched_params = dict(params)
    enriched_client_info = dict(client_info)
    enriched_client_info["tela_bridge_connection_id"] = connection_id
    enriched_params["clientInfo"] = enriched_client_info
    enriched_message["params"] = enriched_params
    return json.dumps(enriched_message).encode("utf-8")


def extract_response_messages(
    *, content_type: str, response_body: bytes
) -> Result[list[bytes], str]:
    """Convert HTTP response body into one-or-more MCP stdio payloads."""
    if response_body == b"":
        return Result(value=[])
    if "text/event-stream" in content_type.lower():
        return parse_sse_payloads(response_body)
    return Result(value=[response_body])


# @shell_complexity: parser handles event boundaries and data line accumulation.
def parse_sse_payloads(raw_body: bytes) -> Result[list[bytes], str]:
    """Parse SSE body into MCP JSON payload bytes."""
    text = raw_body.decode("utf-8", errors="replace")
    payloads: list[bytes] = []
    current_data: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "":
            if current_data:
                payload = "\n".join(current_data)
                if payload and payload != "[DONE]":
                    payloads.append(payload.encode("utf-8"))
                current_data.clear()
            continue
        if stripped.startswith("data:"):
            current_data.append(stripped[5:].lstrip())
    if current_data:
        payload = "\n".join(current_data)
        if payload and payload != "[DONE]":
            payloads.append(payload.encode("utf-8"))
    return Result(value=payloads)
