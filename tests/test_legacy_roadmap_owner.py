import json

import pytest

from agent_board import cli as board


def test_legacy_owner_is_readable_without_rewriting_history(tmp_path):
    item = {"id": "OLD", "title": "Old work", "summary": "Historical item",
            "owner": "sol-master", "status": "IN_PROGRESS", "progress": 10,
            "blocker": "", "revision": 3, "updated_at": "2026-09-08T00:00:00Z"}
    path = tmp_path / "roadmap.v1.json"
    path.write_text(json.dumps({"schema_version": 1, "items": [item]}), encoding="utf-8")
    original = path.read_bytes()
    assert board._read_roadmap_store(tmp_path)["items"][0] == item
    assert path.read_bytes() == original
    with pytest.raises(board.BoardError, match="invalid roadmap owner"):
        board._validate_roadmap_item(item, tmp_path)
    current = dict(item, owner="gpt-master")
    assert board._validate_roadmap_item(current, tmp_path)["owner"] == "gpt-master"
