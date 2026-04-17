# @invar:allow file_size: recovery module extracted from downstream.py in layer-dissolution;
# further splitting would fragment the ADR-006 recovery sequence coherence.
"""Downstream recovery, call-path, and event-handler coordination.

Extracted from ``tela.shell.downstream`` to keep startup/connect-disconnect
logic below maintainability limits. Recovery functions access shared downstream
state via lazy imports to avoid circular module dependencies.

This module is an internal implementation detail — ``tela.shell.downstream``
re-exports its public entry points (``call_tool``) and internal hooks
(``_handle_reconnect``, ``_handle_tools_list_changed``, ``_recover_server_client``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

from tela.core.errors import DOWNSTREAM_ERROR, DOWNSTREAM_UNAVAILABLE
from tela.core.models import ServerConfig, TelaError
from tela.core.recovery_helpers import (
    build_recovery_error as _build_recovery_error,
    get_exception_text as _get_exception_text,
    is_recovery_eligible_exception as _is_recovery_eligible_exception,
)
from tela.shell.result import Result

# Recovery constants
_RECOVERY_TIMEOUT_SECONDS = 15.0
_RECOVERY_STAGE_NOT_ATTEMPTED = "not_attempted"
_RECOVERY_STAGE_RECONNECT_STARTED = "reconnect_started"
_RECOVERY_STAGE_RECONNECT_SUCCEEDED = "reconnect_succeeded"
_RECOVERY_STAGE_CONVERGENCE_REJECTED = "convergence_rejected"
_RECOVERY_STAGE_RETRY_FAILED = "retry_failed"
_RECOVERY_STAGE_RECOVERY_TIMEOUT = "recovery_timeout"
_RECOVERY_STAGE_CLASSIFIER_UNKNOWN = "classifier_unknown"


def _emit_recovery_diagnostic(
    *,
    event: str,
    level: Literal["INFO", "WARNING"],
    server_name: str,
    recovery_stage: str,
    elapsed_ms: float,
    tool_name: str | None,
    underlying_error: str | None,
) -> None:
    """Emit ADR-006 structured recovery diagnostics via logger."""

    entry: dict[str, Any] = {
        "event": event,
        "level": level,
        "server_name": server_name,
        "tool_name": tool_name,
        "elapsed_ms": elapsed_ms,
        "recovery_stage": recovery_stage,
        "underlying_error": underlying_error,
        "request_id": None,
    }
    if level == "INFO":
        logging.info("%s", entry)
        return
    logging.warning("%s", entry)


# --- Recovery config resolution ---


# @shell_complexity: recovery config resolution branches across runtime-missing, server-missing, and config-present fail-closed paths required by ADR-006.
def _get_runtime_server_config(server_name: str) -> Result[ServerConfig, TelaError]:
    """Resolve server config from runtime authority only.

    ADR-006 reload-wins rule: once runtime config no longer contains a server,
    recovery MUST fail closed and must not revive stale hint/cache state.
    """
    from tela.shell.gateway_runtime import get_runtime_config

    runtime_result = get_runtime_config()
    if runtime_result.is_err:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                underlying_error=runtime_result.error or "Runtime config read failed",
                config_missing=True,
            )
        )
    runtime_config = runtime_result.value
    if runtime_config is not None:
        server_config = runtime_config.servers.get(server_name)
        if server_config is not None:
            return Result(value=server_config)

    missing_reason = (
        "Runtime config unavailable"
        if runtime_config is None
        else f"Server '{server_name}' missing from runtime config"
    )
    return Result(
        error=_build_recovery_error(
            server_name,
            recovery_attempted=True,
            recovery_eligible=True,
            recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
            underlying_error=missing_reason,
            config_missing=True,
        )
    )


# --- Recovery lock management ---


async def _acquire_recovery_lock(
    server_name: str,
    *,
    deadline_monotonic: float,
) -> Result[tuple[asyncio.Lock, bool], TelaError]:
    """Acquire per-server recovery lock within shared timeout budget."""

    from tela.shell import downstream as _ds

    async with _ds._registry_lock:
        lock = _ds._recovery_locks.get(server_name)
        if lock is None:
            lock = asyncio.Lock()
            _ds._recovery_locks[server_name] = lock
        wait_contended = lock.locked()

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                underlying_error="Recovery lock wait timed out",
                config_missing=False,
            )
        )
    try:
        await asyncio.wait_for(lock.acquire(), timeout=remaining)
    except asyncio.TimeoutError:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                underlying_error="Recovery lock wait timed out",
                config_missing=False,
            )
        )
    return Result(value=(lock, wait_contended))


async def _prune_recovery_lock_if_unused(server_name: str) -> None:
    """Remove per-server lock entry when no active client remains."""
    from tela.shell import downstream as _ds

    async with _ds._registry_lock:
        existing_lock = _ds._recovery_locks.get(server_name)
        if existing_lock is None:
            return
        if existing_lock.locked():
            return
        if server_name in _ds._clients:
            return
        _ds._recovery_locks.pop(server_name, None)


# --- Client handle cleanup ---


async def _close_handle_best_effort(handle: Any) -> None:
    """Close one temporary handle without surfacing cleanup failures.

    Delegates to downstream._close_handle_best_effort via lazy import.
    """

    from tela.shell import downstream as _ds

    await _ds._close_handle_best_effort(handle)


async def _drop_client_for_server(server_name: str) -> None:
    """Remove one cached client handle and close it best-effort."""
    from tela.shell import downstream as _ds

    async with _ds._registry_lock:
        stale_handle = _ds._clients.pop(server_name, None)
        _ds._server_instructions.pop(server_name, None)

    if stale_handle is not None:
        await _close_handle_best_effort(stale_handle)


# --- Core recovery ---


# @shell_complexity: recovery protocol requires sequential retry/autostart/wait stages across multiple error pathways; 25 branches cover the full downstream lifecycle
async def _recover_server_client(
    server_name: str,
    *,
    deadline_monotonic: float,
) -> Result[None, TelaError]:
    """Recover one downstream client through reload convergence authority."""

    from tela.shell import downstream as _ds
    from tela.shell.reload import on_server_reconnect

    lock_result = await _acquire_recovery_lock(
        server_name,
        deadline_monotonic=deadline_monotonic,
    )
    if lock_result.is_err:
        return Result(error=lock_result.error)
    assert lock_result.value is not None
    lock, wait_contended = lock_result.value

    new_handle: Any = None
    old_handle: Any = None
    previous_tools: list = []
    should_prune_lock = False
    try:
        config_result = _get_runtime_server_config(server_name)
        if config_result.is_err:
            if (
                config_result.error is not None
                and (config_result.error.details or {}).get("config_missing") is True
            ):
                await _drop_client_for_server(server_name)
                should_prune_lock = True
            return Result(error=config_result.error)
        assert config_result.value is not None
        server_config = config_result.value

        if wait_contended:
            async with _ds._registry_lock:
                current_handle = _ds._clients.get(server_name)
                current_registry = _ds._registry.get_all_tools().get(server_name)
            if current_handle is not None and current_registry is not None:
                return Result(value=None)

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted before reconnect",
                    config_missing=False,
                )
            )
        try:
            open_result = await asyncio.wait_for(
                _ds._open_client_for_server(
                    server_name,
                    server_config,
                    message_handler=_ds._build_downstream_message_handler(
                        server_name,
                        server_config,
                    ),
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted while reconnecting",
                    config_missing=False,
                )
            )
        if open_result.is_err:
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                    underlying_error=open_result.error
                    or "Reconnect transport open failed",
                    config_missing=False,
                )
            )
        assert open_result.value is not None
        new_handle = open_result.value

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted before enumeration",
                    config_missing=False,
                )
            )
        try:
            raw_tools_result = await asyncio.wait_for(
                _ds._enumerate_client_tools(server_name, new_handle),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted while enumerating tools",
                    config_missing=False,
                )
            )
        if raw_tools_result.is_err:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                    underlying_error=raw_tools_result.error
                    or "Tool enumeration failed during recovery",
                    config_missing=False,
                )
            )
        assert raw_tools_result.value is not None

        post_enum_config = _get_runtime_server_config(server_name)
        if post_enum_config.is_err:
            await _close_handle_best_effort(new_handle)
            if (
                post_enum_config.error is not None
                and (post_enum_config.error.details or {}).get("config_missing") is True
            ):
                await _drop_client_for_server(server_name)
                should_prune_lock = True
            return Result(error=post_enum_config.error)
        assert post_enum_config.value is not None
        latest_server_config = post_enum_config.value
        if latest_server_config != server_config:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                    underlying_error="Server config changed during recovery",
                    config_missing=False,
                )
            )

        async with _ds._registry_lock:
            previous_tools = list(_ds._registry.get_all_tools().get(server_name, []))

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted before convergence",
                    config_missing=False,
                )
            )
        try:
            convergence_result = await asyncio.wait_for(
                on_server_reconnect(
                    server_name,
                    latest_server_config,
                    raw_tools_result.value,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                    underlying_error="Recovery timeout exhausted during convergence",
                    config_missing=False,
                )
            )

        if convergence_result.is_err:
            await _close_handle_best_effort(new_handle)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_CONVERGENCE_REJECTED,
                    underlying_error=convergence_result.error
                    or "Convergence rejected recovered tools",
                    config_missing=False,
                )
            )

        pre_swap_config = _get_runtime_server_config(server_name)
        if pre_swap_config.is_err:
            await _close_handle_best_effort(new_handle)
            async with _ds._registry_lock:
                if previous_tools:
                    _ds._registry.register(server_name, previous_tools)
                else:
                    _ds._registry.unregister(server_name)
            if (
                pre_swap_config.error is not None
                and (pre_swap_config.error.details or {}).get("config_missing") is True
            ):
                await _drop_client_for_server(server_name)
                should_prune_lock = True
            return Result(error=pre_swap_config.error)
        assert pre_swap_config.value is not None
        if pre_swap_config.value != latest_server_config:
            await _close_handle_best_effort(new_handle)
            async with _ds._registry_lock:
                if previous_tools:
                    _ds._registry.register(server_name, previous_tools)
                else:
                    _ds._registry.unregister(server_name)
            return Result(
                error=_build_recovery_error(
                    server_name,
                    recovery_attempted=True,
                    recovery_eligible=True,
                    recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
                    underlying_error="Server config changed before recovered client swap",
                    config_missing=False,
                )
            )

        async with _ds._registry_lock:
            old_handle = _ds._clients.get(server_name)
            _ds._clients[server_name] = new_handle
            if new_handle.instructions:
                _ds._server_instructions[server_name] = new_handle.instructions

        new_handle = None
        if old_handle is not None:
            await _close_handle_best_effort(old_handle)
        return Result(value=None)
    finally:
        lock.release()
        if should_prune_lock:
            await _prune_recovery_lock_if_unused(server_name)


# --- Event handlers (message handler callbacks) ---


async def _handle_tools_list_changed(
    server_name: str,
    server_config: ServerConfig,
) -> None:
    """Re-enumerate server tools after downstream list-changed notification.

    Contract role: event-entry adapter.
    Enumeration policy: requires_new_enumeration.
    """

    from tela.shell import downstream as _ds
    from tela.shell.reload import on_tools_changed

    async with _ds._registry_lock:
        client = _ds._clients.get(server_name)

    if client is None:
        return

    raw_tools_result = await _ds._enumerate_client_tools(server_name, client)
    if raw_tools_result.is_err:
        logging.warning(
            "Failed downstream tool re-enumeration for %s: %s",
            server_name,
            raw_tools_result.error,
        )
        return
    assert raw_tools_result.value is not None
    raw_tools = raw_tools_result.value

    result = await on_tools_changed(server_name, server_config, raw_tools)
    if result.is_err:
        logging.warning(
            "Rejected downstream tool-list update for %s: %s",
            server_name,
            result.error,
        )


async def _handle_reconnect(
    server_name: str,
    server_config: ServerConfig,
) -> None:
    """Attempt downstream reconnect and route updated tools into reload flow.

    Contract role: event-entry adapter.
    Enumeration policy: reuse_fresh_raw_tools once reconnect enumeration succeeds.
    """

    recovery_started = time.monotonic()
    _emit_recovery_diagnostic(
        event="downstream_recovery_started",
        level="INFO",
        server_name=server_name,
        tool_name=None,
        elapsed_ms=0.0,
        recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
        underlying_error=None,
    )

    recovery_result = await _recover_server_client(
        server_name,
        deadline_monotonic=time.monotonic() + _RECOVERY_TIMEOUT_SECONDS,
    )
    if recovery_result.is_err:
        assert recovery_result.error is not None
        details = recovery_result.error.details or {}
        stage = str(details.get("recovery_stage", _RECOVERY_STAGE_RETRY_FAILED))
        event = (
            "downstream_recovery_rejected"
            if stage == _RECOVERY_STAGE_CONVERGENCE_REJECTED
            else "downstream_recovery_exhausted"
        )
        _emit_recovery_diagnostic(
            event=event,
            level="WARNING",
            server_name=server_name,
            tool_name=None,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=stage,
            underlying_error=str(
                details.get("underlying_error") or recovery_result.error.message
            ),
        )
        logging.warning(
            "Downstream reconnect rejected for %s: %s",
            server_name,
            recovery_result.error.message,
        )
        return

    _emit_recovery_diagnostic(
        event="downstream_recovery_succeeded",
        level="INFO",
        server_name=server_name,
        tool_name=None,
        elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
        recovery_stage=_RECOVERY_STAGE_RECONNECT_SUCCEEDED,
        underlying_error=None,
    )


# --- Public call surface ---


# @shell_complexity: dispatch to builtin vs downstream plus retry/recover/timeout/error-mapping branches; 16 branches reflect the full tool-call state machine
async def call_tool(
    server_name: str,
    tool_name: str,
    arguments: dict,
) -> Result[dict, TelaError]:
    """Call one downstream tool on a connected server session."""

    from tela.shell import downstream as _ds

    async with _ds._registry_lock:
        client = _ds._clients.get(server_name)

    initial_exc: Exception | None = None
    recovery_eligible = False
    deadline_monotonic = time.monotonic() + _RECOVERY_TIMEOUT_SECONDS

    if client is None:
        recovery_eligible = True
    else:
        try:
            downstream_result = await client.session.call_tool(
                tool_name,
                arguments=arguments,
            )
        except Exception as exc:
            initial_exc = exc
            recovery_eligible = _is_recovery_eligible_exception(exc)
            if not recovery_eligible:
                if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    pass
                elif isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                    pass
                else:
                    _emit_recovery_diagnostic(
                        event="downstream_recovery_classifier_unknown",
                        level="WARNING",
                        server_name=server_name,
                        tool_name=tool_name,
                        elapsed_ms=0.0,
                        recovery_stage=_RECOVERY_STAGE_CLASSIFIER_UNKNOWN,
                        underlying_error=_get_exception_text(exc),
                    )
        else:
            payload = downstream_result.model_dump(by_alias=True, exclude_none=True)
            if downstream_result.isError:
                return Result(
                    error=TelaError(
                        code=DOWNSTREAM_ERROR,
                        message=(
                            f"Downstream server '{server_name}' returned tool error for '{tool_name}'"
                        ),
                        details={
                            "server_name": server_name,
                            "tool_name": tool_name,
                            "downstream": payload,
                        },
                    )
                )
            return Result(value=payload)

    if not recovery_eligible:
        underlying = (
            _get_exception_text(initial_exc)
            if initial_exc is not None
            else f"Server '{server_name}' has no connected client"
        )
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=False,
                recovery_eligible=False,
                recovery_stage=_RECOVERY_STAGE_NOT_ATTEMPTED,
                underlying_error=underlying,
            )
        )

    recovery_started = time.monotonic()
    _emit_recovery_diagnostic(
        event="downstream_recovery_started",
        level="INFO",
        server_name=server_name,
        tool_name=tool_name,
        elapsed_ms=0.0,
        recovery_stage=_RECOVERY_STAGE_RECONNECT_STARTED,
        underlying_error=(
            _get_exception_text(initial_exc) if initial_exc is not None else None
        ),
    )
    recovery_result = await _recover_server_client(
        server_name,
        deadline_monotonic=deadline_monotonic,
    )
    if recovery_result.is_err:
        assert recovery_result.error is not None
        details = recovery_result.error.details or {}
        stage = str(details.get("recovery_stage", _RECOVERY_STAGE_RETRY_FAILED))
        failure_event = (
            "downstream_recovery_rejected"
            if stage == _RECOVERY_STAGE_CONVERGENCE_REJECTED
            else "downstream_recovery_exhausted"
        )
        _emit_recovery_diagnostic(
            event=failure_event,
            level="WARNING",
            server_name=server_name,
            tool_name=tool_name,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=stage,
            underlying_error=str(
                details.get("underlying_error") or recovery_result.error.message
            ),
        )
        return Result(error=recovery_result.error)

    async with _ds._registry_lock:
        refreshed_client = _ds._clients.get(server_name)

    if refreshed_client is None:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
                underlying_error="Recovered client handle missing after convergence",
            )
        )

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                underlying_error="Recovery timeout exhausted before retry",
                config_missing=False,
            )
        )

    try:
        retry_result = await asyncio.wait_for(
            refreshed_client.session.call_tool(tool_name, arguments=arguments),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        _emit_recovery_diagnostic(
            event="downstream_recovery_exhausted",
            level="WARNING",
            server_name=server_name,
            tool_name=tool_name,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
            underlying_error="Recovery timeout exhausted during retry",
        )
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RECOVERY_TIMEOUT,
                underlying_error="Recovery timeout exhausted during retry",
                config_missing=False,
            )
        )
    except Exception as exc:
        _emit_recovery_diagnostic(
            event="downstream_recovery_exhausted",
            level="WARNING",
            server_name=server_name,
            tool_name=tool_name,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
            underlying_error=_get_exception_text(exc),
        )
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
                underlying_error=_get_exception_text(exc),
                config_missing=False,
            )
        )

    retry_payload = retry_result.model_dump(by_alias=True, exclude_none=True)
    if retry_result.isError:
        _emit_recovery_diagnostic(
            event="downstream_recovery_exhausted",
            level="WARNING",
            server_name=server_name,
            tool_name=tool_name,
            elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
            recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
            underlying_error="Retry returned downstream tool error",
        )
        return Result(
            error=_build_recovery_error(
                server_name,
                recovery_attempted=True,
                recovery_eligible=True,
                recovery_stage=_RECOVERY_STAGE_RETRY_FAILED,
                underlying_error="Retry returned downstream tool error",
                config_missing=False,
            )
        )
    _emit_recovery_diagnostic(
        event="downstream_recovery_succeeded",
        level="INFO",
        server_name=server_name,
        tool_name=tool_name,
        elapsed_ms=max(0.0, (time.monotonic() - recovery_started) * 1000.0),
        recovery_stage=_RECOVERY_STAGE_RECONNECT_SUCCEEDED,
        underlying_error=None,
    )
    return Result(value=retry_payload)
