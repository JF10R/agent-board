#!/usr/bin/env python3
"""Ticket lifecycle: hash-chained events, derived state, stages, leases, reviews.

Additive to the board: nothing here touches messages, status, or the roadmap-item
store. A ticket's source of truth is its per-ticket event log (hash-chained, append
only, under ``ticket-events/<id>.jsonl``); ``tickets.v1.json`` is a rebuildable cache
of the folded state. Validated process-local projections avoid repeated folds.
Leases are folded from assignment, claim and heartbeat events. Legacy
``leases/<id>.json`` sidecars remain readable until an event supersedes them.
Completion and handoff clear the lease in the same durable lifecycle event.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import json
import os
import hashlib
from uuid import uuid4
from contextlib import contextmanager
from contextvars import ContextVar
from .errors import BoardError, RevisionConflict, TicketConflict, CommitUncertain
from typing import Any, Mapping, Sequence

from . import runtime, identity


TICKET_SCHEMA_VERSION = 1
ACTOR_REGISTRY_SCHEMA_VERSION = 1
ACTORS_FILE = "actors.v1.json"
TICKETS_FILE = "tickets.v1.json"

DISPLAY_IDS_FILE = "ticket-display-ids.v1.json"


def _display_prefix(root: Path) -> str:
    # The store sits in the common git directory, even for linked worktrees.
    common = root.resolve().parent
    name = common.parent.name if common.name == ".git" else root.name
    return re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-") or "PROJECT"


def _read_display_ids(root: Path) -> dict[str, Any]:
    path = root / DISPLAY_IDS_FILE
    signature = _signature(path)
    cached = _DISPLAY_CACHE.get(path)
    if cached and cached[0] == signature:
        return json.loads(cached[1])
    try:
        registry = json.loads((root / DISPLAY_IDS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "schema_version": 1,
            "prefix": _display_prefix(root),
            "next_number": 1,
            "ids": {},
        }
    except (ValueError, OSError) as exc:
        raise BoardError(f"unreadable ticket display IDs: {exc}") from exc
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise BoardError("invalid ticket display ID registry")
    prefix, counter, ids = (
        registry.get("prefix"),
        registry.get("next_number"),
        registry.get("ids"),
    )
    if not isinstance(prefix, str) or not re.fullmatch(
        r"[A-Z0-9]+(?:-[A-Z0-9]+)*", prefix
    ):
        raise BoardError("invalid ticket display ID prefix")
    if type(counter) is not int or counter < 1 or not isinstance(ids, dict):
        raise BoardError("invalid ticket display ID allocation")
    numbers = []
    for ticket_id, display_id in ids.items():
        runtime.require_safe_token("ticket id", ticket_id)
        match = (
            re.fullmatch(re.escape(prefix) + r"-([1-9][0-9]*)", display_id)
            if isinstance(display_id, str)
            else None
        )
        if not match or int(match[1]) < 1 or display_id != f"{prefix}-{int(match[1])}":
            raise BoardError("invalid ticket display ID")
        numbers.append(int(match[1]))
    if len(set(numbers)) != len(numbers) or (numbers and counter <= max(numbers)):
        raise BoardError("duplicate or reused ticket display ID")
    if signature is not None:
        _DISPLAY_CACHE[path] = (signature, json.dumps(registry, ensure_ascii=False))
    return registry


def _allocate_display_id(registry: dict[str, Any], ticket_id: str) -> str:
    if ticket_id not in registry["ids"]:
        registry["ids"][ticket_id] = f"{registry['prefix']}-{registry['next_number']}"
        registry["next_number"] += 1
    return registry["ids"][ticket_id]


def migrate_ticket_display_ids(
    root: Path, *, prefix: str | None = None
) -> dict[str, str]:
    """Explicit, idempotent backfill; preserves event bytes, revisions and raw IDs.

    This sidecar is authoritative identity metadata, not a rebuildable cache.
    Keep it when archiving/deleting tickets or restoring the event store.
    """
    with _ticket_lock(root):
        registry = _read_display_ids(root)
        if prefix is not None:
            if not re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", prefix):
                raise BoardError("invalid ticket display ID prefix")
            if (root / DISPLAY_IDS_FILE).exists() and registry["prefix"] != prefix:
                raise BoardError("ticket display ID prefix is already frozen")
            registry["prefix"] = prefix
        # Order only determines the initial allocation; existing mappings never move.
        existing = []
        for path in _events_dir(root).glob("*.jsonl"):
            events = _read_ticket_events(root, path.stem)
            if events and events[0].get("type") == EV_CREATE:
                existing.append((events[0].get("ts", ""), path.stem))
        for _, ticket_id in sorted(existing):
            _allocate_display_id(registry, ticket_id)
        runtime.write_atomic_replace(
            root / DISPLAY_IDS_FILE, runtime.canonical_json(registry)
        )
        return dict(registry["ids"])


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

_ACTOR_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_.-]{0,63}(?:/[A-Za-z][A-Za-z0-9_.-]{0,63})?$"
)

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
EV_CLAIM = "ticket.claimed"
EV_HANDOFF = "ticket.handoff"
EV_DEP_REMOVE = "ticket.dep_remove"

# Only validated immutable prefixes are memoized, invalidated by file identity/change.
_EVENT_CACHE: dict[
    Path, tuple[tuple[int, int, int, int] | None, list[dict[str, Any]]]
] = {}
# Serialized snapshots give each caller an isolated copy without Python deepcopy overhead.
_FOLD_CACHE: dict[Path, tuple[str, str]] = {}
_DISPLAY_CACHE: dict[Path, tuple[tuple[int, int, int, int], str]] = {}
_RECOVERING = ContextVar("ticket_recovering", default=False)


def _signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------- validation


def _require_actor_name(label: str, value: str) -> str:
    if not isinstance(value, str) or not _ACTOR_RE.fullmatch(value):
        raise BoardError(
            f"invalid {label} {value!r}: letters/digits/._- and an optional master/sub form"
        )
    return value


def master_of(actor: str) -> str:
    """``gpt-master/worker`` -> ``gpt-master``; a bare name is its own master."""

    _require_actor_name("actor", actor)
    return actor.split("/", 1)[0] if "/" in actor else actor


def is_subagent(actor: str) -> bool:
    return "/" in _require_actor_name("actor", actor)


def _require_kind(value: str) -> str:
    kind = (value or "").strip().upper()
    if kind not in TICKET_KINDS:
        raise BoardError(
            f"invalid ticket kind {value!r}; expected one of {TICKET_KINDS}"
        )
    return kind


def _require_stage(value: str) -> str:
    stage = (value or "").strip().upper()
    if stage not in STAGES:
        raise BoardError(f"invalid ticket stage {value!r}; expected one of {STAGES}")
    return stage


def _require_dep_type(value: str) -> str:
    dep = (value or "").strip().upper()
    if dep not in DEP_TYPES:
        raise BoardError(
            f"invalid dependency type {value!r}; expected one of {DEP_TYPES}"
        )
    return dep


def _require_verdict(value: str) -> str:
    verdict = (value or "").strip().upper()
    if verdict not in REVIEW_VERDICTS:
        raise BoardError(
            f"invalid review verdict {value!r}; expected one of {REVIEW_VERDICTS}"
        )
    return verdict


def _require_title(value: str) -> str:
    title = " ".join((value or "").strip().split())
    if not title:
        raise BoardError("ticket title must not be empty")
    if len(title) > MAX_TITLE_CHARS:
        raise BoardError(f"ticket title exceeds {MAX_TITLE_CHARS} characters")
    return title


def _require_optional_summary(value: str) -> str:
    summary = " ".join((value or "").strip().split())
    if len(summary) > MAX_SUMMARY_CHARS:
        raise BoardError(f"ticket summary exceeds {MAX_SUMMARY_CHARS} characters")
    return summary


def _require_body(value: str) -> str:
    body = value or ""
    if len(body) > MAX_BODY_CHARS:
        raise BoardError(f"ticket body exceeds {MAX_BODY_CHARS} characters")
    return body


def _require_criteria(values: Sequence[str] | None) -> list[str]:
    criteria = list(values or [])
    if len(criteria) > MAX_CRITERIA:
        raise BoardError(f"acceptance criteria exceeds {MAX_CRITERIA} entries")
    out = []
    for entry in criteria:
        text = " ".join((entry or "").strip().split())
        if not text:
            continue
        if len(text) > MAX_CRITERION_CHARS:
            raise BoardError(
                f"acceptance criterion exceeds {MAX_CRITERION_CHARS} characters"
            )
        out.append(text)
    return out


def _require_subagents(values: Sequence[str] | None) -> list[str]:
    subagents = list(values or [])
    if len(subagents) > MAX_SUBAGENTS:
        raise BoardError(f"subagent list exceeds {MAX_SUBAGENTS} entries")
    return [_require_actor_name("subagent", name) for name in subagents]


def _utc_now() -> str:
    return runtime.utc_now()


def _ts_to_epoch(value: str) -> float:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(raw).timestamp()


def _offset_ts(ts: str, seconds: int) -> str:
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    moved = datetime.fromisoformat(raw) + timedelta(seconds=seconds)
    return (
        moved.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


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


@contextmanager
def _ticket_lock(root: Path, timeout_seconds: float = 5.0, *, recover: bool = True):
    ensure_ticket_directories(root)
    with runtime.file_lock(root / ".tickets.lock", timeout_seconds):
        if recover:
            _recover_dependency_transactions(root)
        yield


def _recover_dependency_transactions(root: Path) -> None:
    """Replay a committed dependency batch exactly once while holding the project lock."""
    token = _RECOVERING.set(True)
    try:
        for path in sorted((root / "ticket-transactions").glob("*.json")):
            transaction = json.loads(path.read_text(encoding="utf-8"))
            for item in transaction["events"]:
                events = _read_ticket_events(root, item["ticket_id"])
                if any(
                    event.get("transaction_id") == transaction["id"] for event in events
                ):
                    continue
                _append_ticket_event(
                    root,
                    item["ticket_id"],
                    {**item["event"], "transaction_id": transaction["id"]},
                )
            path.unlink()
    finally:
        _RECOVERING.reset(token)


def _commit_dependency_events(
    root: Path, events: list[dict[str, Any]], state: Mapping[str, Any]
) -> dict[str, Any]:
    transaction_id = uuid4().hex
    path = root / "ticket-transactions" / (transaction_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_atomic_replace(
        path, runtime.canonical_json({"id": transaction_id, "events": events})
    )
    # The intent file is the commit point. All future writers replay it before proceeding.
    try:
        _recover_dependency_transactions(root)
    except (OSError, CommitUncertain) as exc:
        return {
            **state,
            "persistence": {
                "committed": True,
                "projection_updated": False,
                "recovery_required": True,
                "transaction_id": transaction_id,
                "error": str(exc),
            },
        }
    return _persist_ticket(root, state)


# ------------------------------------------------------------------ hash-chain log


def _append_ticket_event(
    root: Path, ticket_id: str, event: Mapping[str, Any]
) -> dict[str, Any]:
    """Append one entry to a ticket's hash-chained log. Caller must hold ``_ticket_lock``."""

    path = _ticket_events_path(root, ticket_id)
    existing = _read_ticket_events(root, ticket_id)
    prev_hash = existing[-1]["hash"] if existing else "0" * 64
    seq = (existing[-1]["seq"] + 1) if existing else 1
    body = {
        key: value
        for key, value in event.items()
        if key not in ("hash", "prev", "seq", "ts")
    }
    body["seq"] = seq
    body["prev"] = prev_hash
    body["ts"] = event.get("ts") or _utc_now()
    body["hash"] = hashlib.sha256(
        runtime.canonical_json({key: body[key] for key in sorted(body)}).encode("utf-8")
    ).hexdigest()
    _validate_event(body, seq, prev_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = ""
    if path.exists() and path.stat().st_size:
        with path.open("rb") as existing_file:
            existing_file.seek(-1, os.SEEK_END)
            if existing_file.read(1) != b"\n":
                separator = "\n"
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                separator + json.dumps(body, sort_keys=True, ensure_ascii=False) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _EVENT_CACHE.pop(path, None)
        _FOLD_CACHE.pop(path, None)
        raise CommitUncertain(ticket_id, seq) from exc
    folded = _FOLD_CACHE.get(path)
    if folded and folded[0] == prev_hash:
        state = json.loads(folded[1])
        _apply_event(state, body)
        _FOLD_CACHE[path] = (body["hash"], json.dumps(state, ensure_ascii=False))
    _EVENT_CACHE[path] = (_signature(path), existing + [body])
    return body


def _validate_event(event: Any, seq: int, prev: str) -> None:
    if not isinstance(event, dict):
        raise BoardError("event must be an object")
    if type(event.get("seq")) is not int or event["seq"] != seq:
        raise BoardError(f"non-contiguous sequence: expected {seq}")
    kind = event.get("type")
    required = {
        EV_CREATE: ("title", "kind", "acceptance_criteria"),
        EV_UPSERT: (),
        EV_ASSIGN: ("assignee",),
        EV_STAGE: ("stage",),
        EV_COMMENT: ("summary",),
        EV_WORKLOG: ("evidence",),
        EV_REVIEW: ("verdict", "findings"),
        EV_DEP_ADD: ("dep_type", "target"),
        EV_DEP_REMOVE: ("dep_type", "target"),
        EV_HEARTBEAT: (),
        EV_DONE: (),
        EV_ARCHIVE: ("content_hash",),
        EV_CLAIM: ("assignee", "lease"),
        EV_HANDOFF: ("summary", "evidence", "next_actor", "stage"),
    }
    if kind not in required or any(key not in event for key in required[kind]):
        raise BoardError("unknown event type or missing required fields")
    if (seq == 1) != (kind == EV_CREATE):
        raise BoardError("first event must be CREATE; CREATE cannot repeat")
    _require_actor_name("event actor", event.get("actor"))
    if not isinstance(event.get("ts"), str):
        raise BoardError("event timestamp is required")
    try:
        _ts_to_epoch(event["ts"])
    except (TypeError, ValueError):
        raise BoardError("invalid event timestamp")
    for field in ("title", "summary", "body", "content_hash"):
        if field in event and not isinstance(event[field], str):
            raise BoardError(f"invalid {field}")
    for field in ("assignee", "reviewer", "next_actor"):
        if event.get(field) is not None:
            _require_actor_name(field, event[field])
    if "acceptance_criteria" in event:
        if not isinstance(event["acceptance_criteria"], list) or any(
            not isinstance(item, str) for item in event["acceptance_criteria"]
        ):
            raise BoardError("invalid acceptance criteria")
        _require_criteria(event["acceptance_criteria"])
    if "subagents" in event:
        if not isinstance(event["subagents"], list):
            raise BoardError("invalid subagents")
        _require_subagents(event["subagents"])
    if "evidence" in event:
        _validate_evidence(event["evidence"])
    if "lease" in event and event["lease"] is not None:
        lease = event["lease"]
        if not isinstance(lease, dict):
            raise BoardError("invalid lease")
        _require_actor_name("lease assignee", lease.get("assignee"))
        if type(lease.get("ttl_sec")) is not int or lease["ttl_sec"] <= 0:
            raise BoardError("invalid lease ttl")
        for field in ("granted_at", "heartbeat_at", "expires_at"):
            try:
                _ts_to_epoch(lease[field])
            except (KeyError, TypeError, ValueError, AttributeError):
                raise BoardError("invalid lease timestamp")
        if lease.get("fenced") and (
            not isinstance(lease.get("token"), str) or not lease["token"]
        ):
            raise BoardError("missing lease token")
    if kind in (EV_STAGE, EV_HANDOFF):
        _require_stage(event["stage"])
    if kind == EV_REVIEW:
        _require_verdict(event["verdict"])
        if not isinstance(event["findings"], list) or any(
            not isinstance(item, str) for item in event["findings"]
        ):
            raise BoardError("invalid findings")
    if kind in (EV_DEP_ADD, EV_DEP_REMOVE):
        _require_dep_type(event["dep_type"])
        runtime.require_safe_token("dependency target", event["target"])
    if kind == EV_CREATE:
        _require_title(event["title"])
        _require_kind(event["kind"])
        if not isinstance(event["acceptance_criteria"], list):
            raise BoardError("invalid criteria")
    if event.get("prev") != prev:
        raise BoardError(f"broken prev link at seq={seq}")
    body = {key: value for key, value in event.items() if key != "hash"}
    expected = hashlib.sha256(runtime.canonical_json(body).encode("utf-8")).hexdigest()
    if event.get("hash") != expected:
        raise BoardError(f"hash mismatch at seq={seq}")


def _read_ticket_events(root: Path, ticket_id: str) -> list[dict[str, Any]]:
    runtime.require_safe_token("ticket id", ticket_id)
    if not _RECOVERING.get() and any((root / "ticket-transactions").glob("*.json")):
        raise TicketConflict(
            "committed dependency transaction needs recovery; rebuild ticket cache or retry mutation"
        )
    path = _ticket_events_path(root, ticket_id)
    signature = _signature(path)
    if signature is None:
        return []
    cached = _EVENT_CACHE.get(path)
    if cached and cached[0] == signature:
        return cached[1]
    events = []
    prev = "0" * 64
    for number, line in enumerate(path.read_bytes().splitlines(), 1):
        try:
            event = json.loads(line)
            _validate_event(event, number, prev)
        except (ValueError, TypeError, AttributeError, KeyError, BoardError) as exc:
            raise BoardError(
                f"invalid ticket {ticket_id} line {number}: {exc}"
            ) from exc
        events.append(event)
        prev = event["hash"]
    _EVENT_CACHE[path] = (signature, events)
    return events


def verify_ticket_chain(root: Path, ticket_id: str) -> tuple[bool, str]:
    """Verify schema, contiguous sequence and hashes; hashes do not prove authorship."""
    try:
        _EVENT_CACHE.pop(_ticket_events_path(root, ticket_id), None)
        events = _read_ticket_events(root, ticket_id)
        return (True, "ok") if events else (False, "ticket has no CREATE event")
    except (BoardError, OSError) as exc:
        return False, str(exc)


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

    if kind in (EV_CREATE, EV_HANDOFF, EV_WORKLOG) or (
        kind in (EV_UPSERT, EV_ASSIGN)
        and any(
            key in event and event[key] != state.get(key)
            for key in (
                "title",
                "summary",
                "body",
                "kind",
                "acceptance_criteria",
                "reviewer",
            )
        )
    ):
        state["acceptance_revision"] = event["seq"]
    if "lease" in event:
        state["event_lease"] = event["lease"]
    if kind in (EV_DONE, EV_HANDOFF) or (
        kind == EV_STAGE and event.get("stage") in TERMINAL_STAGES
    ):
        state["event_lease"] = None
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
        state["reviewer"] = event.get("reviewer") or (
            master_of(actor) if actor else None
        )
        state["subagents"] = list(event.get("subagents") or [])
        if event.get("assignee"):
            state["assignee"] = event["assignee"]
    elif kind == EV_UPSERT:
        for key in (
            "title",
            "summary",
            "body",
            "kind",
            "acceptance_criteria",
            "reviewer",
            "subagents",
        ):
            if key in event and event[key] is not None:
                state[key] = event[key]
    elif kind in (EV_ASSIGN, EV_CLAIM):
        state["assignee"] = event.get("assignee")
        if event.get("reviewer"):
            state["reviewer"] = event["reviewer"]
        if event.get("subagents") is not None:
            state["subagents"] = list(event["subagents"])
    elif kind == EV_STAGE:
        _close_stage_interval(state, ts)
        state["stage"] = event.get("stage") or state["stage"]
        state["stage_entered_at"] = ts
        state["stage_history"].append(
            {"stage": state["stage"], "entered_at": ts, "actor": actor}
        )
    elif kind == EV_COMMENT:
        state["comments"].append(
            {
                "ts": ts,
                "actor": actor,
                "summary": event.get("summary") or "",
                "body": event.get("body") or "",
                "seq": event.get("seq"),
            }
        )
    elif kind == EV_WORKLOG:
        state["worklog"].append(
            {
                "ts": ts,
                "actor": actor,
                "evidence": event.get("evidence") or {},
                "summary": event.get("summary") or "",
                "seq": event.get("seq"),
            }
        )
    elif kind == EV_REVIEW:
        state["reviews"].append(
            {
                "ts": ts,
                "actor": actor,
                "verdict": event.get("verdict"),
                "acceptance_revision": event.get(
                    "acceptance_revision", state.get("acceptance_revision", 1)
                ),
                "findings": list(event.get("findings") or []),
                "summary": event.get("summary") or "",
                "seq": event.get("seq"),
            }
        )
        if event.get("return_to_development"):
            _close_stage_interval(state, ts)
            state["stage"] = "DEVELOPMENT"
            state["stage_entered_at"] = ts
            state["stage_history"].append(
                {"stage": "DEVELOPMENT", "entered_at": ts, "actor": actor}
            )
    elif kind == EV_HANDOFF:
        _close_stage_interval(state, ts)
        state["stage"] = event["stage"]
        state["stage_entered_at"] = ts
        state["stage_history"].append(
            {"stage": event["stage"], "entered_at": ts, "actor": actor}
        )
        state["assignee"] = event["next_actor"]
        state["latest_delivery"] = {
            key: event.get(key)
            for key in (
                "summary",
                "body",
                "evidence",
                "next_actor",
                "stage",
                "actor",
                "seq",
                "ts",
            )
        }
        state["comments"].append(
            {
                "summary": event["summary"],
                "body": event.get("body", ""),
                "actor": actor,
                "seq": event["seq"],
                "ts": ts,
            }
        )
        state["worklog"].append(
            {
                "summary": event["summary"],
                "evidence": event["evidence"],
                "actor": actor,
                "seq": event["seq"],
                "ts": ts,
            }
        )
    elif kind == EV_DEP_REMOVE:
        state["deps"] = [
            dep
            for dep in state["deps"]
            if (dep["type"], dep["target"]) != (event["dep_type"], event["target"])
        ]
    elif kind == EV_DEP_ADD:
        dep = {
            "type": event.get("dep_type"),
            "target": event.get("target"),
            "ts": ts,
            "actor": actor,
        }
        state["deps"] = [
            entry
            for entry in state["deps"]
            if not (
                entry.get("type") == dep["type"]
                and entry.get("target") == dep["target"]
            )
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

    events = _read_ticket_events(root, ticket_id)
    if not events:
        return _empty_ticket_state(ticket_id)
    path = _ticket_events_path(root, ticket_id)
    cached = _FOLD_CACHE.get(path)
    if cached and cached[0] == events[-1]["hash"]:
        state = json.loads(cached[1])
    else:
        state = _empty_ticket_state(ticket_id)
        for event in events:
            _apply_event(state, event)
        _FOLD_CACHE[path] = (events[-1]["hash"], json.dumps(state, ensure_ascii=False))

    state["display_id"] = _read_display_ids(root)["ids"].get(ticket_id)

    now = _utc_now()
    now_epoch = _ts_to_epoch(now)
    time_in = dict(state.get("time_in_stage") or {})
    if state.get("stage_entered_at") and state.get("stage") not in TERMINAL_STAGES:
        entered = _ts_to_epoch(state["stage_entered_at"])
        current = state["stage"]
        time_in[current] = time_in.get(current, 0.0) + (now_epoch - entered)
    state["time_in_stage"] = time_in
    state["time_in_stage_human"] = {
        key: format_duration(value) for key, value in time_in.items()
    }

    last = (
        state.get("last_activity_at")
        or state.get("updated_at")
        or state.get("created_at")
    )
    idle_seconds = (now_epoch - _ts_to_epoch(last)) if last else 0.0
    state["idle_seconds"] = idle_seconds
    state["stale"] = bool(
        state.get("stage") in ACTIVE_STAGES and idle_seconds >= DEFAULT_STALE_SEC
    )

    lease = (
        state.get("event_lease")
        if "event_lease" in state
        else _read_lease(root, ticket_id)
    )
    if lease:
        state["lease"] = lease
        expires = lease.get("expires_at")
        state["lease_stale"] = bool(expires and _ts_to_epoch(expires) <= now_epoch)
    else:
        state["lease"] = None
        state["lease_stale"] = False
    return state


def _read_tickets_cache(root: Path) -> dict[str, Any]:
    # Enumerating authoritative logs prevents missing/stale cache entries hiding work.
    return {
        "schema_version": TICKET_SCHEMA_VERSION,
        "tickets": [
            derive_ticket(root, path.stem)
            for path in sorted(_events_dir(root).glob("*.jsonl"))
        ],
    }


def _persist_ticket(root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    """The fsynced event is committed. Projection failure is a recoverable result."""
    fresh = derive_ticket(root, state["id"])
    try:
        runtime.write_atomic_replace(
            _tickets_path(root), runtime.canonical_json(_read_tickets_cache(root))
        )
    except (OSError, BoardError) as exc:
        fresh["persistence"] = {
            "committed": True,
            "projection_updated": False,
            "error": str(exc),
        }
    return fresh


def rebuild_ticket_cache(root: Path) -> dict[str, Any]:
    with _ticket_lock(root):
        cache = _read_tickets_cache(root)
        runtime.write_atomic_replace(_tickets_path(root), runtime.canonical_json(cache))
        return {"tickets": cache["tickets"], "count": len(cache["tickets"])}


def verify_ticket_cache(root: Path) -> dict[str, Any]:
    actual = _read_tickets_cache(root)["tickets"]
    dynamic = {
        "idle_seconds",
        "stale",
        "lease_stale",
        "time_in_stage",
        "time_in_stage_human",
    }

    def stable(ticket: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in ticket.items() if key not in dynamic}

    try:
        cache = json.loads(_tickets_path(root).read_text(encoding="utf-8"))
        valid = (
            cache.get("schema_version") == TICKET_SCHEMA_VERSION
            and isinstance(cache.get("tickets"), list)
            and [stable(t) for t in cache["tickets"]] == [stable(t) for t in actual]
        )
        return {
            "valid": valid,
            "count": len(actual),
            "reason": "ok" if valid else "stale or corrupt cache",
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"valid": False, "count": len(actual), "reason": str(exc)}


def _require_existing(root: Path, ticket_id: str) -> dict[str, Any]:
    state = derive_ticket(root, ticket_id)
    if not state.get("created_at"):
        raise BoardError(f"unknown ticket: {ticket_id}")
    return state


def _require_revision(state: Mapping[str, Any], expected: int | None) -> None:
    if expected is None:
        return
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise BoardError("expected revision must be a non-negative integer")
    actual = int(state.get("revision") or 0)
    if expected != actual:
        raise RevisionConflict(
            f"ticket revision conflict for {state['id']}: expected {expected}, current {actual}"
        )


# ------------------------------------------------------------------------- leases


def _read_lease(root: Path, ticket_id: str) -> dict[str, Any] | None:
    path = _lease_path(root, ticket_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_lease(root: Path, ticket_id: str, lease: Mapping[str, Any]) -> None:
    runtime.write_atomic_replace(
        _lease_path(root, ticket_id), runtime.canonical_json(lease)
    )


def _delete_lease(root: Path, ticket_id: str) -> None:
    path = _lease_path(root, ticket_id)
    if path.is_file():
        path.unlink()


def list_leases(root: Path) -> list[dict[str, Any]]:
    return [
        {**ticket["lease"], "stale": ticket["lease_stale"]}
        for ticket in list_tickets(root)
        if ticket.get("lease")
    ]


# ------------------------------------------------------------------- actor registry


def _read_actors(root: Path) -> dict[str, Any]:
    path = _actors_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": ACTOR_REGISTRY_SCHEMA_VERSION, "actors": {}}
    if not isinstance(value, dict) or set(value) != {"schema_version", "actors"}:
        raise BoardError("invalid actor registry shape")
    return value


def _write_actors(root: Path, data: Mapping[str, Any]) -> None:
    runtime.write_atomic_replace(_actors_path(root), runtime.canonical_json(data))


def register_actor(
    root: Path, name: str, *, role: str, display: str = "", master: str | None = None
) -> dict[str, Any]:
    name = _require_actor_name("actor", name)
    role = (role or "").strip().upper()
    if role not in ACTOR_ROLES:
        raise BoardError(f"invalid actor role {role!r}; expected one of {ACTOR_ROLES}")
    if role == "SUBAGENT" and not is_subagent(name) and not master:
        raise BoardError("a subagent must be named <master>/<name>, or pass master=")
    if role == "SUBAGENT" and master:
        master = _require_actor_name("master", master)
    if role == "MASTER" and is_subagent(name):
        raise BoardError("a master identity cannot use the master/sub form")
    with _ticket_lock(root):
        data = _read_actors(root)
        actors = data.setdefault("actors", {})
        entry = {
            "name": name,
            "role": role,
            "display": display or name,
            "master": master if role == "SUBAGENT" else None,
        }
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
    registered = _read_actors(root).get("actors") or {}
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
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    kind = _require_kind(kind)
    title = _require_title(title)
    summary = _require_optional_summary(summary)
    body = _require_body(body)
    if parent_id is not None:
        parent_id = runtime.require_safe_token("parent ticket id", parent_id)
    criteria = _require_criteria(acceptance_criteria) or list(
        TEMPLATES[kind]["acceptance_criteria"]
    )
    if assignee is not None:
        assignee = _require_actor_name("assignee", assignee)
    if reviewer is not None:
        reviewer = _require_actor_name("reviewer", reviewer)
    else:
        reviewer = resolve_reviewer(root, assignee or actor)
    subs = _require_subagents(subagents)

    with _ticket_lock(root):
        retry, fingerprint = _retry_event(
            root,
            ticket_id,
            actor,
            idempotency_key,
            {
                "type": EV_CREATE,
                "lease": None,
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
        if retry is not None:
            return retry
        if _read_ticket_events(root, ticket_id):
            raise BoardError(f"ticket already exists: {ticket_id}")
        if parent_id is not None and not _require_existing(root, parent_id):
            raise BoardError(f"unknown parent ticket: {parent_id}")
        registry = _read_display_ids(root)
        _allocate_display_id(registry, ticket_id)
        # Reserve before appending: a crash may leave a gap but never reuse an ID.
        runtime.write_atomic_replace(
            root / DISPLAY_IDS_FILE, runtime.canonical_json(registry)
        )
        _append_ticket_event(
            root,
            ticket_id,
            {
                "type": EV_CREATE,
                "lease": None,
                "idempotency_key": idempotency_key,
                "request_hash": fingerprint,
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


def upsert_ticket(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    expected_revision: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
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
        if (
            "acceptance_criteria" in fields
            and fields["acceptance_criteria"] is not None
        ):
            payload["acceptance_criteria"] = _require_criteria(
                fields["acceptance_criteria"]
            )
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

    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    assignee = _require_actor_name("assignee", assignee)
    if not isinstance(ttl_sec, int) or isinstance(ttl_sec, bool) or ttl_sec <= 0:
        raise BoardError("lease ttl_sec must be a positive integer")
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        resolved_reviewer = (
            _require_actor_name("reviewer", reviewer)
            if reviewer
            else resolve_reviewer(root, assignee)
        )
        subs = (
            _require_subagents(subagents)
            if subagents is not None
            else list(state.get("subagents") or [])
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
            "token": uuid4().hex,
            "fenced": bool(state.get("lease")),
        }
        _append_ticket_event(
            root,
            ticket_id,
            {
                "type": EV_ASSIGN,
                "actor": actor,
                "assignee": assignee,
                "reviewer": resolved_reviewer,
                "subagents": subs,
                "lease": lease,
            },
        )
        return _persist_ticket(root, state)


def heartbeat_ticket(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    ttl_sec: int | None = None,
    lease_token: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    actor = _require_actor_name("actor", actor)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        lease = state.get("lease")
        if not lease:
            raise TicketConflict(f"no lease held on {ticket_id}")
        if lease.get("assignee") != actor:
            raise TicketConflict(
                f"lease on {ticket_id} is held by {lease.get('assignee')}, not {actor}"
            )
        if lease.get("fenced") and lease_token != lease.get("token"):
            raise TicketConflict(
                "lease token mismatch; claim again after explicit recovery"
            )
        if state.get("lease_stale"):
            raise TicketConflict("lease expired; explicit recovery required")
        ttl = (
            ttl_sec
            if ttl_sec is not None
            else lease.get("ttl_sec", DEFAULT_LEASE_TTL_SEC)
        )
        if type(ttl) is not int or ttl <= 0:
            raise BoardError("lease ttl_sec must be a positive integer")
        now = _utc_now()
        lease = {
            **lease,
            "heartbeat_at": now,
            "expires_at": _offset_ts(now, ttl),
            "ttl_sec": ttl,
        }
        _append_ticket_event(
            root, ticket_id, {"type": EV_HEARTBEAT, "actor": actor, "lease": lease}
        )
        return _persist_ticket(root, state)


def transition_ticket(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    stage: str,
    expected_revision: int | None = None,
    summary: str = "",
) -> dict[str, Any]:
    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    stage = _require_stage(stage)
    if stage == "DONE":
        raise BoardError(
            "use the done action to finish a ticket; it requires an owning-master review"
        )
    summary = _require_optional_summary(summary)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        if stage in ACTIVE_STAGES - {"BLOCKED"}:
            blockers = _open_blockers(root, state)
            if blockers:
                raise BoardError(
                    f"ticket {ticket_id} is blocked by: {', '.join(blockers)}"
                )
        _append_ticket_event(
            root,
            ticket_id,
            {"type": EV_STAGE, "actor": actor, "stage": stage, "summary": summary},
        )
        return _persist_ticket(root, state)


def comment_ticket(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    summary: str,
    body: str = "",
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    actor = _require_actor_name("actor", actor)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    summary = _require_title(summary)
    body = _require_body(body)
    with _ticket_lock(root):
        payload = {"type": EV_COMMENT, "actor": actor, "summary": summary, "body": body}
        retry, fingerprint = _retry_event(
            root, ticket_id, actor, idempotency_key, payload
        )
        if retry is not None:
            return retry
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        _append_ticket_event(
            root,
            ticket_id,
            {
                **payload,
                "idempotency_key": idempotency_key,
                "request_hash": fingerprint,
            },
        )
        return _persist_ticket(root, state)


def add_worklog(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    summary: str,
    evidence: Mapping[str, Any],
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Evidence pointers only: repo+sha, test command+exit code, or artifact path+content hash."""

    actor = _require_actor_name("actor", actor)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    summary = _require_title(summary)
    evidence = _validate_evidence(evidence)
    with _ticket_lock(root):
        payload = {
            "type": EV_WORKLOG,
            "actor": actor,
            "summary": summary,
            "evidence": evidence,
        }
        retry, fingerprint = _retry_event(
            root, ticket_id, actor, idempotency_key, payload
        )
        if retry is not None:
            return retry
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        _append_ticket_event(
            root,
            ticket_id,
            {
                **payload,
                "idempotency_key": idempotency_key,
                "request_hash": fingerprint,
            },
        )
        return _persist_ticket(root, state)


def _validate_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence, dict) or not evidence:
        raise BoardError(
            "worklog evidence is required: repo/sha, test/exit_code, or artifact/content_hash"
        )
    out: dict[str, Any] = {}
    if "repo" in evidence or "sha" in evidence:
        sha = evidence.get("sha") or ""
        if not sha:
            raise BoardError("evidence.sha is required when recording a commit pointer")
        out["repo"] = str(evidence.get("repo") or "")
        out["sha"] = str(sha)
    if "test" in evidence or "exit_code" in evidence:
        if "exit_code" not in evidence:
            raise BoardError("evidence.exit_code is required with a test command")
        out["test"] = str(evidence.get("test") or "")
        out["exit_code"] = int(evidence["exit_code"])
    if "artifact" in evidence or "content_hash" in evidence:
        content_hash = evidence.get("content_hash") or ""
        if not content_hash:
            raise BoardError("evidence.content_hash is required with an artifact path")
        out["artifact"] = str(evidence.get("artifact") or "")
        out["content_hash"] = str(content_hash)
    if not out:
        raise BoardError(
            "worklog evidence must include a commit, test, or artifact pointer"
        )
    return out


def review_ticket(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    verdict: str,
    summary: str,
    findings: Sequence[str] | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    actor = _require_actor_name("actor", actor)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    verdict = _require_verdict(verdict)
    summary = _require_title(summary)
    findings = list(findings or [])
    if len(findings) > MAX_FINDINGS:
        raise BoardError(f"review findings exceed {MAX_FINDINGS} entries")
    if verdict in ("CONFIRMED_WITH_FIXES", "FAIL") and not findings:
        raise BoardError(f"{verdict} requires numbered findings")
    numbered = []
    for index, finding in enumerate(findings, 1):
        text = " ".join((finding or "").strip().split())
        if not text:
            continue
        if len(text) > MAX_CRITERION_CHARS:
            raise BoardError(f"review finding exceeds {MAX_CRITERION_CHARS} characters")
        numbered.append(text if text[0].isdigit() else f"{index}. {text}")

    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        owner = state.get("reviewer") or master_of(state.get("assignee") or actor)
        owner_root = resolve_reviewer(root, owner)
        if actor != owner_root or is_subagent(actor):
            raise BoardError(
                f"review of {ticket_id} is reserved for its owning master {owner} (actor={actor})"
            )
        _append_ticket_event(
            root,
            ticket_id,
            {
                "type": EV_REVIEW,
                "actor": actor,
                "verdict": verdict,
                "findings": numbered,
                "summary": summary,
                "acceptance_revision": state.get("acceptance_revision", 1),
                "return_to_development": verdict == "FAIL",
            },
        )
        return _persist_ticket(root, state)


def mark_ticket_done(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    summary: str = "",
    expected_revision: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    summary = _require_optional_summary(summary) or "done"
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        reviews = state.get("reviews") or []
        reviewed = bool(
            reviews
            and reviews[-1].get("verdict") in ("PASS", "CONFIRMED_WITH_FIXES")
            and reviews[-1].get("acceptance_revision")
            == state.get("acceptance_revision", 1)
        )
        if not reviewed and not force:
            raise BoardError(
                f"DONE requires an owning-master review of PASS or CONFIRMED_WITH_FIXES on {ticket_id}"
            )
        blockers = _open_blockers(root, state)
        if blockers and not force:
            raise BoardError(
                f"cannot mark {ticket_id} DONE while blocked by: {', '.join(blockers)}"
            )
        _append_ticket_event(
            root, ticket_id, {"type": EV_DONE, "actor": actor, "summary": summary}
        )
        result = _persist_ticket(root, state)
        try:
            _notify_unblocked(root, ticket_id, actor)
        except (OSError, BoardError) as exc:
            result["notification_warning"] = {"committed": True, "error": str(exc)}
        return result


def add_dependency(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    dep_type: str,
    target: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    dep_type = _require_dep_type(dep_type)
    target = runtime.require_safe_token("dependency target ticket id", target)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        if dep_type == "BLOCKED_BY":
            if target == ticket_id or _would_cycle(root, ticket_id, target):
                raise BoardError(
                    f"dependency cycle detected: {ticket_id} BLOCKED_BY {target}"
                )
            _require_existing(root, target)
        events = [
            {
                "ticket_id": ticket_id,
                "event": {
                    "type": EV_DEP_ADD,
                    "actor": actor,
                    "dep_type": dep_type,
                    "target": target,
                },
            }
        ]
        if dep_type == "BLOCKED_BY":
            events.append(
                {
                    "ticket_id": target,
                    "event": {
                        "type": EV_DEP_ADD,
                        "actor": actor,
                        "dep_type": "UNBLOCKS",
                        "target": ticket_id,
                    },
                }
            )
        return _commit_dependency_events(root, events, state)


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
                _append_ticket_event(
                    root,
                    ticket_id,
                    {
                        "type": EV_COMMENT,
                        "actor": actor,
                        "summary": f"blocker {closed_id} closed",
                        "body": "",
                    },
                )
                _persist_ticket(root, {"id": ticket_id})


# --------------------------------------------------------------------------- views


def list_tickets(
    root: Path,
    *,
    stage: str | None = None,
    assignee: str | None = None,
    parent_id: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
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
        out.append(cached)
    return out


def get_ticket(root: Path, ticket_id: str) -> dict[str, Any]:
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
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

    tickets = {
        ticket["id"]: ticket
        for ticket in list_tickets(root)
        if ticket.get("stage") not in TERMINAL_STAGES
    }
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
    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    with _ticket_lock(root):
        state = _require_existing(root, ticket_id)
        if state.get("stage") not in TERMINAL_STAGES:
            raise BoardError("only a DONE or CANCELLED ticket can be archived")
        events = _read_ticket_events(root, ticket_id)
        digest = hashlib.sha256(
            json.dumps(events, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        archive_dir = root / "ticket-archive" / ticket_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        runtime.write_atomic_replace(
            archive_dir / "events.jsonl",
            "\n".join(json.dumps(e, sort_keys=True, ensure_ascii=False) for e in events)
            + "\n",
        )
        runtime.write_atomic_replace(archive_dir / "content.sha256", digest + "\n")
        _append_ticket_event(
            root,
            ticket_id,
            {"type": EV_ARCHIVE, "actor": actor, "content_hash": digest},
        )
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
        lines.extend(
            f"- {dep.get('type')} `{dep.get('target')}`" for dep in ticket["deps"]
        )
        lines.append("")
    if ticket.get("time_in_stage"):
        lines.append("## Time in stage")
        lines.extend(
            f"- {stage}: {format_duration(seconds)}"
            for stage, seconds in ticket["time_in_stage"].items()
        )
        lines.append("")
    if ticket.get("worklog"):
        lines.append("## Work log")
        for entry in ticket["worklog"]:
            lines.append(
                f"- {entry.get('ts')} **{entry.get('actor')}**: {entry.get('summary')}"
            )
            for key, value in (entry.get("evidence") or {}).items():
                lines.append(f"  - {key}: `{value}`")
        lines.append("")
    if ticket.get("reviews"):
        lines.append("## Reviews")
        for review in ticket["reviews"]:
            lines.append(
                f"- {review.get('ts')} **{review.get('actor')}** `{review.get('verdict')}`: {review.get('summary')}"
            )
            lines.extend(f"  - {finding}" for finding in review.get("findings") or [])
        lines.append("")
    if ticket.get("comments"):
        lines.append("## Comments")
        lines.extend(
            f"- {comment.get('ts')} **{comment.get('actor')}**: {comment.get('summary')}"
            for comment in ticket["comments"]
        )
        lines.append("")
    lines.append(
        f"<!-- content derived from the ticket event log; revision {ticket.get('revision')} -->"
    )
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
        master = master_of(
            ticket.get("reviewer")
            or ticket.get("assignee")
            or ticket.get("created_by")
            or "unknown"
        )
        bucket = per_master.setdefault(master, {"tickets": 0, "done": 0, "reviews": 0})
        bucket["tickets"] += 1
        if stage == "DONE":
            bucket["done"] += 1
            done += 1
        bucket["reviews"] += len(ticket.get("reviews") or [])
        reviews += len(ticket.get("reviews") or [])
    avg_time = {
        stage: (
            time_sums[stage] / time_counts[stage] if time_counts.get(stage) else 0.0
        )
        for stage in STAGES
    }
    return {
        "generated_at": _utc_now(),
        "counts_by_stage": by_stage,
        "avg_seconds_by_stage": avg_time,
        "avg_human_by_stage": {
            stage: format_duration(seconds) for stage, seconds in avg_time.items()
        },
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
        result.setdefault(agent, {}).setdefault(ticket["stage"], []).append(
            ticket["id"]
        )
    return result


def _retry_event(
    root: Path, ticket_id: str, actor: str, key: str | None, payload: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    if key is None:
        return None, None
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise BoardError("idempotency key must contain 1..200 characters")
    fingerprint = hashlib.sha256(
        runtime.canonical_json(payload).encode("utf-8")
    ).hexdigest()
    for event in _read_ticket_events(root, ticket_id):
        if event.get("idempotency_key") == key and event["actor"] == actor:
            if event.get("request_hash") != fingerprint:
                raise TicketConflict("idempotency key reused with a different request")
            return derive_ticket(root, ticket_id), fingerprint
    return None, fingerprint


def claim_ticket(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    expected_revision: int | None = None,
    ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
    recover: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Claim atomically; expired leases require explicit recovery, active leases cannot be stolen."""
    actor = _require_actor_name("actor", actor)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    if type(ttl_sec) is not int or ttl_sec <= 0:
        raise BoardError("lease ttl_sec must be a positive integer")
    with _ticket_lock(root):
        retry, fingerprint = _retry_event(
            root,
            ticket_id,
            actor,
            idempotency_key,
            {"type": EV_CLAIM, "ttl_sec": ttl_sec, "recover": recover},
        )
        if retry is not None:
            return retry
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        if state["stage"] in TERMINAL_STAGES:
            raise TicketConflict("cannot claim a terminal ticket")
        if _open_blockers(root, state):
            raise TicketConflict("ticket has open blockers")
        lease = state.get("lease")
        if lease and not state["lease_stale"]:
            raise TicketConflict("ticket already has an active lease")
        if not lease and state.get("assignee") not in (None, actor):
            raise TicketConflict(
                "ticket is reserved for its assigned actor; explicit reassignment required"
            )
        if lease and not recover:
            raise TicketConflict("expired lease requires explicit recovery")
        now = _utc_now()
        lease = {
            "ticket_id": ticket_id,
            "assignee": actor,
            "granted_by": actor,
            "granted_at": now,
            "heartbeat_at": now,
            "expires_at": _offset_ts(now, ttl_sec),
            "ttl_sec": ttl_sec,
            "token": uuid4().hex,
            "fenced": True,
        }
        _append_ticket_event(
            root,
            ticket_id,
            {
                "type": EV_CLAIM,
                "actor": actor,
                "assignee": actor,
                "lease": lease,
                "recovered": recover,
                "idempotency_key": idempotency_key,
                "request_hash": fingerprint,
            },
        )
        return _persist_ticket(root, state)


def handoff_ticket(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    summary: str,
    evidence: Mapping[str, Any],
    next_actor: str,
    stage: str = "QA",
    body: str = "",
    expected_revision: int | None = None,
    lease_token: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """One durable event carries delivery, evidence and next responsibility."""
    actor = _require_actor_name("actor", actor)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    next_actor = _require_actor_name("next actor", next_actor)
    stage = _require_stage(stage)
    if stage not in ACTIVE_STAGES:
        raise BoardError("handoff stage must be active")
    payload = {
        "type": EV_HANDOFF,
        "actor": actor,
        "summary": _require_title(summary),
        "body": _require_body(body),
        "evidence": _validate_evidence(evidence),
        "next_actor": next_actor,
        "stage": stage,
    }
    with _ticket_lock(root):
        retry, fingerprint = _retry_event(
            root, ticket_id, actor, idempotency_key, payload
        )
        if retry is not None:
            return retry
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        if state["assignee"] != actor:
            raise TicketConflict("only the current assignee can hand off")
        lease = state.get("lease")
        if not lease:
            raise TicketConflict("claim a lease before handoff")
        if lease and lease.get("fenced") and lease_token != lease["token"]:
            raise TicketConflict("lease token mismatch")
        if state.get("lease_stale"):
            raise TicketConflict("lease expired; recover before handoff")
        if state["stage"] in TERMINAL_STAGES:
            raise TicketConflict("cannot hand off a terminal ticket")
        if stage != "BLOCKED" and _open_blockers(root, state):
            raise TicketConflict("ticket has open blockers")
        payload.update(idempotency_key=idempotency_key, request_hash=fingerprint)
        _append_ticket_event(root, ticket_id, payload)
        return _persist_ticket(root, state)


def remove_dependency(
    root: Path,
    ticket_id: str,
    *,
    actor: str,
    dep_type: str,
    target: str,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    target = runtime.require_safe_token("dependency target", target)
    dep_type = _require_dep_type(dep_type)
    with _ticket_lock(root):
        payload = {
            "type": EV_DEP_REMOVE,
            "actor": actor,
            "dep_type": dep_type,
            "target": target,
        }
        retry, fingerprint = _retry_event(
            root, ticket_id, actor, idempotency_key, payload
        )
        if retry is not None:
            return retry
        state = _require_existing(root, ticket_id)
        _require_revision(state, expected_revision)
        reverse = {"BLOCKED_BY": "UNBLOCKS", "UNBLOCKS": "BLOCKED_BY"}.get(dep_type)
        if reverse:
            _require_existing(root, target)
        events = [
            {
                "ticket_id": ticket_id,
                "event": {
                    **payload,
                    "idempotency_key": idempotency_key,
                    "request_hash": fingerprint,
                },
            }
        ]
        if reverse:
            events.append(
                {
                    "ticket_id": target,
                    "event": {
                        "type": EV_DEP_REMOVE,
                        "actor": actor,
                        "dep_type": reverse,
                        "target": ticket_id,
                    },
                }
            )
        return _commit_dependency_events(root, events, state)


def actor_context(root: Path, *, actor: str) -> dict[str, Any]:
    actor = _require_actor_name("actor", actor)
    entries = []
    for state in list_tickets(root):
        blockers = _open_blockers(root, state)
        actions = []
        review_owner = resolve_reviewer(
            root, state["reviewer"] or state["assignee"] or actor
        )
        if state["stage"] not in TERMINAL_STAGES:
            if (
                not blockers
                and not state.get("lease")
                and state.get("assignee") in (None, actor)
            ):
                actions.append("claim")
            if state.get("lease_stale") and not blockers:
                actions.append("claim-recover")
            if state["assignee"] == actor:
                actions.extend(["comment", "worklog"])
                if state.get("lease") and not blockers and not state.get("lease_stale"):
                    actions.append("handoff")
            if actor == review_owner and not is_subagent(actor):
                actions.append("review")
        if actions or state["assignee"] == actor or state["reviewer"] == actor:
            entries.append(
                {
                    "id": state["id"],
                    "display_id": state.get("display_id"),
                    "title": state["title"],
                    "stage": state["stage"],
                    "assignee": state["assignee"],
                    "revision": state["revision"],
                    "blockers": blockers,
                    "latest_delivery": state.get("latest_delivery"),
                    "latest_review": (state["reviews"] or [None])[-1],
                    "allowed_actions": actions,
                }
            )
    return {"actor": actor, "tickets": entries}


def ticket_changes(
    root: Path, *, cursor: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """Opaque per-stream cursors never skip a later append to an older ticket."""
    import base64

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise BoardError("limit must be 1..1000")
    try:
        decoded = (
            json.loads(base64.urlsafe_b64decode(cursor).decode("utf-8"))
            if cursor
            else None
        )
        scope = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
        if decoded is not None and (
            not isinstance(decoded, dict) or decoded.get("scope") != scope
        ):
            raise ValueError()
        positions = decoded["positions"] if decoded else {}
        if not isinstance(positions, dict) or any(
            type(n) is not int or n < 0 for n in positions.values()
        ):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise BoardError("invalid change cursor")
    pending = []
    for path in sorted(_events_dir(root).glob("*.jsonl")):
        for event in _read_ticket_events(root, path.stem):
            if event["seq"] > positions.get(path.stem, 0):
                pending.append({**event, "ticket_id": path.stem})
    pending.sort(key=lambda e: (e["ticket_id"], e["seq"]))
    events = pending[:limit]
    for event in events:
        positions[event["ticket_id"]] = event["seq"]
    encoded = base64.urlsafe_b64encode(
        json.dumps({"scope": scope, "positions": positions}, sort_keys=True).encode()
    ).decode()
    return {"events": events, "cursor": encoded, "has_more": len(pending) > limit}


def recover_ticket_tail(
    root: Path, ticket_id: str, *, actor: str, expected_revision: int | None = None
) -> dict[str, Any]:
    """Remove only an incomplete final record, saving the original bytes first."""
    identity.require_identity(actor, root)
    ticket_id = runtime.require_safe_token("ticket id", ticket_id)
    with _ticket_lock(root, recover=False):
        path = _ticket_events_path(root, ticket_id)
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        if not lines:
            raise BoardError("no events to recover")
        prev, prefix = "0" * 64, b""
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if index != len(lines) - 1 or line.endswith(b"\n") or not prefix:
                    raise BoardError(
                        "recovery only permits an incomplete final record after a valid CREATE"
                    )
                break
            _validate_event(event, index + 1, prev)
            prev = event["hash"]
            prefix += line
        else:
            raise BoardError("no incomplete final record to recover")
        _require_revision(
            {"id": ticket_id, "revision": len(lines) - 1}, expected_revision
        )
        backup = root / "ticket-recovery" / (ticket_id + "-" + uuid4().hex + ".jsonl")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = path.with_suffix(".recovery-" + uuid4().hex)
        with temporary.open("xb") as handle:
            handle.write(prefix)
            handle.flush()
            os.fsync(handle.fileno())
        runtime.write_atomic_replace(
            backup.with_suffix(".json"),
            runtime.canonical_json(
                {
                    "actor": actor,
                    "ticket_id": ticket_id,
                    "recovered_at": _utc_now(),
                    "original_sha256": hashlib.sha256(original).hexdigest(),
                    "removed_bytes": len(original) - len(prefix),
                    "revision": len(lines) - 1,
                }
            ),
        )
        os.replace(temporary, path)
        _recover_dependency_transactions(root)
        state = _persist_ticket(root, {"id": ticket_id})
        return {
            "backup_path": str(backup),
            "removed_bytes": len(original) - len(prefix),
            "revision": state["revision"],
        }


def resolve_ticket_reference(root: Path, reference: str) -> str:
    reference = runtime.require_safe_token("ticket reference", reference)
    if _ticket_events_path(root, reference).is_file():
        get_ticket(root, reference)
        return reference
    matches = [
        canonical
        for canonical, display in _read_display_ids(root)["ids"].items()
        if display.casefold() == reference.casefold()
    ]
    if len(matches) == 1:
        get_ticket(root, matches[0])
        return matches[0]
    raise BoardError(f"unknown ticket reference: {reference}")
