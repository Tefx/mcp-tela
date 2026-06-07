# ADR-009: MCP bridge request lifecycle, timeout, and replay semantics

Status: Accepted

## Problem

`tela connect` bridges upstream stdio JSON-RPC frames to the gateway's
Streamable HTTP `/mcp` endpoint. Today the bridge uses one HTTP timeout and one
string-based recovery classifier for both control-plane failures and MCP
data-plane responses.

That collapses three different states:

1. the gateway could not be reached before an MCP request body was sent;
2. a valid MCP request body was sent and is still waiting for a normal response;
3. a sent request lost its response stream and its tool side effects are
   unknown.

The visible failure is `tela_tasca_table_wait` with a long wait: the bridge times
out first, classifies `timed out` as recoverable transport failure, attempts to
replay the in-flight MCP request, and eventually returns
`BRIDGE_RECOVERY_EXHAUSTED: in-flight MCP request could not be replayed`.

This is a class bug, not a Tasca-specific bug. Any slow MCP operation can hit the
same path: long-polling, large file operations, LLM generation, human approval,
remote queueing, or any future tool whose normal response latency exceeds the
bridge's transport timeout.

## Source evidence

| Evidence | What it proves |
|---|---|
| `src/tela/commands/connect_bridge.py:60` defines `HTTP_TIMEOUT_SECONDS = 10.0`. | Current bridge has a fixed HTTP timeout constant. |
| `src/tela/commands/connect_bridge.py:489-573` forwards every MCP message through `post_mcp_message(...)`, which passes that timeout to `retry_http_request`. | The fixed timeout applies to MCP data-plane requests, not only control-plane probes. |
| `src/tela/commands/connect_bridge.py:358-376` treats `timed out` as recoverable. | Response wait timeout is indistinguishable from gateway reachability failure. |
| `src/tela/commands/connect_bridge.py:788-877` retries `_forward_request_with_recovery(...)` after recoverable forwarding errors. | A timed-out in-flight request can be replayed. |
| `src/tela/commands/connect_bridge.py:1021-1043` emits `BRIDGE_RECOVERY_EXHAUSTED: in-flight MCP request could not be replayed`. | The user-facing error is produced by bridge recovery exhaustion. |
| `src/tela/commands/http_client.py:47-140` owns a generic urllib retry skeleton and returns only strings such as `HTTP_CONNECT_ERROR: ...` or `HTTP_{code}: ...`. | Current HTTP layer does not expose send/admission phase or whether replay is safe. |
| `docs/DESIGN.md` `Discovery and readiness` says bridge recovery covers connection refused/reset/broken pipe, HTTP 503, and readiness timeouts. | Recovery is intended for transport/readiness failure. |
| `docs/DESIGN.md` `TimeoutError Policy Divergence` distinguishes Shell connect timeout from Core/tool timeout. | Existing design already recognizes that not all timeouts mean recoverable transport failure. |

## Hard requirements

1. Do not special-case `tela_tasca_table_wait`, Tasca, `wait_ms`, or any specific
   tool argument.
2. Do not assume a particular response duration such as 10 seconds.
3. Preserve bounded recovery for gateway reachability, startup warming, and
   readiness failures.
4. Do not automatically replay arbitrary sent `tools/call` requests.
5. Return request-level JSON-RPC errors for request lifecycle failures without
   killing the bridge when continuing is safe.
6. Keep control-plane HTTP behavior finite and bounded.
7. Use standard library networking only; do not add a new HTTP client dependency.
8. Keep canonical shared-surface behavior unchanged: builtin, downstream, bridge,
   and HTTP paths must continue to obey the same MCP/tool contracts.

## Non-goals

- No change to Tasca server behavior.
- No change to downstream tool implementations.
- No automatic inference that a tool is idempotent from its name.
- No public compatibility alias or malformed-input cleanup on shared surfaces.
- No new readiness authority in the bridge; `GET /status` remains authoritative.
- No streaming/SSE protocol redesign beyond preserving current response extraction
  behavior after the HTTP response body is available.
- No public CLI flag for MCP response deadlines in this ADR. The internal
  executor supports `response_timeout_seconds` so tests and future explicit
  surfaces have a contract, but the default fix is no bridge-imposed response
  cap.

## Vocabulary

| Term | Meaning |
|---|---|
| Control plane | `/status`, `/connect`, `/disconnect`, discovery, readiness, and lifecycle calls. |
| MCP data plane | `POST /mcp` carrying upstream JSON-RPC MCP traffic. |
| `request_sent` | Whether the full HTTP request body bytes were sent. `False` means no body bytes were sent; `True` means the send completed; `None` means partial/unknown. |
| `mcp_admitted` | Whether the gateway admitted the MCP request for normal MCP handling. `False` is set only when gateway evidence proves non-admission, such as warming 503 or reconnect-required before execution. `None` means unknown. |
| Replay-safe | A request whose duplicate execution is safe by protocol contract, not by tool-name heuristics. |

`request_sent` and `mcp_admitted` are deliberately separate. HTTP 503 warming is
received after bytes were sent, so `request_sent=True`, but the gateway response
proves `mcp_admitted=False`; recovery may send the original frame again because
the request did not enter normal MCP execution.

## Decision summary

Separate the bridge into three explicit request lifecycle phases:

1. **Control plane**: `/status`, `/connect`, `/disconnect`, discovery, and
   readiness. These keep finite timeouts and bounded retry/recovery.
2. **MCP data-plane send**: connect to `/mcp`, write request bytes, and record
   whether the full request body was sent.
3. **MCP data-plane response wait**: wait for response headers/body. By default
   this has no bridge-imposed response deadline. If an explicit deadline is
   configured later, expiration is a request timeout, not bridge recovery.

Recovery may automatically send/replay the original frame only when at least one
is true:

- the request definitely was not sent (`request_sent=False`);
- the gateway proved the request was not admitted (`mcp_admitted=False`); or
- the request was sent but admission/execution is unknown and the JSON-RPC
  payload is replay-safe.

Otherwise the bridge returns a request-level error and does not replay.

## Architecture

### Modules and ownership

| Module | New or changed responsibility |
|---|---|
| `tela.core.bridge_protocol` | Pure JSON-RPC classification: request method, request id, notification-vs-request, batch handling, replay policy. Requires `@pre`/`@post` and doctests for each new Core function. |
| `tela.commands.bridge_http` (new) | MCP data-plane HTTP execution with phase-aware errors. Uses `http.client` / `urllib.parse`, not `urllib.request`, so connect/write/response phases can be separated. Shell module returns `Result[T, E]`. |
| `tela.commands.connect_bridge` | Bridge lifecycle, recovery orchestration, session id handling, JSON-RPC error serialization, and integration with replay policy. Continues to own control-plane calls through existing helpers. |
| `tela.commands.http_client` | Remains the generic control-plane retry/backoff skeleton for readiness and lifecycle endpoints. It must not become responsible for MCP data-plane semantics. |

### Public/internal interfaces

Names are implementation guidance; exact names may change only if the behavior
and tests remain equivalent.

```python
# tela.core.bridge_protocol
class BridgeReplayPolicy(Enum):
    SAFE = "safe"
    UNSAFE = "unsafe"

def jsonrpc_request_id(payload: bytes) -> object | None: ...
def jsonrpc_is_notification(payload: bytes) -> bool: ...
def bridge_replay_policy(payload: bytes) -> BridgeReplayPolicy: ...
def response_requires_bridge_recovery(response_messages: list[bytes]) -> bool: ...
```

`response_requires_bridge_recovery` already exists, but this ADR narrows its
replay-authorizing semantics: it must return `True` only for gateway-owned
JSON-RPC error envelopes listed in the reconnect-required marker contract, not
for plain `result.isError` tool-result text.

Replay policy:

| JSON-RPC payload | Policy | Reason |
|---|---|---|
| Single `initialize` request | `SAFE` | MCP session bootstrap can be retried by creating/recovering a session. Current bridge already replays initialize during session re-bootstrap. |
| Single `ping` request | `SAFE` | Protocol liveness check has no tool side effect. |
| Single `tools/list` request | `SAFE` | Enumeration is read-only gateway state. |
| Single notification | `UNSAFE` for duplicate replay after possible send | No response id exists; bridge should not synthesize success or retry invisible side effects. If `request_sent=False`, recovery followed by first delivery is allowed because no duplicate can occur. |
| Single `tools/call` request | `UNSAFE` by default | Tool may have side effects; name/schema heuristics are not authoritative. |
| Unknown single request | `UNSAFE` | Fail closed. |
| JSON-RPC batch array | `UNSAFE` for this ADR | Existing bridge request-id handling is singular. This ADR does not add per-id batch error emission or mixed safe/unsafe batch replay. |
| Malformed JSON / non-object payload | `UNSAFE` | Fail closed and preserve existing malformed payload behavior. |

Batch behavior is intentionally fail-closed. If MCP batch support becomes a hard
requirement later, a superseding ADR must define per-item request ids, mixed
notification handling, and batch replay safety. This ADR must not infer those
rules.

Notification behavior is state-based:

| Notification lifecycle state | Behavior |
|---|---|
| `request_sent=False` | Recover and deliver the original notification once. This is first delivery, not replay. |
| `request_sent=None` | Do not retry; partial delivery is unknown. Emit diagnostic only because no JSON-RPC response id exists. |
| `request_sent=True`, `mcp_admitted=False` | Recover and deliver once because gateway proved non-admission. |
| `request_sent=True`, `mcp_admitted=None` | Do not retry; delivery/execution is unknown. Emit diagnostic only. |
| 2xx empty response | Success; write no stdout frame. |

Notification diagnostics use the existing bridge diagnostic sink:
`_emit_bridge_diagnostic(f"notification delivery unknown: {phase}: {message}",
connection_id)`, which writes best-effort to stderr. After a post-send
notification failure with unknown admission, the bridge continues the forwarding
loop without stdout output or replay. It exits only through existing loop exits:
`should_stop()`, upstream stdin EOF/read failure, or a later non-notification
bridge-level failure. If pre-send/non-admitted notification recovery itself
fails or exhausts, emit a diagnostic with that recovery error and continue.

```python
# tela.commands.bridge_http
@dataclass(frozen=True)
class BridgeHttpError:
    phase: Literal[
        "connect",
        "write",
        "response_headers",
        "response_body",
        "http_status",
    ]
    message: str
    request_sent: bool | None
    mcp_admitted: bool | None
    status_code: int | None = None
    retryable_warming: bool = False

@dataclass(frozen=True)
class BridgeHttpResponse:
    content_type: str
    body: bytes
    session_id: str | None

def post_mcp_http(
    *,
    mcp_url: str,
    bearer_token: str,
    payload: bytes,
    session_id: str | None,
    connect_timeout_seconds: float,
    write_timeout_seconds: float,
    response_timeout_seconds: float | None,
    is_503_retryable: Callable[[bytes], bool],
) -> Result[BridgeHttpResponse, BridgeHttpError]: ...
```

Argument validation:

- `connect_timeout_seconds` and `write_timeout_seconds` must be finite `> 0`.
- `response_timeout_seconds` must be `None` or finite `> 0`.
- Invalid timeout arguments return `BridgeHttpError(phase="connect",
  request_sent=False, mcp_admitted=None, message="INVALID_TIMEOUT: connect_timeout_seconds and write_timeout_seconds must be finite > 0; response_timeout_seconds must be None or finite > 0")`.

`response_timeout_seconds=None` means no bridge-imposed response deadline. This
is the default for MCP data-plane forwarding.

### HTTP execution algorithm

Use `http.client.HTTPConnection` / `HTTPSConnection` directly:

1. Parse `mcp_url` with `urllib.parse.urlsplit`. Reject unsupported schemes with
   `phase="connect"`, `request_sent=False`, `mcp_admitted=None`.
2. Open the socket with `connect_timeout_seconds`.
3. Write request line, headers, and body with `write_timeout_seconds`.
4. Set `request_sent=True` only after the full payload send returns
   successfully. If failure occurs before any body bytes are sent, set
   `request_sent=False`. If partial-send state cannot be proven, set
   `request_sent=None` and fail closed in replay decisions.
5. Before reading the response, set the socket timeout to
   `response_timeout_seconds`.
   - `None`: wait without a bridge-imposed deadline.
   - finite positive float: explicit request deadline.
6. Read response headers and body.
7. Treat any 2xx HTTP status as HTTP success. Return `BridgeHttpResponse` with
   `Content-Type`, body bytes, `mcp-session-id` header, and no status retry.
   Empty 2xx bodies are valid only for notifications; if the payload is a
   request or batch, `connect_bridge` turns the empty response into
   request-level `MCP_FORWARD_FAILED` because the upstream request would
   otherwise receive no JSON-RPC response.
8. On HTTP 503, read the body before closing. If `is_503_retryable(body)` is
   true, return `BridgeHttpError(phase="http_status", request_sent=True,
   mcp_admitted=False, status_code=503, retryable_warming=True, ...)` so bridge
   recovery can use the existing warming path. Otherwise return
   `BridgeHttpError(phase="http_status", request_sent=True, mcp_admitted=None,
   status_code=503, retryable_warming=False, ...)`.
9. On any non-2xx, non-warming status, read the body for diagnostics and return
   `BridgeHttpError(phase="http_status", request_sent=True,
   mcp_admitted=None, status_code=<status>, retryable_warming=False, ...)`.
   Non-2xx bodies are not scanned for reconnect-required markers; only the
   explicit warming 503 contract can prove non-admission at HTTP status level.
10. Always close the HTTP connection in `finally`.

The `http.client` path is intentionally scoped to `/mcp`. Existing
`retry_http_request` remains simpler for control-plane endpoints.

### Worked examples

These examples define fixture-grade expectations. JSON-RPC error bytes use the
existing compact `_jsonrpc_error_response` shape from `connect_bridge.py`.

| Scenario | Phase-aware result | Bridge output |
|---|---|---|
| TCP connect refused before body send | `BridgeHttpError(phase="connect", message="Connection refused", request_sent=False, mcp_admitted=None, status_code=None, retryable_warming=False)` | Recover and send the original frame once under bounded recovery. If exhausted for request id `7`: `b'{"jsonrpc":"2.0","id":7,"error":{"code":-32000,"message":"BRIDGE_RECOVERY_EXHAUSTED: ...","data":{"code":"BRIDGE_RECOVERY_EXHAUSTED"}}}'` |
| Write fails before body bytes are sent | `BridgeHttpError(phase="write", message="Broken pipe", request_sent=False, mcp_admitted=None, status_code=None, retryable_warming=False)` | Recover and send the original frame once because request was not sent. |
| Write fails after partial/unknown send | `BridgeHttpError(phase="write", message="Broken pipe", request_sent=None, mcp_admitted=None, status_code=None, retryable_warming=False)` | Replay only if `bridge_replay_policy(payload)` is `SAFE`; unsafe `tools/call` gets `MCP_RESPONSE_INTERRUPTED`. |
| Explicit finite response deadline expires while waiting for headers | `BridgeHttpError(phase="response_headers", message="timed out", request_sent=True, mcp_admitted=None, status_code=None, retryable_warming=False)` | For request id `8`: `b'{"jsonrpc":"2.0","id":8,"error":{"code":-32000,"message":"MCP_REQUEST_TIMEOUT: timed out","data":{"code":"MCP_REQUEST_TIMEOUT"}}}'`; no recovery budget consumed. |
| EOF/reset while reading response body for unsafe `tools/call` | `BridgeHttpError(phase="response_body", message="Connection reset", request_sent=True, mcp_admitted=None, status_code=None, retryable_warming=False)` | For request id `9`: `b'{"jsonrpc":"2.0","id":9,"error":{"code":-32000,"message":"MCP_RESPONSE_INTERRUPTED: Connection reset","data":{"code":"MCP_RESPONSE_INTERRUPTED"}}}'`; no replay. |
| 503 warming contract | HTTP body `b'{"code":"ADMISSION_REJECTED_WARMING","transient":true,"retry":{"authorized":true,"basis":"gateway_signal","expectation":"bounded"},"gateway_state":"warming"}'` produces `BridgeHttpError(phase="http_status", request_sent=True, mcp_admitted=False, status_code=503, retryable_warming=True, ...)` | Recover/retry because gateway proves non-admission. |
| Plain 503 without warming contract | HTTP body `b'{"error":"busy"}'` produces `BridgeHttpError(phase="http_status", request_sent=True, mcp_admitted=None, status_code=503, retryable_warming=False, ...)` | Request-level `MCP_FORWARD_FAILED`; no warming recovery. |
| Successful JSON response | Headers `Content-Type: application/json`, `mcp-session-id: session-1`, body `b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}'` | `BridgeHttpResponse(content_type="application/json", body=b'{...}', session_id="session-1")`; bridge writes extracted response. |
| Successful notification response | HTTP `202` or other 2xx with empty body for notification payload `b'{"jsonrpc":"2.0","method":"notifications/initialized"}'` | Success; no stdout frame is written. |
| Empty 2xx response for request | HTTP `202` or `204` with empty body for request id `10` | Request-level `MCP_FORWARD_FAILED` for id `10`; no recovery. |
| Recovery infrastructure fails before budget exhaustion | Pre-send request id `13` gets `BridgeHttpError(phase="connect", message="Connection refused", request_sent=False, mcp_admitted=None, status_code=None, retryable_warming=False)`, then `recover_gateway(...)` returns `GATEWAY_RECOVERY_FAILED: readiness=...` | `b'{"jsonrpc":"2.0","id":13,"error":{"code":-32000,"message":"RECOVERY_FAILED_FOR_REQUEST: GATEWAY_RECOVERY_FAILED: readiness=...","data":{"code":"RECOVERY_FAILED_FOR_REQUEST"}}}'`; recovery budget semantics stay with recovery path. |

### Reconnect-required marker contract

Reconnect-required markers are recognized only after a successful 2xx MCP HTTP
response has been parsed by `extract_response_messages(...)`. Non-2xx HTTP
bodies are diagnostics only, except for the explicit warming 503 contract in the
HTTP execution algorithm.

Within parsed 2xx MCP response messages, only gateway-owned JSON-RPC error
envelopes prove bridge non-admission for this ADR. This ADR intentionally
narrows the current broad text extraction behavior: plain MCP tool-result text,
even when `result.isError=true`, must not authorize replay because downstream
tools can produce arbitrary text.

| Marker source | Concrete shape | Why it proves non-admission |
|---|---|---|
| JSON-RPC error message containing `RECONNECT_REQUIRED:` | `b'{"jsonrpc":"2.0","id":11,"error":{"code":-32000,"message":"RECONNECT_REQUIRED: bridge stale"}}'` | Gateway rejected the request at bridge/session admission before normal tool execution. |
| JSON-RPC error message containing `bridge initialize requires pre-registered connection` | `b'{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"bridge initialize requires pre-registered connection"}}'` | Gateway rejected initialize because lifecycle registration is missing. |

No other response text proves non-admission. In particular,
`b'{"jsonrpc":"2.0","id":12,"result":{"isError":true,"content":[{"type":"text","text":"RECONNECT_REQUIRED: bridge stale"}]}}'`
is ordinary tool-result error content for this ADR; it must be forwarded as a
normal MCP response and must not trigger recovery/replay.

### Lifecycle classifier matrix

The bridge must map every phase-aware result through this matrix. No string-only
`"timed out"` recovery is allowed.

| Condition | Fields | Replay policy | Action |
|---|---|---|---|
| Connect failure before body send | `phase="connect"`, `request_sent=False` | request or notification | Bounded recovery + send original frame once. |
| Write failure before body send | `phase="write"`, `request_sent=False` | request or notification | Bounded recovery + send original frame once. |
| Write failure after partial/unknown send | `phase="write"`, `request_sent=None` | `SAFE` | Bounded recovery + replay. |
| Write failure after partial/unknown send | `phase="write"`, `request_sent=None` | `UNSAFE` | Request-level `MCP_RESPONSE_INTERRUPTED`; no replay. |
| Write failure after partial/unknown send for notification | `phase="write"`, `request_sent=None` | notification | Diagnostic only; no JSON-RPC response and no retry. |
| Warming 503 contract | `phase="http_status"`, `request_sent=True`, `mcp_admitted=False`, `retryable_warming=True` | request or notification | Bounded recovery/retry + send original frame once; gateway proved non-admission. |
| Non-warming HTTP status | `phase="http_status"`, `retryable_warming=False` | any | Request-level `MCP_FORWARD_FAILED`; no bridge recovery. |
| 2xx empty body for notification | `BridgeHttpResponse.body == b""` and payload is notification | notification | Success; write no stdout frame. |
| 2xx empty body for request or batch | `BridgeHttpResponse.body == b""` and payload is not notification | request or batch | Request-level `MCP_FORWARD_FAILED`; no recovery. |
| Slow response with `response_timeout_seconds=None` | no error | any | Keep waiting. |
| Explicit deadline expires during header/body read | `phase in {"response_headers", "response_body"}`, `message` is timeout, finite `response_timeout_seconds` was configured | any | Request-level `MCP_REQUEST_TIMEOUT`; no bridge recovery. |
| EOF/reset/abort during header/body read | `phase in {"response_headers", "response_body"}`, `request_sent=True`, `mcp_admitted=None` | `SAFE` | Bounded recovery + replay. |
| EOF/reset/abort during header/body read | `phase in {"response_headers", "response_body"}`, `request_sent=True`, `mcp_admitted=None` | `UNSAFE` | Request-level `MCP_RESPONSE_INTERRUPTED`; no replay. |
| EOF/reset/abort during header/body read for notification | `phase in {"response_headers", "response_body"}`, `request_sent=True`, `mcp_admitted=None` | notification | Diagnostic only; no JSON-RPC response and no retry. |
| Complete HTTP response but `extract_response_messages(...)` fails | response parse error after HTTP success | any | Request-level `MCP_FORWARD_FAILED`; no recovery. |
| Complete 2xx MCP response contains listed reconnect-required markers | `response_requires_bridge_recovery(response_messages) is True`; gateway proves non-admission/stale bridge before execution | request or notification | Bounded recovery + send original frame once, because the response itself proves request was not normally admitted. |
| Complete 2xx MCP tool-result text contains marker-like text | `result.isError=true` content text contains `RECONNECT_REQUIRED:` but no JSON-RPC error envelope marker | any | Treat as ordinary MCP response content; no recovery/replay. |

### Bridge forwarding algorithm

For each framed upstream message:

1. Inject bridge connection id for `initialize` as today.
2. Compute `BridgeReplayPolicy` from the JSON-RPC payload.
3. Call MCP data-plane HTTP executor.
4. On success, extract JSON/SSE response messages as today and update session id.
5. If response messages require bridge recovery, follow the classifier matrix
   row for reconnect-required markers.
6. On phase-aware error, follow the lifecycle classifier matrix exactly.
7. If a request-level error is emitted and stdout write succeeds, reset/avoid
   recovery budget according to the error-code table and continue the bridge
   loop while the upstream stream remains open.

### JSON-RPC error contract

Bridge-generated request errors keep the existing JSON-RPC shape:

```json
{
  "jsonrpc": "2.0",
  "id": "<original id or null>",
  "error": {
    "code": -32000,
    "message": "<PUBLIC_CODE>: <detail>",
    "data": {"code": "<PUBLIC_CODE>"}
  }
}
```

Public codes:

| Code | When emitted | Recovery budget consumed? |
|---|---|---|
| `MCP_REQUEST_TIMEOUT` | Explicit finite response deadline expires after send. Not used by default because default response wait is unbounded. | No |
| `MCP_RESPONSE_INTERRUPTED` | Request send state is unknown or sent, response failed, admission/execution is unknown, and replay is unsafe. | No |
| `BRIDGE_RECOVERY_EXHAUSTED` | Pre-send, non-admitted, or replay-safe transport recovery exhausts its bounded attempts. | Yes |
| `RECOVERY_FAILED_FOR_REQUEST` | Recovery infrastructure fails before budget exhaustion. | Yes |
| `MCP_FORWARD_FAILED` | Non-recoverable HTTP/protocol forwarding failure. | No |

For notifications with no request id, the bridge must not emit a JSON-RPC
response unless the existing transport contract already permits one. If a
notification fails after send with unknown admission, the bridge emits the
stderr diagnostic specified in the notification table, writes no stdout frame,
does not replay, and continues the forwarding loop. It exits only through
existing loop exits: `should_stop()`, upstream stdin EOF/read failure, or a later
non-notification bridge-level failure.

For JSON-RPC batch arrays, this ADR requires fail-closed replay classification
(`UNSAFE`) and preserves the current singular request-id behavior (`id=None` for
bridge-generated errors). It does not add per-item batch error emission.

### Timeout policy

| Timeout | Scope | Default | Failure meaning |
|---|---|---|---|
| control-plane HTTP timeout | `/status`, `/connect`, `/disconnect`, readiness | existing finite value | gateway/control-plane unavailable |
| MCP connect timeout | socket connect to `/mcp` | current `HTTP_TIMEOUT_SECONDS` value (`10.0` today per source evidence) | gateway unreachable before send |
| MCP write timeout | sending MCP JSON payload | current `HTTP_TIMEOUT_SECONDS` value (`10.0` today per source evidence) | request not known to be sent unless full send completed |
| MCP response timeout | response headers/body after send | `None` | no bridge-imposed response cap |

No tool-specific parameter such as `wait_ms` is parsed. The bridge remains method
agnostic after applying JSON-RPC replay safety.

### Data flow

```text
upstream stdio frame
  -> read_framed_message
  -> inject_bridge_connection_id (initialize only)
  -> bridge_replay_policy(payload)
  -> post_mcp_http phase-aware HTTP
      -> success: extract_response_messages -> maybe reconnect-required recovery -> write_framed_message
      -> not sent: recover_gateway -> re-register -> send original frame once
      -> sent but not admitted: recover_gateway -> re-register -> send original frame once
      -> sent/unknown and safe: recover_gateway -> re-register -> replay
      -> sent/unknown and unsafe: JSON-RPC request error -> continue
      -> sent/unknown notification: emit stderr diagnostic only, write no stdout frame, do not replay, and continue the forwarding loop; exit only through should_stop(), upstream stdin EOF/read failure, or a later non-notification bridge-level failure
```

## Failure behavior

| Scenario | Expected behavior |
|---|---|
| Gateway not listening before MCP request body send | Bounded recovery and send original frame once, preserving current startup behavior. |
| Gateway warming returns contract 503 | `request_sent=True`, `mcp_admitted=False`; bounded recovery/retry and send original frame once. |
| Slow long-poll or slow tool call with no response yet | Bridge waits; no recovery, no replay, no fixed cap. |
| Explicit response deadline configured and expires | Request-level `MCP_REQUEST_TIMEOUT`; bridge stays alive. |
| `tools/call` sent, connection drops before response | Request-level `MCP_RESPONSE_INTERRUPTED`; no automatic replay. |
| `tools/list` sent, connection drops before response | Bounded recovery and replay allowed because the JSON-RPC method is replay-safe. |
| Any method receives reconnect-required MCP response proving non-admission | Bounded recovery and replay allowed. |
| Recovery budget exhausted for pre-send, non-admitted, or replay-safe request | Request-level `BRIDGE_RECOVERY_EXHAUSTED`; bridge continues if possible. |
| Malformed upstream JSON-RPC payload | Preserve existing request-id extraction/error behavior; classify replay as unsafe. |
| JSON-RPC batch payload | Classify replay as unsafe; preserve current singular bridge error behavior. |

## Decision ledger

Every decision below states the simpler alternative and why it fails current
requirements.

| Decision | Evidence | Simpler alternative | Why alternative fails current requirements |
|---|---|---|---|
| Add phase-aware MCP HTTP executor for `/mcp`. | `http_client.py` returns string errors only; `connect_bridge.py` cannot distinguish response wait timeout from connect failure. | Keep `retry_http_request` and parse strings. | String parsing cannot identify send/admission state, so slow responses and unsafe replays remain possible. |
| Track `request_sent` separately from `mcp_admitted`. | HTTP 503 warming is observed after bytes are sent but proves gateway non-admission. | One `dispatched` boolean. | It contradicts 503 warming semantics and leaves implementers guessing whether replay is safe. |
| Keep `retry_http_request` for control plane only. | Docs make readiness/lifecycle bounded and recovery-oriented; current helper fits that. | Replace all HTTP with new executor. | Larger blast radius without current need; control-plane behavior is not the bug. |
| Default MCP response timeout is unbounded. | User requirement forbids assuming 10s or method-specific patch; any slow method can be valid. | Increase timeout to a larger finite value. | Any finite default still fails for slower valid methods. |
| Classify replay safety from JSON-RPC method only. | `tools/call` may have side effects; shared tool contracts do not declare idempotence. | Guess idempotence from tool name/schema. | Heuristics would replay unsafe current tools and violate fail-closed behavior. |
| Fail-closed for JSON-RPC batches in this ADR. | Existing bridge request-id and method extraction are singular. | Infer mixed batch behavior now. | Per-item replay and error semantics are a separate protocol design; guessing would be unsafe. |
| Do not auto-replay sent unsafe requests when admission is unknown. | `_forward_request_with_recovery` currently can replay after recoverable forwarding errors. | Always replay after recovery. | Duplicate side effects are worse than a request-level error. |
| Allow replay after gateway proves non-admission. | Existing reconnect-required response markers already mean the bridge/session was rejected before normal MCP handling. | Ban all sent `tools/call` replay even when rejected before execution. | Would break recoverable stale-session flows where gateway explicitly proves no tool execution occurred. |
| Use request-level JSON-RPC errors for post-send lifecycle failures. | Existing `_jsonrpc_error_response` already serializes bridge errors per request. | Exit the bridge on any post-send failure. | One slow/failed request would unnecessarily kill the session and regress bridge survival behavior. |
| Add Core replay-policy helpers with contracts/doctests. | `AGENTS.md` requires `@pre`/`@post` and doctests before Core functions. | Keep JSON parsing inside shell bridge. | Shell-only parsing duplicates protocol semantics and makes replay rules harder to test exhaustively. |

## Implementation plan

1. **Red tests: generic lifecycle failures**
   - slow `tools/call` response exceeding old bridge timeout does not produce
     `BRIDGE_RECOVERY_EXHAUSTED`;
   - `table_wait`-like long wait is represented as a generic slow MCP request,
     not a Tasca-specific fixture;
   - sent unsafe `tools/call` response interruption is not replayed;
   - pre-send connection failure still recovers and sends the original frame once;
   - JSON-RPC batch payload is replay-unsafe.
2. **Core replay policy**
   - add replay-policy enum/helpers in `tela.core.bridge_protocol`;
   - include `@pre`/`@post` and doctests;
   - cover malformed JSON, notification, batch, known safe methods, `tools/call`,
     and unknown methods.
3. **MCP data-plane HTTP executor**
   - add `tela.commands.bridge_http`;
   - use standard library `http.client` and `urllib.parse`;
   - return typed `BridgeHttpError` with phase, `request_sent`, and
     `mcp_admitted`;
   - preserve bearer header, content type, accept header, and `mcp-session-id`;
   - validate timeout arguments.
4. **Bridge integration**
   - route `post_mcp_message` or its replacement through phase-aware executor;
   - update `_forward_request_with_recovery` to use send/admission state + replay
     policy;
   - keep existing control-plane helpers unchanged;
   - ensure request-level errors reset/avoid recovery budget according to the
     table above.
5. **Docs sync**
   - update `docs/DESIGN.md` discovery/recovery section;
   - update `docs/INTERFACES.md` runtime/transport limits for MCP response
     timeout default;
   - update `docs/USAGE.md` troubleshooting text for long-running tools if
     behavior is user-visible.
6. **Verification**
   - run targeted unit tests;
   - run full repo tests;
   - run `invar guard`;
   - run docs/tests parity scans used by this repo if present.

## Acceptance criteria

The implementation is accepted only if all criteria pass:

1. No MCP data-plane request with no explicit response deadline fails solely
   because it exceeded the old fixed bridge HTTP timeout.
2. No code path special-cases Tasca, `table_wait`, `wait_ms`, or any specific
   downstream tool.
3. Connection refused and contract 503 warming still trigger bounded recovery
   when the request was not sent or gateway proves non-admission.
4. Sent `tools/call` is never automatically replayed when admission/execution is
   unknown.
5. Safe protocol requests (`initialize`, `ping`, `tools/list`) retain bounded
   recovery/replay behavior where recovery is semantically safe.
6. Request-level lifecycle failures return JSON-RPC errors with stable
   `error.data.code` values.
7. The bridge remains alive after request-level errors when stdout write
   succeeds and the upstream stream remains open.
8. JSON-RPC batches are replay-unsafe unless a future ADR supersedes this one.
9. Empty 2xx notification responses write no stdout frame and count as success.
10. Post-send notification failures with unknown admission emit the specified
    stderr diagnostic, write no stdout frame, do not replay, and continue the
    forwarding loop until an existing loop-exit condition occurs.
11. Empty 2xx request or batch responses produce `MCP_FORWARD_FAILED` and no
    recovery.
12. Tool-result text containing reconnect-like markers is forwarded as ordinary
    MCP content and never authorizes unsafe replay.
13. `invar guard` passes, including Core contracts and doctests.

## Validation matrix

| Test | Layer | Expected proof |
|---|---|---|
| `bridge_replay_policy` doctests and unit tests | Core | safe/unsafe classification is deterministic and fail-closed for malformed JSON and batches. |
| phase-aware executor connect failure | Shell unit | `BridgeHttpError(phase="connect", request_sent=False, mcp_admitted=None)` and recovery eligibility. |
| phase-aware executor partial write | Shell unit | unknown send state is represented as `request_sent=None` and unsafe request is not replayed. |
| phase-aware executor slow response | Shell unit | `response_timeout_seconds=None` waits without entering recovery; no synthetic timeout error. |
| explicit response deadline | Shell unit | finite deadline emits `MCP_REQUEST_TIMEOUT`, not recovery exhaustion. |
| 503 warming body | Shell unit | warming contract produces `request_sent=True`, `mcp_admitted=False`, `retryable_warming=True`. |
| pre-send recovery | Bridge unit | connection refused before send recovers and sends the original frame once budget allows. |
| unsafe post-send interruption | Bridge unit | `tools/call` is not replayed; JSON-RPC error is written; recovery budget not consumed. |
| post-send notification interruption | Bridge unit | notification emits specified stderr diagnostic, writes no stdout frame, does not replay, and continues loop. |
| safe post-send recovery | Bridge unit | `tools/list` or `ping` may recover/replay under bounded budget. |
| reconnect-required response | Bridge unit | gateway non-admission marker recovers/replays without treating it as arbitrary tool replay. |
| tool-result marker-like text | Bridge unit | `result.isError` text containing `RECONNECT_REQUIRED:` is forwarded normally and does not recover/replay. |
| empty 2xx notification response | Bridge unit | notification succeeds and writes no stdout frame. |
| empty 2xx request response | Bridge unit | request gets `MCP_FORWARD_FAILED`; no recovery. |
| table-wait regression via generic slow call | Integration or black-box | long wait no longer produces `BRIDGE_RECOVERY_EXHAUSTED`. |
| existing readiness recovery tests | Regression | `/status`, `/connect`, `/disconnect`, warming 503 behavior remains unchanged. |
| full guard | Repo gate | `invar guard` and full tests pass. |

## Complexity cost receipt

| Added | Simplest Alternative | Why Alternative Fails Current Requirements |
|---|---|---|
| Phase-aware MCP HTTP executor | Reuse `retry_http_request` string errors | Cannot know send/admission state; cannot distinguish slow response from connect failure. |
| Separate `request_sent` and `mcp_admitted` fields | Single `dispatched` boolean | HTTP 503 warming is sent-but-not-admitted; one boolean creates contradictory replay rules. |
| MCP response timeout policy | Raise fixed timeout | Any fixed default still breaks valid slower methods. |
| Replay policy in Core | Replay every recovered request | Duplicates side-effecting `tools/call` operations. |
| Batch fail-closed rule | Ignore batches | Mixed request/notification arrays would otherwise inherit unsafe singular assumptions. |
| Request-level lifecycle error codes | Collapse into `BRIDGE_RECOVERY_EXHAUSTED` | Misreports normal slow requests as bridge recovery failures and burns recovery budget. |
| Documentation updates in DESIGN/INTERFACES/USAGE | Only add tests | Runtime contract remains undocumented and future patches can reintroduce fixed caps. |

## Open questions

None for this ADR. A future public option for an explicit MCP response deadline
or batch-aware replay may be added later, but this fix does not require either
and must not wait for them.
