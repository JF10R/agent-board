"""Tests for the additive roadmap tree model (tools/agent_board_tree.py).

Backward-compatibility proof: every write goes to roadmap-ext.v1.json; the bytes of
roadmap.v1.json are unchanged by annotations and the frozen v1 validator still accepts it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_board import cli as board
from agent_board import tree


# Items are stamped with the real clock by the frozen v1 upsert, so "now" must be real too.
T0 = datetime.now(timezone.utc)
FIXED = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
OLD = (T0 - timedelta(hours=30)).isoformat(timespec="seconds").replace("+00:00", "Z")


class RoadmapTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "board"
        board.initialize(self.root)

    def upsert(self, item_id: str, *, revision: int = 0, summary: str = "fixture", **overrides: object) -> dict:
        fields = dict(
            actor="claude-master",
            item_id=item_id,
            title=f"Title {item_id}",
            summary=summary,
            status="IN_PROGRESS",
            owner="claude-master",
            progress=0,
            blocker="",
            expected_revision=revision,
        )
        fields.update(overrides)
        return board.upsert_roadmap_item(self.root, **fields)

    def program(self) -> None:
        self.upsert("night", summary="parent program", progress=15)
        self.upsert("a1", summary="child of night. Sonnet worktree.", progress=10)
        self.upsert("a2", summary="child of night. Haiku worktree.")
        self.upsert("done", summary="child of night. ratified.", status="COMPLETE", progress=100)
        self.upsert("orphan", summary="child of nobody-known.")
        self.upsert("blocked", status="BLOCKED", blocker="waiting on provider", summary="child of night.")

    def test_summary_convention_builds_the_tree_without_a_sidecar(self) -> None:
        self.program()
        result = tree.load_roadmap_tree(self.root, now=T0, record=False)
        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual(by_id["a1"]["parent_id"], "night")
        self.assertEqual(by_id["a1"]["parent_source"], "summary")
        self.assertEqual(by_id["night"]["children"], ["a1", "a2", "blocked", "done"])
        self.assertIsNone(by_id["orphan"]["parent_id"])
        self.assertEqual(result["roots"], ["night", "orphan"])
        self.assertEqual(by_id["a1"]["depth"], 1)
        self.assertFalse((self.root / tree.EXT_FILE).exists())

    def test_progress_is_only_reported_when_set_or_closed(self) -> None:
        self.program()
        by_id = {item["id"]: item for item in tree.load_roadmap_tree(self.root, now=T0)["items"]}
        self.assertTrue(by_id["a1"]["progress_reported"])
        self.assertEqual(by_id["a1"]["progress_display"], 10)
        self.assertFalse(by_id["a2"]["progress_reported"])
        self.assertIsNone(by_id["a2"]["progress_display"])
        self.assertTrue(by_id["done"]["progress_reported"])
        rollup = by_id["night"]["rollup"]
        self.assertEqual(rollup["children"], 4)
        self.assertEqual(rollup["by_status"], {"BLOCKED": 1, "COMPLETE": 1, "IN_PROGRESS": 2})
        self.assertEqual(rollup["reported"], 2)
        self.assertEqual(rollup["mean_reported_progress"], 55)
        self.assertEqual(rollup["blocked"], 1)
        self.assertEqual(by_id["blocked"]["blockers"][0]["source"], "item")
        tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a2", progress_reported=True)
        by_id = {item["id"]: item for item in tree.load_roadmap_tree(self.root, now=T0)["items"]}
        self.assertTrue(by_id["a2"]["progress_reported"])
        self.assertEqual(by_id["a2"]["progress_display"], 0)

    def test_annotations_never_touch_the_frozen_v1_store(self) -> None:
        self.program()
        v1_path = self.root / "roadmap.v1.json"
        before = v1_path.read_bytes()
        item = tree.annotate_roadmap_item(
            self.root,
            actor="gpt-master",
            item_id="orphan",
            parent_id="night",
            add_blockers=["needs the I8 proof"],
            gates=[("G-A3-1", "pending", "0.999/0.990/3.0s pre-registered"), ("DETERMINISM", "PASS", "")],
            due="2026-09-02",
        )
        self.assertEqual(v1_path.read_bytes(), before)
        # The frozen validator still reads the store unchanged.
        store = board._read_roadmap_store(self.root)
        self.assertEqual(len(store["items"]), 6)
        self.assertEqual(item["parent_id"], "night")
        self.assertEqual(item["parent_source"], "annotation")
        self.assertEqual([blocker["text"] for blocker in item["blockers"]], ["needs the I8 proof"])
        self.assertEqual(item["blockers"][0]["added_by"], "gpt-master")
        self.assertEqual([(gate["name"], gate["state"]) for gate in item["gates"]], [("G-A3-1", "PENDING"), ("DETERMINISM", "PASS")])
        self.assertEqual(item["due"], "2026-09-02")
        self.assertEqual(item["updated_by"], "gpt-master")
        # Sidecar-only, and the base listing output is byte-identical to before.
        self.assertTrue((self.root / tree.EXT_FILE).exists())
        self.assertEqual(board.list_roadmap(self.root), store["items"])
        # Re-annotating replaces a gate by name and clears blockers on request.
        item = tree.annotate_roadmap_item(
            self.root, actor="gpt-master", item_id="orphan", gates=[("G-A3-1", "FAIL", "band drifted")], clear_blockers=True
        )
        self.assertEqual([(gate["name"], gate["state"]) for gate in item["gates"]], [("DETERMINISM", "PASS"), ("G-A3-1", "FAIL")])
        self.assertEqual(item["blockers"], [])
        item = tree.annotate_roadmap_item(self.root, actor="gpt-master", item_id="orphan", parent_id=None, due=None)
        self.assertIsNone(item["parent_id"])
        self.assertIsNone(item["due"])

    def test_annotation_validation_fails_closed(self) -> None:
        self.program()
        with self.assertRaisesRegex(board.BoardError, "unauthorized"):
            tree.annotate_roadmap_item(self.root, actor="lead", item_id="a1", due="2026-09-02")
        with self.assertRaisesRegex(board.BoardError, "unknown roadmap item"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="missing", due="2026-09-02")
        with self.assertRaisesRegex(board.BoardError, "unknown parent"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a1", parent_id="missing")
        with self.assertRaisesRegex(board.BoardError, "own parent"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a1", parent_id="a1")
        with self.assertRaisesRegex(board.BoardError, "cycle"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="night", parent_id="a1")
        with self.assertRaisesRegex(board.BoardError, "gate state"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a1", gates=[("G", "GREEN", "")])
        with self.assertRaisesRegex(board.BoardError, "ISO date"):
            tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a1", due="tomorrow")
        with self.assertRaisesRegex(board.BoardError, "NAME=STATE"):
            tree.parse_gate_spec("no-equals")
        self.assertEqual(tree.parse_gate_spec("G1=PASS:all green"), ("G1", "PASS", "all green"))
        self.assertFalse((self.root / tree.EXT_FILE).exists())

    def test_annotation_overrides_summary_convention_and_unknown_annotated_parent_warns(self) -> None:
        self.program()
        tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a1", parent_id=None)
        result = tree.load_roadmap_tree(self.root, now=T0)
        by_id = {item["id"]: item for item in result["items"]}
        self.assertIsNone(by_id["a1"]["parent_id"])
        self.assertNotIn("a1", by_id["night"]["children"])
        ext = json.loads((self.root / tree.EXT_FILE).read_text(encoding="utf-8"))
        ext["items"]["a2"] = {"parent_id": "vanished"}
        (self.root / tree.EXT_FILE).write_text(json.dumps(ext), encoding="utf-8")
        result = tree.load_roadmap_tree(self.root, now=T0)
        self.assertIsNone({item["id"]: item for item in result["items"]}["a2"]["parent_id"])
        self.assertTrue(any("vanished" in warning for warning in result["warnings"]))

    def test_summary_cycle_is_broken_deterministically(self) -> None:
        self.upsert("x", summary="child of y")
        self.upsert("y", summary="child of x")
        result = tree.load_roadmap_tree(self.root, now=T0, record=False)
        by_id = {item["id"]: item for item in result["items"]}
        self.assertIsNone(by_id["x"]["parent_id"])
        self.assertEqual(by_id["y"]["parent_id"], "x")
        self.assertEqual(result["roots"], ["x"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_journal_records_each_revision_once_and_moved_reports_diffs(self) -> None:
        self.upsert("a1", summary="fixture")
        with mock.patch.object(board, "utc_now", return_value=OLD):
            self.upsert("old", summary="untouched for thirty hours")
        tree.load_roadmap_tree(self.root, now=T0)
        self.upsert("a1", revision=1, status="IN_PROGRESS", progress=40, owner="shared")
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: tree.load_roadmap_tree(self.root, now=T0), range(32)))
        ext = json.loads((self.root / tree.EXT_FILE).read_text(encoding="utf-8"))
        keys = [(entry["id"], entry["revision"]) for entry in ext["journal"]]
        self.assertEqual(sorted(keys), [("a1", 1), ("a1", 2), ("old", 1)])
        result = results[-1]
        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual(
            by_id["a1"]["last_change"]["changes"],
            [{"field": "progress", "from": 0, "to": 40}, {"field": "owner", "from": "claude-master", "to": "shared"}],
        )
        self.assertTrue(by_id["a1"]["last_change"]["prior_known"])
        self.assertEqual([entry["revision"] for entry in by_id["a1"]["history"]], [1, 2])
        self.assertTrue(by_id["a1"]["moved"])
        self.assertFalse(by_id["old"]["moved"])
        self.assertEqual([entry["id"] for entry in result["moved"]], ["a1"])
        self.assertFalse(result["moved"][0]["new"])
        self.assertEqual(result["counts"]["moved"], 1)
        # A narrower window drops it; a wider one keeps the untouched item too.
        later = T0 + timedelta(hours=2)
        self.assertEqual(tree.load_roadmap_tree(self.root, now=later, since_hours=1)["moved"], [])
        self.assertEqual({entry["id"] for entry in tree.load_roadmap_tree(self.root, now=later, since_hours=48)["moved"]}, {"a1", "old"})

    def test_item_first_seen_at_a_late_revision_is_honest_about_the_unknown_prior(self) -> None:
        self.upsert("late")
        self.upsert("late", revision=1, progress=30)
        self.upsert("late", revision=2, progress=60)
        result = tree.load_roadmap_tree(self.root, now=T0)
        item = result["items"][0]
        self.assertEqual(item["revision"], 3)
        self.assertFalse(item["last_change"]["prior_known"])
        self.assertEqual(item["last_change"]["changes"], [])
        self.assertFalse(result["moved"][0]["prior_known"])
        self.assertFalse(result["moved"][0]["new"])

    def test_read_only_load_never_creates_the_sidecar(self) -> None:
        self.program()
        before = sorted(path.name for path in self.root.iterdir())
        tree.load_roadmap_tree(self.root, now=T0, record=False)
        self.assertFalse((self.root / tree.EXT_FILE).exists())
        # Not even the lock sentinel: read-only means no new file on the board at all.
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)
        with self.assertRaisesRegex(board.BoardError, "since_hours"):
            tree.load_roadmap_tree(self.root, since_hours=0)

    def test_corrupt_sidecar_fails_closed(self) -> None:
        self.program()
        (self.root / tree.EXT_FILE).write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(board.BoardError, "invalid roadmap extension"):
            tree.load_roadmap_tree(self.root, now=T0)
        (self.root / tree.EXT_FILE).write_text(json.dumps({"schema_version": 9, "items": {}, "journal": []}), encoding="utf-8")
        with self.assertRaisesRegex(board.BoardError, "shape"):
            tree.load_roadmap_tree(self.root, now=T0)

    def test_text_rendering_shows_tree_blockers_gates_and_unreported_progress(self) -> None:
        self.program()
        tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="a2", gates=[("G1", "PENDING", "awaits pull")], due="2026-09-02")
        result = tree.load_roadmap_tree(self.root, now=T0)
        text = tree.render_tree_text(result, now=T0)
        self.assertIn("night  IN_PROGRESS   15%", text)
        self.assertIn("├─ a1  IN_PROGRESS   10%", text)
        self.assertIn("├─ a2  IN_PROGRESS    --", text)
        self.assertIn("◇ gate G1: PENDING — awaits pull", text)
        self.assertIn("! blocker: waiting on provider", text)
        self.assertIn("due 2026-09-02", text)
        self.assertIn("[4 children:", text)
        self.assertIn("-- = progress not reported", text)
        subtree = tree.render_tree_text(result, root_id="orphan", now=T0)
        self.assertNotIn("night", subtree)
        with self.assertRaisesRegex(board.BoardError, "unknown roadmap item"):
            tree.render_tree_text(result, root_id="nope")
        self.assertEqual(tree.relative_age("2026-09-01T11:59:30Z", now=FIXED), "30s ago")
        self.assertEqual(tree.relative_age("2026-09-01T10:30:00Z", now=FIXED), "1h ago")
        self.assertEqual(tree.relative_age("2026-08-30T10:30:00Z", now=FIXED), "2d ago")
        self.assertEqual(tree.relative_age("2026-09-01T12:30:00Z", now=FIXED), "in the future")
        self.assertEqual(tree.relative_age("garbage", now=FIXED), "unknown")

    def test_cli_tree_and_annotate_are_additive_subcommands(self) -> None:
        self.program()
        def run(*argv: str) -> str:
            output = io.StringIO()
            with mock.patch.object(board, "board_root", return_value=self.root), redirect_stdout(output):
                self.assertEqual(board.run(["--repo", ".", "roadmap", *argv]), 0)
            return output.getvalue()

        text = run("tree", "--no-journal")
        self.assertIn("└─ done", text)
        self.assertFalse((self.root / tree.EXT_FILE).exists())
        annotated = json.loads(
            run(
                "annotate", "--actor", "claude-master", "--id", "orphan", "--parent", "night",
                "--add-blocker", "needs I8 proof", "--gate", "G1=PASS:green", "--due", "2026-09-02", "--progress-unreported",
            )
        )
        self.assertEqual(annotated["parent_id"], "night")
        self.assertEqual(annotated["gates"][0]["state"], "PASS")
        self.assertFalse(annotated["progress_reported"])
        payload = json.loads(run("tree", "--json", "--root", "night"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("orphan", {item["id"]: item for item in payload["items"]}["night"]["children"])
        # The frozen listing output carries no extension fields.
        self.assertNotIn("parent_id", run("list"))


if __name__ == "__main__":
    unittest.main()
