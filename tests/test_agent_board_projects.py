"""Multi-project board: one store per repo, per-project actors, and a web server serving several at once.

Two stores never see each other's messages; the actor vocabulary is data in the store, not a constant;
and a store with no project.v1.json keeps the historical default vocabulary, unchanged.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_board import cli as board  # noqa: E402
from agent_board import web  # noqa: E402


def _post(root: Path, summary: str, sender: str = "lead", recipient: str = "claude-master") -> dict:
    return board.post_message(
        root, sender=sender, recipient=recipient, kind="STATUS", priority="NORMAL",
        workstream="w", summary=summary, body="b", requires_ack=False, reply_to=None,
    )


# ---------------------------------------------------------------- stores


def test_two_stores_are_isolated(tmp_path: Path) -> None:
    left, right = board.initialize(tmp_path / "left"), board.initialize(tmp_path / "right")
    _post(left, "only in left")
    assert [item["summary"] for item in board.inbox(left, actor="claude-master")] == ["only in left"]
    assert board.inbox(right, actor="claude-master") == []
    assert board.list_recent_messages(right)[0] == []


def test_board_root_is_named_consistently_under_the_git_common_dir(tmp_path: Path, monkeypatch) -> None:
    common = tmp_path / "common"
    monkeypatch.setattr(board, "discover_git_common_dir", lambda repo=None: common)
    assert board.board_root(tmp_path).name == board.STORE_DIRECTORY


# ---------------------------------------------------------------- vocabulary


def test_a_store_without_a_config_keeps_the_historical_actors(tmp_path: Path) -> None:
    root = board.initialize(tmp_path / "board")
    assert not (root / board.PROJECT_CONFIG_FILE).exists()
    assert board.project_config(root)["identities"] == board.IDENTITIES
    _post(root, "historical actors still work")


def test_init_seeds_a_neutral_config_for_a_new_store_and_it_governs(tmp_path: Path) -> None:
    root = board.initialize(tmp_path / board.STORE_DIRECTORY)
    board.seed_project_config(root)
    written = json.loads((root / board.PROJECT_CONFIG_FILE).read_text(encoding="utf-8"))
    assert written["identities"] == ["master"]
    assert board.project_config(root)["message_senders"] == frozenset({"master", "lead", "operator"})
    _post(root, "neutral actors", sender="lead", recipient="master")
    with pytest.raises(board.BoardError, match="unauthorized identity"):
        _post(root, "default actors are not this project's", sender="lead", recipient="claude-master")


def test_a_project_can_declare_its_own_actors(tmp_path: Path) -> None:
    root = board.initialize(tmp_path / "widgets")
    (root / board.PROJECT_CONFIG_FILE).write_text(
        json.dumps({"identities": ["widgets-master"], "message_participants": ["operator"],
                    "roadmap_owners": ["unassigned"], "workstreams": ["engine"]}),
        encoding="utf-8",
    )
    assert board.project_config(root)["identities"] == frozenset({"widgets-master"})
    _post(root, "widgets speaks for itself", sender="operator", recipient="widgets-master")
    with pytest.raises(board.BoardError, match="unauthorized identity"):
        _post(root, "no lead here", sender="lead", recipient="widgets-master")


def test_a_broken_config_fails_closed(tmp_path: Path) -> None:
    root = board.initialize(tmp_path / "broken")
    (root / board.PROJECT_CONFIG_FILE).write_text('{"identities": []}', encoding="utf-8")
    with pytest.raises(board.BoardError, match="identities must not be empty"):
        board.project_config(root)


# ---------------------------------------------------------------- web routing


@pytest.fixture()
def two_project_server(tmp_path: Path):
    alpha = web.Project("alpha", board.initialize(tmp_path / "alpha"))
    beta = web.Project("beta", board.initialize(tmp_path / "beta"))
    instance = web.create_server(alpha.board_root, "127.0.0.1", 0, token="t0ken", projects=[alpha, beta])
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def _get(instance, path: str) -> dict:
    host, port = instance.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("GET", path, headers={"Host": f"{host}:{port}"})
        response = connection.getresponse()
        return {"status": response.status, "body": json.loads(response.read() or b"{}")}
    finally:
        connection.close()


def test_projects_are_listed_with_the_default_first(two_project_server) -> None:
    value = _get(two_project_server, "/api/projects")["body"]
    assert [item["name"] for item in value["projects"]] == ["alpha", "beta"]
    assert value["default"] == "alpha"


def test_state_is_served_per_project(two_project_server) -> None:
    _post(two_project_server.projects["alpha"].board_root, "alpha only")
    assert [item["summary"] for item in _get(two_project_server, "/api/state?project=alpha")["body"]["messages"]] == ["alpha only"]
    assert _get(two_project_server, "/api/state?project=beta")["body"]["messages"] == []
    assert _get(two_project_server, "/api/state")["body"]["project"] == "alpha"


def test_version_carries_one_token_per_project(two_project_server) -> None:
    before = _get(two_project_server, "/api/version")["body"]["projects"]
    assert set(before) == {"alpha", "beta"}
    _post(two_project_server.projects["beta"].board_root, "moves beta only")
    after = _get(two_project_server, "/api/version")["body"]["projects"]
    assert after["beta"] != before["beta"]
    assert after["alpha"] == before["alpha"]


def test_an_unknown_project_is_refused(two_project_server) -> None:
    result = _get(two_project_server, "/api/state?project=ghost")
    assert result["status"] == 404
    assert "unknown project" in result["body"]["error"]
