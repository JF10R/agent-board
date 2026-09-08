from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from agent_board import cli as board, tickets, web


class TicketDisplayIDsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = board.initialize(Path(self.temporary.name) / "atlas" / ".git" / "agent-board")

    def create(self, ticket_id):
        return tickets.create_ticket(self.root, actor="gpt-master", ticket_id=ticket_id, title=ticket_id)

    def test_create_race_allocates_unique_numbers(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            created = list(pool.map(self.create, [f"T{n}" for n in range(12)]))
        self.assertEqual({item["display_id"] for item in created}, {f"ATLAS-{n}" for n in range(1, 13)})
        for item in created:
            self.assertEqual(tickets.get_ticket(self.root, item["id"])["display_id"], item["display_id"])
            self.assertEqual(tickets.verify_ticket_chain(self.root, item["id"]), (True, "ok"))

    def test_backfill_is_idempotent_preserves_history_and_references(self):
        self.create("legacy-parent")
        self.create("legacy-child")
        tickets.add_dependency(self.root, "legacy-child", actor="gpt-master", dep_type="BLOCKED_BY", target="legacy-parent")
        (self.root / tickets.DISPLAY_IDS_FILE).unlink()  # simulate a pre-feature store
        paths = list((self.root / "ticket-events").glob("*.jsonl"))
        before = {path: path.read_bytes() for path in paths}
        mapping = tickets.migrate_ticket_display_ids(self.root)
        registry = (self.root / tickets.DISPLAY_IDS_FILE).read_bytes()
        self.assertEqual(mapping, tickets.migrate_ticket_display_ids(self.root))
        self.assertEqual(registry, (self.root / tickets.DISPLAY_IDS_FILE).read_bytes())
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        child = tickets.get_ticket(self.root, "legacy-child")
        self.assertEqual(child["deps"][0]["target"], "legacy-parent")
        self.assertEqual(child["revision"], 2)
        self.assertEqual(child["display_id"], mapping["legacy-child"])

    def test_archive_and_cache_loss_do_not_renumber_or_reuse(self):
        first = self.create("Z")
        tickets.transition_ticket(self.root, "Z", actor="gpt-master", stage="CANCELLED")
        tickets.archive_ticket(self.root, "Z", actor="gpt-master")
        second = self.create("A")
        self.assertEqual(first["display_id"], "ATLAS-1")
        self.assertEqual(second["display_id"], "ATLAS-2")
        (self.root / tickets.TICKETS_FILE).unlink()
        self.assertEqual(tickets.get_ticket(self.root, "Z")["display_id"], first["display_id"])
        self.assertEqual(self.create("B")["display_id"], "ATLAS-3")

    def test_project_prefix_freezes_and_namespaces_are_independent(self):
        tickets.migrate_ticket_display_ids(self.root, prefix="ATLAS")
        self.create("A")
        with self.assertRaisesRegex(board.BoardError, "already frozen"):
            tickets.migrate_ticket_display_ids(self.root, prefix="OTHER")
        other = board.initialize(Path(self.temporary.name) / "another" / ".git" / "agent-board")
        tickets.migrate_ticket_display_ids(other, prefix="ATLAS")
        self.assertEqual(tickets.create_ticket(other, actor="gpt-master", ticket_id="A", title="A")["display_id"], "ATLAS-1")

    def test_corrupt_registry_fails_closed(self):
        self.create("A")
        path = self.root / tickets.DISPLAY_IDS_FILE
        registry = board.json.loads(path.read_text(encoding="utf-8"))
        registry["next_number"] = 1
        path.write_text(board.json.dumps(registry), encoding="utf-8")
        with self.assertRaises(board.BoardError):
            self.create("B")
        self.assertFalse((self.root / "ticket-events" / "B.jsonl").exists())

    def test_migration_notifies_pollers_and_includes_archived_tickets(self):
        self.create("old")
        tickets.transition_ticket(self.root, "old", actor="gpt-master", stage="CANCELLED")
        tickets.archive_ticket(self.root, "old", actor="gpt-master")
        (self.root / tickets.DISPLAY_IDS_FILE).unlink()
        version = web.data_version(self.root)
        mapping = tickets.migrate_ticket_display_ids(self.root)
        self.assertIn("old", mapping)
        self.assertNotEqual(web.data_version(self.root), version)
