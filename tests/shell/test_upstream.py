"""Contract tests for upstream initialize profile-binding surface."""

from __future__ import annotations

import pytest

from tela.core.models import DefaultProfileResolutionStatus
from tela.shell.upstream import InitializeContext, resolve_initialize_profile_binding


def test_initialize_context_exposes_connection_metadata_contract() -> None:
    context = InitializeContext(connection_metadata={"client": "desktop"})
    assert context.connection_metadata["client"] == "desktop"


def test_resolve_initialize_profile_binding_is_contract_stub() -> None:
    with pytest.raises(NotImplementedError) as exc_info:
        resolve_initialize_profile_binding(
            resolved_default_profile=None,
            default_resolution_status=DefaultProfileResolutionStatus.MISSING,
            context=InitializeContext(connection_metadata={"profile": "dev"}),
        )

    assert "Contract stub" in str(exc_info.value)
