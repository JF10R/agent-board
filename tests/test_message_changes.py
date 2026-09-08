from pathlib import Path

import pytest

from agent_board import cli
from agent_board.changes import message_changes
from agent_board.errors import BoardError


def post(root: Path, summary: str, **kwargs) -> str:
    return cli.post_message(
        root,
        sender="sol-master",
        recipient="lead",
        kind="STATUS",
        priority="NORMAL",
        workstream="test",
        summary=summary,
        body="Details",
        **kwargs,
    )["id"]


def test_resume_includes_late_discovery_and_survives_new_connection(tmp_path):
    root = cli.initialize(tmp_path / "board")
    first = post(root, "First", message_id="z-first", created_at="2026-09-08T12:00:00Z")
    page = message_changes(root, actor="lead", limit=1)
    second = post(
        root, "Second", message_id="a-late", created_at="2026-09-08T12:00:00Z"
    )
    resumed = message_changes(root, actor="lead", cursor=page["cursor"])
    assert [item["message"]["id"] for item in page["events"]] == [first]
    assert [item["message"]["id"] for item in resumed["events"]] == [second]
    assert message_changes(root, actor="lead", cursor=resumed["cursor"])["events"] == []


def test_cursor_scope_and_rebuilt_projection_are_explicit(tmp_path):
    root = cli.initialize(tmp_path / "board")
    post(root, "First")
    page = message_changes(root, actor="lead")
    with pytest.raises(BoardError, match="scope"):
        message_changes(root, cursor=page["cursor"], actor="operator")
    (root / "message-feed.sqlite3").unlink()
    with pytest.raises(BoardError, match="rebuilt"):
        message_changes(root, cursor=page["cursor"], actor="lead")
    assert len(message_changes(root, actor="lead")["events"]) == 1


def test_message_pages_are_bounded_without_dropping_entries(tmp_path):
    root = cli.initialize(tmp_path / "board")
    expected = {post(root, str(index)) for index in range(3)}
    first = message_changes(root, limit=2)
    second = message_changes(root, limit=2, cursor=first["cursor"])
    assert first["has_more"] and not second["has_more"]
    assert {
        item["message"]["id"] for item in first["events"] + second["events"]
    } == expected


def test_malformed_ticket_link_does_not_abort_message_feed(tmp_path):
    root = cli.initialize(tmp_path / "board")
    broken = post(root, "Broken link")
    path = root / "messages" / f"{broken}.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("---\n", "---\nticket_id: 123\n", 1)
    path.write_text(text, encoding="utf-8")
    valid = post(root, "Valid")
    page = message_changes(root)
    assert page["malformed"] == 1
    assert [event["message"]["id"] for event in page["events"]] == [valid]
