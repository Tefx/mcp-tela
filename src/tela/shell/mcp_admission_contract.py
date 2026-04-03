"""Type-level contract for transient ``/mcp`` admission rejection.

This module intentionally contains only shared type definitions and contract
stubs for downstream implementation work. It freezes the external shape that
bridge retry logic must consume when the gateway is reachable but not yet ready
for MCP admission during convergence.

Current-slice constraints:
- The contract applies only to ``POST /mcp`` admission rejection.
- Retry is authorized only when the gateway emits this machine-readable
  contract; HTTP ``503`` alone is insufficient.
- ``POST /connect`` remains registration plumbing only and is not a readiness
  authority, cache, or proof of MCP admission readiness.
- ``gateway_state`` uses the existing readiness vocabulary and remains
  ``"warming"`` for this slice.
- No new public lifecycle state is introduced here.
"""

from __future__ import annotations

from typing import Literal, TypedDict, TypeAlias

TransientAdmissionErrorCode: TypeAlias = Literal["ADMISSION_REJECTED_WARMING"]
TransientAdmissionGatewayState: TypeAlias = Literal["warming"]
RetryAuthorizationBasis: TypeAlias = Literal["gateway_signal"]
RetryExpectation: TypeAlias = Literal["bounded"]


class McpAdmissionRetryContract(TypedDict):
    """Machine-readable retry authorization emitted by the gateway.

    Consumers must derive retry eligibility from these gateway-authored fields,
    not from bridge-local guesswork about whether a transient failure might be
    safe to retry.
    """

    authorized: Literal[True]
    basis: RetryAuthorizationBasis
    expectation: RetryExpectation


class McpAdmissionTransient503(TypedDict):
    """Gateway-authored transient 503 response for not-ready-yet MCP admission.

    Consumers must treat this shape as the retry authority for the current
    slice. HTTP 503 without this contract is insufficient to justify retry.
    This projection does not create a second readiness authority; it reflects
    the gateway runtime lifecycle state already owned by ``GET /status``.
    """

    error: str
    code: TransientAdmissionErrorCode
    transient: Literal[True]
    retry: McpAdmissionRetryContract
    gateway_state: TransientAdmissionGatewayState
