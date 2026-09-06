#!/usr/bin/env python3
"""Thin root entry point: `python -B agent_board_web.py --repo <path> ...` runs the web server.

The real implementation lives in src/agent_board/web.py. This file exists only so the
documented command keeps working without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from agent_board.web import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
