"""Allow running tela as ``python -m tela``."""

from __future__ import annotations

import sys

from tela.cli import main

sys.exit(main(sys.argv[1:]))
