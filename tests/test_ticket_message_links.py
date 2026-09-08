from pathlib import Path
import tempfile
import unittest
from agent_board import cli as board, tickets, web


class TicketMessageLinksTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = board.initialize(Path(self.temporary.name) / "board")
        tickets.create_ticket(self.root, actor="gpt-master", ticket_id="T1", title="Ticket")

    def post(self, **overrides):
        args = dict(sender="gpt-master", recipient="claude-master", kind="STATUS", priority="NORMAL", workstream="test", summary="Status")
        args.update(overrides)
        return board.post_message(self.root, **args)

    def test_link_persists_and_old_messages_remain_unchanged(self):
        legacy = self.post()
        path = self.root / "messages" / (legacy["id"] + ".md")
        before = path.read_bytes()
        linked = self.post(ticket_id="T1")
        self.assertEqual(board.read_message(self.root, linked["id"])[0]["ticket_id"], "T1")
        self.assertNotIn("ticket_id", board.read_message(self.root, legacy["id"])[0])
        self.assertEqual(path.read_bytes(), before)
        detail = web.ticket_detail(self.root, "T1")
        self.assertEqual([item["id"] for item in detail["linked_messages"]], [linked["id"]])
        state, _ = board.list_recent_messages(self.root)
        self.assertEqual(next(item for item in state if item["id"] == linked["id"])["ticket_id"], "T1")

    def test_unknown_cross_project_and_invalid_links_rejected(self):
        for reference in ("absent", "../T1", "OTHER-1"):
            with self.assertRaises(board.BoardError):
                self.post(ticket_id=reference)
        self.assertEqual(list((self.root / "messages").glob("*.md")), [])

    def test_web_post_and_parser_support_optional_link(self):
        status, result = web.send_messages(self.root, dict(actor="gpt-master", to="claude-master", kind="STATUS", priority="NORMAL", workstream="test", summary="Status", body="", requires_ack=False, ticket_id="T1"))
        self.assertEqual(status, 201)
        self.assertEqual(result["results"][0]["message"]["ticket_id"], "T1")
        args = board.build_parser().parse_args(["post", "--from", "gpt-master", "--to", "claude-master", "--kind", "STATUS", "--workstream", "test", "--summary", "Status", "--ticket-id", "T1"])
        self.assertEqual(args.ticket_id, "T1")

    def test_reverse_links_ignore_unrelated_malformed_metadata(self):
        self.post(ticket_id="T1")
        (self.root / "messages" / "malformed.md").write_text("broken", encoding="utf-8")
        detail = web.ticket_detail(self.root, "T1")
        self.assertEqual(len(detail["linked_messages"]), 1)
        self.assertEqual(detail["linked_messages_malformed"], 1)

    def test_reply_descendants_and_acknowledgements_preserve_explicit_link_boundary(self):
        tickets.create_ticket(self.root, actor="gpt-master", ticket_id="T2", title="Other")
        first = self.post(ticket_id="T1", requires_ack=True, created_at="2026-09-06T10:00:00Z")
        reply = self.post(reply_to=first["id"], created_at="2026-09-06T11:00:00Z")
        descendant = self.post(reply_to=reply["id"], created_at="2026-09-06T12:00:00Z")
        other = self.post(reply_to=descendant["id"], ticket_id="T2", created_at="2026-09-06T13:00:00Z")
        other_child = self.post(reply_to=other["id"], created_at="2026-09-06T14:00:00Z")
        before = {path: path.read_bytes() for path in (self.root / "messages").glob("*.md")}
        board.acknowledge(self.root, actor="claude-master", message_id=first["id"])
        linked = web.ticket_detail(self.root, "T1")["linked_messages"]
        self.assertEqual([item["id"] for item in linked], [descendant["id"], reply["id"], first["id"]])
        self.assertTrue(linked[-1]["acked"])
        self.assertTrue(linked[-1]["answered"])
        self.assertFalse(linked[0]["acked"])
        self.assertEqual(linked[-1]["replies"][0]["id"], reply["id"])
        self.assertEqual(linked[0]["replies"][0]["linked_ticket_id"], "T2")
        self.assertNotIn("ticket_id", linked[1])
        self.assertEqual(linked[1]["linked_ticket_id"], "T1")
        self.assertEqual([item["id"] for item in web.ticket_detail(self.root, "T2")["linked_messages"]], [other_child["id"], other["id"]])
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_reply_cycle_terminates_and_unlinked_cycle_stays_unlinked(self):
        first = self.post(ticket_id="T1")
        second = self.post(reply_to=first["id"])
        for reference in ("T1", None):
            fixture = {**first, "reply_to": second["id"]}
            if reference is None:
                fixture.pop("ticket_id")
            (self.root / "messages" / (first["id"] + ".md")).write_text(board._message_markdown(fixture, ""), encoding="utf-8")
            linked = web.ticket_detail(self.root, "T1")["linked_messages"]
            self.assertEqual(len(linked), 2 if reference else 0)

    def test_blockers_are_authoritative_including_archived_and_unknown_targets(self):
        tickets.create_ticket(self.root, actor="gpt-master", ticket_id="B", title="Blocker")
        tickets.add_dependency(self.root, "T1", actor="gpt-master", dep_type="BLOCKED_BY", target="B")
        self.assertEqual(web.ticket_detail(self.root, "T1")["open_blockers"], ["B"])
        tickets.transition_ticket(self.root, "B", actor="gpt-master", stage="CANCELLED")
        tickets.archive_ticket(self.root, "B", actor="gpt-master")
        self.assertEqual(next(item for item in web.ticket_list(self.root) if item["id"] == "T1")["open_blockers"], [])
        with tickets._ticket_lock(self.root):
            tickets._append_ticket_event(self.root, "T1", {"type": tickets.EV_DEP_ADD, "actor": "gpt-master", "dep_type": "BLOCKED_BY", "target": "missing"})
        self.assertEqual(web.ticket_detail(self.root, "T1")["open_blockers"], ["missing"])
