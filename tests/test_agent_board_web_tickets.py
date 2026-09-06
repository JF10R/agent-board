from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent_board import web


class TicketWebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "board"
        self.token = "test-token"
        self.server = web.create_server(self.root, port=0, token=self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: object | None = None, *, token: str | None = "use-default") -> tuple[int, dict[str, object] | str]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        host = f"127.0.0.1:{self.server.server_port}"
        headers: dict[str, str] = {"Host": host}
        encoded: str | None = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        headers["X-Agent-Board-Token"] = self.token if token == "use-default" else (token or "")
        if method == "POST":
            headers["Origin"] = f"http://{host}"
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        connection.close()
        value = json.loads(raw) if raw else {}
        return response.status, value

    def create_ticket(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {"actor": "sol-master", "id": "T1", "title": "Web ticket"}
        payload.update(overrides)
        status, value = self.request("POST", "/api/tickets", payload)
        self.assertEqual(status, 201, value)
        return value["ticket"]  # type: ignore[index]

    def test_create_get_and_list(self) -> None:
        created = self.create_ticket()
        self.assertEqual(created["stage"], "BACKLOG")
        status, value = self.request("GET", "/api/tickets/T1")
        self.assertEqual(status, 200)
        self.assertEqual(value["ticket"]["title"], "Web ticket")  # type: ignore[index]
        status, value = self.request("GET", "/api/tickets")
        self.assertEqual([item["id"] for item in value["tickets"]], ["T1"])  # type: ignore[index]

    def test_assign_review_and_done_flow(self) -> None:
        self.create_ticket()
        status, value = self.request("POST", "/api/tickets/T1/assign", {"actor": "sol-master", "assignee": "sol-master/worker"})
        self.assertEqual(status, 200, value)
        self.assertEqual(value["ticket"]["assignee"], "sol-master/worker")  # type: ignore[index]
        status, value = self.request("POST", "/api/tickets/T1/review", {"actor": "sol-master", "verdict": "PASS", "summary": "ok"})
        self.assertEqual(status, 200, value)
        status, value = self.request("POST", "/api/tickets/T1/done", {"actor": "sol-master"})
        self.assertEqual(status, 200, value)
        self.assertEqual(value["ticket"]["stage"], "DONE")  # type: ignore[index]

    def test_revision_conflict_reports_409(self) -> None:
        created = self.create_ticket()
        status, value = self.request(
            "POST", "/api/tickets/T1/transition",
            {"actor": "sol-master", "stage": "ANALYSIS", "expected_revision": created["revision"] + 1},
        )
        self.assertEqual(status, 409, value)
        self.assertIn("revision conflict", str(value))

    def test_write_routes_require_token(self) -> None:
        status, value = self.request("POST", "/api/tickets", {"actor": "sol-master", "id": "T1", "title": "x"}, token=None)
        self.assertEqual(status, 403)
        self.assertIn("token", str(value))

    def test_unknown_ticket_action_is_rejected(self) -> None:
        self.create_ticket()
        status, value = self.request("POST", "/api/tickets/T1/not-a-real-action", {"actor": "sol-master"})
        self.assertEqual(status, 400)
        self.assertIn("unknown ticket action", str(value))

    def test_actor_register_and_list(self) -> None:
        status, value = self.request("POST", "/api/actors", {"name": "sol-master", "role": "master"})
        self.assertEqual(status, 201, value)
        status, value = self.request("GET", "/api/actors")
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in value["actors"]], ["sol-master"])  # type: ignore[index]

    def test_state_carries_tickets_leases_and_actors_not_claims(self) -> None:
        self.create_ticket()
        status, value = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("tickets", value)
        self.assertIn("leases", value)
        self.assertIn("actors", value)
        self.assertNotIn("claims", value)

    def test_tree_and_critical_path_routes(self) -> None:
        self.create_ticket()
        status, value = self.request("GET", "/api/tickets/tree")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in value["tree"]], ["T1"])  # type: ignore[index]
        status, value = self.request("GET", "/api/tickets/critical-path")
        self.assertEqual(status, 200)
        self.assertEqual(value["critical_path"], ["T1"])


if __name__ == "__main__":
    unittest.main()
