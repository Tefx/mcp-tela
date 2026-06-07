"""Pure JSON-RPC bridge parsing and recovery classification helpers."""

from __future__ import annotations

import enum
import json
import math

from tela.core.contracts import post, pre


class BridgeReplayPolicy(enum.Enum):
    SAFE = "safe"
    UNSAFE = "unsafe"


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


@pre(
    lambda response_messages: (
        isinstance(response_messages, list)
        and all(isinstance(item, bytes) for item in response_messages)
    )
)
@post(lambda result: isinstance(result, bool))
def response_requires_bridge_recovery(response_messages: list[bytes]) -> bool:
    """Return True when a gateway-owned JSON-RPC error requires recovery.

    Tool-result text is downstream application data, not a gateway-owned error
    envelope, so marker-like text inside ``result.isError`` must not trigger
    bridge recovery.

    Examples:
        >>> payload = b'{"error":{"message":"RECONNECT_REQUIRED: bridge stale"}}'
        >>> response_requires_bridge_recovery([payload])
        True
        >>> tool_result = b'{"result":{"isError":true,"content":[{"text":"RECONNECT_REQUIRED: bridge stale"}]}}'
        >>> response_requires_bridge_recovery([tool_result])
        False
        >>> response_requires_bridge_recovery([b'[{"error":{"message":"RECONNECT_REQUIRED: batch"}}]'])
        False
        >>> response_requires_bridge_recovery([])
        False
    """

    recovery_markers = (
        "reconnect_required:",
        "bridge initialize requires pre-registered connection",
    )
    for payload in response_messages:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

        if not isinstance(decoded, dict):
            continue
        error_obj = decoded.get("error")
        if not isinstance(error_obj, dict):
            continue
        message = error_obj.get("message")
        if not isinstance(message, str):
            continue
        normalized_message = message.lower()
        if any(marker in normalized_message for marker in recovery_markers):
            return True
    return False


@pre(lambda payload: isinstance(payload, bytes))
@post(
    lambda result: (
        (isinstance(result, int) and not isinstance(result, bool))
        or isinstance(result, str)
        or result is None
    )
)
def jsonrpc_request_id(payload: bytes) -> object | None:
    """Return a single JSON-RPC object request ID when safely usable.

    Malformed JSON, non-object JSON, batches, missing ids, ``null`` ids, bool
    ids, and fractional numeric ids fail closed to ``None``.

    Examples:
        >>> jsonrpc_request_id(b'{"jsonrpc":"2.0","id":42,"method":"test"}')
        42
        >>> jsonrpc_request_id(b'{"id":"abc"}')
        'abc'
        >>> jsonrpc_request_id(b'{"id":true}') is None
        True
        >>> jsonrpc_request_id(b'{}') is None
        True
        >>> jsonrpc_request_id(b'not json') is None
        True
        >>> jsonrpc_request_id(b'[]') is None
        True
        >>> jsonrpc_request_id(b'[{"id":1}]') is None
        True
    """
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(decoded, dict):
        return None
    req_id = decoded.get("id")
    if isinstance(req_id, bool):
        return None
    if isinstance(req_id, (int, str)):
        return req_id
    return None


@pre(lambda payload: isinstance(payload, bytes))
@post(lambda result: isinstance(result, bool))
def jsonrpc_is_notification(payload: bytes) -> bool:
    """Return True only for a single object with string method and no id.

    Examples:
        >>> jsonrpc_is_notification(b'{"jsonrpc":"2.0","method":"ping"}')
        True
        >>> jsonrpc_is_notification(b'{"jsonrpc":"2.0","method":"ping","id":1}')
        False
        >>> jsonrpc_is_notification(b'{"jsonrpc":"2.0","method":42}')
        False
        >>> jsonrpc_is_notification(b'{}')
        False
        >>> jsonrpc_is_notification(b'not json')
        False
        >>> jsonrpc_is_notification(b'[]')
        False
    """
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False

    if not isinstance(decoded, dict):
        return False
    return isinstance(decoded.get("method"), str) and "id" not in decoded


@pre(lambda payload: isinstance(payload, bytes))
@post(lambda result: isinstance(result, BridgeReplayPolicy))
def bridge_replay_policy(payload: bytes) -> BridgeReplayPolicy:
    """Determine whether a single JSON-RPC request is replay-safe.

    Only ``initialize``, ``ping``, and ``tools/list`` requests with a usable
    request id are safe. Notifications, batches, malformed payloads,
    non-object JSON, unknown methods, and ``tools/call`` fail closed.

    Examples:
        >>> bridge_replay_policy(b'{"jsonrpc":"2.0","method":"ping"}').value
        'unsafe'
        >>> bridge_replay_policy(b'{"jsonrpc":"2.0","method":"initialize","id":1}').value
        'safe'
        >>> bridge_replay_policy(b'{"jsonrpc":"2.0","method":"tools/list","id":"list"}').value
        'safe'
        >>> bridge_replay_policy(b'{"jsonrpc":"2.0","method":"tools/call","id":2}').value
        'unsafe'
        >>> bridge_replay_policy(b'[{"jsonrpc":"2.0","method":"ping","id":1}]').value
        'unsafe'
        >>> bridge_replay_policy(b'not json').value
        'unsafe'
    """
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return BridgeReplayPolicy.UNSAFE

    if not isinstance(decoded, dict):
        return BridgeReplayPolicy.UNSAFE
    method = decoded.get("method")
    if method not in {"initialize", "ping", "tools/list"}:
        return BridgeReplayPolicy.UNSAFE
    if jsonrpc_request_id(payload) is None:
        return BridgeReplayPolicy.UNSAFE
    return BridgeReplayPolicy.SAFE


@pre(
    lambda *, bearer_token, payload_length, session_id: (
        isinstance(bearer_token, str)
        and len(bearer_token) > 0
        and type(payload_length) is int
        and payload_length >= 0
        and (session_id is None or isinstance(session_id, str))
    )
)
@post(
    lambda result: (
        isinstance(result, dict)
        and result.get("Authorization", "").startswith("Bearer ")
        and result.get("Content-Type") == "application/json"
        and result.get("Accept") == "application/json, text/event-stream"
        and "Content-Length" in result
        and "X-Tela-Session-Id" not in result
    )
)
def bridge_http_headers(
    *, bearer_token: str, payload_length: int, session_id: str | None
) -> dict[str, str]:
    """Construct MCP Streamable HTTP headers for bridge payload submission.

    Examples:
        >>> hdrs = bridge_http_headers(bearer_token="x", payload_length=10, session_id=None)
        >>> hdrs["Authorization"]
        'Bearer x'
        >>> hdrs["Content-Type"]
        'application/json'
        >>> hdrs["Accept"]
        'application/json, text/event-stream'
        >>> hdrs["Content-Length"]
        '10'
        >>> "X-Tela-Session-Id" in hdrs
        False
        >>> "mcp-session-id" in hdrs
        False
        >>> hdrs = bridge_http_headers(bearer_token="x", payload_length=10, session_id="abc")
        >>> hdrs["mcp-session-id"]
        'abc'
    """
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Content-Length": str(payload_length),
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


@pre(
    lambda *, connect_timeout_seconds, write_timeout_seconds, response_timeout_seconds: (
        isinstance(connect_timeout_seconds, (int, float))
        and not isinstance(connect_timeout_seconds, bool)
        and isinstance(write_timeout_seconds, (int, float))
        and not isinstance(write_timeout_seconds, bool)
        and (
            response_timeout_seconds is None
            or (
                isinstance(response_timeout_seconds, (int, float))
                and not isinstance(response_timeout_seconds, bool)
            )
        )
    )
)
@post(lambda result: isinstance(result, bool))
def bridge_http_timeouts_valid(
    *,
    connect_timeout_seconds: float,
    write_timeout_seconds: float,
    response_timeout_seconds: float | None,
) -> bool:
    """Validate HTTP timeout configuration values.

    ``None`` means no response deadline. All finite timeout values must be
    strictly positive.

    Examples:
        >>> bridge_http_timeouts_valid(connect_timeout_seconds=1.0, write_timeout_seconds=2.0, response_timeout_seconds=3.0)
        True
        >>> bridge_http_timeouts_valid(connect_timeout_seconds=-1.0, write_timeout_seconds=2.0, response_timeout_seconds=3.0)
        False
        >>> bridge_http_timeouts_valid(connect_timeout_seconds=1.0, write_timeout_seconds=2.0, response_timeout_seconds=None)
        True
        >>> bridge_http_timeouts_valid(connect_timeout_seconds=float("inf"), write_timeout_seconds=2.0, response_timeout_seconds=None)
        False
        >>> bridge_http_timeouts_valid(connect_timeout_seconds=1.0, write_timeout_seconds=2.0, response_timeout_seconds=float("nan"))
        False
    """
    if not math.isfinite(connect_timeout_seconds):
        return False
    if not math.isfinite(write_timeout_seconds):
        return False
    if connect_timeout_seconds <= 0 or write_timeout_seconds <= 0:
        return False
    if response_timeout_seconds is None:
        return True
    return math.isfinite(response_timeout_seconds) and response_timeout_seconds > 0
