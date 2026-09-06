"""Inbox multi-select reducer (tools/agent_board_web/selection.js), run under Node.

One Set is the only source of truth: toggle, Shift-range, prune and copy order are pure functions of it,
so a render can never disagree with the "N selected" banner.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SELECTION_JS = Path(__file__).resolve().parents[1] / "src" / "agent_board" / "web_static" / "selection.js"


def _run(script: str, payload: dict | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    source = (
        SELECTION_JS.read_text(encoding="utf-8")
        + "\nconst input=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        + script
    )
    result = subprocess.run(
        [node, "--input-type=commonjs", "-e", source],
        input=json.dumps(payload or {}),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_toggle_adds_then_removes_and_moves_the_anchor() -> None:
    out = _run(
        "let s=new Set();let a=null;"
        "for(const id of input.clicks){const next=selectionToggle(s,id,{});s=next.selected;a=next.anchor;}"
        "process.stdout.write(JSON.stringify({ids:[...s],anchor:a,label:selectionCountLabel(s)}));",
        {"clicks": ["a", "b", "a", "c"]},
    )
    assert out["ids"] == ["b", "c"]
    assert out["anchor"] == "c"
    assert out["label"] == "2 selected"


def test_shift_range_selects_every_row_between_the_anchor_and_the_click() -> None:
    out = _run(
        "const ordered=input.ordered;let s=new Set();let a=null;"
        "let n=selectionToggle(s,'b',{orderedIds:ordered});s=n.selected;a=n.anchor;"
        "n=selectionToggle(s,'e',{range:true,anchor:a,orderedIds:ordered});s=n.selected;a=n.anchor;"
        "process.stdout.write(JSON.stringify({ids:[...s].sort(),anchor:a}));",
        {"ordered": ["a", "b", "c", "d", "e", "f"]},
    )
    assert out["ids"] == ["b", "c", "d", "e"]
    assert out["anchor"] == "e"


def test_range_without_a_known_anchor_is_a_plain_toggle() -> None:
    out = _run(
        "const n=selectionToggle(new Set(['a']),'d',{range:true,anchor:'ghost',orderedIds:input.ordered});"
        "process.stdout.write(JSON.stringify({ids:[...n.selected].sort()}));",
        {"ordered": ["a", "b", "c", "d"]},
    )
    assert out["ids"] == ["a", "d"]


def test_prune_keeps_the_survivors_and_drops_what_disappeared() -> None:
    out = _run(
        "const kept=selectionPrune(new Set(input.selected),input.valid);"
        "process.stdout.write(JSON.stringify({ids:[...kept],cleared:[...selectionClear()],add:[...selectionAdd(kept,input.add)].sort()}));",
        {"selected": ["a", "b", "c"], "valid": ["c", "a", "z"], "add": ["z", "a"]},
    )
    assert out["ids"] == ["a", "c"]
    assert out["cleared"] == []
    assert out["add"] == ["a", "c", "z"]


def test_copy_order_is_chronological_not_click_order() -> None:
    messages = [
        {"id": "c", "created_at": "2026-09-05T16:00:00Z"},
        {"id": "a", "created_at": "2026-09-05T15:00:00Z"},
        {"id": "b", "created_at": "2026-09-05T16:00:00Z"},
        {"id": "skip", "created_at": "2026-09-05T14:00:00Z"},
    ]
    out = _run(
        "process.stdout.write(JSON.stringify({order:selectionCopyOrder(new Set(input.selected),input.messages)}));",
        {"selected": ["c", "b", "a"], "messages": messages},
    )
    assert out["order"] == ["a", "b", "c"]


def test_toggle_does_not_mutate_the_set_it_was_given() -> None:
    out = _run(
        "const before=new Set(['a']);const after=selectionToggle(before,'b',{}).selected;"
        "process.stdout.write(JSON.stringify({before:[...before],after:[...after].sort()}));"
    )
    assert out["before"] == ["a"]
    assert out["after"] == ["a", "b"]
