from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_board import cli as board
from agent_board import web


class AgentBoardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = board.initialize(Path(self.temporary.name) / "board")

    def post(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "sender": "sol-master",
            "recipient": "claude-master",
            "kind": "STATUS",
            "priority": "NORMAL",
            "workstream": "BOARD",
            "summary": "Board ready",
            "body": "Shared runtime initialized.",
        }
        arguments.update(overrides)
        return board.post_message(self.root, **arguments)  # type: ignore[arg-type]

    def test_init_creates_only_the_runtime_directories(self) -> None:
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["acks", "leases", "locks", "messages", "status", "ticket-events"],
        )

    def test_message_round_trip_and_ack_are_separate(self) -> None:
        message = self.post(requires_ack=True)
        message_id = str(message["id"])
        metadata, body, raw = board.read_message(self.root, message_id)
        self.assertEqual(metadata, message)
        self.assertEqual(body.strip(), "Shared runtime initialized.")
        self.assertIn("requires_ack: true", raw)
        self.assertFalse(board._ack_path(self.root, message_id, "claude-master").exists())

        ack = board.acknowledge(self.root, actor="claude-master", message_id=message_id)
        self.assertEqual(ack["message_id"], message_id)
        self.assertTrue(board._ack_path(self.root, message_id, "claude-master").is_file())
        self.assertTrue(board.inbox(self.root, actor="claude-master")[0]["acked"])

    def test_only_recipient_can_ack(self) -> None:
        message_id = str(self.post()["id"])
        with self.assertRaisesRegex(board.BoardError, "cannot acknowledge"):
            board.acknowledge(self.root, actor="sol-master", message_id=message_id)

    def test_unauthorized_identity_cannot_write(self) -> None:
        with self.assertRaisesRegex(board.BoardError, "unauthorized identity"):
            self.post(sender="subagent-a")
        with self.assertRaisesRegex(board.BoardError, "unauthorized identity"):
            board.publish_status(
                self.root, actor="subagent-a", state="ACTIVE", summary="not allowed"
            )

    def test_human_message_participants_can_receive_and_ack_but_not_mutate(self) -> None:
        for participant in ("lead", "operator"):
            with self.subTest(participant=participant):
                message = self.post(sender=participant)
                self.assertEqual(message["from"], participant)
                self.assertEqual(board.read_message(self.root, str(message["id"]))[0], message)
                inbound = self.post(recipient=participant, requires_ack=True)
                self.assertEqual(board.inbox(self.root, actor=participant)[0]["to"], participant)
                ack = board.acknowledge(
                    self.root, actor=participant, message_id=str(inbound["id"])
                )
                self.assertEqual(ack["by"], participant)
                with self.assertRaisesRegex(board.BoardError, "unauthorized identity"):
                    board.publish_status(
                        self.root, actor=participant, state="ACTIVE", summary="not allowed"
                    )

    def test_message_create_is_exclusive_under_concurrency(self) -> None:
        fixed_id = "fixed-message-id"

        def attempt(index: int) -> str:
            try:
                self.post(message_id=fixed_id, summary=f"writer {index}")
                return "won"
            except board.BoardError:
                return "lost"

        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(pool.map(attempt, range(64)))
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(outcomes.count("lost"), 63)
        messages = list((self.root / "messages").glob("*.md"))
        self.assertEqual(len(messages), 1)
        metadata, _ = board.parse_message(messages[0].read_text(encoding="utf-8"))
        self.assertEqual(metadata["id"], fixed_id)

    def test_message_is_immutable_after_creation(self) -> None:
        message_id = str(self.post(message_id="immutable")["id"])
        original = (self.root / "messages" / f"{message_id}.md").read_bytes()
        with self.assertRaisesRegex(board.BoardError, "immutable"):
            self.post(message_id=message_id, summary="replacement")
        self.assertEqual((self.root / "messages" / f"{message_id}.md").read_bytes(), original)

    def test_exclusive_publish_never_exposes_partial_and_cleans_failure(self) -> None:
        failed_path = self.root / "messages" / "failed.md"
        with mock.patch.object(board.os, "fsync", side_effect=OSError("injected write failure")):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                self.post(message_id="failed")
        self.assertFalse(failed_path.exists())
        self.assertEqual(list((self.root / "messages").glob("*.tmp")), [])

        publish_reached = threading.Event()
        allow_publish = threading.Event()
        original_link = board.os.link

        def paused_link(source: str, destination: Path) -> None:
            publish_reached.set()
            self.assertTrue(allow_publish.wait(timeout=2))
            original_link(source, destination)

        outcome: list[object] = []
        with mock.patch.object(board.os, "link", side_effect=paused_link):
            writer = threading.Thread(
                target=lambda: outcome.append(self.post(message_id="reader-safe", body="x" * 8192))
            )
            writer.start()
            self.assertTrue(publish_reached.wait(timeout=2))
            final_path = self.root / "messages" / "reader-safe.md"
            for _ in range(50):
                self.assertFalse(final_path.exists())
                time.sleep(0.001)
            allow_publish.set()
            writer.join(timeout=2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(len(outcome), 1)
        _, body, _ = board.read_message(self.root, "reader-safe")
        self.assertEqual(body.strip(), "x" * 8192)
        self.assertEqual(list((self.root / "messages").glob("*.tmp")), [])

    def test_exclusive_lock_paths_live_in_a_dedicated_locks_directory(self) -> None:
        self.assertEqual(
            board._exclusive_lock_path(self.root / "messages" / "abc.md"),
            self.root / "locks" / "messages--abc.md.lock",
        )
        self.assertEqual(
            board._exclusive_lock_path(self.root / "acks" / "abc--claude-master.json"),
            self.root / "locks" / "acks--abc--claude-master.json.lock",
        )
        self.assertEqual(
            board._exclusive_lock_path(self.root / "leases" / "T1.json"),
            self.root / "locks" / "leases--T1.json.lock",
        )
        self.assertEqual(
            board._exclusive_lock_path(self.root / "status" / "sol-master.json"),
            self.root / "locks" / "status--sol-master.json.lock",
        )

    def test_posting_a_message_leaves_no_lock_dotfile_beside_it(self) -> None:
        message_id = str(self.post()["id"])
        message_path = self.root / "messages" / f"{message_id}.md"
        self.assertTrue(message_path.is_file())
        self.assertEqual(
            sorted(path.name for path in (self.root / "messages").iterdir()),
            [f"{message_id}.md"],
        )
        self.assertTrue((self.root / "locks" / f"messages--{message_id}.md.lock").is_file())

    def test_write_exclusive_still_serializes_concurrent_writers_via_locks_dir(self) -> None:
        fixed_id = "locks-dir-fixed-id"

        def attempt(index: int) -> str:
            try:
                self.post(message_id=fixed_id, summary=f"writer {index}")
                return "won"
            except board.BoardError:
                return "lost"

        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(pool.map(attempt, range(32)))
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(outcomes.count("lost"), 31)
        self.assertTrue((self.root / "locks" / f"messages--{fixed_id}.md.lock").is_file())

    def test_maintenance_removes_stale_legacy_dotfile_locks_but_keeps_held_ones(self) -> None:
        message_id = str(self.post()["id"])
        legacy_stale = self.root / "messages" / ".stale-leftover.md.lock"
        legacy_stale.write_bytes(b"0")
        legacy_held = self.root / "acks" / ".held.json.lock"
        legacy_held.write_bytes(b"0")
        not_a_lock = self.root / "messages" / f"{message_id}.md"

        held_handle = legacy_held.open("r+b")
        try:
            if board.os.name == "nt":
                import msvcrt

                msvcrt.locking(held_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(held_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            report = board.remove_stale_locks(self.root)
            self.assertEqual(report, {"removed": 1, "kept": 1})
            self.assertFalse(legacy_stale.exists())
            self.assertTrue(legacy_held.exists())
            self.assertTrue(not_a_lock.exists())
        finally:
            if board.os.name == "nt":
                import msvcrt

                msvcrt.locking(held_handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(held_handle.fileno(), fcntl.LOCK_UN)
            held_handle.close()

        second_report = board.remove_stale_locks(self.root)
        self.assertEqual(second_report, {"removed": 1, "kept": 0})
        self.assertFalse(legacy_held.exists())

        idempotent_report = board.remove_stale_locks(self.root)
        self.assertEqual(idempotent_report, {"removed": 0, "kept": 0})

    def test_maintenance_cli_reports_removed_count(self) -> None:
        (self.root / "acks" / ".ghost.json.lock").write_bytes(b"0")
        output = io.StringIO()
        with mock.patch.object(board, "board_root", return_value=self.root), redirect_stdout(output):
            self.assertEqual(board.run(["--repo", ".", "maintenance"]), 0)
        self.assertIn('"removed": 1', output.getvalue())

    def test_listing_readers_ignore_junk_entries_in_messages_directory(self) -> None:
        message_id = str(self.post()["id"])
        (self.root / "messages" / ".junk.md.lock").write_bytes(b"0")
        (self.root / "messages" / ".partial.md.tmp").write_text("garbage", encoding="utf-8")
        messages, meta = board.list_recent_messages(self.root)
        self.assertEqual([message["id"] for message in messages], [message_id])
        self.assertEqual(meta["malformed"], 0)
        self.assertEqual(meta["oversized"], 0)
        inbox_entries = board.inbox(self.root, actor="claude-master")
        self.assertEqual([entry["id"] for entry in inbox_entries], [message_id])

    def test_file_lock_high_contention_closes_every_handle(self) -> None:
        lock_path = self.root / ".contention.lock"
        counter = 0

        def increment(_: int) -> None:
            nonlocal counter
            with board._file_lock(lock_path):
                current = counter
                time.sleep(0.0005)
                counter = current + 1

        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(increment, range(256)))
        self.assertEqual(counter, 256)
        lock_path.unlink()
        self.assertFalse(lock_path.exists())

    def test_status_replace_never_exposes_partial_json(self) -> None:
        def publish(index: int) -> None:
            board.publish_status(
                self.root,
                actor="sol-master",
                state="ACTIVE",
                workstream=f"stream-{index}",
                summary=f"update {index}",
                paths=[f"path/{index}"],
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(publish, range(64)))
        status_path = self.root / "status" / "sol-master.json"
        parsed = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["identity"], "sol-master")
        self.assertEqual(parsed["state"], "ACTIVE")
        self.assertTrue(parsed["summary"].startswith("update "))
        self.assertEqual(list((self.root / "status").glob("*.tmp")), [])

    def test_roadmap_upsert_is_atomic_and_revision_guarded(self) -> None:
        item = board.upsert_roadmap_item(
            self.root,
            actor="sol-master",
            item_id="M0",
            title="Board UI",
            summary="Local coordination dashboard",
            status="IN_PROGRESS",
            owner="shared",
            progress=40,
            expected_revision=0,
        )
        self.assertEqual(item["revision"], 1)
        self.assertEqual(board.get_roadmap_item(self.root, "M0"), item)
        with self.assertRaisesRegex(board.BoardError, "revision conflict"):
            board.upsert_roadmap_item(
                self.root,
                actor="claude-master",
                item_id="M0",
                title="Stale update",
                summary="Must not overwrite",
                status="COMPLETE",
                owner="shared",
                progress=100,
                expected_revision=0,
            )
        stored = json.loads((self.root / "roadmap.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(stored["items"], [item])
        self.assertEqual(list(self.root.glob(".roadmap.v1.json.*.tmp")), [])

    def test_roadmap_concurrent_update_has_one_winner(self) -> None:
        board.upsert_roadmap_item(
            self.root,
            actor="sol-master",
            item_id="M1",
            title="Initial",
            summary="Initial item",
            status="PENDING",
            owner="unassigned",
            progress=0,
            expected_revision=0,
        )

        def attempt(index: int) -> str:
            try:
                board.upsert_roadmap_item(
                    self.root,
                    actor="sol-master" if index % 2 else "claude-master",
                    item_id="M1",
                    title=f"Writer {index}",
                    summary="Concurrent update",
                    status="IN_PROGRESS",
                    owner="shared",
                    progress=index,
                    expected_revision=1,
                )
                return "won"
            except board.BoardError:
                return "lost"

        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(attempt, range(10)))
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(board.get_roadmap_item(self.root, "M1")["revision"], 2)
        json.loads((self.root / "roadmap.v1.json").read_text(encoding="utf-8"))

    def test_roadmap_validation_rejects_bad_values(self) -> None:
        base = dict(
            actor="sol-master",
            item_id="M2",
            title="Validation",
            summary="Validation fixture",
            status="PENDING",
            owner="unassigned",
            progress=0,
            expected_revision=0,
        )
        with self.assertRaisesRegex(board.BoardError, "progress"):
            board.upsert_roadmap_item(self.root, **{**base, "progress": 101})
        with self.assertRaisesRegex(board.BoardError, "owner"):
            board.upsert_roadmap_item(self.root, **{**base, "owner": "subagent"})
        with self.assertRaisesRegex(board.BoardError, "unauthorized"):
            board.upsert_roadmap_item(self.root, **{**base, "actor": "subagent"})
        invalid_cross_fields = (
            ({"status": "BLOCKED", "blocker": ""}, "require a blocker"),
            ({"status": "PENDING", "blocker": "not allowed"}, "only BLOCKED"),
            ({"status": "COMPLETE", "progress": 99}, "progress 100"),
            ({"status": "IN_PROGRESS", "progress": 100}, "requires status COMPLETE"),
        )
        for overrides, error in invalid_cross_fields:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(board.BoardError, error):
                    board.upsert_roadmap_item(self.root, **{**base, **overrides})

    def test_roadmap_cli_upsert_and_get(self) -> None:
        output = io.StringIO()
        with mock.patch.object(board, "board_root", return_value=self.root), redirect_stdout(output):
            self.assertEqual(
                board.run(
                    [
                        "--repo", ".", "roadmap", "upsert",
                        "--actor", "sol-master", "--id", "CLI-1",
                        "--title", "CLI item", "--summary", "Created by CLI",
                        "--status", "COMPLETE", "--owner", "sol-master",
                        "--progress", "100", "--expected-revision", "0",
                    ]
                ),
                0,
            )
            self.assertEqual(board.run(["--repo", ".", "roadmap", "get", "CLI-1"]), 0)
        self.assertIn('"revision": 1', output.getvalue())
        self.assertEqual(board.get_roadmap_item(self.root, "CLI-1")["status"], "COMPLETE")

    def test_legacy_roadmap_blocker_migrates_once_under_concurrency(self) -> None:
        legacy = {
            "schema_version": 1,
            "items": [
                {
                    "id": "LEGACY-1",
                    "title": "Pre-hardening item",
                    "summary": "Must remain readable",
                    "status": "IN_PROGRESS",
                    "owner": "shared",
                    "progress": 60,
                    "blocker": "Waiting for review",
                    "updated_at": "2026-08-30T12:00:00Z",
                    "revision": 7,
                }
            ],
        }
        board._write_atomic_replace(
            self.root / "roadmap.v1.json", board._canonical_json(legacy)
        )

        with ThreadPoolExecutor(max_workers=16) as pool:
            snapshots = list(pool.map(lambda _: board.list_roadmap(self.root), range(64)))
        self.assertTrue(all(snapshot[0]["status"] == "BLOCKED" for snapshot in snapshots))
        self.assertTrue(all(snapshot[0]["revision"] == 8 for snapshot in snapshots))
        migrated = board.get_roadmap_item(self.root, "LEGACY-1")
        self.assertEqual(migrated["blocker"], "Waiting for review")
        self.assertEqual(migrated["owner"], "shared")
        self.assertEqual(migrated["summary"], "Must remain readable")

        updated = board.upsert_roadmap_item(
            self.root,
            actor="sol-master",
            item_id="LEGACY-1",
            title=migrated["title"],
            summary=migrated["summary"],
            status="IN_PROGRESS",
            owner=migrated["owner"],
            progress=70,
            blocker="",
            expected_revision=8,
        )
        self.assertEqual(updated["revision"], 9)
        self.assertEqual(updated["status"], "IN_PROGRESS")
        self.assertEqual(list(self.root.glob(".roadmap.v1.json.*.tmp")), [])


class GitCommonDirTest(unittest.TestCase):
    def git(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_linked_worktrees_resolve_the_same_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "main"
            linked = root / "linked"
            repository.mkdir()
            self.git(repository, "init")
            self.git(repository, "config", "user.email", "test@example.invalid")
            self.git(repository, "config", "user.name", "Agent Board Test")
            (repository / "README.md").write_text("fixture\n", encoding="utf-8")
            self.git(repository, "add", "README.md")
            self.git(repository, "commit", "-m", "fixture")
            self.git(repository, "worktree", "add", "-b", "linked-test", str(linked))

            main_root = board.board_root(repository)
            linked_root = board.board_root(linked)
            self.assertEqual(main_root, linked_root)
            board.initialize(main_root)
            message = board.post_message(
                main_root,
                sender="sol-master",
                recipient="claude-master",
                kind="STATUS",
                priority="NORMAL",
                workstream="BOARD",
                summary="visible from both worktrees",
            )
            self.assertEqual(board.read_message(linked_root, str(message["id"]))[0], message)


class AgentBoardWebTest(unittest.TestCase):
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

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        token: str | None = None,
        content_type: str = "application/json",
        host: str | None = None,
        origin: str | None = "DEFAULT",
    ) -> tuple[int, dict[str, object] | str, str]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        resolved_host = host or f"127.0.0.1:{self.server.server_port}"
        headers: dict[str, str] = {"Host": resolved_host}
        encoded: str | None = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = content_type
        if token is not None:
            headers["X-Agent-Board-Token"] = token
        if method == "POST" and origin == "DEFAULT":
            headers["Origin"] = f"http://{resolved_host}"
        elif origin is not None:
            headers["Origin"] = origin
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        response_type = response.getheader("Content-Type", "")
        connection.close()
        value: dict[str, object] | str = json.loads(raw) if response_type.startswith("application/json") else raw
        return response.status, value, response_type

    def message_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "actor": "sol-master",
            "to": "claude-master",
            "kind": "STATUS",
            "priority": "NORMAL",
            "workstream": "BOARD",
            "summary": "Web fixture",
            "body": "Hello",
            "requires_ack": False,
        }
        payload.update(overrides)
        return payload

    def test_write_api_requires_token_and_strict_json(self) -> None:
        status, value, _ = self.request("POST", "/api/messages", self.message_payload())
        self.assertEqual(status, 403)
        self.assertIn("token", str(value))

        status, value, _ = self.request(
            "POST",
            "/api/messages",
            self.message_payload(extra="rejected"),
            token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown JSON fields", str(value))

        status, value, _ = self.request(
            "POST",
            "/api/messages",
            self.message_payload(),
            token=self.token,
            content_type="text/plain",
        )
        self.assertEqual(status, 400)
        self.assertIn("Content-Type", str(value))

    def test_host_and_origin_defeat_dns_rebinding_and_cross_site_posts(self) -> None:
        status, page, _ = self.request("GET", "/", host="attacker.example")
        self.assertEqual(status, 403)
        self.assertNotIn(self.token, str(page))
        status, _, _ = self.request("GET", "/api/state", host=f"localhost:{self.server.server_port}")
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST",
            "/api/messages",
            self.message_payload(),
            token=self.token,
            origin="https://attacker.example",
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST", "/api/messages", self.message_payload(), token=self.token, origin=None
        )
        self.assertEqual(status, 403)
        self.assertEqual(list((self.root / "messages").glob("*.md")), [])

    def test_both_recipient_api_creates_two_normal_messages(self) -> None:
        status, value, _ = self.request(
            "POST", "/api/messages", self.message_payload(to="BOTH"), token=self.token
        )
        self.assertEqual(status, 201)
        self.assertTrue(value["ok"])  # type: ignore[index]
        results = value["results"]  # type: ignore[index]
        self.assertEqual({item["recipient"] for item in results}, set(board.IDENTITIES))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(len(list((self.root / "messages").glob("*.md"))), 2)

    def test_human_sender_choices_and_both_delivery(self) -> None:
        status, value, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        choices = value["choices"]  # type: ignore[index]
        self.assertEqual(set(choices["message_senders"]), board.MESSAGE_SENDERS)
        self.assertEqual(set(choices["message_recipients"]), board.MESSAGE_RECIPIENTS)
        self.assertEqual(set(choices["message_actors"]), board.MESSAGE_SENDERS)
        self.assertEqual(set(choices["identities"]), board.IDENTITIES)

        status, value, _ = self.request(
            "POST",
            "/api/messages",
            self.message_payload(actor="lead", to="BOTH"),
            token=self.token,
        )
        self.assertEqual(status, 201)
        self.assertTrue(value["ok"])  # type: ignore[index]
        messages = [
            board.read_message(self.root, str(item["message"]["id"]))[0]
            for item in value["results"]  # type: ignore[index]
        ]
        self.assertEqual({message["from"] for message in messages}, {"lead"})
        self.assertEqual({message["to"] for message in messages}, set(board.IDENTITIES))

        status, value, _ = self.request(
            "POST",
            "/api/messages",
            self.message_payload(to="operator", requires_ack=True),
            token=self.token,
        )
        self.assertEqual(status, 201)
        direct = value["results"][0]["message"]  # type: ignore[index]
        self.assertEqual(direct["to"], "operator")
        ack_status, ack_value, _ = self.request(
            "POST",
            "/api/acks",
            {"actor": "operator", "message_id": direct["id"]},
            token=self.token,
        )
        self.assertEqual(ack_status, 201)
        self.assertEqual(ack_value["ack"]["by"], "operator")  # type: ignore[index]

    def test_both_recipient_partial_failure_is_explicit(self) -> None:
        successful = {"id": "one", "to": "claude-master"}
        with mock.patch.object(
            web.board,
            "post_message",
            side_effect=[successful, board.BoardError("second failed")],
        ):
            status, value = web.send_messages(self.root, self.message_payload(to="BOTH"))
        self.assertEqual(status, 207)
        self.assertFalse(value["ok"])
        self.assertEqual([item["ok"] for item in value["results"]], [True, False])
        self.assertIn("second failed", value["results"][1]["error"])

    def test_static_routes_and_traversal_are_bounded(self) -> None:
        status, page, content_type = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        self.assertIn(self.token, str(page))
        status, _, content_type = self.request("GET", "/styles.css")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/css"))
        status, _, _ = self.request("GET", "/../cli.py")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/api/messages/..%2Fstatus%2Fsol-master")
        self.assertEqual(status, 404)

    def test_messages_folder_route_returns_the_real_path(self) -> None:
        status, value, _ = self.request("GET", "/api/messages-folder")
        self.assertEqual(status, 200)
        self.assertEqual(value["path"], str(self.root / "messages"))  # type: ignore[index]

    def test_open_messages_folder_starts_explorer_from_loopback(self) -> None:
        with mock.patch.object(web.os, "startfile", create=True) as startfile:
            status, value, content_type = self.request(
                "POST", "/api/open-messages-folder", None, token=self.token
            )
        self.assertEqual(status, 204)
        self.assertEqual(value, "")
        startfile.assert_called_once_with(str(self.root / "messages"))

    def test_open_messages_folder_refuses_a_non_loopback_bound_server(self) -> None:
        with mock.patch.object(web, "is_loopback_host", return_value=False), mock.patch.object(
            web.os, "startfile", create=True
        ) as startfile:
            status, value, _ = self.request(
                "POST", "/api/open-messages-folder", None, token=self.token
            )
        self.assertEqual(status, 403)
        startfile.assert_not_called()
        self.assertIn("loopback", str(value))

    def test_open_messages_folder_still_requires_token(self) -> None:
        with mock.patch.object(web.os, "startfile", create=True) as startfile:
            status, _, _ = self.request("POST", "/api/open-messages-folder", None)
        self.assertEqual(status, 403)
        startfile.assert_not_called()

    def test_roadmap_api_conflict_and_state(self) -> None:
        payload = {
            "actor": "claude-master",
            "id": "M3",
            "title": "HTTP roadmap",
            "summary": "Created through API",
            "status": "PENDING",
            "owner": "shared",
            "progress": 10,
            "blocker": "",
            "expected_revision": 0,
        }
        status, value, _ = self.request("POST", "/api/roadmap", payload, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(value["item"]["revision"], 1)  # type: ignore[index]
        status, _, _ = self.request("POST", "/api/roadmap", payload, token=self.token)
        self.assertEqual(status, 409)
        status, value, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(value["roadmap"][0]["id"], "M3")  # type: ignore[index]
        invalid = {**payload, "id": "M4", "status": "BLOCKED", "blocker": ""}
        status, value, _ = self.request("POST", "/api/roadmap", invalid, token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("require a blocker", str(value))

    def test_non_loopback_requires_explicit_unsafe_flag(self) -> None:
        web.validate_bind_host("127.0.0.1")
        web.validate_bind_host("::1")
        with self.assertRaisesRegex(board.BoardError, "non-loopback"):
            web.validate_bind_host("0.0.0.0")
        web.validate_bind_host("0.0.0.0", unsafe_allow_non_loopback=True)


if __name__ == "__main__":
    unittest.main()
