"""Core configuration and runtime model contracts for tela.

This file defines type-only model surfaces used by configuration parsing,
validation contracts, and runtime boundaries. It intentionally contains no
business-rule logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator

from tela.core.contracts import post, pre


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
    - ``url`` set + ``transport == "http"`` → Streamable HTTP (MCP 2025-03-26+)
    - ``url`` set (default) → SSE (legacy, backward-compatible)
    """

    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    transport: Literal["http"] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    family: str | None = None
    tool_overrides: dict[str, ToolOverride] = Field(default_factory=dict)
    default_posture: Posture = Posture.NONE


# --- Profile Configuration ---


class ProfileToolOverrides(BaseModel):
    """Per-tool overrides within a profile (by family)."""

    overrides: dict[str, "EnforcementVerdict"] = Field(default_factory=dict)


@pre(lambda raw: raw is None or isinstance(raw, Mapping))
@post(
    lambda result: (
        isinstance(result, dict) and ("capabilities" in result or "tools" not in result)
    )
)
def normalize_profile_config_aliases(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize migration aliases for ``ProfileConfig`` inputs.

    Migration contract:
    - ``tools`` is accepted as an alias for ``capabilities``.
    - If both are provided they must be equal.

    Examples:
        >>> normalize_profile_config_aliases({"name": "dev", "tools": {"fs": Posture.READ_ONLY}})["capabilities"]["fs"]
        <Posture.READ_ONLY: 'read_only'>
        >>> normalize_profile_config_aliases({"name": "dev", "capabilities": {"fs": Posture.READ_WRITE}})["capabilities"]["fs"]
        <Posture.READ_WRITE: 'read_write'>
        >>> normalize_profile_config_aliases({"tools": {"fs": Posture.READ_ONLY}, "capabilities": {"fs": Posture.READ_WRITE}})
        Traceback (most recent call last):
        ...
        ValueError: ProfileConfig.tools and ProfileConfig.capabilities must match when both are provided

    Args:
        raw: Raw profile mapping before pydantic field validation.

    Returns:
        Normalized dict using ``capabilities`` as canonical key.

    Raises:
        ValueError: If both alias keys are provided with different values.
    """

    normalized: dict[str, Any] = {} if raw is None else dict(raw)
    capabilities = normalized.get("capabilities")
    tools = normalized.get("tools")

    if capabilities is None and tools is not None:
        normalized["capabilities"] = tools
    elif capabilities is not None and tools is not None and capabilities != tools:
        raise ValueError(
            "ProfileConfig.tools and ProfileConfig.capabilities must match when both are provided"
        )

    return normalized


class ProfileConfig(BaseModel):
    """Contract shape for a single profile configuration.

    Migration contract: ``capabilities`` is canonical. ``tools`` remains an
    accepted alias during migration.

    `default` marks the profile as the open-mode fallback candidate when the
    CLI does not supply `--default-profile`.

    Examples:
        >>> ProfileConfig(name="dev", capabilities={"fs": Posture.READ_ONLY}).capabilities["fs"]
        <Posture.READ_ONLY: 'read_only'>
        >>> ProfileConfig(name="dev", tools={"fs": Posture.READ_WRITE}).capabilities["fs"]
        <Posture.READ_WRITE: 'read_write'>
    """

    name: str
    capabilities: dict[str, Posture] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("capabilities", "tools"),
    )
    tool_overrides: dict[str, ProfileToolOverrides] = Field(default_factory=dict)
    default: bool = False

    @model_validator(mode="before")
    @classmethod
    @pre(lambda cls, data: cls is ProfileConfig and (data is None or isinstance(data, Mapping) or isinstance(data, dict) or isinstance(data, object)))
    @post(lambda result: result is not None)
    def _normalize_aliases(cls, data: Any) -> Any:
        if isinstance(data, Mapping) or data is None:
            return normalize_profile_config_aliases(data)
        return data

    @property
    @post(lambda result: isinstance(result, dict))
    def tools(self) -> dict[str, Posture]:
        """Backward-compatible alias for ``capabilities`` during migration.

        Examples:
            >>> ProfileConfig(name="dev", capabilities={"fs": Posture.READ_WRITE}).tools["fs"]
            <Posture.READ_WRITE: 'read_write'>
        """

        return self.capabilities


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
    resolved_default_profile: str | None = None


class CapabilityToken(BaseModel):
    """Token presented by upstream client at connection time.

    Examples:
        >>> tok = CapabilityToken(token_id="tok_1", profile_name="dev", issued_at="2026-01-01T00:00:00Z", expires_at="2026-12-31T23:59:59Z", signature="abc")
        >>> tok.profile_name
        'dev'
    """

    token_id: str
    profile_name: str
    persona_ref: str | None = None
    instance_id: str | None = None
    max_depth: int | None = None
    issued_at: str
    expires_at: str
    signature: str


# --- Runtime Types ---


class ResolvedTool(BaseModel):
    """A tool after family mapping and classification."""

    name: str
    server_name: str
    family: str
    posture: Posture | None = None
    schema_: dict = Field(default_factory=dict)


class ConnectionContext(BaseModel):
    """Per-connection state for an upstream client."""

    connection_id: str
    profile_name: str
    connected_at: str
    tool_call_count: int = 0


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

    status: str = "ok"
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
