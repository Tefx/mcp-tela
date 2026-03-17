"""Shell Result type for I/O boundaries.

Canonical location for the Result[T, E] type used by all Shell modules.
Previously defined in config_loader.py, moved here to avoid misleading
import dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True)
class Result(Generic[T, E]):
    """Result type for Shell boundaries."""

    value: T | None = None
    error: E | None = None

    @property
    def is_ok(self) -> bool:
        """Return True when result is success."""
        return self.error is None

    @property
    def is_err(self) -> bool:
        """Return True when result is failure."""
        return self.error is not None
