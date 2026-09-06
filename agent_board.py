#!/usr/bin/env python3
"""Thin root entry point: `python -B agent_board.py <command> ...` runs the packaged CLI.

The real implementation lives in src/agent_board/cli.py. This file exists only so the
documented command keeps working without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from agent_board.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
