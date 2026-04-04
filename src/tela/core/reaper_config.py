"""Core runtime model for connection reaper policy."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReaperPolicyConfig(BaseModel):
    """Connection reaper policy exposed through runtime config."""

    sweep_interval_seconds: float = Field(default=30.0, ge=0.0)
    native_idle_ttl_seconds: float = Field(default=120.0, ge=0.0)
    bridge_idle_ttl_seconds: float = Field(default=900.0, ge=0.0)
