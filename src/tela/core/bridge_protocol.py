"""Pure JSON-RPC bridge parsing and recovery classification helpers."""

from __future__ import annotations

import json

from tela.core.contracts import post, pre


@pre(lambda payload: isinstance(payload, bytes))
@post(lambda result: result is None or isinstance(result, str))
def extract_jsonrpc_method(payload: bytes) -> str | None:
    """Return the JSON-RPC method name from a payload when present.

    Examples:
        >>> extract_jsonrpc_method(b'{"jsonrpc":"2.0","method":"initialize"}')
        'initialize'
        >>> extract_jsonrpc_method(b'[]') is None
        True
    """

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(decoded, dict):
        return None
    method = decoded.get("method")
    return method if isinstance(method, str) else None


@pre(lambda payload: isinstance(payload, bytes))
@post(lambda result: isinstance(result, list))
def extract_bridge_error_messages(payload: bytes) -> list[str]:
    """Return error-text candidates from a JSON-RPC response payload.

    Examples:
        >>> extract_bridge_error_messages(b'{"error":{"message":"boom"}}')
        ['boom']
        >>> extract_bridge_error_messages(b'not-json')
        []
    """

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    if not isinstance(decoded, dict):
        return []

    messages: list[str] = []

    error = decoded.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            messages.append(message)

    result = decoded.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    messages.append(text)

    return messages


@pre(lambda response_messages: isinstance(response_messages, list))
@post(lambda result: isinstance(result, bool))
def response_requires_bridge_recovery(response_messages: list[bytes]) -> bool:
    """Return True when response errors prove the bridge session is stale.

    Examples:
        >>> payload = b'{"error":{"message":"RECONNECT_REQUIRED: bridge stale"}}'
        >>> response_requires_bridge_recovery([payload])
        True
        >>> response_requires_bridge_recovery([])
        False
    """

    recovery_markers = (
        "reconnect_required:",
        "bridge initialize requires pre-registered connection",
    )
    for payload in response_messages:
        for message in extract_bridge_error_messages(payload):
            normalized_message = message.lower()
            if any(marker in normalized_message for marker in recovery_markers):
                return True
    return False
