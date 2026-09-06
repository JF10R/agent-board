"""Read-only web endpoints added by the board redesign: tree in state, ack backlog, threads."""

from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent_board import cli as board
from agent_board import tree
from agent_board import web


class WebTreeEndpointsTest(unittest.TestCase):
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

    def get(self, path: str) -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", path, headers={"Host": f"127.0.0.1:{self.server.server_port}"})
        response = connection.getresponse()
        value = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, value

    def post(self, sender: str, recipient: str, summary: str, *, reply_to: str | None = None, requires_ack: bool = False, body: str = "") -> dict:
        return board.post_message(
            self.root, sender=sender, recipient=recipient, kind="STATUS", priority="NORMAL",
            workstream="BOARD", summary=summary, reply_to=reply_to, requires_ack=requires_ack, body=body,
        )

    def test_state_carries_tree_and_whole_board_ack_backlog(self) -> None:
        board.upsert_roadmap_item(self.root, actor="claude-master", item_id="prog", title="Program", summary="parent", status="IN_PROGRESS", owner="shared", progress=10, expected_revision=0)
        board.upsert_roadmap_item(self.root, actor="claude-master", item_id="kid", title="Kid", summary="child of prog. worktree", status="NOT_STARTED", owner="claude-master", progress=0, expected_revision=0)
        tree.annotate_roadmap_item(self.root, actor="claude-master", item_id="kid", gates=[("G1", "PENDING", "")])
        for index in range(3):
            self.post("lead", "claude-master", f"needs ack {index}", requires_ack=True)
        acked = self.post("lead", "sol-master", "acked one", requires_ack=True)
        board.acknowledge(self.root, actor="sol-master", message_id=acked["id"])
        self.post("lead", "operator", "plain", requires_ack=False)

        status, value = self.get("/api/state")
        self.assertEqual(status, 200)
        payload = value["roadmap_tree"]
        by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(by_id["kid"]["parent_id"], "prog")
        self.assertEqual(by_id["prog"]["children"], ["kid"])
        self.assertEqual(by_id["kid"]["gates"][0]["name"], "G1")
        self.assertIsNone(by_id["kid"]["progress_display"])
        self.assertEqual(payload["roots"], ["prog"])
        backlog = value["ack_backlog"]
        self.assertEqual(backlog["claude-master"]["count"], 3)
        self.assertEqual(len(backlog["claude-master"]["ids"]), 3)
        self.assertEqual(backlog["sol-master"]["count"], 0)
        self.assertEqual(backlog["operator"]["count"], 0)
        self.assertEqual(backlog["lead"]["count"], 0)
        status, value = self.get("/api/ack-backlog")
        self.assertEqual(status, 200)
        self.assertEqual(value["ack_backlog"]["claude-master"]["count"], 3)

    def test_roadmap_tree_route_validates_since_and_state_survives_a_corrupt_sidecar(self) -> None:
        board.upsert_roadmap_item(self.root, actor="claude-master", item_id="solo", title="Solo", summary="alone", status="IN_PROGRESS", owner="shared", progress=5, expected_revision=0)
        status, value = self.get("/api/roadmap-tree?since=1")
        self.assertEqual(status, 200)
        self.assertEqual(value["tree"]["since_hours"], 1.0)
        self.assertEqual([entry["id"] for entry in value["tree"]["moved"]], ["solo"])
        self.assertTrue(value["tree"]["moved"][0]["new"])
        status, value = self.get("/api/roadmap-tree?since=abc")
        self.assertEqual(status, 404)
        self.assertIn("since", value["error"])
        status, _ = self.get("/api/roadmap-tree?since=0")
        self.assertEqual(status, 404)
        (self.root / tree.EXT_FILE).write_text("{broken", encoding="utf-8")
        status, value = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("invalid roadmap extension", value["roadmap_tree"]["error"])
        self.assertEqual(value["roadmap"][0]["id"], "solo")
        status, _ = self.get("/api/roadmap-tree")
        self.assertEqual(status, 404)

    def test_thread_route_returns_the_whole_chain_with_bodies(self) -> None:
        root = self.post("lead", "claude-master", "root question", body="# Question\nWhy?")
        first = self.post("claude-master", "lead", "first answer", reply_to=root["id"], body="Because.")
        second = self.post("lead", "claude-master", "follow-up", reply_to=first["id"], requires_ack=True)
        sibling = self.post("sol-master", "lead", "aside", reply_to=root["id"])
        self.post("operator", "lead", "unrelated")
        status, value = self.get(f"/api/messages/{second['id']}/thread")
        self.assertEqual(status, 200)
        self.assertEqual(value["root"], root["id"])
        self.assertEqual(value["requested"], second["id"])
        self.assertEqual([item["id"] for item in value["messages"]], [root["id"], first["id"], second["id"], sibling["id"]])
        self.assertEqual(value["messages"][0]["body"], "# Question\nWhy?")
        self.assertFalse(value["messages"][2]["acked"])
        self.assertFalse(value["truncated"])
        status, _ = self.get("/api/messages/nope/thread")
        self.assertEqual(status, 404)
        status, _ = self.get("/api/messages/..%2Fstatus/thread")
        self.assertEqual(status, 404)

    def test_board_root_flag_serves_a_snapshot_directory(self) -> None:
        parser = web.build_parser()
        args = parser.parse_args(["--repo", ".", "--board-root", str(self.root), "--port", "0"])
        self.assertEqual(args.board_root, self.root)
        self.assertEqual(web.build_parser().parse_args(["--repo", "."]).board_root, None)


if __name__ == "__main__":
    unittest.main()
