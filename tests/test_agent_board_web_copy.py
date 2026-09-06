"""Clipboard formats of the board web UI (tools/agent_board_web/copy.js), run under Node.

The multi-copy order is chronological (created_at, then id), not the on-screen order, so an
agent reads a conversation oldest first regardless of the filters the human had on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

COPY_JS = Path(__file__).resolve().parents[1] / "src" / "agent_board" / "web_static" / "copy.js"


def _run(payload: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    script = (
        COPY_JS.read_text(encoding="utf-8")
        + "\nconst input=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify({"
        "one:formatMessageMarkdown(input.entries[0].message,input.entries[0].body),"
        "many:formatMessagesMarkdown(input.entries),"
        "item:input.item?formatRoadmapItemMarkdown(input.item):null}));"
    )
    result = subprocess.run([node, "--input-type=commonjs", "-e", script], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def message(message_id: str, created_at: str, **overrides: object) -> dict:
    base = {"id": message_id, "from": "lead", "to": "claude-master", "kind": "DECISION", "priority": "HIGH", "workstream": "A2", "reply_to": None, "requires_ack": True, "created_at": created_at, "summary": "Root acquisition is scarce", "acked": False}
    base.update(overrides)
    return base


def test_single_message_has_heading_provenance_and_body() -> None:
    out = _run({"entries": [{"message": message("m1", "2026-09-05T15:51:03Z"), "body": "  Line one\r\nLine two  \n"}]})["one"]
    assert out.startswith("### [DECISION] Root acquisition is scarce\n")
    assert "_from Lead (lead) to Claude Master (claude-master) · workstream A2 · priority HIGH · acknowledgement pending · created 2026-09-05T15:51:03Z · id m1_" in out
    assert out.endswith("\n\nLine one\nLine two\n")


def test_empty_body_is_stated_not_blank() -> None:
    out = _run({"entries": [{"message": message("m1", "2026-09-05T15:51:03Z", requires_ack=False), "body": ""}]})["one"]
    assert "_(empty body)_" in out
    assert "acknowledgement" not in out


def test_multi_copy_is_chronological_with_separators_and_a_count() -> None:
    entries = [
        {"message": message("b", "2026-09-05T16:00:00Z", summary="second"), "body": "B"},
        {"message": message("a", "2026-09-05T15:00:00Z", summary="first", reply_to=None), "body": "A"},
        {"message": message("c", "2026-09-05T16:00:00Z", summary="tie broken by id", reply_to="a"), "body": "C"},
    ]
    out = _run({"entries": entries})["many"]
    assert out.startswith("<!-- Agent Board: 3 messages, oldest first -->\n\n### [DECISION] first\n")
    assert out.index("### [DECISION] first") < out.index("### [DECISION] second") < out.index("### [DECISION] tie broken by id")
    assert out.count("\n\n---\n\n") == 2
    assert "reply to a" in out


def test_roadmap_item_markdown_lists_only_known_facts() -> None:
    item = {"id": "train-a", "title": "Train A", "status": "IN_PROGRESS", "owner": "claude-master", "progress": 0, "progress_reported": False, "revision": 2, "updated_at": "2026-09-05T18:00:00Z", "summary": "Find edge now.", "blockers": [],
            "derived": {"kind": "MILESTONE", "impact": "first candidate resolved", "depends_on": [{"id": "g1", "resolved": False, "known": True}, {"id": "ghost", "resolved": False, "known": False}], "dependents": [], "feeds": ["goal"], "standby": None, "startable": False}}
    out = _run({"entries": [{"message": message("m", "2026-09-05T15:51:03Z"), "body": ""}], "item": item})["item"]
    assert "progress not reported" in out
    assert "- kind: MILESTONE" in out and "- impact: first candidate resolved" in out
    assert "- depends on: g1 (unresolved), ghost (unknown)" in out
    assert "- feeds: goal" in out
    assert "unblocks" not in out and "standby" not in out and "ready to start" not in out
    assert out.endswith("\n\nFind edge now.\n")
