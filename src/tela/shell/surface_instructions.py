"""Authoritative runtime instruction text for tela-owned surfaces."""

from __future__ import annotations

from tela.shell.config_loader import Result


def get_gateway_surface_instructions() -> Result[str, str]:
    """Return gateway-authoritative runtime instructions text."""

    return Result(
        value=(
            "# tela gateway surface contract\n\n"
            "Authoritative rules for tela-owned surfaces:\n"
            "- Built-in MCP resource: `tela.profiles` (read via `tela://profiles`).\n"
            "- Built-in MCP tools: none.\n"
            "- Operator-only surfaces (not MCP built-ins): `tela profiles`, "
            "`tela status`, `tela connections`, `tela audit`, and `GET /status`.\n"
            "- Do not use `tools/call` for `tela.profiles`; use resource read."
        )
    )


def compose_gateway_and_downstream(
    gateway_instructions: str,
    downstream_instructions: str | None,
) -> Result[str, str]:
    """Compose gateway text followed by downstream instruction sections."""

    if gateway_instructions.strip() == "":
        return Result(
            error="INSTRUCTIONS_COMPOSE_ERROR: gateway instructions are empty"
        )
    if downstream_instructions is None:
        return Result(value=gateway_instructions)
    return Result(value=f"{gateway_instructions}\n\n{downstream_instructions}")
