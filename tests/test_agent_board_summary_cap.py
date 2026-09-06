"""A summary past 300 characters is the body pasted into the index."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_board import cli as board  # noqa: E402


class SummaryCapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        board.initialize(self.root)
        self.addCleanup(self._tmp.cleanup)

    def _post(self, summary: str, **over):
        kwargs = dict(
            sender="claude-master", recipient="lead", kind="STATUS",
            priority="NORMAL", workstream="TEST", summary=summary,
        )
        kwargs.update(over)
        return board.post_message(self.root, **kwargs)

    def test_the_cap_is_three_hundred(self):
        self.assertEqual(board.MAX_SUMMARY_CHARS, 300)

    def test_a_summary_at_the_cap_is_accepted(self):
        self._post("x" * 300)

    def test_a_summary_one_character_over_is_refused(self):
        with self.assertRaises(board.BoardError) as ctx:
            self._post("x" * 301)
        self.assertIn("301 characters", str(ctx.exception))
        self.assertIn("over the 300-character cap by 1", str(ctx.exception))

    def test_the_error_says_where_the_detail_belongs(self):
        with self.assertRaises(board.BoardError) as ctx:
            self._post("x" * 900)
        self.assertIn("--body-file", str(ctx.exception))

    def test_an_over_long_summary_writes_no_message(self):
        """Refusing must not leave a half-written message behind."""
        before = list((self.root / "messages").glob("*.md"))
        with self.assertRaises(board.BoardError):
            self._post("x" * 5000)
        self.assertEqual(list((self.root / "messages").glob("*.md")), before)

    def test_a_long_body_is_still_welcome(self):
        """The cap is on the index line, not on what the message can carry."""
        result = self._post("short and clear", body="y" * 20_000)
        self.assertTrue(result["id"])

    def test_leading_whitespace_does_not_buy_room(self):
        self._post("   " + "x" * 300 + "   ")
        with self.assertRaises(board.BoardError):
            self._post("   " + "x" * 301 + "   ")

    def test_publish_status_is_capped_the_same_way(self):
        board.publish_status(
            self.root, actor="claude-master", state="ACTIVE", summary="x" * 300
        )
        with self.assertRaises(board.BoardError):
            board.publish_status(
                self.root, actor="claude-master", state="ACTIVE", summary="x" * 301
            )


if __name__ == "__main__":
    unittest.main()
