"""Compose rules in the board web UI: reply defaults and the client-side summary cap.

Extracts the pure helpers (MAX_SUMMARY_CHARS .. summaryCounterText) from app.js and runs them
under Node, like the other web tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "src" / "agent_board" / "web_static" / "app.js"
START_MARKER = "const MAX_SUMMARY_CHARS = 300;"
END_MARKER = "function updateSummaryCounter()"


def _helpers() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index(START_MARKER)
    end = source.index(END_MARKER)
    assert end > start
    return source[start:end]


def _run(payload: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    script = (
        "'use strict';\n" + _helpers() + "\n"
        "const input=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify({"
        "reply:replyDefaults(input.reply,input.identities),"
        "status:summaryStatus(input.summary),"
        "counter:summaryCounterText(input.summary)}));"
    )
    result = subprocess.run([node, "--input-type=commonjs", "-e", script], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


IDENTITIES = ["claude-master", "gpt-master"]


def test_reply_to_a_master_defaults_to_answer_and_carries_thread_and_workstream() -> None:
    reply = {"id": "20260901T1", "from": "claude-master", "to": "lead", "kind": "QUESTION", "workstream": "NIGHT-0901", "summary": "Which gate first?"}
    out = _run({"reply": reply, "identities": IDENTITIES, "summary": "x"})["reply"]
    assert out["kind"] == "ANSWER"
    assert out["reply_to"] == "20260901T1"
    assert out["workstream"] == "NIGHT-0901"
    assert out["actor"] == "lead" and out["to"] == "claude-master"
    assert out["summary"] == "Re: Which gate first?"
    assert out["priority"] == "NORMAL"


def test_reply_to_a_human_outbound_message_keeps_the_human_as_author_and_caps_the_summary() -> None:
    # The web UI is operated by humans: following up on lead's own message stays lead -> master.
    reply = {"id": "m2", "from": "lead", "to": "gpt-master", "kind": "ALERT", "workstream": "GENERAL", "summary": "a" * 300}
    out = _run({"reply": reply, "identities": IDENTITIES, "summary": ""})["reply"]
    assert out["kind"] == "ANSWER"
    assert out["actor"] == "lead" and out["to"] == "gpt-master"
    assert len(out["summary"]) == 300
    assert out["summary"].startswith("Re: ")


def test_summary_status_refuses_over_cap_and_empty_before_the_cli_does() -> None:
    reply = {"id": "m3", "from": "lead", "to": "operator", "kind": "STATUS", "workstream": "W", "summary": "s"}
    at_cap = _run({"reply": reply, "identities": IDENTITIES, "summary": "b" * 300})
    assert at_cap["status"] == {"length": 300, "remaining": 0, "over": False, "empty": False}
    assert at_cap["counter"] == "300 / 300"
    over = _run({"reply": reply, "identities": IDENTITIES, "summary": "b" * 301})
    assert over["status"]["over"] is True
    assert over["counter"] == "301 / 300 — 1 over the cap"
    padded = _run({"reply": reply, "identities": IDENTITIES, "summary": "  " + "b" * 300 + "  "})
    assert padded["status"]["over"] is False, "whitespace does not buy room, and does not cost it either"
    empty = _run({"reply": reply, "identities": IDENTITIES, "summary": "   "})
    assert empty["status"]["empty"] is True
