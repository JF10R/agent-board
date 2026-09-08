from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import patch
import pytest
from agent_board import cli as board, tickets


@pytest.fixture
def root(tmp_path):
    return board.initialize(tmp_path / "board")


def create(root, name="T1"):
    return tickets.create_ticket(root, actor="gpt-master", ticket_id=name, title="Ship")


def test_latest_review_and_criteria_gate(root):
    create(root)
    tickets.review_ticket(root, "T1", actor="gpt-master", verdict="PASS", summary="ok")
    tickets.review_ticket(
        root,
        "T1",
        actor="gpt-master",
        verdict="FAIL",
        summary="bad",
        findings=["broken"],
    )
    with pytest.raises(board.BoardError):
        tickets.mark_ticket_done(root, "T1", actor="gpt-master")
    tickets.review_ticket(root, "T1", actor="gpt-master", verdict="PASS", summary="ok")
    tickets.upsert_ticket(root, "T1", actor="gpt-master", acceptance_criteria=["new"])
    with pytest.raises(board.BoardError):
        tickets.mark_ticket_done(root, "T1", actor="gpt-master")


@pytest.mark.parametrize("damage", ["missing", "corrupt", "stale"])
def test_cache_is_disposable(root, damage):
    create(root)
    before = (root / "tickets.v1.json").read_bytes()
    create(root, "T2")
    path = root / "tickets.v1.json"
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("{", encoding="utf-8")
    else:
        path.write_bytes(before)
    assert [t["id"] for t in tickets.list_tickets(root)] == ["T1", "T2"]


def test_projection_failure_reports_commit(root):
    create(root)
    original = tickets.runtime.write_atomic_replace

    def fail(path, body):
        if path.name == "tickets.v1.json":
            raise OSError("disk full")
        return original(path, body)

    with patch.object(tickets.runtime, "write_atomic_replace", side_effect=fail):
        result = tickets.comment_ticket(
            root, "T1", actor="gpt-master", summary="committed"
        )
    assert result["persistence"]["committed"] is True
    assert tickets.get_ticket(root, "T1")["comments"][-1]["summary"] == "committed"


def test_claim_fence_and_atomic_handoff(root):
    create(root)
    claim = tickets.claim_ticket(
        root, "T1", actor="gpt-master/worker", idempotency_key="claim-1"
    )
    assert (
        tickets.claim_ticket(
            root, "T1", actor="gpt-master/worker", idempotency_key="claim-1"
        )["revision"]
        == claim["revision"]
    )
    with pytest.raises(board.BoardError):
        tickets.claim_ticket(root, "T1", actor="gpt-master/other")
    with pytest.raises(board.BoardError):
        tickets.heartbeat_ticket(
            root, "T1", actor="gpt-master/worker", lease_token="wrong"
        )
    delivered = tickets.handoff_ticket(
        root,
        "T1",
        actor="gpt-master/worker",
        summary="implemented",
        evidence={"test": "pytest", "exit_code": 0},
        next_actor="gpt-master",
        lease_token=claim["lease"]["token"],
    )
    assert delivered["revision"] == claim["revision"] + 1
    assert delivered["stage"] == "QA" and delivered["assignee"] == "gpt-master"
    assert delivered["latest_delivery"]["summary"] == "implemented"


def test_tail_recovery_preserves_original(root):
    create(root)
    path = root / "ticket-events/T1.jsonl"
    original = path.read_bytes() + b'{"type":'
    path.write_bytes(original)
    ok, reason = tickets.verify_ticket_chain(root, "T1")
    assert not ok and "line 2" in reason
    recovery = tickets.recover_ticket_tail(root, "T1", actor="gpt-master")
    assert __import__("pathlib").Path(recovery["backup_path"]).read_bytes() == original
    assert tickets.verify_ticket_chain(root, "T1")[0]


def test_sequence_validation(root):
    create(root)
    path = root / "ticket-events/T1.jsonl"
    event = json.loads(path.read_text())
    event["seq"] = 99
    event["hash"] = tickets.hashlib.sha256(
        board._canonical_json({k: v for k, v in event.items() if k != "hash"}).encode()
    ).hexdigest()
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert not tickets.verify_ticket_chain(root, "T1")[0]


def test_reassigned_reviewer_needs_fresh_acceptance(root):
    create(root)
    tickets.review_ticket(root, "T1", actor="gpt-master", verdict="PASS", summary="ok")
    tickets.assign_ticket(
        root,
        "T1",
        actor="gpt-master",
        assignee="gpt-master/w",
        reviewer="claude-master",
    )
    with pytest.raises(board.BoardError):
        tickets.mark_ticket_done(root, "T1", actor="gpt-master")


def test_cache_verification_checks_projection_content(root):
    create(root)
    path = root / "tickets.v1.json"
    cache = json.loads(path.read_text(encoding="utf-8"))
    cache["tickets"][0]["title"] = "WRONG"
    path.write_text(json.dumps(cache), encoding="utf-8")
    assert tickets.verify_ticket_cache(root)["valid"] is False


def test_dependency_commit_recovers_once(root):
    create(root, "A")
    create(root, "B")
    tickets.add_dependency(
        root, "A", actor="gpt-master", dep_type="BLOCKED_BY", target="B"
    )
    original = tickets._append_ticket_event

    def fail(root_arg, ticket_id, event):
        if ticket_id == "B" and event["type"] == tickets.EV_DEP_REMOVE:
            raise OSError("simulated interrupted reciprocal append")
        return original(root_arg, ticket_id, event)

    with patch.object(tickets, "_append_ticket_event", side_effect=fail):
        result = tickets.remove_dependency(
            root,
            "A",
            actor="gpt-master",
            dep_type="BLOCKED_BY",
            target="B",
            idempotency_key="remove",
        )
    assert (
        result["persistence"]["committed"]
        and result["persistence"]["recovery_required"]
    )
    with pytest.raises(board.BoardError):
        tickets.get_ticket(root, "B")
    tickets.remove_dependency(
        root,
        "A",
        actor="gpt-master",
        dep_type="BLOCKED_BY",
        target="B",
        idempotency_key="remove",
    )
    assert (
        tickets.get_ticket(root, "A")["deps"] == []
        and tickets.get_ticket(root, "B")["deps"] == []
    )
    assert (
        len(
            [
                e
                for e in tickets._read_ticket_events(root, "A")
                if e["type"] == tickets.EV_DEP_REMOVE
            ]
        )
        == 1
    )


def test_change_feed_preserves_sequence_and_scope(root, tmp_path):
    with patch.object(tickets, "_utc_now", return_value="2026-09-08T12:00:00Z"):
        create(root)
    with patch.object(tickets, "_utc_now", return_value="2026-09-08T11:00:00Z"):
        tickets.comment_ticket(root, "T1", actor="gpt-master", summary="clock reversed")
    first = tickets.ticket_changes(root, limit=1)
    second = tickets.ticket_changes(root, cursor=first["cursor"], limit=1)
    assert [first["events"][0]["seq"], second["events"][0]["seq"]] == [1, 2]
    with pytest.raises(board.BoardError):
        tickets.ticket_changes(tmp_path / "other", cursor=first["cursor"])


def test_comment_retry_is_bound_to_payload(root):
    create(root)
    first = tickets.comment_ticket(
        root, "T1", actor="gpt-master", summary="one", idempotency_key="post"
    )
    retry = tickets.comment_ticket(
        root, "T1", actor="gpt-master", summary="one", idempotency_key="post"
    )
    assert retry["revision"] == first["revision"]
    with pytest.raises(board.BoardError):
        tickets.comment_ticket(
            root, "T1", actor="gpt-master", summary="two", idempotency_key="post"
        )


def test_two_claims_have_one_winner(root):
    create(root)

    def claim(actor):
        try:
            return tickets.claim_ticket(root, "T1", actor=actor)
        except tickets.TicketConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ["gpt-master/a", "gpt-master/b"]))
    assert sum(c is not None for c in claims) == 1


def test_fsync_failure_has_uncertain_commit_status(root):
    create(root)
    with patch.object(tickets.os, "fsync", side_effect=OSError("fsync failed")):
        with pytest.raises(tickets.CommitUncertain) as caught:
            tickets.comment_ticket(
                root,
                "T1",
                actor="gpt-master",
                summary="maybe committed",
                idempotency_key="uncertain",
            )
    assert caught.value.ticket_id == "T1" and caught.value.revision == 2
    retry = tickets.comment_ticket(
        root,
        "T1",
        actor="gpt-master",
        summary="maybe committed",
        idempotency_key="uncertain",
    )
    assert len(retry["comments"]) == 1


def test_append_after_valid_unterminated_record(root):
    create(root)
    path = root / "ticket-events/T1.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    tickets.comment_ticket(root, "T1", actor="gpt-master", summary="next")
    assert tickets.verify_ticket_chain(root, "T1")[0]


def test_handoff_reserves_next_assignee_and_invalidates_review(root):
    create(root)
    claim = tickets.claim_ticket(root, "T1", actor="gpt-master/worker")
    tickets.review_ticket(
        root, "T1", actor="gpt-master", verdict="PASS", summary="old delivery"
    )
    tickets.handoff_ticket(
        root,
        "T1",
        actor="gpt-master/worker",
        summary="new delivery",
        evidence={"test": "pytest", "exit_code": 0},
        next_actor="gpt-master",
        lease_token=claim["lease"]["token"],
    )
    with pytest.raises(board.BoardError):
        tickets.mark_ticket_done(root, "T1", actor="gpt-master")
    with pytest.raises(board.BoardError):
        tickets.claim_ticket(root, "T1", actor="claude-master")
    tickets.claim_ticket(root, "T1", actor="gpt-master")


def test_expired_claim_requires_recovery_and_old_token_stays_fenced(root):
    create(root)
    with patch.object(tickets, "_utc_now", return_value="2026-09-08T12:00:00Z"):
        first = tickets.claim_ticket(root, "T1", actor="gpt-master/worker", ttl_sec=60)
    with patch.object(tickets, "_utc_now", return_value="2026-09-08T12:01:00Z"):
        with pytest.raises(board.BoardError):
            tickets.claim_ticket(root, "T1", actor="gpt-master/worker")
        recovered = tickets.claim_ticket(
            root, "T1", actor="gpt-master/worker", recover=True
        )
        assert recovered["lease"]["token"] != first["lease"]["token"]
        with pytest.raises(board.BoardError):
            tickets.heartbeat_ticket(
                root,
                "T1",
                actor="gpt-master/worker",
                lease_token=first["lease"]["token"],
            )
        tickets.heartbeat_ticket(
            root,
            "T1",
            actor="gpt-master/worker",
            lease_token=recovered["lease"]["token"],
        )


def test_actor_context_uses_registered_review_owner(root):
    tickets.register_actor(root, "helper", role="SUBAGENT", master="gpt-master")
    tickets.create_ticket(
        root, actor="gpt-master", ticket_id="T1", title="Review", reviewer="helper"
    )
    helper = tickets.actor_context(root, actor="helper")["tickets"][0]
    master = tickets.actor_context(root, actor="gpt-master")["tickets"][0]
    assert "review" not in helper["allowed_actions"]
    assert "review" in master["allowed_actions"]


def test_invalid_middle_record_cannot_be_tail_recovered(root):
    create(root)
    path = root / "ticket-events/T1.jsonl"
    original = path.read_bytes() + b"{invalid}\n" + b"{partial"
    path.write_bytes(original)
    with pytest.raises(board.BoardError):
        tickets.recover_ticket_tail(root, "T1", actor="gpt-master")
    assert path.read_bytes() == original


def test_partial_dependency_append_can_be_recovered_without_rewriting_prefix(root):
    create(root, "A")
    create(root, "B")
    tickets.add_dependency(
        root, "A", actor="gpt-master", dep_type="BLOCKED_BY", target="B"
    )
    path = root / "ticket-events/B.jsonl"
    prefix = path.read_bytes()
    append = tickets._append_ticket_event

    def interrupted(root_arg, ticket_id, event):
        if ticket_id == "B" and event["type"] == tickets.EV_DEP_REMOVE:
            with path.open("ab") as handle:
                handle.write(b'{"type":')
            raise tickets.CommitUncertain("B", 3)
        return append(root_arg, ticket_id, event)

    with patch.object(tickets, "_append_ticket_event", side_effect=interrupted):
        committed = tickets.remove_dependency(
            root,
            "A",
            actor="gpt-master",
            dep_type="BLOCKED_BY",
            target="B",
            idempotency_key="remove",
        )
    assert committed["persistence"]["committed"]
    recovered = tickets.recover_ticket_tail(
        root, "B", actor="gpt-master", expected_revision=2
    )
    assert recovered["revision"] == 3
    assert path.read_bytes().startswith(prefix)
    assert (
        tickets.get_ticket(root, "A")["deps"]
        == tickets.get_ticket(root, "B")["deps"]
        == []
    )
    tickets.add_dependency(
        root, "A", actor="gpt-master", dep_type="BLOCKED_BY", target="B"
    )
    tickets.remove_dependency(
        root,
        "A",
        actor="gpt-master",
        dep_type="BLOCKED_BY",
        target="B",
        idempotency_key="remove",
    )
    assert tickets.get_ticket(root, "A")["deps"][0]["target"] == "B"
    assert tickets.get_ticket(root, "B")["deps"][0]["target"] == "A"


def test_assigned_developer_starts_with_fenced_lease(root):
    create(root)
    claimed = tickets.claim_ticket(root, "T1", actor="gpt-master/dev")
    for actor, stage, token in [("gpt-master/other", "DEVELOPMENT", claimed["lease"]["token"]),
                                ("gpt-master/dev", "QA", claimed["lease"]["token"]),
                                ("gpt-master/dev", "DEVELOPMENT", "wrong")]:
        with pytest.raises(tickets.TicketConflict):
            tickets.transition_ticket(root, "T1", actor=actor, stage=stage, lease_token=token)
    updated = tickets.transition_ticket(root, "T1", actor="gpt-master/dev", stage="DEVELOPMENT",
                                        lease_token=claimed["lease"]["token"], expected_revision=claimed["revision"])
    assert updated["stage"] == "DEVELOPMENT"
    assert tickets._read_ticket_events(root, "T1")[-1]["actor"] == "gpt-master/dev"


def test_assigned_developer_starts_with_assignment_lease(root):
    create(root)
    assigned = tickets.assign_ticket(root, "T1", actor="gpt-master", assignee="gpt-master/dev")
    updated = tickets.transition_ticket(root, "T1", actor="gpt-master/dev", stage="DEVELOPMENT",
                                        expected_revision=assigned["revision"])
    assert updated["stage"] == "DEVELOPMENT"


@pytest.mark.parametrize("condition", ["no_lease", "expired", "blocked", "stale_revision"])
def test_developer_start_preserves_workflow_guards(root, condition):
    create(root)
    if condition == "no_lease":
        tickets.upsert_ticket(root, "T1", actor="gpt-master", assignee="gpt-master/dev")
    elif condition == "expired":
        with patch.object(tickets, "_utc_now", return_value="2020-01-01T00:00:00Z"):
            tickets.assign_ticket(root, "T1", actor="gpt-master", assignee="gpt-master/dev")
    else:
        tickets.assign_ticket(root, "T1", actor="gpt-master", assignee="gpt-master/dev")
    if condition == "blocked":
        create(root, "BLOCKER")
        tickets.add_dependency(root, "T1", actor="gpt-master", dep_type="BLOCKED_BY", target="BLOCKER")
    with pytest.raises(board.BoardError):
        tickets.transition_ticket(root, "T1", actor="gpt-master/dev", stage="DEVELOPMENT",
                                  expected_revision=0 if condition == "stale_revision" else None)


def test_transition_parser_accepts_assigned_actor_and_token():
    args = board.build_parser().parse_args(["ticket", "transition", "--actor", "gpt-master/dev",
                                           "--id", "T1", "--stage", "DEVELOPMENT", "--lease-token", "fence"])
    assert args.actor == "gpt-master/dev" and args.lease_token == "fence"
