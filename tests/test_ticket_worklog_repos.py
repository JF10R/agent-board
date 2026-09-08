from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_board import cli as board, tickets


class WorklogRepositoryTest(unittest.TestCase):
    def test_global_board_repo_is_separate_from_commit_evidence_repo(self):
        with tempfile.TemporaryDirectory() as temporary:
            board_repo = Path(temporary) / "atlas"
            evidence_repo = Path(temporary) / "implementation"
            roots = {board_repo: board.initialize(board_repo / ".git" / "agent-board"), evidence_repo: board.initialize(evidence_repo / ".git" / "agent-board")}
            for root in roots.values():
                tickets.create_ticket(root, actor="gpt-master", ticket_id="T1", title="Work")
            wrong_log = roots[evidence_repo] / "ticket-events" / "T1.jsonl"
            wrong_before = wrong_log.read_bytes()
            argv = ["--repo", str(board_repo), "ticket", "worklog", "--actor", "gpt-master", "--id", "T1", "--summary", "Implemented"]
            with patch.object(board, "board_root", side_effect=lambda repo: roots[Path(repo)]) as resolver, redirect_stdout(StringIO()):
                board.run(argv + ["--repo", str(evidence_repo), "--sha", "abc123"])
                self.assertTrue(all(call.args == (board_repo,) for call in resolver.call_args_list))
                board.run(argv + ["--artifact", "report.md", "--content-hash", "a" * 64])
            worklog = tickets.get_ticket(roots[board_repo], "T1")["worklog"]
            self.assertEqual(worklog[0]["evidence"], {"repo": str(evidence_repo), "sha": "abc123"})
            self.assertEqual(worklog[1]["evidence"], {"artifact": "report.md", "content_hash": "a" * 64})
            self.assertEqual(wrong_log.read_bytes(), wrong_before)
            self.assertEqual(tickets.get_ticket(roots[evidence_repo], "T1")["worklog"], [])
