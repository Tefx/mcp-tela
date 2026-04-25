"""Shell diagnostic handler for authorization explain surfaces."""

from __future__ import annotations

from tela.core.enforcement import explain_authorization
from tela.core.errors import GATEWAY_NOT_STARTED, is_gateway_not_started_error
from tela.core.models import EnforcementResult, EnforcementVerdict, Posture
from tela.core.contracts import post, pre
from tela.shell.downstream import get_all_tools
from tela.shell.gateway_runtime import get_runtime_config, set_runtime_config  # noqa: F401 — used in doctests
from tela.shell.result import Result


@pre(lambda profile_id=None: profile_id is None or isinstance(profile_id, str))
@post(
    lambda result: (
        (
            result.is_ok
            and result.value is not None
            and isinstance(result.value.get("profiles"), list)
        )
        or (
            result.is_err
            and isinstance(result.error, str)
            and (
                is_gateway_not_started_error(result.error)
                or result.error.startswith("PROFILE_NOT_FOUND")
                or result.error.startswith("DOWNSTREAM_")
            )
        )
    )
)
# @shell_complexity: diagnostic surface traverses runtime config, profiles, servers, and tools without mutating authorization state.
def handle_authorization_explain(
    profile_id: str | None = None,
) -> Result[dict[str, object], str]:
    """HTTP-equivalent handler for authorization visibility diagnostics.

    The handler snapshots configured profiles, downstream tools, and server
    default postures, then delegates each per-tool explanation to
    ``tela.core.enforcement.explain_authorization``. It does not admit
    sessions, call tools, mutate connections, or alter enforcement verdicts.

    Args:
        profile_id: Optional profile id to explain. When absent, all configured
            profiles are included.

    Returns:
        Result containing a JSON-serializable authorization explain payload.

    Examples:
        >>> set_runtime_config(None)
        >>> result = handle_authorization_explain()
        >>> result.is_err
        True
    """

    config = get_runtime_config().value
    if config is None:
        return Result(error=f"{GATEWAY_NOT_STARTED}: gateway has not been started")

    profile_ids = [profile_id] if profile_id is not None else sorted(config.profiles)
    profile_payloads: list[dict[str, object]] = []

    tools_result = get_all_tools()
    if tools_result.is_err:
        return Result(error=str(tools_result.error))
    assert tools_result.value is not None
    tools_by_server = tools_result.value
    allowed_token = EnforcementResult(verdict=EnforcementVerdict.ALLOW)

    for current_profile_id in profile_ids:
        profile = config.profiles.get(current_profile_id)
        if profile is None:
            return Result(
                error=f"PROFILE_NOT_FOUND: profile '{current_profile_id}' not found"
            )
        tool_payloads: list[dict[str, object]] = []
        for server_name, tools in sorted(tools_by_server.items()):
            server_config = config.servers.get(server_name)
            default_posture = (
                server_config.default_posture if server_config is not None else Posture.NONE
            )
            for tool in tools:
                routing_name = tool.raw_name or tool.name
                explanation = explain_authorization(
                    routing_name,
                    tool,
                    profile,
                    allowed_token,
                    default_posture,
                )
                tool_payloads.append(
                    {
                        "name": tool.name,
                        "server_name": tool.server_name,
                        "family": tool.family,
                        "posture": tool.posture.value if tool.posture is not None else None,
                        "default_posture": default_posture.value,
                        **explanation,
                    }
                )
        profile_payloads.append(
            {
                "profile_id": current_profile_id,
                "capabilities": {
                    family: posture.value
                    for family, posture in sorted(profile.capabilities.items())
                },
                "tools": tool_payloads,
            }
        )

    return Result(value={"profiles": profile_payloads})
