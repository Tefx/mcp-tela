"""Core configuration model contracts for tela.

This file defines type-only model surfaces used by configuration parsing and
validation contracts. It intentionally contains no business-rule logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field


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


class ProfileConfig(BaseModel):
    """Contract shape for a single profile configuration.

    `default` marks the profile as the open-mode fallback candidate when the
    CLI does not supply `--default-profile`.
    """

    name: str
    tools: dict[str, Posture] = Field(default_factory=dict)
    side_effect_policy: SideEffectPolicy = SideEffectPolicy.ALLOW
    default: bool = False


class AuthConfig(BaseModel):
    """Authentication contract shape."""

    mode: AuthMode = AuthMode.TOKEN
    secrets: list[str] = Field(default_factory=list)


class TelaConfig(BaseModel):
    """Top-level configuration contract shape used by Core and Shell."""

    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    resolved_default_profile: str | None = None


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
