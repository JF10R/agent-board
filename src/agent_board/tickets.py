#!/usr/bin/env python3
"""Ticket lifecycle: hash-chained events, derived state, stages, leases, reviews.

Additive to the board: nothing here touches messages, status, or the roadmap-item
store. A ticket's source of truth is its per-ticket event log (hash-chained, append
only, under ``ticket-events/<id>.jsonl``); ``tickets.v1.json`` is a rebuildable cache
of the folded state, written after every mutation so reads never re-fold history.
Leases (``leases/<id>.json``) are the only work-in-progress-visibility mechanism:
assigning a ticket opens one, a heartbeat extends it, and DONE or reassignment
closes it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

try:
    from agent_board import cli as board
except ImportError:  # Direct execution from within the package directory.
    import cli as board  # type: ignore[no-redef]


TICKET_SCHEMA_VERSION = 1
ACTOR_REGISTRY_SCHEMA_VERSION = 1
ACTORS_FILE = "actors.v1.json"
TICKETS_FILE = "tickets.v1.json"

STAGES = (
    "BACKLOG",
    "ANALYSIS",
    "DEVELOPMENT",
    "QA",
    "INTEGRATION",
    "DONE",
    "BLOCKED",
    "CANCELLED",
)
ACTIVE_STAGES = frozenset({"ANALYSIS", "DEVELOPMENT", "QA", "INTEGRATION", "BLOCKED"})
TERMINAL_STAGES = frozenset({"DONE", "CANCELLED"})
TICKET_KINDS = ("RESEARCH", "ENGINEERING", "REVIEW", "OPS")
DEP_TYPES = ("BLOCKED_BY", "UNBLOCKS", "ADVANCES", "SUPERSEDES")
REVIEW_VERDICTS = ("PASS", "CONFIRMED_WITH_FIXES", "FAIL")
ACTOR_ROLES = ("LEAD", "OPERATOR", "MASTER", "SUBAGENT")

DEFAULT_LEASE_TTL_SEC = 3600
DEFAULT_STALE_SEC = 2 * 3600

MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 300
MAX_BODY_CHARS = 32_768
MAX_CRITERIA = 32
MAX_CRITERION_CHARS = 300
MAX_SUBAGENTS = 32
MAX_FINDINGS = 64

TEMPLATES: dict[str, dict[str, Any]] = {
    "RESEARCH": {
        "acceptance_criteria": [
            "Five-line alpha summary present",
            "Sources cited as pointers (urls/paths)",
            "Open questions listed",
        ],
    },
    "ENGINEERING": {
        "acceptance_criteria": [
            "Implementation complete against scope",
            "Tests listed with exit codes",
            "No unreviewed DONE",
        ],
    },
    "REVIEW": {
        "acceptance_criteria": [
            "Verdict recorded (PASS|CONFIRMED_WITH_FIXES|FAIL)",
            "Findings numbered when not PASS",
        ],
    },
    "OPS": {
        "acceptance_criteria": [
            "Runbook pointer present",
            "Impact and rollback noted",
        ],
    },
}

_ACTOR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}(?:/[A-Za-z][A-Za-z0-9_.-]{0,63})?$")

# Event types on a ticket's hash-chained log.
EV_CREATE = "ticket.created"
EV_UPSERT = "ticket.upserted"
EV_ASSIGN = "ticket.assigned"
EV_STAGE = "ticket.stage"
EV_COMMENT = "ticket.comment"
EV_WORKLOG = "ticket.worklog"
EV_REVIEW = "ticket.review"
EV_DEP_ADD = "ticket.dep_add"
EV_HEARTBEAT = "ticket.heartbeat"
EV_DONE = "ticket.done"
EV_ARCHIVE = "ticket.archived"


# --------------------------------------------------------------------- validation


def _require_actor_name(label: str, value: str) -> str:
    if not isinstance(value, str) or not _ACTOR_RE.fullmatch(value):
        raise board.BoardError(
            f"invalid {label} {value!r}: letters/digits/._- and an optional master/sub form"
        )
    return value


def master_of(actor: str) -> str:
    """``sol-master/worker`` -> ``sol-master``; a bare name is its own master."""

    _require_actor_name("actor", actor)
    return actor.split("/", 1)[0] if "/" in actor else actor


def is_subagent(actor: str) -> bool:
    return "/" in _require_actor_name("actor", actor)


def _require_kind(value: str) -> str:
    kind = (value or "").strip().upper()
    if kind not in TICKET_KINDS:
        raise board.BoardError(f"invalid ticket kind {value!r}; expected one of {TICKET_KINDS}")
    return kind


def _require_stage(value: str) -> str:
    stage = (value or "").strip().upper()
    if stage not in STAGES:
        raise board.BoardError(f"invalid ticket stage {value!r}; expected one of {STAGES}")
    return stage


def _require_dep_type(value: str) -> str:
    dep = (value or "").strip().upper()
    if dep not in DEP_TYPES:
        raise board.BoardError(f"invalid dependency type {value!r}; expected one of {DEP_TYPES}")
    return dep


def _require_verdict(value: str) -> str:
    verdict = (value or "").strip().upper()
    if verdict not in REVIEW_VERDICTS:
        raise board.BoardError(f"invalid review verdict {value!r}; expected one of {REVIEW_VERDICTS}")
    return verdict


def _require_title(value: str) -> str:
    title = " ".join((value or "").strip().split())
    if not title:
        raise board.BoardError("ticket title must not be empty")
    if len(title) > MAX_TITLE_CHARS:
        raise board.BoardError(f"ticket title exceeds {MAX_TITLE_CHARS} characters")
    return title


def _require_optional_summary(value: str) -> str:
    summary = " ".join((value or "").strip().split())
    if len(summary) > MAX_SUMMARY_CHARS:
        raise board.BoardError(f"ticket summary exceeds {MAX_SUMMARY_CHARS} characters")
    return summary


def _require_body(value: str) -> str:
    body = value or ""
    if len(body) > MAX_BODY_CHARS:
        raise board.BoardError(f"ticket body exceeds {MAX_BODY_CHARS} characters")
    return body


def _require_criteria(values: Sequence[str] | None) -> list[str]:
    criteria = list(values or [])
    if len(criteria) > MAX_CRITERIA:
        raise board.BoardError(f"acceptance criteria exceeds {MAX_CRITERIA} entries")
    out = []
    for entry in criteria:
        text = " ".join((entry or "").strip().split())
        if not text:
            continue
        if len(text) > MAX_CRITERION_CHARS:
            raise board.BoardError(f"acceptance criterion exceeds {MAX_CRITERION_CHARS} characters")
        out.append(text)
    return out


def _require_subagents(values: Sequence[str] | None) -> list[str]:
    subagents = list(values or [])
    if len(subagents) > MAX_SUBAGENTS:
        raise board.BoardError(f"subagent list exceeds {MAX_SUBAGENTS} entries")
    return [_require_actor_name("subagent", name) for name in subagents]


def _utc_now() -> str:
    return board.utc_now()


def _ts_to_epoch(value: str) -> float:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(raw).timestamp()


def _offset_ts(ts: str, seconds: int) -> str:
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    moved = datetime.fromisoformat(raw) + timedelta(seconds=seconds)
    return moved.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


# --------------------------------------------------------------------------- paths


def _tickets_path(root: Path) -> Path:
    return root / TICKETS_FILE


def _events_dir(root: Path) -> Path:
    return root / "ticket-events"


def _ticket_events_path(root: Path, ticket_id: str) -> Path:
    return _events_dir(root) / f"{ticket_id}.jsonl"


def _leases_dir(root: Path) -> Path:
    return root / "leases"


def _lease_path(root: Path, ticket_id: str) -> Path:
    return _leases_dir(root) / f"{ticket_id}.json"


def _actors_path(root: Path) -> Path:
    return root / ACTORS_FILE


def ensure_ticket_directories(root: Path) -> None:
    for name in ("ticket-events", "leases"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _ticket_lock(root: Path, timeout_seconds: float = 5.0):
    ensure_ticket_directories(root)
    return board._file_lock(root / ".tickets.lock", timeout_seconds)


# ------------------------------------------------------------------ hash-chain log


def _append_ticket_event(root: Path, ticket_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """Append one entry to a ticket's hash-chained log. Caller must hold ``_ticket_lock``."""

    path = _ticket_events_path(root, ticket_id)
    existing = _read_ticket_events(root, ticket_id)
    prev_hash = existing[-1]["hash"] if existing else "0" * 64
    seq = (existing[-1]["seq"] + 1) if existing else 1
    body = {key: value for key, value in event.items() if key not in ("hash", "prev", "seq", "ts")}
    body["seq"] = seq
    body["prev"] = prev_hash
    body["ts"] = event.get("ts") or _utc_now()
    body["hash"] = board.hashlib.sha256(
        board._canonical_json({key: body[key] for key in sorted(body)}).encode("utf-8")
    ).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(board.json.dumps(body, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        board.os.fsync(handle.fileno())
    return body


def _read_ticket_events(root: Path, ticket_id: str) -> list[dict[str, Any]]:
    path = _ticket_events_path(root, ticket_id)
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(board.json.loads(line))
    return events


def verify_ticket_chain(root: Path, ticket_id: str) -> tuple[bool, str]:
    """Recompute every hash in the ticket's event log; the log is the source of truth."""

    events = _read_ticket_events(root, ticket_id)
    prev = "0" * 64
    for event in events:
        if event.get("prev") != prev:
            return False, f"broken prev link at seq={event.get('seq')}"
        body = {key: value for key, value in event.items() if key != "hash"}
        expected = board.hashlib.sha256(
            board._canonical_json({key: body[key] for key in sorted(body)}).encode("utf-8")
        ).hexdigest()
        if event.get("hash") != expected:
            return False, f"hash mismatch at seq={event.get('seq')}"
        prev = event["hash"]
    return True, "ok"


# ------------------------------------------------------------------- derived state


def _empty_ticket_state(ticket_id: str) -> dict[str, Any]:
    return {
        "id": ticket_id,
        "title": "",
        "kind": "ENGINEERING",
        "stage": "BACKLOG",
        "parent_id": None,
        "assignee": None,
        "reviewer": None,
        "subagents": [],
        "acceptance_criteria": [],
        "summary": "",
        "body": "",
        "revision": 0,
        "created_at": None,
        "updated_at": None,
        "created_by": None,
        "stage_entered_at": None,
        "deps": [],
        "comments": [],
        "worklog": [],
        "reviews": [],
        "stage_history": [],
        "time_in_stage": {},
        "last_activity_at": None,
        "done_at": None,
        "archived": False,
    }


def _apply_event(state: dict[str, Any], event: Mapping[str, Any]) -> None:
    kind = event.get("type")
    ts = event.get("ts") or _utc_now()
    actor = event.get("actor")
    state["updated_at"] = ts
    state["last_activity_at"] = ts
    state["revision"] = event.get("seq", state.get("revision", 0))

    if kind == EV_CREATE:
        state["title"] = event.get("title") or state["title"]
        state["kind"] = event.get("kind") or "ENGINEERING"
        state["summary"] = event.get("summary") or ""
        state["body"] = event.get("body") or ""
        state["parent_id"] = event.get("parent_id")
        state["acceptance_criteria"] = list(event.get("acceptance_criteria") or [])
        state["created_at"] = ts
        state["created_by"] = actor
        state["stage"] = "BACKLOG"
        state["stage_entered_at"] = ts
        state["reviewer"] = event.get("reviewer") or (master_of(actor) if actor else None)
        state["subagents"] = list(event.get("subagents") or [])
        if event.get("assignee"):
            state["assignee"] = event["assignee"]
    elif kind == EV_UPSERT:
        for key in ("title", "summary", "body", "kind", "acceptance_criteria", "reviewer", "subagents"):
            if key in event and event[key] is not None:
                state[key] = event[key]
    elif kind == EV_ASSIGN:
        state["assignee"] = event.get("assignee")
        if event.get("reviewer"):
            state["reviewer"] = event["reviewer"]
        if event.get("subagents") is not None:
            state["subagents"] = list(event["subagents"])
    elif kind == EV_STAGE:
        _close_stage_interval(state, ts)
        state["stage"] = event.get("stage") or state["stage"]
        state["stage_entered_at"] = ts
        state["stage_history"].append({"stage": state["stage"], "entered_at": ts, "actor": actor})
    elif kind == EV_COMMENT:
        state["comments"].append(
            {"ts": ts, "actor": actor, "summary": event.get("summary") or "", "body": event.get("body") or "", "seq": event.get("seq")}
        )
    elif kind == EV_WORKLOG:
        state["worklog"].append(
            {"ts": ts, "actor": actor, "evidence": event.get("evidence") or {}, "summary": event.get("summary") or "", "seq": event.get("seq")}
        )
    elif kind == EV_REVIEW:
        state["reviews"].append(
            {
                "ts": ts,
                "actor": actor,
                "verdict": event.get("verdict"),
                "findings": list(event.get("findings") or []),
                "summary": event.get("summary") or "",
                "seq": event.get("seq"),
            }
        )
    elif kind == EV_DEP_ADD:
        dep = {"type": event.get("dep_type"), "target": event.get("target"), "ts": ts, "actor": actor}
        state["deps"] = [
            entry for entry in state["deps"]
            if not (entry.get("type") == dep["type"] and entry.get("target") == dep["target"])
        ]
        state["deps"].append(dep)
    elif kind == EV_HEARTBEAT:
        pass  # only bumps last_activity_at, already done above
    elif kind == EV_DONE:
        _close_stage_interval(state, ts)
        state["stage"] = "DONE"
        state["stage_entered_at"] = ts
        state["done_at"] = ts
    elif kind == EV_ARCHIVE:
        state["archived"] = True


def _close_stage_interval(state: dict[str, Any], ts: str) -> None:
    entered = state.get("stage_entered_at")
    stage = state.get("stage")
    if not entered or not stage:
        return
    delta = max(0.0, _ts_to_epoch(ts) - _ts_to_epoch(entered))
    time_in = state.setdefault("time_in_stage", {})
    time_in[stage] = time_in.get(stage, 0.0) + delta
    history = state.get("stage_history") or []
    if history and "exited_at" not in history[-1]:
        history[-1]["exited_at"] = ts


def derive_ticket(root: Path, ticket_id: str) -> dict[str, Any]:
    """Fold a ticket's event log into its current state. The log is authoritative."""

    state = _empty_ticket_state(ticket_id)
    events = _read_ticket_events(root, ticket_id)
    if not events:
        return state
    for event in events:
        _apply_event(state, event)

    now = _utc_now()
    now_epoch = _ts_to_epoch(now)
    time_in = dict(state.get("time_in_stage") or {})
    if state.get("stage_entered_at") and state.get("stage") not in TERMINAL_STAGES:
        entered = _ts_to_epoch(state["stage_entered_at"])
        current = state["stage"]
        time_in[current] = time_in.get(current, 0.0) + (now_epoch - entered)
    state["time_in_stage"] = time_in
    state["time_in_stage_human"] = {key: format_duration(value) for key, value in time_in.items()}

    last = state.get("last_activity_at") or state.get("updated_at") or state.get("created_at")
    idle_seconds = (now_epoch - _ts_to_epoch(last)) if last else 0.0
    state["idle_seconds"] = idle_seconds
    state["stale"] = bool(state.get("stage") in ACTIVE_STAGES and idle_seconds >= DEFAULT_STALE_SEC)

    lease = _read_lease(root, ticket_id)
    if lease:
        state["lease"] = lease
        expires = lease.get("expires_at")
        state["lease_stale"] = bool(expires and _ts_to_epoch(expires) < now_epoch)
    else:
        state["lease"] = None
        state["lease_stale"] = False
    return state


def _read_tickets_cache(root: Path) -> dict[str, Any]:
    path = _tickets_path(root)
    try:
        value = board.json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": TICKET_SCHEMA_VERSION, "tickets": []}
    if not isinstance(value, dict) or set(value) != {"schema_version", "tickets"}:
        raise board.BoardError("invalid tickets cache shape")
    return value


def _persist_ticket(root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    """Refold from the event log and refresh the cache entry. Caller holds the lock."""

    fresh = derive_ticket(root, state["id"])
    cache = _read_tickets_cache(root)
    tickets = [item for item in cache["tickets"] if item["id"] != fresh["id"]]
    tickets.append(fresh)
    tickets.sort(key=lambda item: item["id"])
    board._write_atomic_replace(
        _tickets_path(root), board._canonical_json({"schema_version": TICKET_SCHEMA_VERSION, "tickets": tickets})
    )
    return fresh


def _require_existing(root: Path, ticket_id: str) -> dict[str, Any]:
    state = derive_ticket(root, ticket_id)
    if not state.get("created_at"):
        raise board.BoardError(f"unknown ticket: {ticket_id}")
    return state


def _require_revision(state: Mapping[str, Any], expected: int | None) -> None:
    if expected is None:
        return
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise board.BoardError("expected revision must be a non-negative integer")
    actual = int(state.get("revision") or 0)
    if expected != actual:
        raise board.BoardError(
            f"ticket revision conflict for {state['id']}: expected {expected}, current {actual}"
        )


# ------------------------------------------------------------------------- leases


def _read_lease(root: Path, ticket_id: str) -> dict[str, Any] | None:
    path = _lease_path(root, ticket_id)
    if not path.is_file():
        return None
    return board.json.loads(path.read_text(encoding="utf-8"))


def _write_lease(root: Path, ticket_id: str, lease: Mapping[str, Any]) -> None:
    board._write_atomic_replace(_lease_path(root, ticket_id), board._canonical_json(lease))


def _delete_lease(root: Path, ticket_id: str) -> None:
    path = _lease_path(root, ticket_id)
    if path.is_file():
        path.unlink()


def list_leases(root: Path) -> list[dict[str, Any]]:
    directory = _leases_dir(root)
    if not directory.is_dir():
        return []
    now_epoch = _ts_to_epoch(_utc_now())
    out = []
    for path in sorted(directory.glob("*.json")):
        lease = board.json.loads(path.read_text(encoding="utf-8"))
        expires = lease.get("expires_at")
        lease = {**lease, "stale": bool(expires and _ts_to_epoch(expires) < now_epoch)}
        out.append(lease)
    return out


# ------------------------------------------------------------------- actor registry


def _read_actors(root: Path) -> dict[str, Any]:
    path = _actors_path(root)
    try:
        value = board.json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": ACTOR_REGISTRY_SCHEMA_VERSION, "actors": {}}
    if not isinstance(value, dict) or set(value) != {"schema_version", "actors"}:
        raise board.BoardError("invalid actor registry shape")
    return value


def _write_actors(root: Path, data: Mapping[str, Any]) -> None:
    board._write_atomic_replace(_actors_path(root), board._canonical_json(data))


def register_actor(root: Path, name: str, *, role: str, display: str = "", master: str | None = None) -> dict[str, Any]:
    name = _require_actor_name("actor", name)
    role = (role or "").strip().upper()
    if role not in ACTOR_ROLES:
        raise board.BoardError(f"invalid actor role {role!r}; expected one of {ACTOR_ROLES}")
    if role == "SUBAGENT" and not is_subagent(name) and not master:
        raise board.BoardError("a subagent must be named <master>/<name>, or pass master=")
    if role == "SUBAGENT" and master:
        master = _require_actor_name("master", master)
    if role == "MASTER" and is_subagent(name):
        raise board.BoardError("a master identity cannot use the master/sub form")
    with _ticket_lock(root):
        data = _read_actors(root)
        actors = data.setdefault("actors", {})
        entry = {"name": name, "role": role, "display": display or name, "master": master if role == "SUBAGENT" else None}
        actors[name] = entry
        _write_actors(root, data)
        return entry


def list_actors(root: Path, *, role: str | None = None) -> list[dict[str, Any]]:
    data = _read_actors(root)
    out = []
    for name, info in sorted((data.get("actors") or {}).items()):
        entry = {**info, "name": info.get("name", name)}
        if role and entry.get("role") != role.strip().upper():
            continue
        out.append(entry)
    return out


def get_actor(root: Path, name: str) -> dict[str, Any] | None:
    name = _require_actor_name("actor", name)
    return (_read_actors(root).get("actors") or {}).get(name)


def resolve_reviewer(root: Path, actor: str) -> str:
    """The owning master for a subagent; otherwise the actor itself."""

    info = get_actor(root, actor)
    if info and info.get("role") == "SUBAGENT" and info.get("master"):
        return _require_actor_name("master", info["master"])
    root_name = master_of(actor)
    if root_name == actor:
        return actor
    registered = (_read_actors(root).get("actors") or {})
    if root_name in registered and registered[root_name].get("role") == "MASTER":
        return root_name
    candidate = f"{root_name}-master"
    if candidate in registered:
        return candidate
    return root_name


# ------------------------------------------------------------------- ticket lifecycle


def create_ticket(
    root: Path,
    *,
    actor: str,
    ticket_id: str,
    title: str,
    kind: str = "ENGINEERING",
    summary: str = "",
    body: str = "",
    parent_id: str | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    assignee: str | None = None,
    reviewer: str | None = None,
    subagents: Sequence[str] | None = None,
) -> dict[str, Any]:
    board._require_identity(actor, root)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    kind = _require_kind(kind)
    title = _require_title(title)
    summary = _require_optional_summary(summary)
    body = _require_body(body)
    if parent_id is not None:
        parent_id = board._require_safe_token("parent ticket id", parent_id)
    criteria = _require_criteria(acceptance_criteria) or list(TEMPLATES[kind]["acceptance_criteria"])
    if assignee is not None:
        assignee = _require_actor_name("assignee", assignee)
    if reviewer is not None:
        reviewer = _require_actor_name("reviewer", reviewer)
    else:
        reviewer = resolve_reviewer(root, assignee or actor)
    subs = _require_subagents(subagents)

    with _ticket_lock(root):
        if _read_ticket_events(root, ticket_id):
            raise board.BoardError(f"ticket already exists: {ticket_id}")
        if parent_id is not None and not _require_existing(root, parent_id):
            raise board.BoardError(f"unknown parent ticket: {parent_id}")
        _append_ticket_event(
            root,
            ticket_id,
            {
                "type": EV_CREATE,
                "actor": actor,
                "title": title,
                "kind": kind,
                "summary": summary,
                "body": body,
                "parent_id": parent_id,
                "acceptance_criteria": criteria,
                "assignee": assignee,
                "reviewer": reviewer,
                "subagents": subs,
            },
        )
        return _persist_ticket(root, {"id": ticket_id})


def upsert_ticket(root: Path, ticket_id: str, *, actor: str, expected_revision: int | None = None, **fields: Any) -> dict[str, Any]:
    board._require_identity(actor, root)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        payload: dict[str, Any] = {"type": EV_UPSERT, "actor": actor}
        if "title" in fields and fields["title"] is not None:
            payload["title"] = _require_title(fields["title"])
        if "summary" in fields and fields["summary"] is not None:
            payload["summary"] = _require_optional_summary(fields["summary"])
        if "body" in fields and fields["body"] is not None:
            payload["body"] = _require_body(fields["body"])
        if "kind" in fields and fields["kind"] is not None:
            payload["kind"] = _require_kind(fields["kind"])
        if "acceptance_criteria" in fields and fields["acceptance_criteria"] is not None:
            payload["acceptance_criteria"] = _require_criteria(fields["acceptance_criteria"])
        if "reviewer" in fields and fields["reviewer"] is not None:
            payload["reviewer"] = _require_actor_name("reviewer", fields["reviewer"])
        if "subagents" in fields and fields["subagents"] is not None:
            payload["subagents"] = _require_subagents(fields["subagents"])
        _append_ticket_event(root, ticket_id, payload)
        return _persist_ticket(root, state)


def assign_ticket(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    assignee: str,
    expected_revision: int | None = None,
    ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
    reviewer: str | None = None,
    subagents: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Self-assign (or reassign) a ticket and open a fresh lease on it."""

    board._require_identity(actor, root)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    assignee = _require_actor_name("assignee", assignee)
    if not isinstance(ttl_sec, int) or isinstance(ttl_sec, bool) or ttl_sec <= 0:
        raise board.BoardError("lease ttl_sec must be a positive integer")
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        resolved_reviewer = _require_actor_name("reviewer", reviewer) if reviewer else resolve_reviewer(root, assignee)
        subs = _require_subagents(subagents) if subagents is not None else list(state.get("subagents") or [])
        _append_ticket_event(
            root,
            ticket_id,
            {"type": EV_ASSIGN, "actor": actor, "assignee": assignee, "reviewer": resolved_reviewer, "subagents": subs},
        )
        now = _utc_now()
        lease = {
            "ticket_id": ticket_id,
            "assignee": assignee,
            "granted_by": actor,
            "granted_at": now,
            "heartbeat_at": now,
            "expires_at": _offset_ts(now, ttl_sec),
            "ttl_sec": ttl_sec,
        }
        _write_lease(root, ticket_id, lease)
        return _persist_ticket(root, state)


def heartbeat_ticket(root: Path, ticket_id: str, *, actor: str, ttl_sec: int | None = None) -> dict[str, Any]:
    actor = _require_actor_name("actor", actor)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    with _ticket_lock(root):
        lease = _read_lease(root, ticket_id)
        if not lease:
            raise board.BoardError(f"no lease held on {ticket_id}")
        if lease.get("assignee") != actor:
            raise board.BoardError(f"lease on {ticket_id} is held by {lease.get('assignee')}, not {actor}")
        now = _utc_now()
        ttl = int(ttl_sec) if ttl_sec else int(lease.get("ttl_sec") or DEFAULT_LEASE_TTL_SEC)
        if ttl <= 0:
            raise board.BoardError("lease ttl_sec must be a positive integer")
        lease = {**lease, "heartbeat_at": now, "expires_at": _offset_ts(now, ttl), "ttl_sec": ttl}
        _write_lease(root, ticket_id, lease)
        _append_ticket_event(root, ticket_id, {"type": EV_HEARTBEAT, "actor": actor})
        return _persist_ticket(root, {"id": ticket_id})


def transition_ticket(root: Path, ticket_id: str, *, actor: str, stage: str, expected_revision: int | None = None, summary: str = "") -> dict[str, Any]:
    board._require_identity(actor, root)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    stage = _require_stage(stage)
    if stage == "DONE":
        raise board.BoardError("use the done action to finish a ticket; it requires an owning-master review")
    summary = _require_optional_summary(summary)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        if stage in ACTIVE_STAGES - {"BLOCKED"}:
            blockers = _open_blockers(root, state)
            if blockers:
                raise board.BoardError(f"ticket {ticket_id} is blocked by: {', '.join(blockers)}")
        _append_ticket_event(root, ticket_id, {"type": EV_STAGE, "actor": actor, "stage": stage, "summary": summary})
        return _persist_ticket(root, state)


def comment_ticket(root: Path, ticket_id: str, *, actor: str, summary: str, body: str = "", expected_revision: int | None = None) -> dict[str, Any]:
    actor = _require_actor_name("actor", actor)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    summary = _require_title(summary)
    body = _require_body(body)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        _append_ticket_event(root, ticket_id, {"type": EV_COMMENT, "actor": actor, "summary": summary, "body": body})
        return _persist_ticket(root, state)


def add_worklog(root: Path, ticket_id: str, *, actor: str, summary: str, evidence: Mapping[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
    """Evidence pointers only: repo+sha, test command+exit code, or artifact path+content hash."""

    actor = _require_actor_name("actor", actor)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    summary = _require_title(summary)
    evidence = _validate_evidence(evidence)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        _append_ticket_event(root, ticket_id, {"type": EV_WORKLOG, "actor": actor, "summary": summary, "evidence": evidence})
        return _persist_ticket(root, state)


def _validate_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence, dict) or not evidence:
        raise board.BoardError("worklog evidence is required: repo/sha, test/exit_code, or artifact/content_hash")
    out: dict[str, Any] = {}
    if "repo" in evidence or "sha" in evidence:
        sha = evidence.get("sha") or ""
        if not sha:
            raise board.BoardError("evidence.sha is required when recording a commit pointer")
        out["repo"] = str(evidence.get("repo") or "")
        out["sha"] = str(sha)
    if "test" in evidence or "exit_code" in evidence:
        if "exit_code" not in evidence:
            raise board.BoardError("evidence.exit_code is required with a test command")
        out["test"] = str(evidence.get("test") or "")
        out["exit_code"] = int(evidence["exit_code"])
    if "artifact" in evidence or "content_hash" in evidence:
        content_hash = evidence.get("content_hash") or ""
        if not content_hash:
            raise board.BoardError("evidence.content_hash is required with an artifact path")
        out["artifact"] = str(evidence.get("artifact") or "")
        out["content_hash"] = str(content_hash)
    if not out:
        raise board.BoardError("worklog evidence must include a commit, test, or artifact pointer")
    return out


def review_ticket(root: Path, ticket_id: str, *, actor: str, verdict: str, summary: str, findings: Sequence[str] | None = None, expected_revision: int | None = None) -> dict[str, Any]:
    actor = _require_actor_name("actor", actor)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    verdict = _require_verdict(verdict)
    summary = _require_title(summary)
    findings = list(findings or [])
    if len(findings) > MAX_FINDINGS:
        raise board.BoardError(f"review findings exceed {MAX_FINDINGS} entries")
    if verdict in ("CONFIRMED_WITH_FIXES", "FAIL") and not findings:
        raise board.BoardError(f"{verdict} requires numbered findings")
    numbered = []
    for index, finding in enumerate(findings, 1):
        text = " ".join((finding or "").strip().split())
        if not text:
            continue
        if len(text) > MAX_CRITERION_CHARS:
            raise board.BoardError(f"review finding exceeds {MAX_CRITERION_CHARS} characters")
        numbered.append(text if text[0].isdigit() else f"{index}. {text}")

    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        owner = state.get("reviewer") or master_of(state.get("assignee") or actor)
        actor_root, owner_root = master_of(actor), master_of(owner)
        if actor not in (owner, owner_root) and actor_root not in (owner, owner_root):
            raise board.BoardError(f"review of {ticket_id} is reserved for its owning master {owner} (actor={actor})")
        _append_ticket_event(root, ticket_id, {"type": EV_REVIEW, "actor": actor, "verdict": verdict, "findings": numbered, "summary": summary})
        if verdict == "FAIL":
            _append_ticket_event(root, ticket_id, {"type": EV_STAGE, "actor": actor, "stage": "DEVELOPMENT", "summary": "review FAIL"})
        return _persist_ticket(root, state)


def mark_ticket_done(root: Path, ticket_id: str, *, actor: str, summary: str = "", expected_revision: int | None = None, force: bool = False) -> dict[str, Any]:
    board._require_identity(actor, root)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    summary = _require_optional_summary(summary) or "done"
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        reviews = state.get("reviews") or []
        reviewed = any(review.get("verdict") in ("PASS", "CONFIRMED_WITH_FIXES") for review in reviews)
        if not reviewed and not force:
            raise board.BoardError(f"DONE requires an owning-master review of PASS or CONFIRMED_WITH_FIXES on {ticket_id}")
        blockers = _open_blockers(root, state)
        if blockers and not force:
            raise board.BoardError(f"cannot mark {ticket_id} DONE while blocked by: {', '.join(blockers)}")
        _append_ticket_event(root, ticket_id, {"type": EV_DONE, "actor": actor, "summary": summary})
        _delete_lease(root, ticket_id)
        result = _persist_ticket(root, state)
        _notify_unblocked(root, ticket_id, actor)
        return result


def add_dependency(root: Path, ticket_id: str, *, actor: str, dep_type: str, target: str) -> dict[str, Any]:
    board._require_identity(actor, root)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    dep_type = _require_dep_type(dep_type)
    target = board._require_safe_token("dependency target ticket id", target)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        if dep_type == "BLOCKED_BY":
            if target == ticket_id or _would_cycle(root, ticket_id, target):
                raise board.BoardError(f"dependency cycle detected: {ticket_id} BLOCKED_BY {target}")
            _require_existing(root, target)
        _append_ticket_event(root, ticket_id, {"type": EV_DEP_ADD, "actor": actor, "dep_type": dep_type, "target": target})
        if dep_type == "BLOCKED_BY":
            # Symmetric convenience edge: A BLOCKED_BY B implies B UNBLOCKS A.
            _append_ticket_event(root, target, {"type": EV_DEP_ADD, "actor": actor, "dep_type": "UNBLOCKS", "target": ticket_id})
            _persist_ticket(root, {"id": target})
        return _persist_ticket(root, state)


def _open_blockers(root: Path, state: Mapping[str, Any]) -> list[str]:
    open_ids = []
    for dep in state.get("deps") or []:
        if dep.get("type") != "BLOCKED_BY":
            continue
        target = str(dep.get("target"))
        other = derive_ticket(root, target)
        if not other.get("created_at") or other.get("stage") not in TERMINAL_STAGES:
            open_ids.append(target)
    return open_ids


def _would_cycle(root: Path, src: str, target: str) -> bool:
    """True if adding ``src BLOCKED_BY target`` would create a cycle."""

    if src == target:
        return True
    seen: set[str] = set()

    def walk(ticket_id: str) -> bool:
        if ticket_id == src:
            return True
        if ticket_id in seen:
            return False
        seen.add(ticket_id)
        node = derive_ticket(root, ticket_id)
        if not node.get("created_at"):
            return False
        return any(
            dep.get("type") == "BLOCKED_BY" and walk(str(dep.get("target")))
            for dep in node.get("deps") or []
        )

    return walk(target)


def _notify_unblocked(root: Path, closed_id: str, actor: str) -> None:
    """A closed ticket's dependents get a comment noting the blocker cleared."""

    for node in _read_tickets_cache(root)["tickets"]:
        for dep in node.get("deps") or []:
            if dep.get("type") == "BLOCKED_BY" and dep.get("target") == closed_id:
                ticket_id = node["id"]
                _append_ticket_event(root, ticket_id, {"type": EV_COMMENT, "actor": actor, "summary": f"blocker {closed_id} closed", "body": ""})
                _persist_ticket(root, {"id": ticket_id})


# --------------------------------------------------------------------------- views


def list_tickets(root: Path, *, stage: str | None = None, assignee: str | None = None, parent_id: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
    out = []
    for cached in _read_tickets_cache(root)["tickets"]:
        if not include_archived and cached.get("archived"):
            continue
        if stage and cached.get("stage") != stage.strip().upper():
            continue
        if assignee and cached.get("assignee") != assignee:
            continue
        if parent_id is not None and cached.get("parent_id") != parent_id:
            continue
        out.append(derive_ticket(root, cached["id"]))
    return out


def get_ticket(root: Path, ticket_id: str) -> dict[str, Any]:
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    return _require_existing(root, ticket_id)


def ticket_tree(root: Path) -> list[dict[str, Any]]:
    """Parent/child nested structure over open (non-archived) tickets."""

    tickets = list_tickets(root, include_archived=False)
    by_id = {ticket["id"]: {**ticket, "children": []} for ticket in tickets}
    roots = []
    for ticket in by_id.values():
        parent_id = ticket.get("parent_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(ticket)
        else:
            roots.append(ticket)
    return roots


def critical_path(root: Path) -> list[str]:
    """The longest open BLOCKED_BY chain, as ticket ids, root-most first."""

    tickets = {ticket["id"]: ticket for ticket in list_tickets(root) if ticket.get("stage") not in TERMINAL_STAGES}
    memo: dict[str, list[str]] = {}

    def dfs(ticket_id: str, stack: set[str]) -> list[str]:
        if ticket_id in memo:
            return memo[ticket_id]
        if ticket_id in stack:
            return [ticket_id]
        ticket = tickets.get(ticket_id)
        if not ticket:
            return []
        stack = stack | {ticket_id}
        best: list[str] = []
        for dep in ticket.get("deps") or []:
            if dep.get("type") != "BLOCKED_BY":
                continue
            chain = dfs(str(dep.get("target")), stack)
            if len(chain) > len(best):
                best = chain
        memo[ticket_id] = [ticket_id] + best
        return memo[ticket_id]

    longest: list[str] = []
    for ticket_id in tickets:
        chain = dfs(ticket_id, set())
        if len(chain) > len(longest):
            longest = chain
    return longest


def archive_ticket(root: Path, ticket_id: str, *, actor: str) -> dict[str, Any]:
    board._require_identity(actor, root)
    ticket_id = board._require_safe_token("ticket id", ticket_id)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        if state.get("stage") not in TERMINAL_STAGES:
            raise board.BoardError("only a DONE or CANCELLED ticket can be archived")
        events = _read_ticket_events(root, ticket_id)
        digest = board.hashlib.sha256(
            board.json.dumps(events, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        archive_dir = root / "ticket-archive" / ticket_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        board._write_atomic_replace(archive_dir / "events.jsonl", "\n".join(board.json.dumps(e, sort_keys=True, ensure_ascii=False) for e in events) + "\n")
        board._write_atomic_replace(archive_dir / "content.sha256", digest + "\n")
        _append_ticket_event(root, ticket_id, {"type": EV_ARCHIVE, "actor": actor, "content_hash": digest})
        return {"ticket": _persist_ticket(root, state), "content_hash": digest}


def export_ticket_markdown(root: Path, ticket_id: str) -> str:
    ticket = get_ticket(root, ticket_id)
    lines = [
        f"# {ticket['id']} — {ticket.get('title')}",
        "",
        f"- stage: `{ticket.get('stage')}`",
        f"- kind: `{ticket.get('kind')}`",
        f"- assignee: `{ticket.get('assignee') or '(unassigned)'}`",
        f"- reviewer: `{ticket.get('reviewer') or '(none)'}`",
        f"- revision: {ticket.get('revision')}",
        f"- created: {ticket.get('created_at')}",
        f"- updated: {ticket.get('updated_at')}",
        "",
        "## Summary",
        ticket.get("summary") or "_(none)_",
        "",
    ]
    if ticket.get("acceptance_criteria"):
        lines.append("## Acceptance criteria")
        lines.extend(f"- {item}" for item in ticket["acceptance_criteria"])
        lines.append("")
    if ticket.get("deps"):
        lines.append("## Dependencies")
        lines.extend(f"- {dep.get('type')} `{dep.get('target')}`" for dep in ticket["deps"])
        lines.append("")
    if ticket.get("time_in_stage"):
        lines.append("## Time in stage")
        lines.extend(f"- {stage}: {format_duration(seconds)}" for stage, seconds in ticket["time_in_stage"].items())
        lines.append("")
    if ticket.get("worklog"):
        lines.append("## Work log")
        for entry in ticket["worklog"]:
            lines.append(f"- {entry.get('ts')} **{entry.get('actor')}**: {entry.get('summary')}")
            for key, value in (entry.get("evidence") or {}).items():
                lines.append(f"  - {key}: `{value}`")
        lines.append("")
    if ticket.get("reviews"):
        lines.append("## Reviews")
        for review in ticket["reviews"]:
            lines.append(f"- {review.get('ts')} **{review.get('actor')}** `{review.get('verdict')}`: {review.get('summary')}")
            lines.extend(f"  - {finding}" for finding in review.get("findings") or [])
        lines.append("")
    if ticket.get("comments"):
        lines.append("## Comments")
        lines.extend(f"- {comment.get('ts')} **{comment.get('actor')}**: {comment.get('summary')}" for comment in ticket["comments"])
        lines.append("")
    lines.append(f"<!-- content derived from the ticket event log; revision {ticket.get('revision')} -->")
    lines.append("")
    return "\n".join(lines)


def metrics_digest(root: Path) -> dict[str, Any]:
    tickets = list_tickets(root, include_archived=True)
    by_stage: dict[str, int] = {stage: 0 for stage in STAGES}
    time_sums: dict[str, float] = {stage: 0.0 for stage in STAGES}
    time_counts: dict[str, int] = {stage: 0 for stage in STAGES}
    per_master: dict[str, dict[str, int]] = {}
    reviews = 0
    blocked_time = 0.0
    done = 0
    for ticket in tickets:
        stage = ticket.get("stage") or "BACKLOG"
        by_stage[stage] = by_stage.get(stage, 0) + 1
        for stage_name, seconds in (ticket.get("time_in_stage") or {}).items():
            time_sums[stage_name] = time_sums.get(stage_name, 0.0) + float(seconds)
            time_counts[stage_name] = time_counts.get(stage_name, 0) + 1
        blocked_time += float((ticket.get("time_in_stage") or {}).get("BLOCKED", 0.0))
        master = master_of(ticket.get("reviewer") or ticket.get("assignee") or ticket.get("created_by") or "unknown")
        bucket = per_master.setdefault(master, {"tickets": 0, "done": 0, "reviews": 0})
        bucket["tickets"] += 1
        if stage == "DONE":
            bucket["done"] += 1
            done += 1
        bucket["reviews"] += len(ticket.get("reviews") or [])
        reviews += len(ticket.get("reviews") or [])
    avg_time = {stage: (time_sums[stage] / time_counts[stage] if time_counts.get(stage) else 0.0) for stage in STAGES}
    return {
        "generated_at": _utc_now(),
        "counts_by_stage": by_stage,
        "avg_seconds_by_stage": avg_time,
        "avg_human_by_stage": {stage: format_duration(seconds) for stage, seconds in avg_time.items()},
        "throughput_done": done,
        "review_events": reviews,
        "blocked_time_seconds": blocked_time,
        "blocked_time_human": format_duration(blocked_time),
        "per_master": per_master,
        "critical_path": critical_path(root),
        "stale": [ticket["id"] for ticket in tickets if ticket.get("stale")],
    }


def wip_by_agent(root: Path) -> dict[str, dict[str, list[str]]]:
    """``assignee -> stage -> [ticket ids]`` for tickets that are actually in flight."""

    result: dict[str, dict[str, list[str]]] = {}
    for ticket in list_tickets(root):
        if ticket.get("stage") in TERMINAL_STAGES or ticket.get("stage") == "BACKLOG":
            continue
        agent = ticket.get("assignee") or "(unassigned)"
        result.setdefault(agent, {}).setdefault(ticket["stage"], []).append(ticket["id"])
    return result
