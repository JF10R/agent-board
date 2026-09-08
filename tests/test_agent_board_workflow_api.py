from __future__ import annotations

from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
import threading
from unittest.mock import patch

import pytest

from agent_board import cli, tickets, web
from agent_board.errors import BoardError, CommitUncertain, RevisionConflict


@pytest.fixture
def api(tmp_path):
    server = web.create_server(tmp_path / "board", port=0, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, payload=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        origin = f"http://127.0.0.1:{server.server_port}"
        headers = {"Origin": origin, "X-Agent-Board-Token": "test-token", "Content-Type": "application/json"}
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value
    yield request
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_capabilities_describe_actual_parser_without_repo():
    output = io.StringIO()
    with patch.object(cli, "board_root", side_effect=BoardError("not a repo")), redirect_stdout(output):
        assert cli.run(["capabilities"]) == 0
    value = json.loads(output.getvalue())
    commands = value["cli"]["commands"]["ticket"]["commands"]
    assert {"claim", "handoff", "dep-remove", "changes", "recover", "cache-rebuild"} <= commands.keys()
    assert value["conventions"]["roadmap_tree_journals_by_default"] is True


def test_http_conflicts_use_error_type_not_message(api):
    payload = {"actor": "gpt-master"}
    with patch.object(tickets, "claim_ticket", side_effect=RevisionConflict("stale state")):
        assert api("POST", "/api/tickets/T1/claim", payload)[0] == 409
    with patch.object(tickets, "claim_ticket", side_effect=BoardError("revision conflict is not a type")):
        assert api("POST", "/api/tickets/T1/claim", payload)[0] == 400


def test_claim_retries_and_context_feed(api):
    assert api("POST", "/api/tickets", {"actor": "gpt-master", "id": "T1", "title": "Workflow"})[0] == 201
    payload = {"actor": "gpt-master", "expected_revision": 1, "idempotency_key": "claim-once"}
    status, claimed = api("POST", "/api/tickets/T1/claim", payload)
    assert status == 200, claimed
    status, replay = api("POST", "/api/tickets/T1/claim", payload)
    assert status == 200, replay
    assert replay["ticket"]["revision"] == claimed["ticket"]["revision"]
    assert api("GET", "/api/actors/gpt-master/context")[0] == 200
    status, changes = api("GET", "/api/tickets/changes?limit=1")
    assert status == 200, changes
    assert len(changes["events"]) == 1
    assert changes["cursor"]
    assert api("GET", "/api/tickets/cache/verify")[0] == 200
    assert api("POST", "/api/tickets/cache/rebuild", {})[0] == 200


def test_handoff_rejects_non_object_evidence_and_unknown_fields(api):
    for payload in [
        {"actor": "gpt-master", "summary": "Delivered", "next_actor": "claude-master", "evidence": []},
        {"actor": "gpt-master", "summary": "Delivered", "next_actor": "claude-master", "evidence": {}, "unexpected": True},
    ]:
        assert api("POST", "/api/tickets/T1/handoff", payload)[0] == 400


def test_cli_handoff_preserves_evidence_repo_and_retry_key(tmp_path):
    args = cli.build_parser().parse_args(["--repo", str(tmp_path), "ticket", "handoff", "--id", "T1", "--actor", "gpt-master", "--summary", "Delivered", "--next-actor", "claude-master", "--repo", "evidence-repo", "--sha", "abc", "--idempotency-key", "once"])
    with patch.object(tickets, "handoff_ticket", return_value={}) as handoff, redirect_stdout(io.StringIO()):
        cli._run_ticket_command(args, tmp_path)
    assert handoff.call_args.kwargs["evidence"] == {"repo": "evidence-repo", "sha": "abc"}
    assert handoff.call_args.kwargs["idempotency_key"] == "once"


def test_message_feed_route_precedes_message_detail(api):
    status, feed = api("GET", "/api/messages/changes?limit=1")
    assert status == 200, feed
    assert feed["events"] == []
    assert "cursor" in feed
    status, response = api("GET", "/api/capabilities")
    assert status == 200, response
    assert response["capabilities"]["http"]["message_changes"] == "/api/messages/changes"


def test_atomic_handoff_validation_and_retry(api):
    api("POST", "/api/tickets", {"actor": "gpt-master", "id": "T1", "title": "Delivery"})
    status, claimed = api("POST", "/api/tickets/T1/claim", {"actor": "gpt-master"})
    assert status == 200, claimed
    ticket = claimed["ticket"]
    payload = {"actor": "gpt-master", "summary": "Verified delivery", "next_actor": "claude-master",
               "stage": "QA", "evidence": {"test": "pytest", "exit_code": 0},
               "expected_revision": ticket["revision"], "lease_token": ticket["lease"]["token"], "idempotency_key": "deliver-once"}
    status, invalid = api("POST", "/api/tickets/T1/handoff", {**payload, "evidence": {"test": "pytest"}})
    assert status == 400, invalid
    assert api("GET", "/api/tickets/T1")[1]["ticket"]["revision"] == ticket["revision"]
    status, delivered = api("POST", "/api/tickets/T1/handoff", payload)
    assert status == 200, delivered
    assert delivered["ticket"]["stage"] == "QA"
    assert delivered["ticket"]["assignee"] == "claude-master"
    assert delivered["ticket"]["revision"] == ticket["revision"] + 1
    status, retry = api("POST", "/api/tickets/T1/handoff", payload)
    assert status == 200, retry
    assert retry["ticket"]["revision"] == delivered["ticket"]["revision"]
    status, changed = api("POST", "/api/tickets/T1/handoff", {**payload, "summary": "Different delivery"})
    assert status == 409, changed


def test_dependency_removal_exposes_reciprocal_update(api):
    for ticket_id in ("T1", "T2"):
        assert api("POST", "/api/tickets", {"actor": "gpt-master", "id": ticket_id, "title": ticket_id})[0] == 201
    payload = {"actor": "gpt-master", "dep_type": "BLOCKED_BY", "target": "T2"}
    status, blocked = api("POST", "/api/tickets/T1/dependencies", {**payload, "expected_revision": 1})
    assert status == 200, blocked
    status, removed = api("POST", "/api/tickets/T1/dep-remove", {**payload, "expected_revision": blocked["ticket"]["revision"], "idempotency_key": "remove-once"})
    assert status == 200, removed
    assert not removed["ticket"]["deps"]
    assert not api("GET", "/api/tickets/T2")[1]["ticket"]["deps"]


def test_uncertain_commit_is_not_reported_as_rejection(api):
    with patch.object(tickets, "claim_ticket", side_effect=CommitUncertain("T1", 2)):
        status, response = api("POST", "/api/tickets/T1/claim", {"actor": "gpt-master"})
    assert status == 503
    assert response["ok"] is False
    assert response["committed"] is None
    assert response["ticket_id"] == "T1"
    assert response["revision"] == 2
    assert "uncertain" in response["error"]


def test_create_comment_worklog_retry_keys(api):
    create = {"actor": "gpt-master", "id": "T1", "title": "Retry", "idempotency_key": "create-once"}
    assert api("POST", "/api/tickets", create)[0] == 201
    assert api("POST", "/api/tickets", create)[0] == 201
    for action, payload in [("comment", {"summary": "Progress"}), ("worklog", {"summary": "Tested", "evidence": {"test": "pytest", "exit_code": 0}})]:
        payload.update(actor="gpt-master", idempotency_key=action + "-once")
        status, first = api("POST", "/api/tickets/T1/" + action, payload)
        assert status == 200, first
        status, retry = api("POST", "/api/tickets/T1/" + action, payload)
        assert status == 200, retry
        assert first["ticket"]["revision"] == retry["ticket"]["revision"]
    assert api("POST", "/api/tickets", {"actor": "gpt-master", "title": "No stable id", "idempotency_key": "unsafe"})[0] == 400
