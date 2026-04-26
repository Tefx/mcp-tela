"""Authoritative runtime instruction text for tela-owned surfaces."""

from __future__ import annotations

from tela.core.surface_manifest import build_manifest_header as build_manifest_header
from tela.shell.result import Result


def get_gateway_surface_instructions(
    manifest_header: str | None = None,
) -> Result[str, str]:
    """Return gateway-authoritative runtime instructions text.

    When manifest_header is provided, prepend it to the surface text.
    Updates built-in MCP tools line to include tela_list_providers.
    """
    base_instructions = """# tela gateway surface contract

This document defines the authoritative instruction surface presented to
upstream MCP clients connecting to the tela gateway.

## Gateway capabilities

- Built-in MCP tools: `tela_list_providers`, `tela_list_profiles`.
- Operator-only surfaces (not MCP built-ins): `tela profiles`, `tela status`, `tela connections`, `tela audit`, `GET /status`, `GET /operator/audit`, `GET /health`, `POST /connect`, `POST /disconnect`, `POST /mcp`.
- Gateway does not proxy or forward to operator surfaces.

## Server instructions

Instructions for individual servers are provided below, where available.

"""
    if manifest_header is None:
        return Result(value=base_instructions)
    return Result(value=f"{manifest_header}\n\n{base_instructions}")


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
