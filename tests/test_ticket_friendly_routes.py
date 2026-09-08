from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent_board import cli as board, tickets, web


class FriendlyTicketRoutesTest(unittest.TestCase):
    def test_clean_reload_resolution_legacy_api_and_human_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = board.initialize(Path(temporary) / "board")
            tickets.migrate_ticket_display_ids(root, prefix="ATLAS")
            created = tickets.create_ticket(root, actor="gpt-master", ticket_id="OLD-RAW", title="Ticket")
            server = web.create_server(root, port=0, token="test", projects=[web.Project("Atlas", root)])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path in ("/tickets/atlas/atlas-1", "/tickets/Atlas/OLD-RAW", "/"):
                    connection = HTTPConnection("127.0.0.1", server.server_port)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200, path)
                    self.assertIn("text/html", response.getheader("Content-Type"))
                    self.assertIn("no-store", response.getheader("Cache-Control"))
                    response.read()
                    connection.close()
                for path in ("/api/tickets/resolve/atlas-1?project=atlas", "/api/tickets/OLD-RAW?project=Atlas"):
                    connection = HTTPConnection("127.0.0.1", server.server_port)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["ticket"]["id"], created["id"])
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            for actor in ("operator", "lead"):
                comment = tickets.comment_ticket(root, "OLD-RAW", actor=actor, summary="Human input")
                self.assertEqual(comment["comments"][-1]["actor"], actor)
                with self.assertRaises(board.BoardError):
                    tickets.transition_ticket(root, "OLD-RAW", actor=actor, stage="DEVELOPMENT")
                with self.assertRaises(board.BoardError):
                    tickets.review_ticket(root, "OLD-RAW", actor=actor, verdict="PASS", summary="Review")
            with self.assertRaises(board.BoardError):
                web.resolve_ticket_reference(root, "atlas-999")
            with self.assertRaises(board.BoardError):
                web.resolve_ticket_reference(root, "../atlas-1")
