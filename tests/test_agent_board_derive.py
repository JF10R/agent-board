"""Derived roadmap views (tools/agent_board_derive.py) and the sidecar fields that feed them.

Rules under test, one per case: startable, waiting, standby (stale / explicit), blocked-by,
parallel-by-owner, milestone aggregate (never invents progress), feeds/impact, unknown and
cyclic dependencies stay unresolved, and the write-side guards + CLI/web plumbing.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from agent_board import cli as board
from agent_board import derive
from agent_board import tree
from agent_board import web


NOW = datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)


def stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds").replace("+00:00", "Z")


def item(item_id: str, status: str = "NOT_STARTED", *, progress: int = 0, owner: str = "claude-master", blocker: str = "", hours_ago: float = 1.0, summary: str = "fixture") -> dict:
    return {"id": item_id, "title": f"Title {item_id}", "summary": summary, "status": status, "owner": owner, "progress": progress, "blocker": blocker, "updated_at": stamp(hours_ago), "revision": 1}


def ext(entries: dict[str, dict]) -> dict:
    return {"schema_version": 1, "items": entries, "journal": []}


def views(base: list[dict], entries: dict[str, dict] | None = None, **kwargs) -> dict:
    return derive.derive_roadmap_views(tree.build_tree(base, ext(entries or {}), now=NOW), now=NOW, **kwargs)


class DerivationRulesTest(unittest.TestCase):
    def test_startable_requires_not_started_without_blocker_or_unresolved_dependency(self) -> None:
        base = [item("free"), item("dep-done", "CLOSED", progress=100), item("after-done"), item("after-open"), item("open", "IN_PROGRESS", progress=10),
                item("legacy0", "PENDING"), item("legacy80", "PENDING", progress=80), item("stuck", "BLOCKED", blocker="provider outage")]
        result = views(base, {"after-done": {"depends_on": ["dep-done"]}, "after-open": {"depends_on": ["open"]}})
        self.assertEqual(result["views"]["startable"], ["after-done", "free", "legacy0"])
        self.assertEqual(result["views"]["waiting"], ["after-open"])
        by_id = {entry["id"]: entry for entry in result["items"]}
        self.assertEqual(by_id["after-open"]["derived"]["blocked_by"], {"blockers": [], "waiting_on": ["open"]})
        self.assertIsNone(by_id["free"]["derived"]["blocked_by"])
        self.assertFalse(by_id["legacy80"]["derived"]["startable"])

    def test_standby_is_stale_in_progress_or_explicitly_flagged(self) -> None:
        base = [item("fresh", "IN_PROGRESS", progress=20, hours_ago=3), item("stale", "IN_PROGRESS", progress=20, hours_ago=60),
                item("paused", "IN_PROGRESS", progress=20, hours_ago=3), item("old-todo", hours_ago=500), item("old-done", "CLOSED", progress=100, hours_ago=500)]
        result = views(base, {"paused": {"standby": {"reason": "waiting for Tuesday", "set_by": "claude-master", "set_at": stamp(2)}}})
        self.assertEqual(result["views"]["standby"], ["stale", "paused"])
        by_id = {entry["id"]: entry for entry in result["items"]}
        self.assertEqual(by_id["stale"]["derived"]["standby"]["kind"], "stale")
        self.assertEqual(by_id["stale"]["derived"]["age_hours"], 60.0)
        self.assertEqual(by_id["paused"]["derived"]["standby"], {"kind": "explicit", "reason": "waiting for Tuesday", "set_by": "claude-master", "age_hours": 3.0})
        self.assertIsNone(by_id["fresh"]["derived"]["standby"])
        self.assertIsNone(by_id["old-todo"]["derived"]["standby"], "only IN_PROGRESS items can be in standby")
        self.assertEqual(views(base, standby_hours=2)["views"]["standby"], ["stale", "fresh", "paused"], "threshold is configurable")
        with self.assertRaises(board.BoardError):
            views(base, standby_hours=0)

    def test_blocked_view_carries_the_blocker_text_and_the_dependencies_it_waits_on(self) -> None:
        base = [item("stuck", "BLOCKED", progress=30, blocker="Lead ruling pending"), item("upstream", "IN_PROGRESS", progress=50), item("ok")]
        result = views(base, {"stuck": {"depends_on": ["upstream"], "blockers": [{"text": "disk full", "added_by": "sol-master", "added_at": stamp(1)}]}})
        self.assertEqual(result["views"]["blocked"], [{"id": "stuck", "blockers": ["Lead ruling pending", "disk full"], "waiting_on": ["upstream"]}])

    def test_parallel_frontier_is_mutually_independent_and_grouped_by_owner(self) -> None:
        base = [item("a", "IN_PROGRESS", progress=10, owner="claude-master"), item("b", "IN_PROGRESS", progress=10, owner="sol-master"),
                item("c", owner="sol-master"), item("after-a", "IN_PROGRESS", progress=5, owner="claude-master"), item("ready", "READY", progress=90, owner="shared"),
                item("stale", "IN_PROGRESS", progress=10, hours_ago=100), item("stuck", "BLOCKED", blocker="x", owner="shared")]
        result = views(base, {"after-a": {"depends_on": ["a"]}})
        self.assertEqual(result["views"]["parallel"], {"items": ["a", "b", "c", "ready"], "by_owner": {"claude-master": ["a"], "shared": ["ready"], "sol-master": ["b", "c"]}})

    def test_milestone_aggregates_children_and_dependencies_without_inventing_progress(self) -> None:
        base = [item("train-a", "IN_PROGRESS", progress=0), item("kid", "IN_PROGRESS", progress=40, summary="child of train-a."), item("dep", "CLOSED", progress=100),
                item("todo", summary="child of train-a."), item("goal", "NOT_STARTED"), item("unreported-ms", "IN_PROGRESS", progress=0)]
        entries = {"train-a": {"kind": "MILESTONE", "depends_on": ["dep"], "impact": "first Train A candidate resolved"},
                   "goal": {"kind": "OBJECTIVE", "depends_on": ["train-a"]}, "unreported-ms": {"kind": "MILESTONE", "depends_on": ["todo"]}}
        result = views(base, entries)
        by_id = {entry["id"]: entry for entry in result["items"]}
        aggregate = by_id["train-a"]["derived"]["aggregate"]
        self.assertEqual(aggregate["members"], ["dep", "kid", "todo"])
        self.assertEqual(aggregate["by_status"], {"CLOSED": 1, "IN_PROGRESS": 1, "NOT_STARTED": 1})
        self.assertEqual((aggregate["closed"], aggregate["reported"], aggregate["mean_reported_progress"]), (1, 2, 70))
        # a CLOSED member at 100 (superseded / killed) never lifts the mission number
        self.assertEqual(aggregate["mean_open_progress"], 40)
        self.assertIsNone(aggregate["self_reported_progress"], "the milestone itself reported nothing")
        self.assertEqual(aggregate["open"], ["kid", "todo"])
        self.assertIsNone(by_id["unreported-ms"]["derived"]["aggregate"]["mean_reported_progress"], "no reported child progress means no number")
        self.assertEqual(by_id["kid"]["derived"]["feeds"], ["goal", "train-a"], "a task feeds every milestone above it, through parent or dependency")
        self.assertEqual(by_id["dep"]["derived"]["dependents"], ["train-a"])
        self.assertEqual(by_id["train-a"]["derived"]["impact"], "first Train A candidate resolved")
        self.assertEqual(by_id["todo"]["derived"]["kind"], "UNKNOWN")
        self.assertEqual(result["views"]["milestones"], ["goal", "train-a", "unreported-ms"])
        self.assertIsNone(by_id["kid"]["derived"]["aggregate"])

    def test_unknown_and_cyclic_dependencies_stay_unresolved_with_a_warning(self) -> None:
        base = [item("x"), item("y"), item("z")]
        result = views(base, {"x": {"depends_on": ["y", "ghost"]}, "y": {"depends_on": ["x"]}})
        by_id = {entry["id"]: entry for entry in result["items"]}
        self.assertEqual(by_id["x"]["derived"]["unresolved_deps"], ["y", "ghost"])
        self.assertFalse(by_id["x"]["derived"]["depends_on"][1]["known"])
        self.assertTrue(by_id["x"]["derived"]["on_cycle"] and by_id["y"]["derived"]["on_cycle"])
        self.assertFalse(by_id["z"]["derived"]["on_cycle"])
        self.assertEqual(result["views"]["startable"], ["z"])
        self.assertTrue(any("cycle" in warning for warning in result["warnings"]))
        self.assertTrue(any("ghost" in warning for warning in result["warnings"]))


class SidecarWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "board"
        board.initialize(self.root)
        for item_id in ("a", "b", "c"):
            board.upsert_roadmap_item(self.root, actor="claude-master", item_id=item_id, title=item_id, summary="s", status="IN_PROGRESS", owner="claude-master", progress=5, expected_revision=0)

    def test_annotate_round_trips_the_new_fields_and_leaves_v1_bytes_untouched(self) -> None:
        before = (self.root / "roadmap.v1.json").read_bytes()
        merged = tree.annotate_roadmap_item(self.root, actor="sol-master", item_id="a", depends_on=["b", "c", "b"], kind="milestone", impact="  unblocks Tuesday  ", standby="paused until ruling")
        self.assertEqual((self.root / "roadmap.v1.json").read_bytes(), before)
        self.assertEqual(merged["depends_on_ids"], ["b", "c"])
        self.assertEqual((merged["kind"], merged["impact"], merged["standby_flag"]["reason"], merged["standby_flag"]["set_by"]), ("MILESTONE", "unblocks Tuesday", "paused until ruling", "sol-master"))
        cleared = tree.annotate_roadmap_item(self.root, actor="sol-master", item_id="a", depends_on=[], kind=None, impact="", standby=None)
        self.assertEqual((cleared["depends_on_ids"], cleared["kind"], cleared["impact"], cleared["standby_flag"]), ([], None, None, None))
        self.assertEqual(board.list_roadmap(self.root)[0]["revision"], 1, "sidecar writes never bump the v1 revision")

    def test_annotate_refuses_self_unknown_cycle_and_bad_kind(self) -> None:
        with self.assertRaisesRegex(board.BoardError, "itself"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a", depends_on=["a"])
        with self.assertRaisesRegex(board.BoardError, "unknown dependency"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a", depends_on=["nope"])
        tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a", depends_on=["b"])
        tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="b", depends_on=["c"])
        with self.assertRaisesRegex(board.BoardError, "cycle"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="c", depends_on=["a"])
        with self.assertRaisesRegex(board.BoardError, "kind must be"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a", kind="EPIC")
        with self.assertRaisesRegex(board.BoardError, "300"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a", impact="x" * 301)
        with self.assertRaisesRegex(board.BoardError, "unauthorized"):
            tree.annotate_roadmap_item(self.root, actor="lead", item_id="a", kind="TASK")

    def test_cli_upsert_and_annotate_accept_the_optional_flags(self) -> None:
        output = io.StringIO()
        with mock.patch.object(board, "board_root", return_value=self.root), redirect_stdout(output):
            self.assertEqual(board.run(["--repo", ".", "roadmap", "upsert", "--actor", "claude-master", "--id", "ms", "--title", "Train A", "--summary", "milestone",
                                        "--status", "IN_PROGRESS", "--owner", "shared", "--progress", "0", "--expected-revision", "0",
                                        "--kind", "MILESTONE", "--depends-on", "a", "--depends-on", "b", "--impact", "resolves the first candidate"]), 0)
            self.assertEqual(board.run(["--repo", ".", "roadmap", "annotate", "--actor", "claude-master", "--id", "c", "--standby", "waiting on provider GO"]), 0)
            self.assertEqual(board.run(["--repo", ".", "roadmap", "tree", "--json", "--no-journal"]), 0)
        printed = output.getvalue()
        self.assertIn('"kind": "MILESTONE"', printed)
        self.assertIn('"revision": 1', printed)
        self.assertIn('"views"', printed)
        merged = derive.derive_roadmap_views(tree.load_roadmap_tree(self.root, record=False))
        by_id = {entry["id"]: entry for entry in merged["items"]}
        self.assertEqual(by_id["ms"]["derived"]["aggregate"]["members"], ["a", "b"])
        self.assertEqual(by_id["c"]["derived"]["standby"]["reason"], "waiting on provider GO")
        self.assertEqual(merged["views"]["milestones"], ["ms"])


class WebPlumbingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "board"
        self.token = "test-token"
        self.server = web.create_server(self.root, port=0, token=self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        port = self.server.server_port
        headers = {"Host": f"127.0.0.1:{port}"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", "X-Agent-Board-Token": self.token, "Content-Length": str(len(body))})
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, value

    def test_roadmap_post_accepts_optional_sidecar_fields_and_state_carries_views(self) -> None:
        base = {"actor": "claude-master", "id": "dep", "title": "Dep", "summary": "s", "status": "IN_PROGRESS", "owner": "sol-master", "progress": 10, "blocker": "", "expected_revision": 0}
        self.assertEqual(self.request("POST", "/api/roadmap", base)[0], 200)
        status, value = self.request("POST", "/api/roadmap", {**base, "id": "ms", "owner": "shared", "kind": "MILESTONE", "depends_on": ["dep"], "impact": "Tuesday readiness", "standby": "paused"})
        self.assertEqual(status, 200, value)
        self.assertEqual(value["item"]["depends_on_ids"], ["dep"])
        status, value = self.request("POST", "/api/roadmap", {**base, "id": "bad", "depends_on": "dep"})
        self.assertEqual(status, 400)
        # updated_at is second-truncated, so a microsecond threshold makes every item stale deterministically.
        status, value = self.request("GET", "/api/state?standby=0.000000001")
        self.assertEqual(status, 200)
        payload = value["roadmap_tree"]
        self.assertEqual(payload["views"]["milestones"], ["ms"])
        self.assertEqual(sorted(payload["views"]["standby"]), ["dep", "ms"], "a tiny threshold makes both fresh items stale")
        self.assertIn("MILESTONE", value["choices"]["roadmap_kinds"])
        status, value = self.request("GET", "/api/state")
        self.assertEqual(value["roadmap_tree"]["views"]["standby"], ["ms"], "default 48 h: only the explicit flag counts")
        self.assertEqual(self.request("GET", "/api/state?standby=abc")[0], 404)
        status, value = self.request("GET", "/api/roadmap-tree?standby=72")
        self.assertEqual(value["tree"]["views"]["standby_hours"], 72.0)


if __name__ == "__main__":
    unittest.main()
