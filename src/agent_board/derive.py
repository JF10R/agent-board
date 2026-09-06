#!/usr/bin/env python3
"""Derived roadmap views for humans: startable, standby, blocked-by, parallel frontier, milestones.

Pure functions over the tree payload of ``agent_board_tree.build_tree``. Nothing here reads or
writes the board. Absent data stays UNKNOWN: an unknown dependency is unresolved, a milestone
with no reported child progress has no progress.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

try:
    from agent_board import cli as board
    from agent_board import tree
except ImportError:  # Direct execution from within the package directory.
    import cli as board  # type: ignore[no-redef]
    import tree  # type: ignore[no-redef]


DEFAULT_STANDBY_HOURS = 48.0
MAX_STANDBY_HOURS = 24.0 * 366
CLOSED = tree.CLOSED_STATUSES
AGGREGATING_KINDS = frozenset({"MILESTONE", "OBJECTIVE"})


def validate_standby_hours(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAX_STANDBY_HOURS:
        raise board.BoardError("standby_hours must be a positive number of hours up to one year")
    return float(value)


def age_hours(updated_at: Any, *, now: datetime | None = None) -> float | None:
    moment = tree.parse_time(updated_at)
    if moment is None:
        return None
    return max(0.0, ((now or datetime.now(timezone.utc)).astimezone(timezone.utc) - moment).total_seconds() / 3600)


def is_open(item: Mapping[str, Any]) -> bool:
    return item["status"] not in CLOSED


def is_startable(item: Mapping[str, Any], unresolved: list[str]) -> bool:
    """NOT_STARTED (or legacy PENDING at 0 %) with no blocker and no unresolved dependency."""

    not_started = item["status"] == "NOT_STARTED" or (item["status"] == "PENDING" and item["progress"] == 0)
    return not_started and not item["blockers"] and not unresolved


def _dependency_cycles(by_id: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """Ids that can reach themselves through depends_on edges (the board is small; O(n·e) is fine)."""

    on_cycle: set[str] = set()
    for start in by_id:
        stack = [dep for dep in by_id[start]["depends_on_ids"] if dep in by_id]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == start:
                on_cycle.add(start)
                break
            if node in seen:
                continue
            seen.add(node)
            stack.extend(dep for dep in by_id[node]["depends_on_ids"] if dep in by_id)
    return on_cycle


def _reverse_closure(start: str, edges: Mapping[str, list[str]]) -> list[str]:
    seen: set[str] = set()
    stack = list(edges.get(start, []))
    while stack:
        node = stack.pop()
        if node in seen or node == start:
            continue
        seen.add(node)
        stack.extend(edges.get(node, []))
    return sorted(seen)


def derive_roadmap_views(payload: Mapping[str, Any], *, standby_hours: float = DEFAULT_STANDBY_HOURS, now: datetime | None = None) -> dict[str, Any]:
    """Return a copy of the tree payload with ``derived`` per item and a top-level ``views`` block."""

    standby_hours = validate_standby_hours(standby_hours)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    items = [dict(item) for item in payload.get("items", [])]
    by_id = {item["id"]: item for item in items}
    warnings = list(payload.get("warnings", []))
    cycles = _dependency_cycles(by_id)
    if cycles:
        warnings.append(f"dependency cycle through {', '.join(sorted(cycles))}; those dependencies stay unresolved")
    dependents: dict[str, list[str]] = {item_id: [] for item_id in by_id}
    upward: dict[str, list[str]] = {item_id: [] for item_id in by_id}  # task -> items it feeds (dependents + parent)
    for item in items:
        for dep in item["depends_on_ids"]:
            if dep in by_id:
                dependents[dep].append(item["id"])
                upward[dep].append(item["id"])
            else:
                warnings.append(f"{item['id']}: dependency {dep!r} is unknown; treated as unresolved")
        if item.get("parent_id") in by_id:
            upward[item["id"]].append(item["parent_id"])

    for item in items:
        deps = []
        for dep in item["depends_on_ids"]:
            target = by_id.get(dep)
            resolved = bool(target) and target["status"] in CLOSED and dep not in cycles
            deps.append({"id": dep, "title": target["title"] if target else None, "status": target["status"] if target else None, "known": bool(target), "resolved": resolved})
        unresolved = [dep["id"] for dep in deps if not dep["resolved"]]
        age = age_hours(item["updated_at"], now=moment)
        flag = item.get("standby_flag")
        standby = None
        if item["status"] == "IN_PROGRESS":
            if flag:
                standby = {"kind": "explicit", "reason": flag["reason"], "set_by": flag.get("set_by"), "age_hours": age}
            elif age is not None and age >= standby_hours:
                standby = {"kind": "stale", "reason": f"no update for {age / 24:.1f} d (threshold {standby_hours:g} h)", "set_by": None, "age_hours": age}
        startable = is_startable(item, unresolved)
        blocked_by = {"blockers": [blocker["text"] for blocker in item["blockers"]], "waiting_on": unresolved} if item["status"] == "BLOCKED" or (unresolved and is_open(item)) else None
        actionable = item["status"] in ("IN_PROGRESS", "READY") or startable
        item["derived"] = {
            "kind": item.get("kind") or "UNKNOWN",
            "impact": item.get("impact"),
            "depends_on": deps,
            "unresolved_deps": unresolved,
            "dependents": sorted(dependents[item["id"]]),
            "feeds": [],
            "age_hours": None if age is None else round(age, 2),
            "standby": standby,
            "blocked_by": blocked_by,
            "startable": startable,
            "waiting": item["status"] in ("NOT_STARTED", "PENDING") and bool(unresolved),
            "parallel": actionable and not item["blockers"] and not unresolved and standby is None,
            "on_cycle": item["id"] in cycles,
            "aggregate": None,
        }
    for item in items:
        item["derived"]["feeds"] = [target for target in _reverse_closure(item["id"], upward) if by_id[target].get("kind") in AGGREGATING_KINDS]
    for item in items:
        if item.get("kind") not in AGGREGATING_KINDS:
            continue
        member_ids = sorted({*item["children"], *(dep for dep in item["depends_on_ids"] if dep in by_id)} - {item["id"]})
        members = [by_id[member] for member in member_ids]
        reported = [member["progress"] for member in members if member["progress_reported"]]
        # CLOSED members (superseded, folded, negative) do not advance a milestone: a killed
        # mechanism at 100 must not read as mission progress (operator, 2026-09-06).
        open_reported = [member["progress"] for member in members if member["progress_reported"] and member["status"] not in CLOSED]
        by_status: dict[str, int] = {}
        for member in members:
            by_status[member["status"]] = by_status.get(member["status"], 0) + 1
        item["derived"]["aggregate"] = {
            "members": member_ids,
            "count": len(members),
            "by_status": dict(sorted(by_status.items())),
            "closed": sum(1 for member in members if member["status"] in CLOSED),
            "blocked": sum(1 for member in members if member["status"] == "BLOCKED"),
            "standby": sum(1 for member in members if member["derived"]["standby"]),
            "startable": sum(1 for member in members if member["derived"]["startable"]),
            "open": [member["id"] for member in members if is_open(member)],
            "reported": len(reported),
            "mean_reported_progress": round(sum(reported) / len(reported)) if reported else None,
            "mean_open_progress": round(sum(open_reported) / len(open_reported)) if open_reported else None,
            "self_reported_progress": item["progress"] if item["progress_reported"] else None,
        }

    def key_age(item_id: str) -> tuple[float, str]:
        age = by_id[item_id]["derived"]["age_hours"]
        return (-(age if age is not None else 0.0), item_id)

    parallel_ids = sorted(item["id"] for item in items if item["derived"]["parallel"])
    by_owner: dict[str, list[str]] = {}
    for item_id in parallel_ids:
        by_owner.setdefault(by_id[item_id]["owner"], []).append(item_id)
    views = {
        "standby_hours": standby_hours,
        "startable": sorted(item["id"] for item in items if item["derived"]["startable"]),
        "waiting": sorted(item["id"] for item in items if item["derived"]["waiting"]),
        "standby": sorted((item["id"] for item in items if item["derived"]["standby"]), key=key_age),
        "blocked": [
            {"id": item["id"], **item["derived"]["blocked_by"]}
            for item in sorted(items, key=lambda entry: entry["id"])
            if item["status"] == "BLOCKED"
        ],
        "parallel": {"items": parallel_ids, "by_owner": dict(sorted(by_owner.items()))},
        "milestones": sorted(item["id"] for item in items if item.get("kind") in AGGREGATING_KINDS),
    }
    return {**payload, "items": items, "warnings": warnings, "views": views}
