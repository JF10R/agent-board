"""Project vocabulary and actor authorization, independent of CLI parsing."""

from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any

from .errors import BoardError
from .runtime import canonical_json, require_safe_token, write_exclusive

IDENTITIES = frozenset({"gpt-master", "claude-master"})
MESSAGE_SENDERS = IDENTITIES | frozenset({"lead", "operator"})
MESSAGE_RECIPIENTS = MESSAGE_SENDERS
ROADMAP_OWNERS = IDENTITIES | frozenset({"shared", "unassigned"})
PROJECT_CONFIG_FILE = "project.v1.json"
PROJECT_CONFIG_KEYS = (
    "identities",
    "message_participants",
    "roadmap_owners",
    "workstreams",
)
DEFAULT_PROJECT_CONFIG = {
    "identities": ["master"],
    "message_participants": ["lead", "operator"],
    "roadmap_owners": ["shared", "unassigned"],
    "workstreams": [],
}
_PROJECT_CONFIG_CACHE: dict[str, tuple[int, dict[str, Any]]] = {}
_PROJECT_CONFIG_LOCK = threading.Lock()


def default_project_config() -> dict[str, Any]:
    """Seed values only: every new store starts with the same neutral vocabulary."""

    return {key: list(DEFAULT_PROJECT_CONFIG[key]) for key in PROJECT_CONFIG_KEYS}


def seed_project_config(root: Path) -> Path | None:
    """Written by `init` only. A store without the file uses the fallback vocabulary."""

    path = Path(root) / PROJECT_CONFIG_FILE
    if path.exists():
        return None
    write_exclusive(path, canonical_json(default_project_config()))
    return path


def _validated_project_config(raw: Any) -> dict[str, Any]:
    defaults = default_project_config()
    if not isinstance(raw, dict):
        raise BoardError(f"{PROJECT_CONFIG_FILE} must be a JSON object")
    value: dict[str, Any] = {}
    for key in PROJECT_CONFIG_KEYS:
        entries = raw.get(key, defaults[key])
        if not isinstance(entries, list) or any(
            not isinstance(entry, str) for entry in entries
        ):
            raise BoardError(f"{PROJECT_CONFIG_FILE}: {key} must be a list of strings")
        value[key] = [require_safe_token(f"{key} entry", entry) for entry in entries]
    if not value["identities"]:
        raise BoardError(f"{PROJECT_CONFIG_FILE}: identities must not be empty")
    return value


def project_config(root: Path | None) -> dict[str, Any]:
    """Return project vocabulary, or fallback identities when no config exists."""

    if root is None:
        return {
            "identities": IDENTITIES,
            "message_senders": MESSAGE_SENDERS,
            "message_recipients": MESSAGE_RECIPIENTS,
            "roadmap_owners": ROADMAP_OWNERS,
            "workstreams": (),
        }
    path = Path(root) / PROJECT_CONFIG_FILE
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = -1
    key = str(path)
    with _PROJECT_CONFIG_LOCK:
        cached = _PROJECT_CONFIG_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    if stamp == -1:
        return project_config(None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BoardError(f"{PROJECT_CONFIG_FILE} is unreadable: {exc}") from exc
    value = _validated_project_config(raw)
    identities = frozenset(value["identities"])
    participants = identities | frozenset(value["message_participants"])
    sets = {
        "identities": identities,
        "message_senders": participants,
        "message_recipients": participants,
        "roadmap_owners": identities | frozenset(value["roadmap_owners"]),
        "workstreams": tuple(value["workstreams"]),
    }
    with _PROJECT_CONFIG_LOCK:
        _PROJECT_CONFIG_CACHE[key] = (stamp, sets)
    return sets


def require_identity(identity: str, root: Path | None = None) -> str:
    allowed_set = project_config(root)["identities"]
    if identity not in allowed_set:
        allowed = ", ".join(sorted(allowed_set))
        raise BoardError(f"unauthorized identity {identity!r}; allowed: {allowed}")
    return identity


def require_message_sender(identity: str, root: Path | None = None) -> str:
    allowed_set = project_config(root)["message_senders"]
    if identity not in allowed_set:
        allowed = ", ".join(sorted(allowed_set))
        raise BoardError(
            f"unauthorized identity {identity!r} for message sender; allowed: {allowed}"
        )
    return identity


def require_message_recipient(identity: str, root: Path | None = None) -> str:
    allowed_set = project_config(root)["message_recipients"]
    if identity not in allowed_set:
        allowed = ", ".join(sorted(allowed_set))
        raise BoardError(
            f"unauthorized identity {identity!r} for message recipient; allowed: {allowed}"
        )
    return identity
