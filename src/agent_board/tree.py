#!/usr/bin/env python3
"""Additive roadmap tree model for the agent board (parent/child, blockers, gates, journal).

The v1 store ``roadmap.v1.json`` is FROZEN: its validator rejects unknown fields, so
every extension lives in a sidecar ``roadmap-ext.v1.json`` that v1 readers never open.
Old items read unchanged; a missing sidecar means "no annotations, no history yet".
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

try:
    from agent_board import cli as board
except ImportError:  # Direct execution from within the package directory.
    import cli as board  # type: ignore[no-redef]


EXT_FILE = "roadmap-ext.v1.json"
EXT_SCHEMA_VERSION = 1
GATE_STATES = frozenset({"PASS", "FAIL", "PENDING", "UNKNOWN"})
CLOSED_STATUSES = frozenset({"CLOSED", "COMPLETE"})
MAX_EXT_FILE_BYTES = 4 * 1024 * 1024
MAX_BLOCKERS = 20
MAX_GATES = 20
MAX_BLOCKER_CHARS = 500
MAX_GATE_NOTE_CHARS = 500
MAX_GATE_NAME_CHARS = 64
JOURNAL_LIMIT = 5000
HISTORY_PER_ITEM = 50
DEFAULT_SINCE_HOURS = 24.0
# Additive (2026-09-05): dependencies, kind, impact and standby live in the sidecar too.
ITEM_KINDS = frozenset({"TASK", "MILESTONE", "OBJECTIVE"})
MAX_DEPENDS_ON = 50
MAX_ONE_LINER_CHARS = 300
CHILD_OF = re.compile(r"\bchild of ([A-Za-z0-9][A-Za-z0-9._-]{0,127})")
_UNSET = object()


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ext_path(root: Path) -> Path:
    return root / EXT_FILE


@contextmanager
def _ext_lock(root: Path) -> Iterable[None]:
    board.initialize(root)
    with board._file_lock(root / ".roadmap-ext.lock"):
        yield


def _empty_ext() -> dict[str, Any]:
    return {"schema_version": EXT_SCHEMA_VERSION, "items": {}, "journal": []}


def _read_ext(root: Path) -> dict[str, Any]:
    path = _ext_path(root)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return _empty_ext()
    if size > MAX_EXT_FILE_BYTES:
        raise board.BoardError(f"roadmap extension exceeds {MAX_EXT_FILE_BYTES} bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise board.BoardError(f"invalid roadmap extension: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != EXT_SCHEMA_VERSION
        or not isinstance(value.get("items"), dict)
        or not isinstance(value.get("journal"), list)
    ):
        raise board.BoardError("invalid roadmap extension shape")
    for item_id, entry in value["items"].items():
        if not isinstance(entry, dict):
            raise board.BoardError(f"invalid roadmap extension entry: {item_id}")
    return value


def _write_ext(root: Path, ext: Mapping[str, Any]) -> None:
    board._write_atomic_replace(_ext_path(root), board._canonical_json(ext))


def _journal_entry(item: Mapping[str, Any], observed_at: str) -> dict[str, Any]:
    return {
        "id": item["id"],
        "revision": item["revision"],
        "status": item["status"],
        "progress": item["progress"],
        "owner": item["owner"],
        "title": item["title"],
        "updated_at": item["updated_at"],
        "observed_at": observed_at,
    }


def record_journal(root: Path, base_items: Sequence[Mapping[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Append every (id, revision) not yet journaled. Returns the current extension."""

    observed_at = _iso(_now(now))
    with _ext_lock(root):
        ext = _read_ext(root)
        seen = {(entry.get("id"), entry.get("revision")) for entry in ext["journal"]}
        changed = False
        for item in base_items:
            key = (item["id"], item["revision"])
            if key in seen:
                continue
            ext["journal"].append(_journal_entry(item, observed_at))
            seen.add(key)
            changed = True
        if len(ext["journal"]) > JOURNAL_LIMIT:
            ext["journal"] = ext["journal"][-JOURNAL_LIMIT:]
            changed = True
        if changed:
            _write_ext(root, ext)
        return ext


def _derived_parent(summary: str, known_ids: set[str]) -> str | None:
    match = CHILD_OF.search(summary or "")
    if not match:
        return None
    candidate = match.group(1)
    while candidate and candidate not in known_ids and candidate[-1] in "._-":
        candidate = candidate[:-1]  # "child of night." — sentence punctuation is not part of the id
    return candidate if candidate in known_ids else None


def _raw_parents(base: Sequence[Mapping[str, Any]], ext_items: Mapping[str, Any]) -> tuple[dict[str, str | None], dict[str, str | None], list[str]]:
    """Annotation first, then the summary convention; cycles are NOT broken here."""

    known = {item["id"] for item in base}
    parent: dict[str, str | None] = {}
    source: dict[str, str | None] = {}
    warnings: list[str] = []
    for item in base:
        entry = ext_items.get(item["id"], {})
        if "parent_id" in entry:
            candidate = entry["parent_id"]
            if candidate is None:
                parent[item["id"]], source[item["id"]] = None, None
            elif candidate in known and candidate != item["id"]:
                parent[item["id"]], source[item["id"]] = candidate, "annotation"
            else:
                parent[item["id"]], source[item["id"]] = None, None
                warnings.append(f"{item['id']}: annotated parent {candidate!r} is unknown; link ignored")
            continue
        derived = _derived_parent(item["summary"], known)
        if derived == item["id"]:
            derived = None
        parent[item["id"]] = derived
        source[item["id"]] = "summary" if derived else None
    return parent, source, warnings


def _would_cycle(parent: Mapping[str, str | None], item_id: str, parent_id: str) -> bool:
    cursor: str | None = parent_id
    seen: set[str] = set()
    while cursor is not None and cursor not in seen:
        if cursor == item_id:
            return True
        seen.add(cursor)
        cursor = parent.get(cursor)
    return False


def _resolve_parents(base: Sequence[Mapping[str, Any]], ext_items: Mapping[str, Any]) -> tuple[dict[str, str | None], dict[str, str | None], list[str]]:
    parent, source, warnings = _raw_parents(base, ext_items)
    # Break cycles deterministically: the lexically smallest member loses its parent link.
    for start in sorted(parent):
        chain, cursor = [], start
        while cursor is not None and cursor not in chain:
            chain.append(cursor)
            cursor = parent.get(cursor)
        if cursor is not None:
            loser = min(chain[chain.index(cursor):])
            warnings.append(f"{loser}: parent link dropped to break a cycle through {cursor}")
            parent[loser], source[loser] = None, None
    return parent, source, warnings


def _progress_reported(item: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    flag = entry.get("progress_reported")
    if isinstance(flag, bool):
        return flag
    return item["status"] in CLOSED_STATUSES or item["progress"] > 0


def _changes(current: Mapping[str, Any], prior: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if prior is None:
        return []
    return [
        {"field": field, "from": prior.get(field), "to": current.get(field)}
        for field in ("status", "progress", "owner", "title")
        if prior.get(field) != current.get(field)
    ]


def build_tree(
    base: Sequence[Mapping[str, Any]],
    ext: Mapping[str, Any],
    *,
    since_hours: float = DEFAULT_SINCE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure merge of the v1 items and the sidecar into the tree payload."""

    moment = _now(now)
    cutoff = moment - timedelta(hours=since_hours)
    ext_items: Mapping[str, Any] = ext.get("items", {})
    parent, parent_source, warnings = _resolve_parents(base, ext_items)
    children: dict[str, list[str]] = {item["id"]: [] for item in base}
    for child_id, parent_id in parent.items():
        if parent_id is not None:
            children[parent_id].append(child_id)
    history: dict[str, list[dict[str, Any]]] = {item["id"]: [] for item in base}
    for entry in ext.get("journal", []):
        if entry.get("id") in history:
            history[entry["id"]].append(entry)
    for entries in history.values():
        entries.sort(key=lambda entry: (entry.get("revision", 0), entry.get("observed_at", "")))

    def depth_of(item_id: str) -> int:
        depth, cursor = 0, parent.get(item_id)
        while cursor is not None:
            depth, cursor = depth + 1, parent.get(cursor)
        return depth

    merged: dict[str, dict[str, Any]] = {}
    for item in base:
        entry = ext_items.get(item["id"], {})
        blockers = [{"text": item["blocker"], "source": "item", "added_by": None, "added_at": item["updated_at"]}] if item["blocker"] else []
        # Absent or malformed sidecar fields read as UNKNOWN (None / empty), never as a value.
        raw_deps = entry.get("depends_on")
        depends_on_ids = [dep for dep in raw_deps if isinstance(dep, str)] if isinstance(raw_deps, list) else []
        kind = entry.get("kind") if entry.get("kind") in ITEM_KINDS else None
        impact = entry.get("impact") if isinstance(entry.get("impact"), str) and entry.get("impact").strip() else None
        standby_flag = entry.get("standby") if isinstance(entry.get("standby"), dict) and isinstance(entry["standby"].get("reason"), str) else None
        blockers.extend({**blocker, "source": "annotation"} for blocker in entry.get("blockers", []) if isinstance(blocker, dict))
        entries = history[item["id"]]
        current = next((candidate for candidate in reversed(entries) if candidate.get("revision") == item["revision"]), None)
        prior = next((candidate for candidate in reversed(entries) if candidate.get("revision", 0) < item["revision"]), None)
        updated = parse_time(item["updated_at"])
        ext_updated = parse_time(entry.get("updated_at"))
        reported = _progress_reported(item, entry)
        merged[item["id"]] = {
            **item,
            "parent_id": parent[item["id"]],
            "parent_source": parent_source[item["id"]],
            "children": sorted(children[item["id"]]),
            "depth": depth_of(item["id"]),
            "blockers": blockers,
            "gates": [gate for gate in entry.get("gates", []) if isinstance(gate, dict)],
            "due": entry.get("due"),
            "updated_by": entry.get("updated_by") if ext_updated and updated and ext_updated >= updated else None,
            "annotation_updated_at": entry.get("updated_at"),
            "progress_reported": reported,
            "progress_display": item["progress"] if reported else None,
            "depends_on_ids": depends_on_ids,
            "kind": kind,
            "impact": impact,
            "standby_flag": standby_flag,
            "moved": bool(updated and updated >= cutoff),
            "last_change": None if current is None else {
                "revision": item["revision"],
                "updated_at": item["updated_at"],
                "observed_at": current.get("observed_at"),
                "prior_known": prior is not None or item["revision"] == 1,
                "changes": _changes(current, prior),
            },
            "history": [
                {key: entry.get(key) for key in ("revision", "status", "progress", "owner", "updated_at", "observed_at")}
                for entry in entries[-HISTORY_PER_ITEM:]
            ],
            "rollup": None,
        }
    for item in merged.values():
        if not item["children"]:
            continue
        kids = [merged[child] for child in item["children"]]
        by_status: dict[str, int] = {}
        for kid in kids:
            by_status[kid["status"]] = by_status.get(kid["status"], 0) + 1
        reported = [kid["progress"] for kid in kids if kid["progress_reported"]]
        item["rollup"] = {
            "children": len(kids),
            "by_status": dict(sorted(by_status.items())),
            "closed": sum(1 for kid in kids if kid["status"] in CLOSED_STATUSES),
            "blocked": sum(1 for kid in kids if kid["blockers"]),
            "reported": len(reported),
            "mean_reported_progress": round(sum(reported) / len(reported)) if reported else None,
            "moved": sum(1 for kid in kids if kid["moved"]),
        }
    items = [merged[key] for key in sorted(merged)]
    roots = sorted((item["id"] for item in items if item["parent_id"] is None), key=lambda item_id: (not merged[item_id]["children"], item_id))
    moved = sorted(
        (
            {
                "id": item["id"],
                "title": item["title"],
                "status": item["status"],
                "revision": item["revision"],
                "updated_at": item["updated_at"],
                "parent_id": item["parent_id"],
                "new": item["revision"] == 1,
                "prior_known": bool(item["last_change"] and item["last_change"]["prior_known"]),
                "changes": item["last_change"]["changes"] if item["last_change"] else [],
            }
            for item in items
            if item["moved"]
        ),
        key=lambda entry: entry["updated_at"],
        reverse=True,
    )
    counts = {"items": len(items), "roots": len(roots), "moved": len(moved)}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema_version": EXT_SCHEMA_VERSION,
        "generated_at": _iso(moment),
        "since_hours": since_hours,
        "items": items,
        "roots": roots,
        "moved": moved,
        "counts": counts,
        "warnings": warnings,
    }


def load_roadmap_tree(
    root: Path,
    *,
    since_hours: float = DEFAULT_SINCE_HOURS,
    now: datetime | None = None,
    record: bool = True,
) -> dict[str, Any]:
    """Merged tree. ``record=False`` reads the sidecar without appending to the journal."""

    if not isinstance(since_hours, (int, float)) or isinstance(since_hours, bool) or since_hours <= 0 or since_hours > 24 * 366:
        raise board.BoardError("since_hours must be a positive number of hours up to one year")
    base = board.list_roadmap(root)
    if record:
        ext = record_journal(root, base, now=now)
    else:
        # Lock-free on purpose: taking the lock would create its sentinel file on a board
        # that a read-only caller promised not to touch. The sidecar is atomically replaced,
        # so an unlocked read sees either the old or the new whole file.
        ext = _read_ext(root)
    return build_tree(base, ext, since_hours=float(since_hours), now=now)


def _gate(name: str, state: str, note: str, *, actor: str, at: str) -> dict[str, Any]:
    name = name.strip()
    if not name or len(name) > MAX_GATE_NAME_CHARS or "\n" in name:
        raise board.BoardError(f"gate name must be one line of at most {MAX_GATE_NAME_CHARS} characters")
    state = state.strip().upper()
    if state not in GATE_STATES:
        raise board.BoardError(f"gate state must be one of {', '.join(sorted(GATE_STATES))}")
    note = note.strip()
    if len(note) > MAX_GATE_NOTE_CHARS:
        raise board.BoardError(f"gate note exceeds {MAX_GATE_NOTE_CHARS} characters")
    return {"name": name, "state": state, "note": note, "updated_by": actor, "updated_at": at}


def parse_gate_spec(spec: str) -> tuple[str, str, str]:
    """``NAME=STATE`` or ``NAME=STATE:note`` as typed on the command line."""

    name, separator, rest = spec.partition("=")
    if not separator:
        raise board.BoardError(f"gate must look like NAME=STATE[:note], got {spec!r}")
    state, _, note = rest.partition(":")
    return name, state, note


def _dependency_would_cycle(ext_items: Mapping[str, Any], item_id: str, depends_on: Sequence[str]) -> str | None:
    """Return the dependency that reaches back to ``item_id`` through existing depends_on edges."""

    for start in depends_on:
        stack, seen = [start], set()
        while stack:
            cursor = stack.pop()
            if cursor == item_id:
                return start
            if cursor in seen:
                continue
            seen.add(cursor)
            raw = ext_items.get(cursor, {}).get("depends_on")
            if isinstance(raw, list):
                stack.extend(dep for dep in raw if isinstance(dep, str))
    return None


def _one_liner(label: str, value: Any) -> str:
    text = board._require_text(label, str(value))
    if len(text) > MAX_ONE_LINER_CHARS:
        raise board.BoardError(f"{label} exceeds {MAX_ONE_LINER_CHARS} characters")
    return text


def annotate_roadmap_item(
    root: Path,
    *,
    actor: str,
    item_id: str,
    parent_id: Any = _UNSET,
    add_blockers: Sequence[str] = (),
    clear_blockers: bool = False,
    gates: Sequence[tuple[str, str, str]] = (),
    clear_gates: bool = False,
    due: Any = _UNSET,
    progress_reported: bool | None = None,
    depends_on: Any = _UNSET,
    kind: Any = _UNSET,
    impact: Any = _UNSET,
    standby: Any = _UNSET,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write sidecar annotations for one existing v1 item; never touches roadmap.v1.json."""

    board._require_identity(actor)
    item_id = board._require_safe_token("roadmap item id", item_id)
    at = _iso(_now(now))
    base = board.list_roadmap(root)
    known = {item["id"] for item in base}
    if item_id not in known:
        raise board.BoardError(f"unknown roadmap item: {item_id}")
    with _ext_lock(root):
        ext = _read_ext(root)
        entry = dict(ext["items"].get(item_id, {}))
        if depends_on is not _UNSET:
            deps: list[str] = []
            for dep in depends_on or []:
                dep = board._require_safe_token("dependency id", str(dep))
                if dep == item_id:
                    raise board.BoardError("an item cannot depend on itself")
                if dep not in known:
                    raise board.BoardError(f"unknown dependency roadmap item: {dep}")
                if dep not in deps:
                    deps.append(dep)
            if len(deps) > MAX_DEPENDS_ON:
                raise board.BoardError(f"at most {MAX_DEPENDS_ON} dependencies per item")
            offender = _dependency_would_cycle(ext["items"], item_id, deps)
            if offender is not None:
                raise board.BoardError(f"dependency {offender} would create a cycle with {item_id}")
            entry["depends_on"] = deps
        if kind is not _UNSET:
            if kind is None:
                entry["kind"] = None
            else:
                kind = str(kind).strip().upper()
                if kind not in ITEM_KINDS:
                    raise board.BoardError(f"kind must be one of {', '.join(sorted(ITEM_KINDS))}")
                entry["kind"] = kind
        if impact is not _UNSET:
            entry["impact"] = None if impact is None or not str(impact).strip() else _one_liner("impact", impact)
        if standby is not _UNSET:
            entry["standby"] = None if standby is None else {"reason": _one_liner("standby reason", standby), "set_by": actor, "set_at": at}
        if parent_id is not _UNSET:
            if parent_id is not None:
                parent_id = board._require_safe_token("parent id", parent_id)
                if parent_id == item_id:
                    raise board.BoardError("an item cannot be its own parent")
                if parent_id not in known:
                    raise board.BoardError(f"unknown parent roadmap item: {parent_id}")
                raw, _, _ = _raw_parents(base, ext["items"])
                if _would_cycle(raw, item_id, parent_id):
                    raise board.BoardError(f"parent {parent_id} would create a cycle with {item_id}")
            entry["parent_id"] = parent_id
        blockers = [] if clear_blockers else list(entry.get("blockers", []))
        for text in add_blockers:
            text = board._require_text("blocker", text)
            if len(text) > MAX_BLOCKER_CHARS:
                raise board.BoardError(f"blocker exceeds {MAX_BLOCKER_CHARS} characters")
            blockers.append({"text": text, "added_by": actor, "added_at": at})
        if len(blockers) > MAX_BLOCKERS:
            raise board.BoardError(f"at most {MAX_BLOCKERS} blockers per item")
        entry["blockers"] = blockers
        gate_list = [] if clear_gates else list(entry.get("gates", []))
        for name, state, note in gates:
            gate = _gate(name, state, note, actor=actor, at=at)
            gate_list = [existing for existing in gate_list if existing.get("name") != gate["name"]]
            gate_list.append(gate)
        if len(gate_list) > MAX_GATES:
            raise board.BoardError(f"at most {MAX_GATES} gates per item")
        entry["gates"] = gate_list
        if due is not _UNSET:
            if due is not None:
                try:
                    due = date.fromisoformat(str(due)).isoformat()
                except ValueError as exc:
                    raise board.BoardError("due must be an ISO date (YYYY-MM-DD)") from exc
            entry["due"] = due
        if progress_reported is not None:
            entry["progress_reported"] = bool(progress_reported)
        entry["updated_by"] = actor
        entry["updated_at"] = at
        ext["items"][item_id] = entry
        _write_ext(root, ext)
    tree = load_roadmap_tree(root, now=now)
    return next(item for item in tree["items"] if item["id"] == item_id)


def relative_age(value: Any, *, now: datetime | None = None) -> str:
    moment = parse_time(value)
    if moment is None:
        return "unknown"
    seconds = int((_now(now) - moment).total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def render_tree_text(tree: Mapping[str, Any], *, root_id: str | None = None, now: datetime | None = None) -> str:
    by_id = {item["id"]: item for item in tree["items"]}
    if root_id is not None and root_id not in by_id:
        raise board.BoardError(f"unknown roadmap item: {root_id}")
    lines: list[str] = []

    def progress_label(item: Mapping[str, Any]) -> str:
        return f"{item['progress']:>3d}%" if item["progress_reported"] else "  --"

    def emit(item_id: str, prefix: str, is_last: bool, is_root: bool) -> None:
        item = by_id[item_id]
        connector = "" if is_root else ("└─ " if is_last else "├─ ")
        rollup = item["rollup"]
        summary = ""
        if rollup:
            parts = [f"{count} {status.lower().replace('_', ' ')}" for status, count in rollup["by_status"].items()]
            mean = f", mean {rollup['mean_reported_progress']}% over {rollup['reported']} reported" if rollup["reported"] else ", no child progress reported"
            summary = f"  [{rollup['children']} children: {', '.join(parts)}{mean}]"
        moved = " *" if item["moved"] else ""
        lines.append(
            f"{prefix}{connector}{item['id']}  {item['status']}  {progress_label(item)}  {item['owner']}  r{item['revision']}  {relative_age(item['updated_at'], now=now)}{moved}{summary}"
        )
        child_prefix = prefix + ("" if is_root else ("   " if is_last else "│  "))
        for blocker in item["blockers"]:
            lines.append(f"{child_prefix}   ! blocker: {blocker['text']}")
        for gate in item["gates"]:
            note = f" — {gate['note']}" if gate.get("note") else ""
            lines.append(f"{child_prefix}   ◇ gate {gate['name']}: {gate['state']}{note}")
        if item["due"]:
            lines.append(f"{child_prefix}   due {item['due']}")
        for index, child in enumerate(item["children"]):
            emit(child, child_prefix, index == len(item["children"]) - 1, False)

    roots = [root_id] if root_id else tree["roots"]
    for root in roots:
        emit(root, "", True, True)
        lines.append("")
    counts = tree["counts"]
    lines.append(f"{counts['items']} items, {counts['roots']} roots, {counts['moved']} moved in the last {tree['since_hours']:g} h (*). -- = progress not reported.")
    for warning in tree["warnings"]:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)
