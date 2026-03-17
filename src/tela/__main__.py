"""Allow running tela as ``python -m tela``.

This module must be import-safe for doctest collection.
"""

from __future__ import annotations

import sys

from tela.cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
