#!/usr/bin/env python3
"""Run the native message listener without an editable install."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from agent_board.watch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
