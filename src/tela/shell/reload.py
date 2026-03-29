"""Hot reload orchestration.

Contract boundary notes:
- Event-entry adapters own reconnect, reload, watcher, and manual
  re-enumeration triggers.
- The single-server convergence kernel owns resolve/register/conflict/rollback
  semantics only.
- Notify/audit policy remains outside the convergence kernel even when current
  wrapper functions still perform those side effects.

No-drop-connection invariant: active upstream connections are never dropped
during hot reload. On conflict, the previous tool list is preserved.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Protocol, TypeAlias

from tela.core.conflict import detect_conflicts
from tela.core.family import resolve_tools
from tela.core.models import (
    AuditLevel,
    ConnectionContext,
    EnforcementResult,
    EnforcementVerdict,
    ServerConfig,
    TelaConfig,
)
from tela.shell.audit import audit_write, build_audit_entry
from tela.shell.config_loader import Result
from tela.shell.gateway_runtime import get_runtime_config, set_runtime_config
from tela.shell.downstream import (
    _registry_lock,
    connect_all,
    disconnect_all,
    get_registry,
)


ConvergenceTrigger: TypeAlias = Literal[
    "reconnect",
    "reload",
    "watcher",
    "manual_reenumeration",
]
ReconnectEnumerationPolicy: TypeAlias = Literal[
    "reuse_fresh_raw_tools",
    "requires_new_enumeration",
]
KernelDisposition: TypeAlias = Literal["applied", "conflict"]


@dataclass(frozen=True)
class ConvergenceConflictNote:
    """Structured conflict fact surfaced by the convergence kernel."""

    tool_name: str
    servers: tuple[str, ...]


@dataclass(frozen=True)
class SingleServerConvergenceResult:
    """Structured kernel result returned before notify/audit policy is applied."""

    disposition: KernelDisposition
    trigger: ConvergenceTrigger
    server_name: str
    rollback_applied: bool
    resolved_tool_names: tuple[str, ...]
    conflicts: tuple[ConvergenceConflictNote, ...] = ()


class SingleServerConvergenceKernel(Protocol):
    """Resolve/register/conflict/rollback boundary for one server update."""

    async def converge(
        self,
        server_name: str,
        server_config: ServerConfig,
        raw_tools: list[dict],
        *,
        trigger: ConvergenceTrigger,
    ) -> Result[SingleServerConvergenceResult, str]: ...


class ConvergencePolicyConsumer(Protocol):
    """Adapter/orchestrator policy surface consuming kernel facts."""

    async def handle_result(
        self,
        outcome: SingleServerConvergenceResult,
    ) -> Result[None, str]: ...


CONVERGENCE_BEHAVIORAL_NOTES: tuple[str, ...] = (
    "Entry adapters own reconnect, reload, watcher, and manual re-enumeration triggers.",
    "The single-server convergence kernel is limited to resolve/register/conflict/rollback semantics for one server update.",
    "The kernel returns structured results; it does not own notify or audit policy.",
    "Reconnect reuses fresh raw_tools already enumerated by the reconnect adapter.",
    "Reload, watcher, and manual re-enumeration paths must enumerate fresh raw_tools before invoking the kernel.",
    "connect_all remains startup coordination outside the single-server convergence kernel.",
)


# Callback types for upstream notification
NotifyCallback = Callable[[str], Awaitable[None]]  # tools_digest -> None

_notify_callback: NotifyCallback | None = None


@dataclass(frozen=True)
class ReconnectEnumerationContract:
    """Contract for reconnect payload handling in reload flow."""

    authoritative_payload_name: str
    authoritative_payload_fields: tuple[str, ...]
    consumer_rule: str
    forbidden_behavior: str


RECONNECT_ENUMERATION_CONTRACT = ReconnectEnumerationContract(
    authoritative_payload_name="tool_list",
    authoritative_payload_fields=("raw_tools",),
    consumer_rule=(
        "When reconnect handling already carries fresh raw_tools, downstream reload consumers must reuse that payload as authoritative for the reconnect event."
    ),
    forbidden_behavior=(
        "Do not blindly trigger a second enumeration or re_enumerate call when authoritative fresh enumeration is already present."
    ),
)


RELOAD_BEHAVIORAL_NOTES: tuple[str, ...] = (
    "Accepted reload updates downstream convergence state but do not redefine discovery truth.",
    "Reconnect payloads may already contain fresh raw_tools and therefore short-circuit duplicate enumeration.",
)


@dataclass(frozen=True)
class _RegistrySingleServerConvergenceKernel:
    """Registry-backed implementation of the single-server convergence kernel."""

    async def converge(
        self,
        server_name: str,
        server_config: ServerConfig,
        raw_tools: list[dict],
        *,
        trigger: ConvergenceTrigger,
    ) -> Result[SingleServerConvergenceResult, str]:
        """Apply one server update with resolve/register/conflict/rollback semantics."""

        async with _registry_lock:
            registry = get_registry()
            snap = registry.snapshot()

            resolved = resolve_tools(server_name, server_config, raw_tools)
            registry.register(server_name, resolved)

            conflicts = detect_conflicts(registry.get_all_tools())
            if conflicts:
                registry.restore(snap)
                conflict_notes = tuple(
                    ConvergenceConflictNote(
                        tool_name=conflict.tool_name,
                        servers=tuple(conflict.servers),
                    )
                    for conflict in conflicts
                )
                return Result(
                    value=SingleServerConvergenceResult(
                        disposition="conflict",
                        trigger=trigger,
                        server_name=server_name,
                        rollback_applied=True,
                        resolved_tool_names=(),
                        conflicts=conflict_notes,
                    )
                )

            return Result(
                value=SingleServerConvergenceResult(
                    disposition="applied",
                    trigger=trigger,
                    server_name=server_name,
                    rollback_applied=False,
                    resolved_tool_names=tuple(tool.name for tool in resolved),
                )
            )


_single_server_kernel: SingleServerConvergenceKernel = (
    _RegistrySingleServerConvergenceKernel()
)


def set_notify_callback(callback: NotifyCallback | None) -> Result[None, str]:
    """Set the upstream notification callback for tools/list_changed."""
    global _notify_callback
    _notify_callback = callback
    return Result(value=None)


# @shell_complexity: Lifecycle event handlers with inherently branching behavior — routes/priorities/status modes are mutually exclusive by design.
async def on_tools_changed(
    server_name: str,
    server_config: ServerConfig,
    new_tool_list: list[dict],
) -> Result[None, str]:
    """Handle a downstream server's tools/list_changed notification.

    1. Re-enumerate the server's tool list
    2. Re-assign families
    3. Re-run conflict detection against all servers
    4. No conflict: update resolved tool set, notify upstream via callback
    5. Conflict: reject change, keep previous tools, emit TOOL_CONFLICT warning

    Active upstream connections are NOT dropped during this process.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import ServerConfig
        >>> from tela.shell.downstream import connect_all, disconnect_all
        >>> servers = {"fs": ServerConfig(name="fs", command="cmd")}
        >>> asyncio.run(connect_all(servers, tool_lists={"fs": [{"name": "t1", "inputSchema": {}}]}))
        Result(value=None, error=None)
        >>> r = asyncio.run(on_tools_changed("fs", servers["fs"], [{"name": "t1", "inputSchema": {}}, {"name": "t2", "inputSchema": {}}]))
        >>> r.is_ok
        True
        >>> asyncio.run(disconnect_all())
        Result(value=None, error=None)

    Args:
        server_name: Name of the server whose tools changed.
        server_config: Server configuration for family/classification.
        new_tool_list: New raw tool list from the server.

    Returns:
        Result[None, str] on success, or error string if conflict detected.
    """
    kernel_result = await _single_server_kernel.converge(
        server_name,
        server_config,
        new_tool_list,
        trigger="reload",
    )
    if kernel_result.is_err:
        return Result(error=kernel_result.error)
    assert kernel_result.value is not None
    outcome = kernel_result.value

    if outcome.disposition == "conflict":
        conflict_desc = "; ".join(
            f"{c.tool_name} in [{', '.join(c.servers)}]" for c in outcome.conflicts
        )

        warning_entry_result = build_audit_entry(
            level=AuditLevel.L1,
            connection=ConnectionContext(
                connection_id="system",
                profile_name="system",
                connected_at="",
            ),
            tool_name=outcome.conflicts[0].tool_name,
            server_name=server_name,
            result=EnforcementResult(
                verdict=EnforcementVerdict.DENY,
                denied_by="tool_conflict",
                error_code="TOOL_CONFLICT",
                error_message=conflict_desc,
            ),
        )
        if warning_entry_result.is_err:
            return Result(error=warning_entry_result.error)
        assert warning_entry_result.value is not None
        _ = await audit_write(warning_entry_result.value)
        return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

    if _notify_callback is not None:
        async with _registry_lock:
            registry = get_registry()
            tool_names = sorted(
                t.name for ts in registry.get_all_tools().values() for t in ts
            )
        raw = ":".join(tool_names).encode()
        digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        await _notify_callback(digest)

    return Result(value=None)


# @shell_complexity: Lifecycle event handlers with inherently branching behavior — routes/priorities/status modes are mutually exclusive by design.
async def on_server_reconnect(
    server_name: str,
    server_config: ServerConfig,
    tool_list: list[dict],
) -> Result[None, str]:
    """Handle a downstream server reconnecting after disconnect.

    The fresh tool list is already enumerated by _handle_reconnect before this
    is called. This function reuses that enumeration rather than re-enumerating.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import ServerConfig
        >>> from tela.shell.downstream import connect_all, disconnect_all
        >>> servers = {"fs": ServerConfig(name="fs", command="cmd")}
        >>> asyncio.run(connect_all(servers, tool_lists={"fs": [{"name": "t1", "inputSchema": {}}]}))
        Result(value=None, error=None)
        >>> r = asyncio.run(on_server_reconnect("fs", servers["fs"], [{"name": "t1", "inputSchema": {}}]))
        >>> r.is_ok
        True
        >>> asyncio.run(disconnect_all())
        Result(value=None, error=None)

    Args:
        server_name: Name of the reconnecting server.
        server_config: Server configuration.
        tool_list: Already-enumerated tool list from the reconnected server.

    Returns:
        Result[None, str].
    """
    kernel_result = await _single_server_kernel.converge(
        server_name,
        server_config,
        tool_list,
        trigger="reconnect",
    )
    if kernel_result.is_err:
        return Result(error=kernel_result.error)
    assert kernel_result.value is not None
    outcome = kernel_result.value

    if outcome.disposition == "conflict":
        conflict_desc = "; ".join(
            f"{c.tool_name} in [{', '.join(c.servers)}]" for c in outcome.conflicts
        )
        warning_entry_result = build_audit_entry(
            level=AuditLevel.L1,
            connection=ConnectionContext(
                connection_id="system",
                profile_name="system",
                connected_at="",
            ),
            tool_name=outcome.conflicts[0].tool_name,
            server_name=server_name,
            result=EnforcementResult(
                verdict=EnforcementVerdict.DENY,
                denied_by="tool_conflict",
                error_code="TOOL_CONFLICT",
                error_message=conflict_desc,
            ),
        )
        if warning_entry_result.is_err:
            return Result(error=warning_entry_result.error)
        assert warning_entry_result.value is not None
        _ = await audit_write(warning_entry_result.value)
        return Result(error=f"TOOL_CONFLICT: {conflict_desc}")

    if _notify_callback is not None:
        async with _registry_lock:
            registry = get_registry()
            tool_names = sorted(
                t.name for ts in registry.get_all_tools().values() for t in ts
            )
        raw = ":".join(tool_names).encode()
        digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        await _notify_callback(digest)

    return Result(value=None)


# Production callback target for runtime config-file watcher wiring.
async def on_config_changed(new_config: TelaConfig) -> Result[None, str]:
    """Handle configuration file change.

    Handle configuration file change. Updates runtime config.

    Examples:
        >>> import asyncio
        >>> from tela.core.models import TelaConfig
        >>> r = asyncio.run(on_config_changed(TelaConfig()))
        >>> r.is_ok
        True

    Args:
        new_config: New TelaConfig.

    Returns:
        Result[None, str] once implemented.
    """
    old_config = get_runtime_config().value

    # Update runtime config via locked accessor.
    set_runtime_config(new_config)

    # Detect server changes and re-connect changed/new servers
    if old_config is not None:
        old_servers = set(old_config.servers.keys())
        new_servers = set(new_config.servers.keys())

        removed = old_servers - new_servers
        added = new_servers - old_servers
        # Servers present in both configs but with changed settings
        changed = {
            name
            for name in old_servers & new_servers
            if old_config.servers[name] != new_config.servers[name]
        }

        servers_to_reconnect = added | changed

        if removed or servers_to_reconnect:
            # Disconnect all and reconnect with new config.
            # Per-server disconnect is not yet supported; full reconnect
            # is the safe path that preserves conflict-detection invariants.
            await disconnect_all()
            connect_result = await connect_all(new_config.servers)
            if connect_result.is_err:
                return Result(error=connect_result.error)

    return Result(value=None)
