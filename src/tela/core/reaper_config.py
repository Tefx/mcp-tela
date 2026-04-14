"""Core runtime model for connection reaper policy.

Lifecycle contract (idle_recovery.idle_contract):
- Live sessions are NOT idle-reaped by default (native_idle_ttl_seconds=0)
- The reaper only removes connections whose upstream session is gone (orphan
  detection via session probe on conn_* IDs)
- ``idle_timeout`` governs process shutdown only after the idle manager's
  connection count reaches zero
- Explicit operator overrides (native_idle_ttl_seconds > 0) still reap
  stale native connections when configured
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReaperPolicyConfig(BaseModel):
    """Connection reaper policy exposed through runtime config.

    Attributes:
        sweep_interval_seconds: How often the reaper runs a sweep cycle.
        native_idle_ttl_seconds: Max idle time for native (non-bridge)
            connections before they are reaped. ``0`` (default) disables
            native idle-reaping — only orphaned connections (session gone)
            are removed. Set > 0 to enable TTL-based native reaping.
        bridge_idle_ttl_seconds: Max idle time for bridge connections
            before they are reaped. ``0`` disables bridge reaping.
    """

    sweep_interval_seconds: float = Field(default=30.0, ge=0.0)
    native_idle_ttl_seconds: float = Field(default=0.0, ge=0.0)
    bridge_idle_ttl_seconds: float = Field(default=900.0, ge=0.0)
