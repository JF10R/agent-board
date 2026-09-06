#!/usr/bin/env python3
"""Vendorable forwarder to the canonical agent board CLI.

Copy this one file into another repo (anywhere), point AGENT_BOARD_CLI at the
standalone board's root agent_board.py entry point, and every subcommand works
unchanged. No board code is duplicated: the store stays git-local to the repo
this file lives in, and --repo is filled in from that repo unless the caller
passes its own.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ENV_CLI = "AGENT_BOARD_CLI"
ENV_REPO = "AGENT_BOARD_REPO"


def canonical_cli() -> Path:
    raw = os.environ.get(ENV_CLI)
    if not raw:
        raise SystemExit(f"agent-board-launcher: set {ENV_CLI} to the absolute path of the canonical agent_board.py")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise SystemExit(f"agent-board-launcher: {ENV_CLI} does not point at a file: {path}")
    return path


def default_repo() -> Path:
    """The repo this launcher was vendored into: its git top level, or AGENT_BOARD_REPO."""

    override = os.environ.get(ENV_REPO)
    if override:
        return Path(override).expanduser()
    here = Path(__file__).resolve().parent
    result = subprocess.run(
        ["git", "-C", str(here), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode != 0:
        raise SystemExit("agent-board-launcher: not inside a Git checkout; pass --repo or set " + ENV_REPO)
    return Path(result.stdout.strip())


def build_argv(argv: list[str]) -> list[str]:
    forwarded = list(argv)
    if not any(item == "--repo" or item.startswith("--repo=") for item in forwarded):
        forwarded = ["--repo", str(default_repo()), *forwarded]
    return [sys.executable, "-B", str(canonical_cli()), *forwarded]


def main() -> int:
    return subprocess.run(build_argv(sys.argv[1:]), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
