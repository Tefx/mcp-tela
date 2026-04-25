"""Tool posture classification from available sources.

Determines a tool's posture from explicit overrides, MCP annotations,
or leaves unclassified for the caller to apply a server default.
"""

from __future__ import annotations

import json
from enum import Enum

from pydantic import BaseModel, Field

from tela.core.contracts import post, pre
from tela.core.models import Posture, ServerConfig


# --------------------------------------------------------------------
# ADR-008: Enumerations
# --------------------------------------------------------------------


class AttachmentDisplayState(str, Enum):
    """ADR-008 display state for client attachments.

    Corresponds to user-visible attachment lifecycle states.
    """

    UNKNOWN = "unknown"
    STARTED = "started"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE_CANDIDATE = "stale_candidate"
    RECOVERING = "recovering"
    EXITED = "exited"


class RuntimeState(str, Enum):
    """ADR-008 runtime state classification for a client attachment.

    Derived from the client's runtime classification.
    """

    UNKNOWN = "unknown"
    INITIALIZING = "initializing"
    ACTIVE = "active"
    IDLE = "idle"
    RECOVERING = "recovering"
    EXITED = "exited"


class Recoverability(str, Enum):
    """ADR-008 recoverability classification for a client attachment.

    Determines whether automatic recovery is appropriate.
    """

    UNKNOWN = "unknown"
    RECOVERABLE = "recoverable"
    NOT_RECOVERABLE = "not_recoverable"
    STALE = "stale"


# --------------------------------------------------------------------
# ADR-008: Pydantic Models
# --------------------------------------------------------------------


class ClientAttachment(BaseModel):
    """ADR-008 client attachment model.

    Represents a client attachment record with lifecycle metadata.
    Forbids extra fields and rejects persisted stale_candidate/unknown states.

    Examples:
        >>> att = ClientAttachment(client_id="c1", client_kind="cli", display_state=AttachmentDisplayState.HEALTHY, runtime_state=RuntimeState.ACTIVE, recoverability=Recoverability.RECOVERABLE, connected_at="2026-01-01T00:00:00Z", last_heartbeat="2026-01-01T00:01:00Z")
        >>> att.client_id
        'c1'
        >>> att.display_state
        <AttachmentDisplayState.HEALTHY: 'healthy'>
    """

    model_config = {"extra": "forbid"}

    client_id: str = Field(..., min_length=1)
    client_kind: str = Field(..., min_length=1)
    display_state: AttachmentDisplayState
    runtime_state: RuntimeState
    recoverability: Recoverability
    connected_at: str = Field(..., min_length=1)
    last_heartbeat: str = Field(..., min_length=1)
    stale_candidate: bool = Field(default=False)
    unknown_state: bool = Field(default=False)


class AttachmentRegistry(BaseModel):
    """ADR-008 attachment registry holding all client attachments.

    Forbids extra fields. Empty list means no attachments.

    Examples:
        >>> reg = AttachmentRegistry(attachments=[])
        >>> len(reg.attachments)
        0
    """

    model_config = {"extra": "forbid"}

    attachments: list[ClientAttachment] = Field(default_factory=list)


class RuntimeEventKind(str, Enum):
    """ADR-008 runtime event kind."""

    CLIENT_ATTACHMENT_STARTED = "client_attachment_started"
    HEARTBEAT = "heartbeat"
    RECOVERY_PROBE = "recovery_probe"
    CLIENT_PROVIDER_EXIT = "client_provider_exit"
    RECOVERY_FAILED = "recovery_failed"
    RECOVERY_SUCCEEDED = "recovery_succeeded"


class RuntimeEvent(BaseModel):
    """ADR-008 runtime event for diagnostics logging.

    Guard validation is pure only where it remains I/O-free.
    Forbids extra fields. Rejects events larger than 16 KiB when serialized.

    Examples:
        >>> evt = RuntimeEvent(kind=RuntimeEventKind.CLIENT_ATTACHMENT_STARTED, client_id="c1", client_kind="cli", timestamp="2026-01-01T00:00:00Z")
        >>> evt.kind
        <RuntimeEventKind.CLIENT_ATTACHMENT_STARTED: 'client_attachment_started'>
        >>> evt.client_id
        'c1'
    """

    model_config = {"extra": "forbid"}

    kind: RuntimeEventKind
    client_id: str = Field(..., min_length=1)
    client_kind: str = Field(..., min_length=1)
    timestamp: str = Field(..., min_length=1)
    details: dict[str, object] = Field(default_factory=dict)

    @post(lambda result: isinstance(result, bool))
    def guard_size(self) -> bool:
        """Return True if event is within the 16 KiB size limit.

        Pure validation — no I/O.

        Examples:
            >>> evt = RuntimeEvent(kind=RuntimeEventKind.HEARTBEAT, client_id="c1", client_kind="cli", timestamp="2026-01-01T00:00:00Z")
            >>> evt.guard_size()
            True
        """
        serialized = json.dumps(self.model_dump())
        return len(serialized.encode()) <= 16 * 1024


# --------------------------------------------------------------------
# ADR-008: Pure Helper Functions
# --------------------------------------------------------------------


@pre(lambda runtime_state, recoverability, stale_candidate, unknown_state: isinstance(runtime_state, RuntimeState) and isinstance(recoverability, Recoverability) and isinstance(stale_candidate, bool) and isinstance(unknown_state, bool))
@post(lambda result: isinstance(result, AttachmentDisplayState))
def classify_attachment_display_state(
    runtime_state: RuntimeState,
    recoverability: Recoverability,
    stale_candidate: bool,
    unknown_state: bool,
) -> AttachmentDisplayState:
    """Classify the display state of a client attachment.

    Pure function — no I/O.

    Priority:
    1. If unknown_state is True -> UNKNOWN
    2. If stale_candidate is True -> STALE_CANDIDATE
    3. If runtime_state is EXITED -> EXITED
    4. If runtime_state is RECOVERING -> RECOVERING
    5. If recoverability is STALE -> DEGRADED
    6. If runtime_state is ACTIVE and recoverability is RECOVERABLE -> HEALTHY
    7. If runtime_state is IDLE -> DEGRADED
    8. Otherwise -> UNKNOWN

    Examples:
        >>> classify_attachment_display_state(RuntimeState.ACTIVE, Recoverability.RECOVERABLE, False, False)
        <AttachmentDisplayState.HEALTHY: 'healthy'>
        >>> classify_attachment_display_state(RuntimeState.EXITED, Recoverability.RECOVERABLE, False, False)
        <AttachmentDisplayState.EXITED: 'exited'>
        >>> classify_attachment_display_state(RuntimeState.ACTIVE, Recoverability.RECOVERABLE, True, False)
        <AttachmentDisplayState.STALE_CANDIDATE: 'stale_candidate'>
    """
    if unknown_state:
        return AttachmentDisplayState.UNKNOWN
    if stale_candidate:
        return AttachmentDisplayState.STALE_CANDIDATE
    if runtime_state == RuntimeState.EXITED:
        return AttachmentDisplayState.EXITED
    if runtime_state == RuntimeState.RECOVERING:
        return AttachmentDisplayState.RECOVERING
    if recoverability == Recoverability.STALE:
        return AttachmentDisplayState.DEGRADED
    if runtime_state == RuntimeState.ACTIVE and recoverability == Recoverability.RECOVERABLE:
        return AttachmentDisplayState.HEALTHY
    if runtime_state == RuntimeState.IDLE:
        return AttachmentDisplayState.DEGRADED
    return AttachmentDisplayState.UNKNOWN


@pre(lambda client_kind, init_mode, connection_active: isinstance(client_kind, str) and len(client_kind) > 0 and isinstance(init_mode, str | None) and isinstance(connection_active, bool))
@post(lambda result: isinstance(result, RuntimeState))
def classify_runtime_state(
    client_kind: str,
    init_mode: str | None,
    connection_active: bool,
) -> RuntimeState:
    """Classify the runtime state of a client attachment.

    Pure function — no I/O.

    Priority:
    1. If client_kind is "unknown" -> UNKNOWN
    2. If connection_active is False -> EXITED
    3. If init_mode is None -> INITIALIZING
    4. If init_mode is "recovery" -> RECOVERING
    5. If connection_active is True -> ACTIVE
    6. Otherwise -> IDLE

    Examples:
        >>> classify_runtime_state("cli", "normal", True)
        <RuntimeState.ACTIVE: 'active'>
        >>> classify_runtime_state("cli", None, True)
        <RuntimeState.INITIALIZING: 'initializing'>
        >>> classify_runtime_state("cli", "normal", False)
        <RuntimeState.EXITED: 'exited'>
    """
    if client_kind == "unknown":
        return RuntimeState.UNKNOWN
    if not connection_active:
        return RuntimeState.EXITED
    if init_mode is None:
        return RuntimeState.INITIALIZING
    if init_mode == "recovery":
        return RuntimeState.RECOVERING
    if connection_active:
        return RuntimeState.ACTIVE
    return RuntimeState.IDLE


@pre(lambda client_kind, runtime_state, last_heartbeat_age_seconds: isinstance(client_kind, str) and len(client_kind) > 0 and isinstance(runtime_state, RuntimeState) and (last_heartbeat_age_seconds is None or isinstance(last_heartbeat_age_seconds, (int, float))))
@post(lambda result: isinstance(result, Recoverability))
def classify_recoverability(
    client_kind: str,
    runtime_state: RuntimeState,
    last_heartbeat_age_seconds: float | None,
) -> Recoverability:
    """Classify recoverability of a client attachment.

    Pure function — no I/O.

    Priority:
    1. If client_kind is "unknown" -> UNKNOWN
    2. If runtime_state is EXITED -> NOT_RECOVERABLE
    3. If runtime_state is UNKNOWN -> UNKNOWN
    4. If last_heartbeat_age_seconds is None -> RECOVERABLE
    5. If last_heartbeat_age_seconds > 90.0 -> STALE
    6. If last_heartbeat_age_seconds > 60.0 -> RECOVERABLE
    7. Otherwise -> RECOVERABLE

    Examples:
        >>> classify_recoverability("cli", RuntimeState.ACTIVE, 30.0)
        <Recoverability.RECOVERABLE: 'recoverable'>
        >>> classify_recoverability("cli", RuntimeState.EXITED, 30.0)
        <Recoverability.NOT_RECOVERABLE: 'not_recoverable'>
        >>> classify_recoverability("unknown", RuntimeState.ACTIVE, 30.0)
        <Recoverability.UNKNOWN: 'unknown'>
    """
    if client_kind == "unknown":
        return Recoverability.UNKNOWN
    if runtime_state == RuntimeState.EXITED:
        return Recoverability.NOT_RECOVERABLE
    if runtime_state == RuntimeState.UNKNOWN:
        return Recoverability.UNKNOWN
    if last_heartbeat_age_seconds is None:
        return Recoverability.RECOVERABLE
    if last_heartbeat_age_seconds > 90.0:
        return Recoverability.STALE
    if last_heartbeat_age_seconds > 60.0:
        return Recoverability.RECOVERABLE
    return Recoverability.RECOVERABLE


# --------------------------------------------------------------------
# Tool Posture Classification (pre-existing)
# --------------------------------------------------------------------


@pre(lambda tool_name, server_config, mcp_annotations=None: isinstance(tool_name, str) and len(tool_name) > 0 and isinstance(server_config, ServerConfig) and (mcp_annotations is None or isinstance(mcp_annotations, dict)))
@post(lambda result: result is None or isinstance(result, Posture))
def classify_tool(
    tool_name: str,
    server_config: ServerConfig,
    mcp_annotations: dict | None = None,
) -> Posture | None:
    """Determine posture for a tool from available sources.

    Priority:
    1. server_config.tool_overrides[tool_name].posture (explicit override)
    2. MCP tool annotations (readOnlyHint, destructiveHint)
    3. None (unclassified -- caller uses server default_posture)

    Examples:
        >>> from tela.core.models import ServerConfig, ToolOverride, Posture
        >>> cfg = ServerConfig(name="srv", command="cmd", tool_overrides={"t": ToolOverride(posture=Posture.READ_ONLY)})
        >>> classify_tool("t", cfg)
        <Posture.READ_ONLY: 'read_only'>
        >>> classify_tool("other", cfg)

    Args:
        tool_name: Name of the tool to classify.
        server_config: Server configuration with potential overrides.
        mcp_annotations: Optional MCP tool annotations dict.

    Returns:
        Classified Posture, or None if unclassified.
    """

    override = server_config.tool_overrides.get(tool_name)
    if override is not None and override.posture is not None:
        return override.posture

    if mcp_annotations is not None:
        return posture_from_annotations(mcp_annotations)

    return None


@pre(lambda annotations: isinstance(annotations, dict))
@post(lambda result: result is None or isinstance(result, Posture))
def posture_from_annotations(annotations: dict) -> Posture | None:
    """Extract posture from MCP tool annotations.

    readOnlyHint=True, destructiveHint=True -> DESTRUCTIVE (most restrictive wins)
    readOnlyHint=True  -> READ_ONLY
    destructiveHint=True -> DESTRUCTIVE
    readOnlyHint=False, destructiveHint=False -> READ_WRITE
    No relevant annotations -> None

    Examples:
        >>> posture_from_annotations({"readOnlyHint": True})
        <Posture.READ_ONLY: 'read_only'>
        >>> posture_from_annotations({"destructiveHint": True})
        <Posture.DESTRUCTIVE: 'destructive'>
        >>> posture_from_annotations({"readOnlyHint": True, "destructiveHint": True})
        <Posture.DESTRUCTIVE: 'destructive'>
        >>> posture_from_annotations({"readOnlyHint": False, "destructiveHint": False})
        <Posture.READ_WRITE: 'read_write'>
        >>> posture_from_annotations({})

    Args:
        annotations: MCP annotations dict.

    Returns:
        Classified Posture, or None if no relevant annotations.
    """

    read_only = annotations.get("readOnlyHint")
    destructive = annotations.get("destructiveHint")

    if read_only is None and destructive is None:
        return None

    if destructive is True:
        return Posture.DESTRUCTIVE

    if read_only is True:
        return Posture.READ_ONLY

    if read_only is False and destructive is False:
        return Posture.READ_WRITE

    if read_only is False:
        return Posture.READ_WRITE

    return None