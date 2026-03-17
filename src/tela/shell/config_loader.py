"""Shell contracts for loading local runtime configuration.

This module defines I/O boundary signatures only. No file/env implementation is
included in this contract step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Generic, TypeVar

from tela.core.models import TelaConfig

T = TypeVar("T")
E = TypeVar("E")


class Result(Generic[T, E]):
    """Result contract marker type for Shell boundaries."""


# @invar:allow dead_export: shell loader contract stub; runtime wiring deferred
def load_config(path: Path | None = None) -> Result[TelaConfig, str]:
    """Load local config from disk and environment, then delegate to Core.

    The local config file remains runtime source of truth.

    Args:
        path: Optional file path override. Defaults to local `tela.yaml`.

    Returns:
        `Result[TelaConfig, str]` once implemented.

    Raises:
        NotImplementedError: This step defines contracts only.
    """

    _ = path
    raise NotImplementedError("Contract stub: load_config")
