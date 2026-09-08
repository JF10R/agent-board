"""Protect test imports when an editable install already exposes src."""

import subprocess
import sys
from pathlib import Path


def test_existing_src_path_takes_precedence_over_root_launcher():
    root = Path(__file__).resolve().parents[1]
    script = """
import runpy
import sys
from pathlib import Path
root = Path.cwd()
src = str(root / "src")
sys.path = [str(root)] + [entry for entry in sys.path if entry != src] + [src]
runpy.run_path(str(root / "tests" / "conftest.py"))
import agent_board.cli
assert Path(agent_board.cli.__file__).resolve() == root / "src" / "agent_board" / "cli.py"
"""
    subprocess.run([sys.executable, "-B", "-c", script], cwd=root, check=True)
