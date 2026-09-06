from __future__ import annotations

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from agent_board import cli as board
from agent_board import web


def _write_message(
    root: Path,
    message_id: str,
    created_at: str,
    *,
    recipient: str = "claude-master",
    body: bytes = b"body\n",
) -> None:
    metadata = {
        "id": message_id,
        "from": "sol-master",
        "to": recipient,
        "kind": "STATUS",
        "priority": "NORMAL",
        "workstream": "BOARD-BOUNDS",
        "reply_to": None,
        "requires_ack": False,
        "created_at": created_at,
        "summary": f"Message {message_id}",
    }
    front_matter = board._message_markdown(metadata, "").encode("utf-8")
    (root / "messages" / f"{message_id}.md").write_bytes(front_matter + body)


class StateMessageBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = board.initialize(Path(self.temporary.name))

    def test_thousand_files_read_only_bounded_front_matter(self) -> None:
        for index in range(1000):
            _write_message(
                self.root,
                f"message-{index:04d}",
                f"2026-08-30T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z",
                body=b"\xff body intentionally is not UTF-8",
            )

        original_metadata_reader = board._read_message_metadata
        with (
            mock.patch.object(
                board, "parse_message", side_effect=AssertionError("state must not parse bodies")
            ),
            mock.patch.object(
                board, "_read_message_metadata", wraps=original_metadata_reader
            ) as metadata_reader,
        ):
            messages, meta = board.list_recent_messages(self.root)

        self.assertEqual(len(messages), board.STATE_MESSAGE_LIMIT)
        self.assertEqual(messages[0]["id"], "message-0999")
        self.assertEqual(messages[-1]["id"], "message-0800")
        self.assertEqual(metadata_reader.call_count, 1000)
        self.assertEqual(
            len({call.args[0] for call in metadata_reader.call_args_list}), 1000
        )
        self.assertEqual(
            meta,
            {
                "total": 1000,
                "returned": 200,
                "has_more": True,
                "malformed": 0,
                "oversized": 0,
            },
        )
        with mock.patch.object(
            board, "_read_message_metadata", wraps=original_metadata_reader
        ) as cached_reader:
            cached_messages, cached_meta = board.list_recent_messages(self.root)
            self.assertEqual(cached_reader.call_count, 0)
            _write_message(self.root, "message-1000", "2026-08-30T01:00:00Z")
            incremented_messages, incremented_meta = board.list_recent_messages(self.root)
            self.assertEqual(cached_reader.call_count, 1)
        self.assertEqual(incremented_messages[0]["id"], "message-1000")
        self.assertEqual(incremented_meta["total"], 1001)

    def test_malformed_and_oversized_files_are_isolated(self) -> None:
        _write_message(self.root, "valid", "2026-08-30T12:00:00Z")
        (self.root / "messages" / "malformed.md").write_text(
            "---\nid: \"malformed\"\n", encoding="utf-8"
        )
        (self.root / "messages" / "oversized.md").write_bytes(
            b"x" * (board.MAX_MESSAGE_FILE_BYTES + 1)
        )

        messages, meta = board.list_recent_messages(self.root)

        self.assertEqual([message["id"] for message in messages], ["valid"])
        self.assertEqual(meta["malformed"], 1)
        self.assertEqual(meta["oversized"], 1)
        self.assertFalse(meta["has_more"])
        with self.assertRaisesRegex(board.BoardError, "exceeds"):
            board.read_message(self.root, "oversized")

    def test_at_most_two_hundred_matches_legacy_state_exactly(self) -> None:
        _write_message(
            self.root, "zeta", "2026-08-30T12:00:00Z", recipient="sol-master"
        )
        _write_message(self.root, "alpha", "2026-08-30T12:00:00Z")
        expected_by_id: dict[str, dict[str, object]] = {}
        for actor in sorted(board.IDENTITIES):
            for message in board.inbox(self.root, actor=actor):
                expected_by_id[str(message["id"])] = message
        expected = sorted(
            expected_by_id.values(), key=lambda item: str(item["created_at"]), reverse=True
        )

        messages, meta = board.list_recent_messages(self.root)

        self.assertEqual(messages, expected)
        self.assertEqual(meta["total"], 2)
        self.assertEqual(meta["returned"], 2)
        self.assertFalse(meta["has_more"])

    def test_front_matter_is_exact_and_rejects_nonregular_entries(self) -> None:
        _write_message(self.root, "valid", "2026-08-30T12:00:00Z")
        valid = (self.root / "messages" / "valid.md").read_text(encoding="utf-8")
        duplicate = valid.replace(
            'summary: "Message valid"',
            'summary: "first"\nsummary: "second"',
        ).replace('id: "valid"', 'id: "duplicate"')
        unknown = valid.replace(
            'id: "valid"', 'id: "unknown"\nextra: "not allowed"'
        )
        (self.root / "messages" / "duplicate.md").write_text(duplicate, encoding="utf-8")
        (self.root / "messages" / "unknown.md").write_text(unknown, encoding="utf-8")
        (self.root / "messages" / "directory.md").mkdir()

        messages, meta = board.list_recent_messages(self.root)

        self.assertEqual([message["id"] for message in messages], ["valid"])
        self.assertEqual(meta["malformed"], 3)
        for message_id in ("duplicate", "unknown", "directory"):
            with self.assertRaises(board.BoardError):
                board.read_message(self.root, message_id)

    def test_symlink_message_entry_is_rejected(self) -> None:
        outside = self.root / "outside.md"
        _write_message(self.root, "source", "2026-08-30T12:00:00Z")
        source = self.root / "messages" / "source.md"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        symlink = self.root / "messages" / "source.md"
        try:
            os.symlink(outside, symlink)
        except OSError:
            self.skipTest("symlink creation is unavailable on this Windows host")

        messages, meta = board.list_recent_messages(self.root)

        self.assertEqual(messages, [])
        self.assertEqual(meta["malformed"], 1)
        with self.assertRaises(board.BoardError):
            board.read_message(self.root, "source")

    def test_hardlink_message_entry_is_rejected(self) -> None:
        _write_message(self.root, "hardlink", "2026-08-30T12:00:00Z")
        hardlink = self.root / "messages" / "hardlink.md"
        outside = self.root / "outside-hardlink.md"
        outside.write_bytes(hardlink.read_bytes())
        hardlink.unlink()
        try:
            os.link(outside, hardlink)
        except OSError:
            self.skipTest("hardlink creation is unavailable on this filesystem")

        messages, meta = board.list_recent_messages(self.root)

        self.assertEqual(messages, [])
        self.assertEqual(meta["malformed"], 1)
        with self.assertRaises(board.BoardError):
            board.read_message(self.root, "hardlink")


class StateApiIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = board.initialize(Path(self.temporary.name))
        self.server = web.create_server(self.root, port=0, token="state-bounds-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, path: str) -> tuple[int, dict[str, object]]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(
            "GET", path, headers={"Host": f"127.0.0.1:{self.server.server_port}"}
        )
        response = connection.getresponse()
        value = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, value

    def test_state_stays_live_but_direct_detail_fails_closed(self) -> None:
        _write_message(self.root, "valid", "2026-08-30T12:00:00Z")
        (self.root / "messages" / "malformed.md").write_text("not a message", encoding="utf-8")
        (self.root / "messages" / "oversized.md").write_bytes(
            b"x" * (board.MAX_MESSAGE_FILE_BYTES + 1)
        )

        status, state = self.get("/api/state")
        malformed_status, _ = self.get("/api/messages/malformed")
        oversized_status, _ = self.get("/api/messages/oversized")

        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in state["messages"]], ["valid"])
        self.assertEqual(state["messages_meta"]["malformed"], 1)
        self.assertEqual(state["messages_meta"]["oversized"], 1)
        self.assertEqual(malformed_status, 404)
        self.assertEqual(oversized_status, 404)

    def test_bad_status_files_cannot_break_state(self) -> None:
        board.publish_status(
            self.root,
            actor="sol-master",
            state="ACTIVE",
            summary="valid status",
        )
        (self.root / "status" / "bad.json").write_text("{", encoding="utf-8")
        (self.root / "status" / "huge.json").write_bytes(
            b"x" * (board.MAX_STATE_JSON_FILE_BYTES + 1)
        )
        external_status = self.root / "external-status.json"
        external_status.write_text(
            json.dumps(
                {
                    "identity": "claude-master",
                    "state": "ACTIVE",
                    "workstream": "BOARD-BOUNDS",
                    "head": "",
                    "paths": [],
                    "summary": "must not follow",
                    "updated_at": "2026-08-30T12:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        try:
            os.symlink(
                external_status,
                self.root / "status" / "claude-master.json",
            )
        except OSError:
            pass

        status, state = self.get("/api/state")

        self.assertEqual(status, 200)
        self.assertEqual([item["identity"] for item in state["status"]], ["sol-master"])


if __name__ == "__main__":
    unittest.main()
