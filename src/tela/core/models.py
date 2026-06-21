"""Core configuration and runtime model contracts for tela.

This file defines type-only model surfaces used by configuration parsing,
validation contracts, and runtime boundaries. It intentionally contains no
business-rule logic.
"""

# @invar:allow file_size: configuration schemas, token models, and shared runtime payloads are the authoritative Pydantic contract surfaces for tela; splitting them here would scatter the canonical model boundary across multiple modules.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import TypedDict, Any

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tela.core.contracts import post, pre
from tela.core.errors import NESTED_TELA_PREFIX_REQUIRED
from tela.core.reaper_config import ReaperPolicyConfig


_CAPABILITY_TOKEN_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_NESTED_GATEWAY_PREFIX_REQUIRED_MESSAGE = (
    f"{NESTED_TELA_PREFIX_REQUIRED}: nested_gateway true requires a non-empty tool_prefix"
)


@pre(lambda value: isinstance(value, str) and len(value) > 0)
@post(lambda result: isinstance(result, str) and len(result) > 0)
def _validate_capability_token_datetime(value: str) -> str:
    """Validate canonical CapabilityToken date-time fields.

    Examples:
        >>> _validate_capability_token_datetime("2026-01-01T00:00:00Z")
        '2026-01-01T00:00:00Z'
    """

    if _CAPABILITY_TOKEN_DATETIME_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "must be a canonical date-time with timezone (RFC3339/JSON-schema date-time)"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("must be a valid date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError("must include timezone information")
    return value


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

    Examples:
        >>> try:
        ...     ServerConfig(name="child", command="cmd", exclude_tool=["x"])
        ... except Exception as exc:
        ...     print("exclude_tools" in str(exc))
        True
        >>> try:
        ...     ServerConfig(name="child", command="cmd", nested_gateway=True)
        ... except Exception as exc:
        ...     print("NESTED_TELA_PREFIX_REQUIRED" in str(exc))
        True
        >>> ServerConfig(name="child", command="cmd", tool_prefix="host_", nested_gateway=True).tool_prefix
        'host_'
    """

    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    transport: Literal["http", "sse"] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    family: str | None = None
    tool_prefix: str | None = None
    exclude_tools: list[str] = Field(default_factory=list, strict=True)
    nested_gateway: bool = Field(default=False, strict=True)
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

    @field_validator("tool_prefix")
    @classmethod
    @post(
        lambda result: (
            result is None
            or not (
                result.startswith("tela.")
                or result.startswith("tela_")
                or "." in result
            )
        )
    )
    def _reject_reserved_prefix(cls, v: str | None) -> str | None:
        """Reject tool_prefix values that use the reserved tela namespace or dotted syntax.

        Per USAGE.md §Tool Prefix Configuration and ServerConfig contract:
        tool_prefix starting with "tela." or "tela_" is reserved for
        built-in surfaces and must be rejected at construction time.
        Dotted syntax is also invalid.
        This model-level validation mirrors the config-level check in
        validate_config() (config.py) and the resolve-time check in
        resolve_tools() (family.py) for defense in depth.
        """
        if v == "":
            raise ValueError("tool_prefix cannot be empty")
        if v is not None:
            if v.startswith("tela.") or v.startswith("tela_"):
                raise ValueError(
                    f"ServerConfig.tool_prefix '{v}' uses reserved 'tela.'/'tela_' namespace"
                )
            if "." in v:
                raise ValueError(
                    f"ServerConfig.tool_prefix '{v}' contains invalid dotted syntax; use snake_case"
                )
        return v

    @model_validator(mode="before")
    @classmethod
    @pre(lambda cls, data: data is not None)
    @post(
        lambda result: (
            not isinstance(result, dict)
            or not result.get("nested_gateway", False)
            or bool(result.get("tool_prefix", None))
        )
    )
    def _reject_aliases_and_shorthand(cls, data: Any) -> Any:
        """Reject alias fields and nested-gateway shorthand before coercion."""
        if isinstance(data, dict):
            for alias in ("exclude_tool", "excluded_tools", "hide_tools"):
                if alias in data:
                    raise ValueError(f"Alias '{alias}' is not allowed, use 'exclude_tools'")
            nested = data.get("nested_gateway", False)
            # A 'None' or empty 'tool_prefix' string both trigger this requirement.
            prefix = data.get("tool_prefix", None)
            if nested and not prefix:
                raise ValueError(_NESTED_GATEWAY_PREFIX_REQUIRED_MESSAGE)
        return data

    @model_validator(mode="after")
    @pre(lambda self: isinstance(self.name, str) and len(self.name) > 0)
    @post(lambda result: not result.nested_gateway or bool(result.tool_prefix))
    def _validate_nested_gateway_requires_prefix(self) -> ServerConfig:
        """Guarantee explicit nested gateways keep a non-empty prefix."""
        if self.nested_gateway and not self.tool_prefix:
            raise ValueError(_NESTED_GATEWAY_PREFIX_REQUIRED_MESSAGE)
        return self


# --- Profile Configuration ---


class ProfileToolOverrides(BaseModel):
    """Per-tool overrides within a profile (by family)."""

    overrides: dict[str, "EnforcementVerdict"] = Field(default_factory=dict)


class ProfileConfig(BaseModel):
    """Contract shape for a single profile configuration.

    ``capabilities`` is the canonical field for tool-family posture ceilings.
    No legacy profile-key alias remains active.

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

    The canonical identity field is ``profile_id``. Legacy alias fields are
    rejected fail-closed before authorization.

    Examples:
        >>> tok = CapabilityToken(token_id="tok_1", profile_id="dev", persona_ref="persona.dev", instance_id="inst-1", issued_at="2026-01-01T00:00:00Z", expires_at="2026-12-31T23:59:59Z", token_version="0.1.0", signature="abc")
        >>> tok.profile_id
        'dev'
    """

    model_config = {"extra": "forbid"}

    token_id: str = Field(pattern=r"^tok_")
    profile_id: str
    persona_ref: str
    instance_id: str
    max_depth: int | None = Field(default=None, ge=0, strict=True)
    issued_at: str
    expires_at: str
    token_version: Literal["0.1.0"]
    signature: str

    @field_validator("issued_at", "expires_at")
    @classmethod
    @pre(lambda cls, value: isinstance(value, str) and len(value) > 0)
    @post(lambda result: isinstance(result, str) and len(result) > 0)
    def _validate_canonical_datetime(cls, value: str) -> str:
        return _validate_capability_token_datetime(value)


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

    provider_name: str
    profile_id: str
    status: str  # "connected" | "disconnected" | "failed"
    tool_prefix: str | None
    tool_count: int
    tool_names: list[str]  # post-enforcement-filter exposed names


class ProfileInfo(TypedDict):
    """Per-profile summary returned by tela_list_profiles.

    Canonical schema (hard cut): ``profile_id``, ``capabilities``, ``default``.
    No retired payload keys are emitted.
    """

    profile_id: str
    capabilities: dict[str, str]
    default: bool


class ConnectionContext(BaseModel):
    """Per-connection state for an upstream client.

    The canonical identity field is ``profile_id``. All shared
    runtime/audit/tool-facing surfaces bind canonical ``profile_id`` only.

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

    model_config = {"extra": "forbid"}

    connection_id: str
    profile_id: str
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
    """A single audit log entry.

    The canonical identity field is ``profile_id`` and audit entries bind that
    canonical identity only.
    """

    model_config = {"extra": "forbid"}

    timestamp: str
    level: AuditLevel
    connection_id: str
    profile_id: str
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

    model_config = {"extra": "forbid"}

    server_name: str = Field(strict=True)


class DisconnectRequest(BaseModel):
    """Deregistration payload for bridge connection endpoints."""

    model_config = {"extra": "forbid"}

    connection_id: str = Field(strict=True)


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

    The canonical identity field is ``profile_id`` and all shared surfaces bind
    that canonical identity only.

    Examples:
        >>> from tela.core.models import EnforcementResult, EnforcementVerdict
        >>> result = EnforcementResult(verdict=EnforcementVerdict.ALLOW)
        >>> binding = TokenInitBinding(token_result=result, profile_id="dev")
        >>> binding.profile_id
        'dev'
        >>> binding.token_result.verdict
        <EnforcementVerdict.ALLOW: 'allow'>
    """

    token_result: EnforcementResult
    profile_id: str
