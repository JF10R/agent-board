"""Unit tests for the agent-board web viewer's markdown-to-HTML converter.

tools/agent_board_web/markdown.js holds the pure rendering functions (escapeText ..
renderMarkdown); this extracts that block as source text and executes it under Node
with minimal document/window stubs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "src" / "agent_board" / "web_static" / "markdown.js"
START_MARKER = "function escapeText(value) {"
END_MARKER = None

NODE_HARNESS = """
'use strict';
function makeSpan() {
  let text = '';
  return {
    set textContent(value) { text = String(value); },
    get innerHTML() {
      return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    },
  };
}
const document = { createElement: () => makeSpan() };
const window = { location: { href: 'http://127.0.0.1:8765/' } };

%s

const input = require('fs').readFileSync(0, 'utf8');
process.stdout.write(renderMarkdown(input));
"""


def _extract_renderer() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index(START_MARKER)
    return source[start:]


def _render(markdown: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    script = NODE_HARNESS % _extract_renderer()
    result = subprocess.run(
        [node, "--input-type=commonjs", "-e", script],
        input=markdown,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_gfm_table_renders_as_html_table():
    body = (
        "| date | V_A | V_B (cal/trd) | V_C (cal/trd) |\n"
        "|---|---|---|---|\n"
        "| 2026-08-29 | **1.23** | 4/5 | 2/3 |\n"
    )
    html = _render(body)
    assert "<table>" in html
    assert "<td>" in html
    assert "<th>date</th>" in html
    assert "<strong>1.23</strong>" in html
    assert "|---|" not in html
    assert "| date |" not in html


def test_table_with_alignment_row():
    body = "| A | B |\n| :--- | ---: |\n| left | right |\n"
    html = _render(body)
    assert 'style="text-align:right"' in html
    assert "<table>" in html


def test_non_table_pipe_text_is_untouched():
    # A single line with pipes but no separator row on the next line is not a table.
    body = "a | b | c\nnot a separator\n"
    html = _render(body)
    assert "<table>" not in html
    assert "a | b | c" in html


def test_bold_inline_code_and_list_still_render():
    body = "**bold** and `code` text\n\n- one\n- two\n"
    html = _render(body)
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
    assert "<ul><li>one</li><li>two</li></ul>" in html
