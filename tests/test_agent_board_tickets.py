from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_board import cli as board
from agent_board import tickets


class TicketLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = board.initialize(Path(self.temporary.name) / "board")

    def create(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "actor": "sol-master",
            "ticket_id": "T1",
            "title": "Ship the thing",
        }
        arguments.update(overrides)
        return tickets.create_ticket(self.root, **arguments)  # type: ignore[arg-type]

    def test_create_get_and_list_round_trip(self) -> None:
        created = self.create()
        self.assertEqual(created["stage"], "BACKLOG")
        self.assertEqual(created["revision"], 1)
        self.assertEqual(tickets.get_ticket(self.root, "T1")["title"], "Ship the thing")
        self.assertEqual([item["id"] for item in tickets.list_tickets(self.root)], ["T1"])

    def test_create_rejects_duplicate_id(self) -> None:
        self.create()
        with self.assertRaisesRegex(board.BoardError, "already exists"):
            self.create()

    def test_get_unknown_ticket_raises(self) -> None:
        with self.assertRaisesRegex(board.BoardError, "unknown ticket"):
            tickets.get_ticket(self.root, "ghost")

    def test_revision_conflict_on_stale_write(self) -> None:
        created = self.create()
        tickets.comment_ticket(self.root, "T1", actor="sol-master", summary="first")
        with self.assertRaisesRegex(board.BoardError, "revision conflict"):
            tickets.transition_ticket(
                self.root, "T1", actor="sol-master", stage="ANALYSIS", expected_revision=created["revision"]
            )

    def test_assign_opens_a_lease_and_heartbeat_extends_it(self) -> None:
        self.create()
        assigned = tickets.assign_ticket(self.root, "T1", actor="sol-master", assignee="sol-master/worker", ttl_sec=60)
        self.assertEqual(assigned["assignee"], "sol-master/worker")
        self.assertIsNotNone(assigned["lease"])
        self.assertFalse(assigned["lease_stale"])
        heartbeat = tickets.heartbeat_ticket(self.root, "T1", actor="sol-master/worker", ttl_sec=120)
        self.assertEqual(heartbeat["lease"]["ttl_sec"], 120)

    def test_heartbeat_requires_the_lease_holder(self) -> None:
        self.create()
        tickets.assign_ticket(self.root, "T1", actor="sol-master", assignee="sol-master/worker")
        with self.assertRaisesRegex(board.BoardError, "held by"):
            tickets.heartbeat_ticket(self.root, "T1", actor="someone-else")

    def test_done_clears_the_lease(self) -> None:
        self.create()
        tickets.assign_ticket(self.root, "T1", actor="sol-master", assignee="sol-master/worker")
        tickets.review_ticket(self.root, "T1", actor="sol-master", verdict="PASS", summary="looks good")
        done = tickets.mark_ticket_done(self.root, "T1", actor="sol-master")
        self.assertEqual(done["stage"], "DONE")
        self.assertIsNone(done["lease"])

    def test_transition_into_active_stage_is_blocked_by_open_dependency(self) -> None:
        self.create(ticket_id="BLOCKER", title="Must land first")
        self.create(ticket_id="T1")
        tickets.add_dependency(self.root, "T1", actor="sol-master", dep_type="BLOCKED_BY", target="BLOCKER")
        with self.assertRaisesRegex(board.BoardError, "blocked by"):
            tickets.transition_ticket(self.root, "T1", actor="sol-master", stage="DEVELOPMENT")
        # Closing the blocker (with a force done, no review needed for this check) clears the way.
        tickets.mark_ticket_done(self.root, "BLOCKER", actor="sol-master", force=True)
        moved = tickets.transition_ticket(self.root, "T1", actor="sol-master", stage="DEVELOPMENT")
        self.assertEqual(moved["stage"], "DEVELOPMENT")

    def test_dependency_add_is_symmetric_and_rejects_cycles(self) -> None:
        self.create(ticket_id="A")
        self.create(ticket_id="B")
        tickets.add_dependency(self.root, "A", actor="sol-master", dep_type="BLOCKED_BY", target="B")
        self.assertEqual(
            [dep["target"] for dep in tickets.get_ticket(self.root, "B")["deps"] if dep["type"] == "UNBLOCKS"],
            ["A"],
        )
        with self.assertRaisesRegex(board.BoardError, "cycle"):
            tickets.add_dependency(self.root, "B", actor="sol-master", dep_type="BLOCKED_BY", target="A")

    def test_transition_to_done_is_rejected_use_the_done_action(self) -> None:
        self.create()
        with self.assertRaisesRegex(board.BoardError, "use the done action"):
            tickets.transition_ticket(self.root, "T1", actor="sol-master", stage="DONE")

    def test_done_requires_a_passing_review_unless_forced(self) -> None:
        self.create()
        with self.assertRaisesRegex(board.BoardError, "requires an owning-master review"):
            tickets.mark_ticket_done(self.root, "T1", actor="sol-master")
        done = tickets.mark_ticket_done(self.root, "T1", actor="sol-master", force=True)
        self.assertEqual(done["stage"], "DONE")

    def test_review_requires_findings_when_not_pass(self) -> None:
        self.create()
        with self.assertRaisesRegex(board.BoardError, "requires numbered findings"):
            tickets.review_ticket(self.root, "T1", actor="sol-master", verdict="FAIL", summary="nope")
        reviewed = tickets.review_ticket(
            self.root, "T1", actor="sol-master", verdict="FAIL", summary="nope", findings=["missing tests"]
        )
        self.assertEqual(reviewed["stage"], "DEVELOPMENT")  # bounced back on FAIL

    def test_review_is_reserved_for_the_owning_master(self) -> None:
        self.create(reviewer="claude-master")
        with self.assertRaisesRegex(board.BoardError, "reserved for its owning master"):
            tickets.review_ticket(self.root, "T1", actor="sol-master", verdict="PASS", summary="ok")

    def test_worklog_requires_an_evidence_pointer(self) -> None:
        self.create()
        with self.assertRaisesRegex(board.BoardError, "evidence"):
            tickets.add_worklog(self.root, "T1", actor="sol-master", summary="did stuff", evidence={})
        logged = tickets.add_worklog(
            self.root, "T1", actor="sol-master", summary="ran tests",
            evidence={"test": "pytest -q", "exit_code": 0},
        )
        self.assertEqual(logged["worklog"][0]["evidence"]["exit_code"], 0)

    def test_archive_requires_a_terminal_stage(self) -> None:
        self.create()
        with self.assertRaisesRegex(board.BoardError, "DONE or CANCELLED"):
            tickets.archive_ticket(self.root, "T1", actor="sol-master")
        tickets.mark_ticket_done(self.root, "T1", actor="sol-master", force=True)
        archived = tickets.archive_ticket(self.root, "T1", actor="sol-master")
        self.assertTrue(archived["ticket"]["archived"])
        self.assertEqual(len(archived["content_hash"]), 64)
        self.assertEqual(tickets.list_tickets(self.root), [])  # archived tickets are hidden by default
        self.assertEqual(len(tickets.list_tickets(self.root, include_archived=True)), 1)

    def test_hash_chain_verifies_and_detects_tampering(self) -> None:
        self.create()
        tickets.comment_ticket(self.root, "T1", actor="sol-master", summary="a note")
        ok, detail = tickets.verify_ticket_chain(self.root, "T1")
        self.assertTrue(ok)
        self.assertEqual(detail, "ok")
        path = tickets._ticket_events_path(self.root, "T1")
        lines = path.read_text(encoding="utf-8").splitlines()
        tampered = lines[0].replace("Ship the thing", "Tampered title")
        path.write_text("\n".join([tampered] + lines[1:]) + "\n", encoding="utf-8")
        ok, detail = tickets.verify_ticket_chain(self.root, "T1")
        self.assertFalse(ok)
        self.assertIn("hash mismatch", detail)

    def test_ticket_tree_nests_children_under_their_parent(self) -> None:
        self.create(ticket_id="PARENT", title="Epic")
        self.create(ticket_id="CHILD", title="Slice", parent_id="PARENT")
        roots = tickets.ticket_tree(self.root)
        self.assertEqual([item["id"] for item in roots], ["PARENT"])
        self.assertEqual([item["id"] for item in roots[0]["children"]], ["CHILD"])

    def test_critical_path_follows_the_longest_blocked_by_chain(self) -> None:
        self.create(ticket_id="A")
        self.create(ticket_id="B")
        self.create(ticket_id="C")
        tickets.add_dependency(self.root, "B", actor="sol-master", dep_type="BLOCKED_BY", target="A")
        tickets.add_dependency(self.root, "C", actor="sol-master", dep_type="BLOCKED_BY", target="B")
        self.assertEqual(tickets.critical_path(self.root), ["C", "B", "A"])

    def test_metrics_digest_counts_by_stage_and_lists_stale(self) -> None:
        self.create()
        digest = tickets.metrics_digest(self.root)
        self.assertEqual(digest["counts_by_stage"]["BACKLOG"], 1)
        self.assertEqual(digest["throughput_done"], 0)

    def test_export_markdown_includes_title_and_stage(self) -> None:
        self.create()
        text = tickets.export_ticket_markdown(self.root, "T1")
        self.assertIn("# T1 — Ship the thing", text)
        self.assertIn("stage: `BACKLOG`", text)


class ActorRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = board.initialize(Path(self.temporary.name) / "board")

    def test_register_and_list_round_trip(self) -> None:
        tickets.register_actor(self.root, "sol-master", role="master")
        tickets.register_actor(self.root, "sol-master/worker", role="subagent")
        names = [item["name"] for item in tickets.list_actors(self.root)]
        self.assertEqual(names, ["sol-master", "sol-master/worker"])
        masters = [item["name"] for item in tickets.list_actors(self.root, role="master")]
        self.assertEqual(masters, ["sol-master"])

    def test_subagent_without_master_form_requires_explicit_master(self) -> None:
        with self.assertRaisesRegex(board.BoardError, "must be named"):
            tickets.register_actor(self.root, "worker", role="subagent")
        registered = tickets.register_actor(self.root, "worker", role="subagent", master="sol-master")
        self.assertEqual(registered["master"], "sol-master")

    def test_resolve_reviewer_prefers_registered_master(self) -> None:
        tickets.register_actor(self.root, "sol-master", role="master")
        tickets.register_actor(self.root, "sol-master/worker", role="subagent")
        self.assertEqual(tickets.resolve_reviewer(self.root, "sol-master/worker"), "sol-master")
        self.assertEqual(tickets.resolve_reviewer(self.root, "sol-master"), "sol-master")

    def test_master_role_rejects_master_sub_form(self) -> None:
        with self.assertRaisesRegex(board.BoardError, "cannot use the master/sub form"):
            tickets.register_actor(self.root, "sol-master/x", role="master")


if __name__ == "__main__":
    unittest.main()
