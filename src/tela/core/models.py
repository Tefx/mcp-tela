"""Core configuration and runtime model contracts for tela.

This file defines type-only model surfaces used by configuration parsing,
validation contracts, and runtime boundaries. It intentionally contains no
business-rule logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypedDict

from typing import Literal

from pydantic import BaseModel, Field

from tela.core.contracts import post, pre
from tela.core.reaper_config import ReaperPolicyConfig


# --- Enumerations ---


class Posture(str, Enum):
    """Tool posture levels used by profile ceilings."""

    NONE = "none"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"
    DESTRUCTIVE = "destructive"


class AuthMode(str, Enum):
    """Authentication mode for gateway startup."""

    TOKEN = "token"
    OPEN = "open"


class GatewayTransport(str, Enum):
    """Gateway transport contract for runtime startup.

    ``HTTP`` is the MCP Streamable HTTP transport (spec 2025-03-26+).
    ``SSE`` is the legacy SSE transport, retained for backward compatibility.
    """

    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"


class DefaultProfileResolutionStatus(str, Enum):
    """Outcome contract for open-mode default-profile resolution."""

    RESOLVED = "resolved"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


class EnforcementVerdict(str, Enum):
    """Outcome of the enforcement chain for a tool call."""

    ALLOW = "allow"
    DENY = "deny"


class AuditLevel(str, Enum):
    """Audit logging granularity level."""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


# --- Server Configuration ---


class ToolOverride(BaseModel):
    """Per-tool override within a server config."""

    family: str | None = None
    posture: Posture | None = None


class ServerConfig(BaseModel):
    """Configuration for a single downstream server.

    Transport selection:
    - ``command`` set → stdio
    - ``url`` set (default) → Streamable HTTP (MCP 2025-03-26+)
    - ``url`` set + ``transport == "sse"`` → SSE (legacy)

    Tool-prefix acceptance semantics:
    - ``tool_prefix`` is applied during registration/resolution, never at
      downstream call time.
    - ``tool_overrides`` remain keyed by raw downstream tool names even when
      ``tool_prefix`` changes the exposed upstream name.
    - ``tool_prefix=None`` preserves backward-compatible exposed names.
    - ``tool_prefix="tela."`` is reserved-input and must be rejected.
    - Prefix-only changes count as tool-surface changes because upstream
      discovery, conflict detection, reload diffs, and observability are keyed
      to the exposed tool set.
    """

    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    transport: Literal["http", "sse"] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    family: str | None = None
    tool_prefix: str | None = None
    # NOTE: Acceptance contract only. Prefix is part of exposed-name resolution
    # and must not be applied lazily at tools/call routing time.
    tool_overrides: dict[str, ToolOverride] = Field(default_factory=dict)
    rewrite_descriptions: bool = False
    # NOTE: When True, backtick-quoted raw tool names in tool descriptions
    # are replaced with their prefixed exposed names during resolution.
    # Only rewrites references to tools within the SAME server's tool set.
    # NOTE: Override keys remain raw downstream tool names, not prefixed
    # exposed names, so routing and classification continue to bind to the
    # downstream-advertised inventory.
    default_posture: Posture = Posture.NONE
    instructions: bool | str | None = None


# --- Profile Configuration ---


class ProfileToolOverrides(BaseModel):
    """Per-tool overrides within a profile (by family)."""

    overrides: dict[str, "EnforcementVerdict"] = Field(default_factory=dict)


class ProfileConfig(BaseModel):
    """Contract shape for a single profile configuration.

    ``capabilities`` is the canonical field for tool-family posture ceilings.
    The legacy ``tools`` alias has been removed (hard cut).

    `default` marks the profile as the open-mode fallback candidate when the
    CLI does not supply `--default-profile`.

    Examples:
        >>> ProfileConfig(name="dev", capabilities={"fs": Posture.READ_ONLY}).capabilities["fs"]
        <Posture.READ_ONLY: 'read_only'>
    """

    model_config = {"extra": "forbid"}

    name: str
    capabilities: dict[str, Posture] = Field(default_factory=dict)
    tool_overrides: dict[str, ProfileToolOverrides] = Field(default_factory=dict)
    default: bool = False


# --- Auth and Audit Configuration ---


class AuthConfig(BaseModel):
    """Authentication contract shape."""

    mode: AuthMode = AuthMode.TOKEN
    secrets: list[str] = Field(default_factory=list)


class AuditConfig(BaseModel):
    """Audit logging contract shape."""

    level: AuditLevel = AuditLevel.L2
    output: str = "~/.tela/audit.jsonl"


# --- Top-level Configuration ---


class TelaConfig(BaseModel):
    """Top-level configuration contract shape used by Core and Shell."""

    servers: dict[str, ServerConfig] = Field(default_factory=dict)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    reaper: ReaperPolicyConfig = Field(default_factory=ReaperPolicyConfig)
    resolved_default_profile: str | None = None


class CapabilityToken(BaseModel):
    """Token presented by upstream client at connection time.

    The canonical identity field is ``profile_id``. The legacy ``profile_name``
    alias is rejected fail-closed: tokens bearing ``profile_name`` instead of
    ``profile_id`` will be rejected before authorization.

    Examples:
        >>> tok = CapabilityToken(token_id="tok_1", profile_id="dev", issued_at="2026-01-01T00:00:00Z", expires_at="2026-12-31T23:59:59Z", signature="abc")
        >>> tok.profile_id
        'dev'
    """

    token_id: str
    profile_id: str
    persona_ref: str | None = None
    instance_id: str | None = None
    max_depth: int | None = None
    issued_at: str
    expires_at: str
    signature: str


# --- Runtime Types ---


class ResolvedTool(BaseModel):
    """A tool after family mapping, name exposure, and classification.

    Acceptance semantics for tool-prefix work:
    - ``name`` is the final exposed upstream tool name used by tools/list,
      tools/call lookup, conflict detection, and reload diff semantics.
    - ``raw_name`` stores the downstream-advertised tool name before any
      configured prefix is applied.
    - Downstream routing continues to target ``raw_name``; callers must not
      derive routing names by stripping ``name`` at call time.
    - Key-path observability for this feature must preserve
      ``server_name + raw_name + name`` together anywhere audit/status/log
      evidence is claimed.
    """

    name: str
    raw_name: str | None = None
    # NOTE: ``None`` preserves compatibility for pre-contract instances; the
    # authoritative interface for prefixed resolution expects this to carry the
    # downstream-advertised name.
    server_name: str
    family: str
    posture: Posture | None = None
    schema_: dict = Field(default_factory=dict)
    description: str = ""
    annotations: dict | None = None
    title: str | None = None
    output_schema: dict | None = None


class ProviderInfo(TypedDict):
    """Per-provider summary returned by tela_list_providers."""

    name: str
    status: str  # "connected" | "disconnected" | "failed"
    tool_prefix: str | None
    tool_count: int
    tool_names: list[str]  # post-enforcement-filter exposed names


class ConnectionContext(BaseModel):
    """Per-connection state for an upstream client.

    Recovery-critical fields (idle reconnect / explicit re-initialize):
    - ``init_mode``: Records which auth path established this connection
      (TOKEN or OPEN). Cannot be derived from empty initialize — required
      for correct reconnect semantics.
    - ``client_info_snapshot``: Preserves the clientInfo dict from MCP
      initialize. For token mode, carries the original capability-token
      fields needed for revalidation. Without this snapshot, reconnect
      cannot re-derive the token validation context.
    - ``bridge_connection_id``: Records the HTTP /connect bridge
      connection ID when the upstream client connected via bridge. Allows
      the gateway to correlate initialized MCP sessions back to their
      /connect-registration.
    """

    connection_id: str
    profile_name: str
    connected_at: str
    tool_call_count: int = 0
    last_activity: str = ""
    init_mode: AuthMode | None = None
    client_info_snapshot: dict[str, str] | None = None
    bridge_connection_id: str | None = None


class EnforcementResult(BaseModel):
    """Result of the enforcement chain for a single tool call."""

    verdict: EnforcementVerdict
    denied_by: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class MetaField(BaseModel):
    """Per-call tracing metadata injected by anima.

    Validation policy: tela accepts any dict as _meta from tool call
    arguments and stores it as-is. The MetaField model is used for
    typed access to known fields only.
    """

    trace_id: str
    event_id: str | None = None
    idempotency_key: str | None = None
    instance_id: str | None = None
    persona_id: str | None = None


class AuditEntry(BaseModel):
    """A single audit log entry."""

    timestamp: str
    level: AuditLevel
    connection_id: str
    profile_name: str
    tool_name: str
    server_name: str
    verdict: EnforcementVerdict
    denied_by: str | None = None
    error_code: str | None = None
    latency_ms: float | None = None
    param_hash: str | None = None
    request_content: dict | None = None
    response_content: dict | None = None
    meta: MetaField | None = None


class GatewayStatus(BaseModel):
    """Runtime status of the gateway."""

    uptime_seconds: float
    server_count: int
    connected_servers: list[str] = Field(default_factory=list)
    active_connections: int
    profile_count: int
    total_tool_calls: int
    # Lifecycle and diagnostic fact fields from BRIDGE_STATUS_FACT_FIELDS contract
    state: str | None = None
    discovery_source: (
        Literal["lockfile", "autostart", "explicit_server", "startup_follower"] | None
    ) = None
    config_path: str | None = None
    requested_config_path: str | None = None
    config_mismatch: bool = False
    degraded_reason: str | None = None


class LockfileData(BaseModel):
    """Persisted gateway lockfile contract.

    Required fields:
    - pid: Process id of the running gateway.
    - host: Host bound by gateway startup.
    - port: Port bound by gateway startup.
    - token: Bearer token stored for local client reuse.
    - started_at: ISO-8601 start timestamp.
    - config_path: Source config path used at startup.
    - version: Gateway/runtime contract version.
    """

    pid: int
    host: str
    port: int
    token: str
    started_at: str
    config_path: str
    version: str


class HealthResponse(BaseModel):
    """Liveness response for `GET /health`."""

    status: Literal["ok"] = "ok"
    pid: int


class StatusResponse(GatewayStatus):
    """Full runtime status response for `GET /status`."""

    connections: list[ConnectionContext] = Field(default_factory=list)
    audit_entries: list[AuditEntry] = Field(default_factory=list)


class ConnectRequest(BaseModel):
    """Registration payload for bridge connection endpoints."""

    connection_id: str


class DisconnectRequest(BaseModel):
    """Deregistration payload for bridge connection endpoints."""

    connection_id: str


class TelaError(BaseModel):
    """Structured error response."""

    code: str
    message: str
    details: dict | None = None


# --- Contract dataclasses ---


@dataclass(frozen=True)
class RuntimeBindingContract:
    """CLI-to-gateway runtime binding authority for `tela start`.

    Contract semantics:
    - `transport=GatewayTransport.STDIO` when CLI omits `--port`.
    - `transport=GatewayTransport.HTTP` when CLI provides `--port` (default remote).
    - `transport=GatewayTransport.SSE` when CLI provides `--port --transport sse`.
    - `cli_default_profile` reflects `--default-profile` without guessing.
    """

    config_path: str
    transport: GatewayTransport
    port: int | None
    cli_default_profile: str | None


@dataclass(frozen=True)
class InitializeProfileBinding:
    """Explicit upstream initialize profile-binding contract for open mode.

    `status` captures acceptance outcome. If status is not `RESOLVED`,
    `resolved_default_profile` must remain `None` and initialization must be
    rejected by the shell boundary.
    """

    status: DefaultProfileResolutionStatus
    resolved_default_profile: str | None


@dataclass(frozen=True)
class TokenInitBinding:
    """Token-mode initialize binding contract.

    Binds a capability token validation result to the connection's profile.
    Shell must reject initialization if `token_result.verdict` is DENY.

    Examples:
        >>> from tela.core.models import EnforcementResult, EnforcementVerdict
        >>> result = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        >>> binding = TokenInitBinding(token_result=result, profile_name="dev")
        >>> binding.profile_name
        'dev'
        >>> binding.token_result.verdict
        <EnforcementVerdict.ALLOW: 'allow'>
    """

    token_result: EnforcementResult
    profile_name: str
