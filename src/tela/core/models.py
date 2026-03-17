"""Core configuration and runtime model contracts for tela.

This file defines type-only model surfaces used by configuration parsing,
validation contracts, and runtime boundaries. It intentionally contains no
business-rule logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field


# --- Enumerations ---


class Posture(str, Enum):
    """Tool posture levels used by profile ceilings."""

    NONE = "none"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"
    DESTRUCTIVE = "destructive"


class SideEffectPolicy(str, Enum):
    """Profile side-effect policy mode."""

    ALLOW = "allow"
    READ_ONLY = "read_only"


class AuthMode(str, Enum):
    """Authentication mode for gateway startup."""

    TOKEN = "token"
    OPEN = "open"


class GatewayTransport(str, Enum):
    """Gateway transport contract for runtime startup."""

    STDIO = "stdio"
    SSE = "sse"


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
    """Configuration for a single downstream server."""

    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    family: str | None = None
    tool_overrides: dict[str, ToolOverride] = Field(default_factory=dict)
    default_posture: Posture = Posture.NONE


# --- Profile Configuration ---


class ProfileToolOverrides(BaseModel):
    """Per-tool overrides within a profile (by family)."""

    overrides: dict[str, "EnforcementVerdict"] = Field(default_factory=dict)


class ProfileConfig(BaseModel):
    """Contract shape for a single profile configuration.

    `default` marks the profile as the open-mode fallback candidate when the
    CLI does not supply `--default-profile`.
    """

    name: str
    tools: dict[str, Posture] = Field(default_factory=dict)
    tool_overrides: dict[str, ProfileToolOverrides] = Field(default_factory=dict)
    side_effect_policy: SideEffectPolicy = SideEffectPolicy.ALLOW
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
    resolved_default_profile: str | None = None


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
    - `transport=GatewayTransport.SSE` when CLI provides `--port`.
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
