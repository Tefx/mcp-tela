"""Pure downstream provider startup diagnostic helpers."""

from __future__ import annotations

from dataclasses import dataclass

from tela.core.contracts import post, pre


@dataclass(frozen=True)
class ProviderStartupFailure:
    """Diagnostic for one downstream provider startup failure."""

    server_name: str
    phase: str
    reason: str
    timeout: bool = False
    elapsed_ms: float | None = None


@dataclass(frozen=True)
class DownstreamStartupSnapshot:
    """Detached snapshot of the current/last downstream startup convergence."""

    attempted_servers: tuple[str, ...]
    successful_servers: tuple[str, ...]
    failed_servers: dict[str, ProviderStartupFailure]
    in_progress_servers: tuple[str, ...]
    complete: bool
    degraded_reason: str | None


@pre(lambda failure: isinstance(failure, ProviderStartupFailure))
@post(lambda result: result.startswith("provider_") and ":" in result)
def failure_reason(failure: ProviderStartupFailure) -> str:
    """Return a compact status degraded_reason token for one provider failure.

    Examples:
        >>> failure_reason(ProviderStartupFailure("browseros", "initialize", "boom"))
        'provider_initialize_failed:browseros'
        >>> failure_reason(ProviderStartupFailure("slow", "tools_list", "boom", timeout=True))
        'provider_tools_list_timeout:slow'
    """

    suffix = "timeout" if failure.timeout else "failed"
    return f"provider_{failure.phase}_{suffix}:{failure.server_name}"


@pre(lambda failures: isinstance(failures, dict))
@post(lambda result: result is None or isinstance(result, str))
def degraded_reason_from_failures(
    failures: dict[str, ProviderStartupFailure],
) -> str | None:
    """Return stable semicolon-separated provider diagnostics for /status.

    Examples:
        >>> degraded_reason_from_failures({}) is None
        True
        >>> degraded_reason_from_failures({"b": ProviderStartupFailure("b", "initialize", "boom"), "a": ProviderStartupFailure("a", "tools_list", "boom", timeout=True)})
        'provider_tools_list_timeout:a;provider_initialize_failed:b'
    """

    if not failures:
        return None
    return ";".join(failure_reason(failures[name]) for name in sorted(failures.keys()))


@pre(lambda server_name, error: isinstance(server_name, str) and (error is None or isinstance(error, str)))
@post(lambda result: isinstance(result, ProviderStartupFailure))
def startup_failure_from_error(
    server_name: str,
    error: str | None,
) -> ProviderStartupFailure:
    """Classify a provider startup error into phase-aware diagnostics.

    Examples:
        >>> startup_failure_from_error("browseros", "DOWNSTREAM_CONNECT_FAILED: provider_initialize_cancelled:browseros cancel").phase
        'initialize'
        >>> startup_failure_from_error("srv", "DOWNSTREAM_CONNECT_FAILED: provider_tools_list_timeout:srv timeout_seconds=30").timeout
        True
        >>> startup_failure_from_error("srv", "DOWNSTREAM_ENUMERATE_FAILED: boom").phase
        'tools_list'
    """

    message = error or "unknown"
    if f"provider_initialize_timeout:{server_name}" in message:
        return ProviderStartupFailure(server_name, "initialize", message, timeout=True)
    if f"provider_initialize_cancelled:{server_name}" in message:
        return ProviderStartupFailure(server_name, "initialize", message)
    if f"provider_tools_list_timeout:{server_name}" in message:
        return ProviderStartupFailure(server_name, "tools_list", message, timeout=True)
    if f"provider_tools_list_cancelled:{server_name}" in message:
        return ProviderStartupFailure(server_name, "tools_list", message)
    if "enumeration failed" in message or "DOWNSTREAM_ENUMERATE_FAILED" in message:
        return ProviderStartupFailure(server_name, "tools_list", message)
    return ProviderStartupFailure(server_name, "initialize", message)
