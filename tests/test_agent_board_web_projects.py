"""Regression checks for explicit project scope in the browser client."""

from pathlib import Path
import shutil
import subprocess

import pytest


APP = Path(__file__).resolve().parents[1] / "src/agent_board/web_static/app.js"


def run_browser_helpers(source: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    completed = subprocess.run(
        [node, "--input-type=commonjs", "-"], input=source,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_portfolio_preserves_isolation_and_never_exposes_first_project() -> None:
    source = APP.read_text(encoding="utf-8")
    helpers = source[source.index("async function fetchBoardState("):source.index("// Live without a restart:")]
    active_name = source[source.index("function activeProjectName()"):source.index("function multiProject()")]
    run_browser_helpers("""
const assert = require('node:assert/strict');
const ALL_PROJECTS = '__all'; let activeProject = ALL_PROJECTS;
const projects = [{name:'Atlas'}, {name:'Orbit'}]; let projectSnapshots = [];
const emptyState = {tickets:[],roadmap:[],messages:[],choices:{identities:[]}};
const multiProject = () => true;
const fetchProjectState = async name => ({project:name,tickets:[{id:'same-id',title:name}],roadmap:[{id:'same-id'}],messages:[{id:'same-id'}],choices:{identities:[name]}});
""" + active_name + helpers + """
(async () => {
  assert.equal(activeProjectName(), '__all');
  const portfolio = await fetchBoardState();
  assert.deepEqual(portfolio.tickets, []);
  assert.deepEqual(portfolio.roadmap, []);
  assert.deepEqual(portfolio.messages, []);
  assert.deepEqual(projectSnapshots.map(p => p.project), ['Atlas','Orbit']);
  assert.deepEqual(portfolio.choices.identities, ['Atlas','Orbit']);
  activeProject = 'Orbit';
  const scoped = await fetchBoardState();
  assert.equal(scoped.project, 'Orbit');
  assert.equal(scoped.tickets[0].title, 'Orbit');
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_portfolio_rejects_partial_fetch_instead_of_showing_false_totals() -> None:
    source = APP.read_text(encoding="utf-8")
    helpers = source[source.index("async function fetchBoardState("):source.index("// Live without a restart:")]
    run_browser_helpers("""
const assert = require('node:assert/strict');
const ALL_PROJECTS = '__all'; const activeProject = ALL_PROJECTS;
const projects = [{name:'Atlas'}, {name:'Orbit'}]; let projectSnapshots = [];
const multiProject = () => true;
const fetchProjectState = async name => { if (name === 'Orbit') throw new Error('unavailable'); return {project:name}; };
""" + helpers + """
(async () => {
  await assert.rejects(fetchBoardState(), /unavailable/);
  assert.deepEqual(projectSnapshots, []);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_view_navigation_updates_reload_query_without_losing_scope() -> None:
    source = APP.read_text(encoding="utf-8")
    helper = source[source.index("function setView("):source.index("function applyTheme(")]
    run_browser_helpers("""
const assert = require('node:assert/strict');
const VIEWS = ['messages','roadmap','tickets','presence'];
const STORAGE = {view:'view'}; const store = () => {};
let currentView = 'tickets'; const activeProject = 'Atlas';
const activeProjectName = () => activeProject;
let location = new URL('http://localhost/?view=tickets&project=Atlas#tickets');
const history = {replaceState: (_state, _title, url) => { location = new URL(url, location); }};
const document = {body:{dataset:{}}};
const $ = () => ({setAttribute(){},focus(){}});
const parseTicketRoute = () => null;
""" + helper + """
setView('roadmap');
assert.equal(location.searchParams.get('view'), 'roadmap');
assert.equal(location.searchParams.get('project'), 'Atlas');
assert.equal(location.hash, '#roadmap');
assert.equal(currentView, 'roadmap');
""")
