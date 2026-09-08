#!/usr/bin/env python3
"""Measure ticket histories and read views using a disposable board."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickets", type=int, default=20)
    parser.add_argument("--events", type=int, default=20, help="Comments per ticket")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--reads", type=int, default=10)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1] / "src"
    )
    args = parser.parse_args()
    if min(args.tickets, args.events, args.workers, args.reads) < 1:
        parser.error("workload sizes must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    from agent_board import cli, tickets

    with tempfile.TemporaryDirectory(prefix="agent-board-benchmark-") as directory:
        root = cli.initialize(Path(directory) / "board")
        for index in range(args.tickets):
            tickets.create_ticket(
                root, actor="sol-master", ticket_id=f"B{index}", title="Benchmark"
            )

        def write_history(index: int) -> list[float]:
            durations = []
            for event in range(args.events):
                started = time.perf_counter()
                tickets.comment_ticket(
                    root,
                    f"B{index}",
                    actor="sol-master",
                    summary=f"Entry {event}",
                    body="x" * 256,
                )
                durations.append(time.perf_counter() - started)
            return durations

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            writes = [
                duration
                for group in executor.map(write_history, range(args.tickets))
                for duration in group
            ]
        elapsed = time.perf_counter() - started
        reads = []
        for _ in range(args.reads):
            started = time.perf_counter()
            assert len(tickets.list_tickets(root)) == args.tickets
            reads.append(time.perf_counter() - started)
        print(
            json.dumps(
                {
                    "python": sys.version.split()[0],
                    "tickets": args.tickets,
                    "comments_per_ticket": args.events,
                    "comment_body_bytes": 256,
                    "workers": args.workers,
                    "write_wall_seconds": round(elapsed, 4),
                    "write_median_ms": round(statistics.median(writes) * 1000, 3),
                    "write_max_ms": round(max(writes) * 1000, 3),
                    "list_median_ms": round(statistics.median(reads) * 1000, 3),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
