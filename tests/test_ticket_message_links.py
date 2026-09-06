from pathlib import Path
import tempfile
import unittest
from agent_board import cli as board, tickets, web


class TicketMessageLinksTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = board.initialize(Path(self.temporary.name) / "board")
        tickets.create_ticket(self.root, actor="sol-master", ticket_id="T1", title="Ticket")

    def post(self, **overrides):
        args = dict(sender="sol-master", recipient="claude-master", kind="STATUS", priority="NORMAL", workstream="test", summary="Status")
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
        status, result = web.send_messages(self.root, dict(actor="sol-master", to="claude-master", kind="STATUS", priority="NORMAL", workstream="test", summary="Status", body="", requires_ack=False, ticket_id="T1"))
        self.assertEqual(status, 201)
        self.assertEqual(result["results"][0]["message"]["ticket_id"], "T1")
        args = board.build_parser().parse_args(["post", "--from", "sol-master", "--to", "claude-master", "--kind", "STATUS", "--workstream", "test", "--summary", "Status", "--ticket-id", "T1"])
        self.assertEqual(args.ticket_id, "T1")

    def test_reverse_links_ignore_unrelated_malformed_metadata(self):
        self.post(ticket_id="T1")
        (self.root / "messages" / "malformed.md").write_text("broken", encoding="utf-8")
        detail = web.ticket_detail(self.root, "T1")
        self.assertEqual(len(detail["linked_messages"]), 1)
        self.assertEqual(detail["linked_messages_malformed"], 1)
