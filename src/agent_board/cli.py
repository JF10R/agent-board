#!/usr/bin/env python3
"""Shared, file-backed coordination board for masters and human operators.

The runtime lives below ``git rev-parse --git-common-dir`` and is therefore
shared by every worktree while remaining outside the versioned working tree.
Only ``sol-master`` and ``claude-master`` may mutate operational board state.
``lead`` and ``operator`` are message participants: they may send, receive and
acknowledge messages, but cannot mutate status, claims or roadmap state.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
import uuid


IDENTITIES = frozenset({"sol-master", "claude-master"})
MESSAGE_SENDERS = IDENTITIES | frozenset({"lead", "operator"})
MESSAGE_RECIPIENTS = MESSAGE_SENDERS
# One store = one project. Who may act on it is data, not a hard-coded list: project.v1.json in the
# store carries it, seeded on init with a neutral default vocabulary.
PROJECT_CONFIG_FILE = "project.v1.json"
PROJECT_CONFIG_KEYS = ("identities", "message_participants", "roadmap_owners", "workstreams")
DEFAULT_PROJECT_CONFIG = {
    "identities": ["master"],
    "message_participants": ["lead", "operator"],
    "roadmap_owners": ["shared", "unassigned"],
    "workstreams": [],
}
_PROJECT_CONFIG_CACHE: dict[str, tuple[int, dict[str, Any]]] = {}
_PROJECT_CONFIG_LOCK = threading.Lock()
KINDS = frozenset(
    {
        "STATUS",
        "QUESTION",
        "ANSWER",
        "BLOCKER",
        "PROPOSAL",
        "DECISION",
        "HANDOFF",
        "ALERT",
    }
)
PRIORITIES = frozenset({"LOW", "NORMAL", "HIGH", "CRITICAL"})
STATUS_STATES = frozenset({"ACTIVE", "IDLE", "BLOCKED", "OFFLINE"})
ROADMAP_STATUSES = frozenset(
    {
        # canonical (docs/roadmap/STATUS-VOCABULARY.md)
        "NOT_STARTED",
        "IN_PROGRESS",
        "READY",
        "CLOSED",
        "BLOCKED",
        # legacy, accepted for backward compatibility only; new writes use the canonical set above
        "PENDING",
        "COMPLETE",
    }
)
ROADMAP_OWNERS = frozenset({"sol-master", "claude-master", "shared", "unassigned"})
ROADMAP_SCHEMA_VERSION = 1
MESSAGE_FIELDS = (
    "id",
    "from",
    "to",
    "kind",
    "priority",
    "workstream",
    "reply_to",
    "requires_ack",
    "created_at",
    "summary",
)
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
STATE_MESSAGE_LIMIT = 200
MAX_MESSAGE_FILE_BYTES = 128 * 1024
MAX_MESSAGE_FRONT_MATTER_BYTES = 16 * 1024
MAX_STATE_JSON_FILE_BYTES = 64 * 1024
MESSAGE_STATE_CACHE_ROOTS = 8
STATUS_FIELDS = frozenset(
    {"identity", "state", "workstream", "head", "paths", "summary", "updated_at"}
)


class _OversizedRuntimeFile(RuntimeError):
    pass


_MESSAGE_STATE_CACHE_LOCK = threading.Lock()
_MESSAGE_STATE_CACHE: dict[
    str, dict[str, tuple[tuple[int, ...], str, datetime | None, dict[str, Any] | None]]
] = {}


class BoardError(RuntimeError):
    """Expected board or command contract failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_identity(identity: str, root: Path | None = None) -> str:
    allowed_set = project_config(root)["identities"]
    if identity not in allowed_set:
        allowed = ", ".join(sorted(allowed_set))
        raise BoardError(f"unauthorized identity {identity!r}; allowed: {allowed}")
    return identity


def _require_message_sender(identity: str, root: Path | None = None) -> str:
    allowed_set = project_config(root)["message_senders"]
    if identity not in allowed_set:
        allowed = ", ".join(sorted(allowed_set))
        raise BoardError(f"unauthorized identity {identity!r} for message sender; allowed: {allowed}")
    return identity


def _require_message_recipient(identity: str, root: Path | None = None) -> str:
    allowed_set = project_config(root)["message_recipients"]
    if identity not in allowed_set:
        allowed = ", ".join(sorted(allowed_set))
        raise BoardError(
            f"unauthorized identity {identity!r} for message recipient; allowed: {allowed}"
        )
    return identity


# A summary is the one line a reader sees in a listing. Past 300 characters it
# stops being a summary and becomes the body pasted into the index, which is
# what it exists to spare the reader. Enforced here rather than written down:
# a convention I can forget is not a bound, and this one was forgotten.
MAX_SUMMARY_CHARS = 300


def _require_summary(value: str) -> str:
    summary = _require_text("summary", value)
    if len(summary) > MAX_SUMMARY_CHARS:
        raise BoardError(
            f"summary is {len(summary)} characters, over the "
            f"{MAX_SUMMARY_CHARS}-character cap by {len(summary) - MAX_SUMMARY_CHARS}. "
            "Put the detail in --body-file and keep the summary to the one line a "
            "reader needs to decide whether to open the message."
        )
    return summary


def _require_text(label: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise BoardError(f"{label} must not be empty")
    if "\n" in value or "\r" in value:
        raise BoardError(f"{label} must be one line")
    return value


def _require_safe_token(label: str, value: str) -> str:
    if not SAFE_TOKEN.fullmatch(value):
        raise BoardError(f"invalid {label} {value!r}")
    return value


def discover_git_common_dir(repo: Path | str | None = None) -> Path:
    """Resolve Git's shared administrative directory for the selected worktree."""

    cwd = Path(repo or Path.cwd()).resolve()
    completed = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        raise BoardError(f"cannot resolve git common dir from {cwd}: {detail}")
    raw = completed.stdout.strip()
    if not raw:
        raise BoardError("git returned an empty common directory")
    common = Path(raw)
    if not common.is_absolute():
        common = cwd / common
    return common.resolve()


# One store per repo, git-local, under this fixed directory name.
STORE_DIRECTORY = "agent-board"


def board_root(repo: Path | str | None = None) -> Path:
    return discover_git_common_dir(repo) / STORE_DIRECTORY


def initialize(root: Path) -> Path:
    for name in ("messages", "acks", "status", "locks", "ticket-events", "leases"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def default_project_config() -> dict[str, Any]:
    """Seed values only: every new store starts with the same neutral vocabulary."""

    return {key: list(DEFAULT_PROJECT_CONFIG[key]) for key in PROJECT_CONFIG_KEYS}


def seed_project_config(root: Path) -> Path | None:
    """Written by `init` only. A store without the file keeps the historical vocabulary, unchanged."""

    path = Path(root) / PROJECT_CONFIG_FILE
    if path.exists():
        return None
    _write_exclusive(path, _canonical_json(default_project_config()))
    return path


def _validated_project_config(raw: Any) -> dict[str, Any]:
    defaults = default_project_config()
    if not isinstance(raw, dict):
        raise BoardError(f"{PROJECT_CONFIG_FILE} must be a JSON object")
    value: dict[str, Any] = {}
    for key in PROJECT_CONFIG_KEYS:
        entries = raw.get(key, defaults[key])
        if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
            raise BoardError(f"{PROJECT_CONFIG_FILE}: {key} must be a list of strings")
        value[key] = [_require_safe_token(f"{key} entry", entry) for entry in entries]
    if not value["identities"]:
        raise BoardError(f"{PROJECT_CONFIG_FILE}: identities must not be empty")
    return value


def project_config(root: Path | None) -> dict[str, Any]:
    """The project's vocabulary, as frozensets. root None = the historical default identities (back-compatible)."""

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
    if stamp == -1:  # no file: the historical vocabulary, so an existing store never changes meaning
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


def _store_root_of(path: Path) -> Path:
    """messages/<id>.md, status/<actor>.json … all sit one directory below the store root."""

    return path.parent.parent


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and getattr(file_stat, "st_file_attributes", 0) & flag)


def _runtime_directory(directory: Path) -> Path:
    try:
        directory_stat = directory.lstat()
    except OSError as exc:
        raise BoardError(f"runtime directory is unavailable: {directory}") from exc
    if (
        stat.S_ISLNK(directory_stat.st_mode)
        or _is_reparse_point(directory_stat)
        or not stat.S_ISDIR(directory_stat.st_mode)
    ):
        raise BoardError(f"runtime directory must be a direct regular directory: {directory}")
    try:
        resolved = directory.resolve(strict=True)
        root_resolved = directory.parent.resolve(strict=True)
    except OSError as exc:
        raise BoardError(f"runtime directory containment cannot be verified: {directory}") from exc
    if resolved.parent != root_resolved or resolved.name != directory.name:
        raise BoardError(f"runtime directory escapes the board root: {directory}")
    return resolved


def _runtime_file_signature(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        int(file_stat.st_mode),
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
        int(getattr(file_stat, "st_file_attributes", 0)),
        int(file_stat.st_nlink),
    )


def _same_opened_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
        and before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and getattr(before, "st_file_attributes", 0)
        == getattr(after, "st_file_attributes", 0)
        and before.st_nlink == after.st_nlink
    )


@contextmanager
def _open_regular_runtime_file(
    path: Path,
    directory_resolved: Path,
    *,
    expected_stat: os.stat_result | None = None,
) -> Iterable[Any]:
    """Open a contained single-link regular file and detect replacement races.

    The single-link check detects a live extra hardlink, but no portable API can
    prove historical hardlink provenance after the other name is removed. This
    limitation is especially relevant on Windows; the runtime directory remains
    the trust boundary.
    """

    try:
        before = expected_stat or path.lstat()
        # CPython's Windows DirEntry cache can omit file IDs/link counts; use a
        # real lstat only for new/changed cache entries before opening them.
        if before.st_ino == 0 or before.st_nlink == 0:
            before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse_point(before)
            or not stat.S_ISREG(before.st_mode)
        ):
            raise BoardError(f"runtime entry is not a regular file: {path.name}")
        if before.st_nlink != 1:
            raise BoardError(f"runtime entry has multiple hardlinks: {path.name}")
        parent_resolved = (
            path.parent
            if path.parent == directory_resolved
            else path.parent.resolve(strict=True)
        )
        if parent_resolved != directory_resolved:
            raise BoardError(f"runtime entry escapes its directory: {path.name}")
        with path.open("rb") as handle:
            after = os.fstat(handle.fileno())
            if (
                stat.S_ISLNK(after.st_mode)
                or _is_reparse_point(after)
                or not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or not _same_opened_file(before, after)
            ):
                raise BoardError(f"runtime entry changed while opening: {path.name}")
            yield handle
    except FileNotFoundError as exc:
        raise BoardError(f"runtime entry disappeared: {path.name}") from exc


def _read_bounded_runtime_bytes(
    path: Path,
    directory_resolved: Path,
    maximum: int,
    *,
    expected_stat: os.stat_result | None = None,
) -> bytes:
    with _open_regular_runtime_file(
        path, directory_resolved, expected_stat=expected_stat
    ) as handle:
        encoded = handle.read(maximum + 1)
    if len(encoded) > maximum:
        raise _OversizedRuntimeFile(path.name)
    return encoded


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BoardError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _read_bounded_runtime_json(
    path: Path, directory_resolved: Path, *, expected_stat: os.stat_result | None = None
) -> Any:
    encoded = _read_bounded_runtime_bytes(
        path,
        directory_resolved,
        MAX_STATE_JSON_FILE_BYTES,
        expected_stat=expected_stat,
    )
    try:
        return json.loads(
            encoded.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoardError(f"invalid runtime JSON: {path.name}") from exc


@contextmanager
def _file_lock(path: Path, timeout_seconds: float = 5.0) -> Iterable[None]:
    """Hold a one-byte OS lock until the protected filesystem operation ends."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as initializer:
            initializer.write(b"0")
            initializer.flush()
    except FileExistsError:
        pass
    deadline = time.monotonic() + timeout_seconds
    handle = None
    locked = False
    try:
        while not locked:
            candidate = None
            try:
                candidate = path.open("r+b")
                if path.stat().st_size < 1:
                    raise PermissionError("lock sentinel is still initializing")
                candidate.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(candidate.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle = candidate
                candidate = None
                locked = True
            except OSError as exc:
                if candidate is not None:
                    candidate.close()
                if time.monotonic() >= deadline:
                    raise BoardError(f"timed out waiting for filesystem lock: {path.name}") from exc
                time.sleep(0.01)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if handle is not None:
            handle.close()


def _exclusive_lock_path(path: Path) -> Path:
    """Map a runtime target (``<root>/<subdir>/<name>``) to its lock file.

    Locks live in a dedicated ``<root>/locks/`` directory instead of beside
    their target so ``messages/`` (opened directly in Explorer) never
    accumulates one dotfile per message ever written.
    """

    root = path.parent.parent
    flattened = f"{path.parent.name}--{path.name}.lock"
    return root / "locks" / flattened


def _write_exclusive(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(_exclusive_lock_path(path)):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Hard-link publication is atomic and create-exclusive. The final
            # path cannot expose the temporary file until every byte is durable.
            os.link(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _write_atomic_replace(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(12):
            try:
                os.replace(temporary_name, path)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                # Windows can briefly deny replacement while another reader or
                # replacer closes the destination handle. Keep the operation
                # bounded and preserve the same atomic replace primitive.
                time.sleep(min(0.002 * (2**attempt), 0.05))
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _new_message_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"


def _message_markdown(metadata: Mapping[str, Any], body: str) -> str:
    lines = ["---"]
    for field in (*MESSAGE_FIELDS, *(key for key in ("ticket_id",) if key in metadata)):
        lines.append(
            f"{field}: {json.dumps(metadata[field], ensure_ascii=False, separators=(',', ':'))}"
        )
    lines.extend(("---", "", body.rstrip(), ""))
    return "\n".join(lines)


def _parse_message_metadata_lines(lines: Sequence[str]) -> tuple[dict[str, Any], int]:
    if not lines or lines[0] != "---":
        raise BoardError("message is missing metadata front matter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise BoardError("message metadata is not closed") from exc
    metadata: dict[str, Any] = {}
    for line in lines[1:closing]:
        key, separator, raw_value = line.partition(": ")
        if not separator:
            raise BoardError(f"invalid metadata line: {line!r}")
        if key not in (*MESSAGE_FIELDS, "ticket_id"):
            raise BoardError(f"unknown message metadata field: {key}")
        if key in metadata:
            raise BoardError(f"duplicate message metadata field: {key}")
        try:
            metadata[key] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise BoardError(f"invalid metadata value for {key}") from exc
    missing = [field for field in MESSAGE_FIELDS if field not in metadata]
    if missing:
        raise BoardError(f"message is missing metadata: {', '.join(missing)}")
    return metadata, closing


def parse_message(content: str) -> tuple[dict[str, Any], str]:
    lines = content.splitlines()
    metadata, closing = _parse_message_metadata_lines(lines)
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")
    return metadata, body


def _read_message_metadata(
    path: Path,
    directory_resolved: Path | None = None,
    *,
    expected_stat: os.stat_result | None = None,
) -> dict[str, Any]:
    """Read only bounded front matter; message bodies are detail-route data."""

    directory_resolved = directory_resolved or _runtime_directory(path.parent)
    lines: list[str] = []
    consumed = 0
    with _open_regular_runtime_file(
        path, directory_resolved, expected_stat=expected_stat
    ) as handle:
        if os.fstat(handle.fileno()).st_size > MAX_MESSAGE_FILE_BYTES:
            raise _OversizedRuntimeFile(path.name)
        while consumed <= MAX_MESSAGE_FRONT_MATTER_BYTES:
            raw_line = handle.readline(MAX_MESSAGE_FRONT_MATTER_BYTES - consumed + 1)
            if not raw_line:
                break
            consumed += len(raw_line)
            if consumed > MAX_MESSAGE_FRONT_MATTER_BYTES:
                raise BoardError("message metadata exceeds the front matter limit")
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise BoardError("message metadata is not valid UTF-8") from exc
            lines.append(line)
            if len(lines) > 1 and line == "---":
                break
    metadata, _ = _parse_message_metadata_lines(lines)
    return metadata


def _validate_state_message(path: Path, metadata: Mapping[str, Any]) -> datetime:
    string_fields = (
        "id",
        "from",
        "to",
        "kind",
        "priority",
        "workstream",
        "created_at",
        "summary",
    )
    if any(not isinstance(metadata.get(field), str) for field in string_fields):
        raise BoardError("message metadata has invalid field types")
    message_id = _require_safe_token("message id", metadata["id"])
    if message_id != path.stem:
        raise BoardError("message id does not match its filename")
    _require_message_sender(metadata["from"], _store_root_of(path))
    _require_message_recipient(metadata["to"], _store_root_of(path))
    if metadata["kind"] not in KINDS or metadata["priority"] not in PRIORITIES:
        raise BoardError("message kind or priority is invalid")
    if not isinstance(metadata.get("requires_ack"), bool):
        raise BoardError("message requires_ack must be boolean")
    ticket_id = metadata.get("ticket_id")
    if ticket_id is not None:
        _require_safe_token("ticket_id", ticket_id)
    reply_to = metadata.get("reply_to")
    if reply_to is not None:
        if not isinstance(reply_to, str):
            raise BoardError("message reply_to must be a string or null")
        _require_safe_token("reply_to", reply_to)
    try:
        created_at = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise BoardError("message created_at is invalid") from exc
    if created_at.tzinfo is None:
        raise BoardError("message created_at must include a timezone")
    return created_at.astimezone(timezone.utc)


def list_recent_messages(
    root: Path, *, limit: int = STATE_MESSAGE_LIMIT
) -> tuple[list[dict[str, Any]], dict[str, int | bool]]:
    """Return one bounded state page from one directory scan.

    Every candidate's small front matter is inspected once so ordering and
    quarantine counts are exact. Bodies remain unread until the detail route.
    """

    initialize(root)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise BoardError("message state limit must be a positive integer")
    directory_resolved = _runtime_directory(root / "messages")
    directory_stat = directory_resolved.stat()
    cache_key = (
        f"{directory_resolved}|{directory_stat.st_dev}:"
        f"{directory_stat.st_ino}"
    )
    valid: list[tuple[datetime, str, dict[str, Any]]] = []
    malformed = 0
    oversized = 0
    with _MESSAGE_STATE_CACHE_LOCK:
        old_cache = _MESSAGE_STATE_CACHE.pop(cache_key, {})
        new_cache: dict[
            str, tuple[tuple[int, ...], str, datetime | None, dict[str, Any] | None]
        ] = {}
        try:
            entries = sorted(
                (
                    entry
                    for entry in os.scandir(directory_resolved)
                    if entry.name.endswith(".md")
                ),
                key=lambda entry: entry.name,
            )
        except OSError as exc:
            raise BoardError("message directory cannot be scanned") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
                signature = _runtime_file_signature(entry_stat)
            except OSError:
                malformed += 1
                continue
            cached = old_cache.get(entry.name)
            if cached is not None and cached[0] == signature:
                record = cached
            else:
                try:
                    metadata = _read_message_metadata(
                        path,
                        directory_resolved,
                        expected_stat=entry_stat,
                    )
                    created_at = _validate_state_message(path, metadata)
                    record = (
                        signature,
                        "valid",
                        created_at,
                        dict(metadata),
                    )
                except _OversizedRuntimeFile:
                    record = (signature, "oversized", None, None)
                except (BoardError, OSError, UnicodeError, ValueError, TypeError):
                    record = (signature, "malformed", None, None)
            new_cache[entry.name] = record
            _, category, created_at, message = record
            if category == "oversized":
                oversized += 1
                continue
            if category == "malformed" or created_at is None or message is None:
                malformed += 1
                continue
            valid.append((created_at, message["id"], message))
        _MESSAGE_STATE_CACHE[cache_key] = new_cache
        while len(_MESSAGE_STATE_CACHE) > MESSAGE_STATE_CACHE_ROOTS:
            del _MESSAGE_STATE_CACHE[next(iter(_MESSAGE_STATE_CACHE))]
    # Preserve the v1 tie order: recipient groups and filenames were ascending
    # before the stable created_at-descending sort.
    valid.sort(key=lambda item: (item[2]["to"], item[1]))
    valid.sort(key=lambda item: item[0], reverse=True)
    ack_directory = _runtime_directory(root / "acks")
    try:
        ack_names = {
            entry.name
            for entry in os.scandir(ack_directory)
            if entry.name.endswith(".json")
        }
    except OSError as exc:
        raise BoardError("ack directory cannot be scanned") from exc
    messages = [
        {
            **item[2],
            "acked": f"{item[2]['id']}--{item[2]['to']}.json" in ack_names,
        }
        for item in valid[:limit]
    ]
    total = len(valid)
    return messages, {
        "total": total,
        "returned": len(messages),
        "has_more": total > len(messages),
        "malformed": malformed,
        "oversized": oversized,
    }


def _message_path(root: Path, message_id: str) -> Path:
    return root / "messages" / f"{_require_safe_token('message id', message_id)}.md"


def post_message(
    root: Path,
    *,
    sender: str,
    recipient: str,
    kind: str,
    priority: str,
    workstream: str,
    summary: str,
    body: str = "",
    reply_to: str | None = None,
    ticket_id: str | None = None,
    requires_ack: bool = False,
    message_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    initialize(root)
    sender = _require_message_sender(sender, root)
    recipient = _require_message_recipient(recipient, root)
    kind = kind.upper()
    priority = priority.upper()
    if kind not in KINDS:
        raise BoardError(f"invalid kind {kind!r}")
    if priority not in PRIORITIES:
        raise BoardError(f"invalid priority {priority!r}")
    workstream = _require_text("workstream", workstream)
    summary = _require_summary(summary)
    if reply_to is not None:
        _require_safe_token("reply_to", reply_to)
        if not _message_path(root, reply_to).is_file():
            raise BoardError(f"reply target does not exist: {reply_to}")
    if ticket_id is not None:
        from agent_board import tickets
        ticket_id = _require_safe_token("ticket_id", ticket_id)
        tickets.get_ticket(root, ticket_id)
    resolved_id = _require_safe_token("message id", message_id or _new_message_id())
    metadata: dict[str, Any] = {
        "id": resolved_id,
        "from": sender,
        "to": recipient,
        "kind": kind,
        "priority": priority,
        "workstream": workstream,
        "reply_to": reply_to,
        "requires_ack": bool(requires_ack),
        "created_at": created_at or utc_now(),
        "summary": summary,
    }
    if ticket_id is not None:
        metadata["ticket_id"] = ticket_id
    try:
        _write_exclusive(_message_path(root, resolved_id), _message_markdown(metadata, body))
    except FileExistsError as exc:
        raise BoardError(f"message already exists and is immutable: {resolved_id}") from exc
    return metadata


def read_message(root: Path, message_id: str) -> tuple[dict[str, Any], str, str]:
    path = _message_path(root, message_id)
    try:
        encoded = _read_bounded_runtime_bytes(
            path, _runtime_directory(root / "messages"), MAX_MESSAGE_FILE_BYTES
        )
    except _OversizedRuntimeFile as exc:
        raise BoardError(f"message exceeds {MAX_MESSAGE_FILE_BYTES} bytes: {message_id}") from exc
    except BoardError as exc:
        raise BoardError(f"unknown or invalid message: {message_id}") from exc
    try:
        raw = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoardError(f"message is not valid UTF-8: {message_id}") from exc
    metadata, body = parse_message(raw)
    _validate_state_message(path, metadata)
    return metadata, body, raw


def _ack_path(root: Path, message_id: str, actor: str) -> Path:
    _require_safe_token("message id", message_id)
    _require_message_recipient(actor, root)
    return root / "acks" / f"{message_id}--{actor}.json"


def acknowledge(root: Path, *, actor: str, message_id: str) -> dict[str, Any]:
    initialize(root)
    actor = _require_message_recipient(actor, root)
    message, _, _ = read_message(root, message_id)
    if message["to"] != actor:
        raise BoardError(f"{actor} cannot acknowledge a message addressed to {message['to']}")
    ack = {"message_id": message_id, "by": actor, "acked_at": utc_now()}
    try:
        _write_exclusive(_ack_path(root, message_id, actor), _canonical_json(ack))
    except FileExistsError as exc:
        raise BoardError(f"message already acknowledged by {actor}: {message_id}") from exc
    return ack


def inbox(root: Path, *, actor: str, pending_ack_only: bool = False) -> list[dict[str, Any]]:
    initialize(root)
    actor = _require_message_recipient(actor, root)
    result: list[dict[str, Any]] = []
    for path in sorted((root / "messages").glob("*.md")):
        metadata, _ = parse_message(path.read_text(encoding="utf-8"))
        if metadata["to"] != actor:
            continue
        acked = _ack_path(root, metadata["id"], actor).is_file()
        if pending_ack_only and (not metadata["requires_ack"] or acked):
            continue
        result.append({**metadata, "acked": acked})
    return result


def publish_status(
    root: Path,
    *,
    actor: str,
    state: str,
    summary: str,
    workstream: str = "",
    head: str = "",
    paths: Sequence[str] = (),
) -> dict[str, Any]:
    initialize(root)
    actor = _require_identity(actor, root)
    state = state.upper()
    if state not in STATUS_STATES:
        raise BoardError(f"invalid status state {state!r}")
    status = {
        "identity": actor,
        "state": state,
        "workstream": workstream.strip(),
        "head": head.strip(),
        "paths": list(paths),
        "summary": _require_summary(summary),
        "updated_at": utc_now(),
    }
    _write_atomic_replace(root / "status" / f"{actor}.json", _canonical_json(status))
    return status


def _validate_status_file(path: Path, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != STATUS_FIELDS:
        raise BoardError("status JSON has an invalid shape")
    if value.get("identity") != path.stem:
        raise BoardError("status identity does not match its filename")
    _require_identity(value["identity"], _store_root_of(path))
    if value.get("state") not in STATUS_STATES:
        raise BoardError("status state is invalid")
    for field in ("workstream", "head", "summary", "updated_at"):
        if not isinstance(value.get(field), str):
            raise BoardError(f"status {field} must be a string")
    paths = value.get("paths")
    if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
        raise BoardError("status paths must be a string array")
    return value


def list_status(root: Path, actor: str | None = None) -> list[dict[str, Any]]:
    initialize(root)
    directory = root / "status"
    directory_resolved = _runtime_directory(directory)
    if actor is not None:
        _require_identity(actor, root)
        paths: Iterable[Path] = (directory / f"{actor}.json",)
    else:
        try:
            paths = [
                Path(entry.path)
                for entry in sorted(os.scandir(directory_resolved), key=lambda item: item.name)
                if entry.name.endswith(".json")
            ]
        except OSError as exc:
            raise BoardError("status directory cannot be scanned") from exc
    result = []
    for path in paths:
        try:
            value = _read_bounded_runtime_json(path, directory_resolved)
            result.append(_validate_status_file(path, value))
        except (_OversizedRuntimeFile, BoardError, OSError, ValueError, TypeError):
            continue
    return result


_LEGACY_LOCK_DIRECTORIES = ("messages", "acks", "status")


def remove_stale_locks(root: Path) -> dict[str, int]:
    """One-shot maintenance: delete legacy dotfile locks left beside their
    target from before locks moved into ``locks/`` (2026-09-05). A lock is
    deleted only when it can be acquired non-blocking first; a lock another
    process currently holds is left in place. Idempotent: a directory with
    no legacy dotfiles reports zero removed.
    """

    initialize(root)
    removed = 0
    kept = 0
    for subdir in _LEGACY_LOCK_DIRECTORIES:
        directory = root / subdir
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if not (name.startswith(".") and name.endswith(".lock")):
                continue
            path = Path(entry.path)
            try:
                handle = path.open("r+b")
            except OSError:
                continue
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                kept += 1
                continue
            finally:
                handle.close()
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
    return {"removed": removed, "kept": kept}


def _roadmap_path(root: Path) -> Path:
    return root / "roadmap.v1.json"


def _validate_roadmap_item(item: Mapping[str, Any], root: Path | None = None) -> dict[str, Any]:
    required = {
        "id",
        "title",
        "summary",
        "status",
        "owner",
        "progress",
        "blocker",
        "updated_at",
        "revision",
    }
    if set(item) != required:
        missing = sorted(required - set(item))
        extra = sorted(set(item) - required)
        detail = []
        if missing:
            detail.append(f"missing: {', '.join(missing)}")
        if extra:
            detail.append(f"unknown: {', '.join(extra)}")
        raise BoardError(f"invalid roadmap item fields ({'; '.join(detail)})")
    for field in ("id", "title", "summary", "status", "owner"):
        if not isinstance(item[field], str):
            raise BoardError(f"roadmap {field} must be a string")
    item_id = _require_safe_token("roadmap item id", item["id"])
    title = _require_text("roadmap title", item["title"])
    if len(title) > 200:
        raise BoardError("roadmap title exceeds 200 characters")
    summary = item["summary"].strip()
    if not summary:
        raise BoardError("roadmap summary must not be empty")
    if len(summary) > 2000:
        raise BoardError("roadmap summary exceeds 2000 characters")
    status = item["status"].upper()
    if status not in ROADMAP_STATUSES:
        raise BoardError(f"invalid roadmap status {status!r}")
    owner = item["owner"]
    if owner not in project_config(root)["roadmap_owners"]:
        raise BoardError(f"invalid roadmap owner {owner!r}")
    progress = item["progress"]
    if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
        raise BoardError("roadmap progress must be an integer from 0 through 100")
    blocker = item["blocker"]
    if not isinstance(blocker, str):
        raise BoardError("roadmap blocker must be a string")
    if len(blocker) > 2000:
        raise BoardError("roadmap blocker exceeds 2000 characters")
    blocker = blocker.strip()
    if status == "BLOCKED" and not blocker:
        raise BoardError("BLOCKED roadmap items require a blocker")
    if status != "BLOCKED" and blocker:
        raise BoardError("only BLOCKED roadmap items may have a blocker")
    if status in ("COMPLETE", "CLOSED") and progress != 100:
        raise BoardError(f"{status} roadmap items require progress 100")
    if progress == 100 and status not in ("COMPLETE", "CLOSED"):
        raise BoardError("roadmap progress 100 requires status COMPLETE or CLOSED")
    updated_at = item["updated_at"]
    if not isinstance(updated_at, str) or not updated_at:
        raise BoardError("roadmap updated_at must be a non-empty string")
    revision = item["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise BoardError("roadmap revision must be a positive integer")
    return {
        "id": item_id,
        "title": title,
        "summary": summary,
        "status": status,
        "owner": owner,
        "progress": progress,
        "blocker": blocker,
        "updated_at": updated_at,
        "revision": revision,
    }


def _read_roadmap_store(root: Path, *, migrate_legacy: bool = False) -> dict[str, Any]:
    path = _roadmap_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": ROADMAP_SCHEMA_VERSION, "items": []}
    if not isinstance(value, dict) or set(value) != {"schema_version", "items"}:
        raise BoardError("invalid roadmap store shape")
    if value["schema_version"] != ROADMAP_SCHEMA_VERSION:
        raise BoardError(f"unsupported roadmap schema version {value['schema_version']!r}")
    if not isinstance(value["items"], list):
        raise BoardError("roadmap items must be a list")
    items = []
    changed = False
    for raw_item in value["items"]:
        if not isinstance(raw_item, dict):
            raise BoardError("roadmap items must be objects")
        try:
            item = _validate_roadmap_item(raw_item, root)
        except BoardError:
            blocker = raw_item.get("blocker")
            status = raw_item.get("status")
            revision = raw_item.get("revision")
            if not (
                migrate_legacy
                and isinstance(blocker, str)
                and blocker.strip()
                and isinstance(status, str)
                and status.upper() != "BLOCKED"
                and isinstance(revision, int)
                and not isinstance(revision, bool)
                and revision >= 1
            ):
                raise
            migrated = dict(raw_item)
            migrated["status"] = "BLOCKED"
            migrated["revision"] = revision + 1
            migrated["updated_at"] = utc_now()
            item = _validate_roadmap_item(migrated, root)
            changed = True
        items.append(item)
    if len({item["id"] for item in items}) != len(items):
        raise BoardError("roadmap item ids must be unique")
    store = {"schema_version": ROADMAP_SCHEMA_VERSION, "items": items}
    if changed:
        _write_atomic_replace(path, _canonical_json(store))
    return store


@contextmanager
def _roadmap_lock(root: Path, timeout_seconds: float = 5.0) -> Iterable[None]:
    """Serialize roadmap read/check/write across threads and processes."""

    initialize(root)
    with _file_lock(root / ".roadmap.lock", timeout_seconds):
        yield


def list_roadmap(root: Path) -> list[dict[str, Any]]:
    initialize(root)
    with _roadmap_lock(root):
        return sorted(
            _read_roadmap_store(root, migrate_legacy=True)["items"],
            key=lambda item: item["id"],
        )


def get_roadmap_item(root: Path, item_id: str) -> dict[str, Any]:
    item_id = _require_safe_token("roadmap item id", item_id)
    for item in list_roadmap(root):
        if item["id"] == item_id:
            return item
    raise BoardError(f"unknown roadmap item: {item_id}")


def upsert_roadmap_item(
    root: Path,
    *,
    actor: str,
    item_id: str,
    title: str,
    summary: str,
    status: str,
    owner: str,
    progress: int,
    blocker: str = "",
    expected_revision: int,
) -> dict[str, Any]:
    _require_identity(actor, root)
    item_id = _require_safe_token("roadmap item id", item_id)
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise BoardError("expected revision must be a non-negative integer")
    with _roadmap_lock(root):
        store = _read_roadmap_store(root, migrate_legacy=True)
        existing = next((item for item in store["items"] if item["id"] == item_id), None)
        current_revision = existing["revision"] if existing else 0
        if expected_revision != current_revision:
            raise BoardError(
                f"roadmap revision conflict for {item_id}: expected {expected_revision}, "
                f"current {current_revision}"
            )
        item = _validate_roadmap_item(
            {
                "id": item_id,
                "title": title,
                "summary": summary,
                "status": status,
                "owner": owner,
                "progress": progress,
                "blocker": blocker,
                "updated_at": utc_now(),
                "revision": current_revision + 1,
            },
            root,
        )
        store["items"] = [candidate for candidate in store["items"] if candidate["id"] != item_id]
        store["items"].append(item)
        store["items"].sort(key=lambda candidate: candidate["id"])
        _write_atomic_replace(_roadmap_path(root), _canonical_json(store))
        return item


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    return args.body or ""


def build_parser(config: Mapping[str, Any] | None = None) -> argparse.ArgumentParser:
    config = config or project_config(None)
    senders = sorted(config["message_senders"])
    recipients = sorted(config["message_recipients"])
    identities = sorted(config["identities"])
    owners = sorted(config["roadmap_owners"])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, help="worktree used to resolve the Git common dir")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create the shared runtime directories")

    maintenance = subparsers.add_parser(
        "maintenance", help="remove stale legacy lock dotfiles left beside their target"
    )
    maintenance.add_argument(
        "--root",
        dest="maintenance_root",
        type=Path,
        default=None,
        help="board root directory (overrides --repo resolution)",
    )

    post = subparsers.add_parser("post", help="create an immutable Markdown message")
    post.add_argument("--from", dest="sender", required=True, choices=senders)
    post.add_argument("--to", dest="recipient", required=True, choices=recipients)
    post.add_argument("--kind", required=True, choices=sorted(KINDS))
    post.add_argument("--priority", default="NORMAL", choices=sorted(PRIORITIES))
    post.add_argument("--workstream", required=True)
    post.add_argument("--summary", required=True)
    body_group = post.add_mutually_exclusive_group()
    body_group.add_argument("--body")
    body_group.add_argument("--body-file", type=Path)
    post.add_argument("--reply-to")
    post.add_argument("--ticket-id", help="existing ticket in this project")
    post.add_argument("--requires-ack", action="store_true")

    inbox_parser = subparsers.add_parser("inbox", help="list messages addressed to one participant")
    inbox_parser.add_argument("--actor", required=True, choices=recipients)
    inbox_parser.add_argument("--pending-ack", action="store_true")

    read = subparsers.add_parser("read", help="print one immutable Markdown message")
    read.add_argument("id")

    ack = subparsers.add_parser("ack", help="acknowledge a message in a separate file")
    ack.add_argument("id")
    ack.add_argument("--actor", required=True, choices=recipients)

    status = subparsers.add_parser("status", help="publish or list master status")
    status.add_argument("--actor", choices=identities)
    status.add_argument("--state", choices=sorted(STATUS_STATES))
    status.add_argument("--summary")
    status.add_argument("--workstream", default="")
    status.add_argument("--head", default="")
    status.add_argument("--path", action="append", default=[])

    _add_ticket_subparsers(subparsers, identities)

    actor_cmd = subparsers.add_parser("actor", help="register or list per-project actors (roles, master/subagent links)")
    actor_commands = actor_cmd.add_subparsers(dest="actor_command", required=True)
    actor_register = actor_commands.add_parser("register", help="register an actor with a role")
    actor_register.add_argument("name")
    actor_register.add_argument("--role", required=True, choices=[role.lower() for role in _tickets_module().ACTOR_ROLES])
    actor_register.add_argument("--display", default="")
    actor_register.add_argument("--master", help="owning master, for a subagent not named <master>/<name>")
    actor_list = actor_commands.add_parser("list", help="list registered actors")
    actor_list.add_argument("--role", choices=[role.lower() for role in _tickets_module().ACTOR_ROLES])

    roadmap = subparsers.add_parser("roadmap", help="list, get, or update roadmap items")
    roadmap_commands = roadmap.add_subparsers(dest="roadmap_command", required=True)
    roadmap_commands.add_parser("list", help="list roadmap items")
    roadmap_get = roadmap_commands.add_parser("get", help="get one roadmap item")
    roadmap_get.add_argument("id")
    roadmap_upsert = roadmap_commands.add_parser("upsert", help="create or update one roadmap item")
    roadmap_upsert.add_argument("--actor", required=True, choices=identities)
    roadmap_upsert.add_argument("--id", required=True)
    roadmap_upsert.add_argument("--title", required=True)
    roadmap_upsert.add_argument("--summary", required=True)
    roadmap_upsert.add_argument("--status", required=True, choices=sorted(ROADMAP_STATUSES))
    roadmap_upsert.add_argument("--owner", required=True, choices=owners)
    roadmap_upsert.add_argument("--progress", required=True, type=int)
    roadmap_upsert.add_argument("--blocker", default="")
    roadmap_upsert.add_argument("--expected-revision", required=True, type=int)
    # Additive (2026-09-05): optional sidecar fields; absent = untouched, never invented.
    for target in (roadmap_upsert,):
        _add_roadmap_extension_flags(target)
    # Additive (2026-09-01): tree/annotate read and write the sidecar only; roadmap.v1.json stays frozen.
    roadmap_tree = roadmap_commands.add_parser("tree", help="print the parent/child tree with status, progress, blockers, gates")
    roadmap_tree.add_argument("--root", help="only this item and its descendants")
    roadmap_tree.add_argument("--since", type=float, default=24.0, help="hours; items updated within are marked *")
    roadmap_tree.add_argument("--json", action="store_true", help="print the full tree payload as JSON")
    roadmap_tree.add_argument("--no-journal", action="store_true", help="read-only: do not record new revisions in the sidecar journal")
    roadmap_annotate = roadmap_commands.add_parser("annotate", help="attach parent, blockers, gates or a due date to one item")
    roadmap_annotate.add_argument("--actor", required=True, choices=identities)
    roadmap_annotate.add_argument("--id", required=True)
    parent_group = roadmap_annotate.add_mutually_exclusive_group()
    parent_group.add_argument("--parent", help="parent item id")
    parent_group.add_argument("--no-parent", action="store_true", help="clear the parent link (also overrides the summary convention)")
    roadmap_annotate.add_argument("--add-blocker", action="append", default=[], metavar="TEXT")
    roadmap_annotate.add_argument("--clear-blockers", action="store_true")
    roadmap_annotate.add_argument("--gate", action="append", default=[], metavar="NAME=STATE[:note]", help="state: PASS, FAIL, PENDING, UNKNOWN")
    roadmap_annotate.add_argument("--clear-gates", action="store_true")
    due_group = roadmap_annotate.add_mutually_exclusive_group()
    due_group.add_argument("--due", help="ISO date YYYY-MM-DD")
    due_group.add_argument("--no-due", action="store_true")
    reported_group = roadmap_annotate.add_mutually_exclusive_group()
    reported_group.add_argument("--progress-reported", action="store_true", help="the v1 progress integer is a real report")
    reported_group.add_argument("--progress-unreported", action="store_true", help="the v1 progress integer is a placeholder")
    _add_roadmap_extension_flags(roadmap_annotate)
    return parser


def _add_roadmap_extension_flags(parser: argparse.ArgumentParser) -> None:
    deps = parser.add_mutually_exclusive_group()
    deps.add_argument("--depends-on", action="append", default=[], metavar="ITEM_ID", help="repeatable; replaces the dependency list")
    deps.add_argument("--clear-depends-on", action="store_true")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--kind", choices=("TASK", "MILESTONE", "OBJECTIVE"))
    kind.add_argument("--no-kind", action="store_true")
    impact = parser.add_mutually_exclusive_group()
    impact.add_argument("--impact", metavar="TEXT", help="one line: what this item changes for the programme")
    impact.add_argument("--no-impact", action="store_true")
    standby = parser.add_mutually_exclusive_group()
    standby.add_argument("--standby", metavar="REASON", help="flag an IN_PROGRESS item as deliberately paused")
    standby.add_argument("--no-standby", action="store_true")


def _roadmap_extension_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if args.depends_on:
        kwargs["depends_on"] = args.depends_on
    elif args.clear_depends_on:
        kwargs["depends_on"] = []
    if args.kind is not None:
        kwargs["kind"] = args.kind
    elif args.no_kind:
        kwargs["kind"] = None
    if args.impact is not None:
        kwargs["impact"] = args.impact
    elif args.no_impact:
        kwargs["impact"] = None
    if args.standby is not None:
        kwargs["standby"] = args.standby
    elif args.no_standby:
        kwargs["standby"] = None
    return kwargs


def _tree_module():
    try:
        from agent_board import tree as tree_module
    except ImportError:  # Direct execution from within the package directory.
        import tree as tree_module  # type: ignore[no-redef]
    return tree_module


def _run_roadmap_extension(args: argparse.Namespace, root: Path) -> None:
    tree_module = _tree_module()
    if args.roadmap_command == "tree":
        tree = tree_module.load_roadmap_tree(root, since_hours=args.since, record=not args.no_journal)
        if args.json:
            try:
                from agent_board import derive as derive_module
            except ImportError:  # Direct execution from within the package directory.
                import derive as derive_module  # type: ignore[no-redef]
            _print_json(derive_module.derive_roadmap_views(tree))
        else:
            print(tree_module.render_tree_text(tree, root_id=args.root))
        return
    kwargs: dict[str, Any] = _roadmap_extension_kwargs(args)
    if args.parent is not None:
        kwargs["parent_id"] = args.parent
    elif args.no_parent:
        kwargs["parent_id"] = None
    if args.due is not None:
        kwargs["due"] = args.due
    elif args.no_due:
        kwargs["due"] = None
    if args.progress_reported:
        kwargs["progress_reported"] = True
    elif args.progress_unreported:
        kwargs["progress_reported"] = False
    _print_json(
        tree_module.annotate_roadmap_item(
            root,
            actor=args.actor,
            item_id=args.id,
            add_blockers=args.add_blocker,
            clear_blockers=args.clear_blockers,
            gates=[tree_module.parse_gate_spec(spec) for spec in args.gate],
            clear_gates=args.clear_gates,
            **kwargs,
        )
    )


def _tickets_module():
    try:
        from agent_board import tickets as tickets_module
    except ImportError:  # Direct execution from within the package directory.
        import tickets as tickets_module  # type: ignore[no-redef]
    return tickets_module


def _add_ticket_subparsers(subparsers: argparse._SubParsersAction, identities: Sequence[str]) -> None:
    tickets_module = _tickets_module()
    ticket = subparsers.add_parser("ticket", help="create, list, and drive tickets through their lifecycle")
    commands = ticket.add_subparsers(dest="ticket_command", required=True)

    create = commands.add_parser("create", help="create a new ticket")
    create.add_argument("--actor", required=True, choices=identities)
    create.add_argument("--id", required=True, dest="ticket_id")
    create.add_argument("--title", required=True)
    create.add_argument("--kind", default="ENGINEERING", choices=tickets_module.TICKET_KINDS)
    create.add_argument("--summary", default="")
    body_group = create.add_mutually_exclusive_group()
    body_group.add_argument("--body")
    body_group.add_argument("--body-file", type=Path)
    create.add_argument("--parent")
    create.add_argument("--acceptance-criterion", action="append", default=[], dest="acceptance_criteria")
    create.add_argument("--assignee")
    create.add_argument("--reviewer")
    create.add_argument("--subagent", action="append", default=[], dest="subagents")

    upsert = commands.add_parser("upsert", help="update mutable fields of an existing ticket")
    upsert.add_argument("--actor", required=True, choices=identities)
    upsert.add_argument("--id", required=True, dest="ticket_id")
    upsert.add_argument("--expected-revision", required=True, type=int)
    upsert.add_argument("--title")
    upsert.add_argument("--summary")
    upsert_body = upsert.add_mutually_exclusive_group()
    upsert_body.add_argument("--body")
    upsert_body.add_argument("--body-file", type=Path)
    upsert.add_argument("--kind", choices=tickets_module.TICKET_KINDS)
    upsert.add_argument("--acceptance-criterion", action="append", default=None, dest="acceptance_criteria")
    upsert.add_argument("--reviewer")
    upsert.add_argument("--subagent", action="append", default=None, dest="subagents")

    assign = commands.add_parser("assign", help="self-assign (or reassign) a ticket and open a lease")
    assign.add_argument("--actor", required=True, choices=identities)
    assign.add_argument("--id", required=True, dest="ticket_id")
    assign.add_argument("--assignee", required=True)
    assign.add_argument("--expected-revision", type=int)
    assign.add_argument("--ttl-sec", type=int, default=tickets_module.DEFAULT_LEASE_TTL_SEC)
    assign.add_argument("--reviewer")
    assign.add_argument("--subagent", action="append", default=None, dest="subagents")

    heartbeat = commands.add_parser("heartbeat", help="extend the lease held by the actor")
    heartbeat.add_argument("--actor", required=True)
    heartbeat.add_argument("--id", required=True, dest="ticket_id")
    heartbeat.add_argument("--ttl-sec", type=int)

    transition = commands.add_parser("transition", help="move a ticket to a new stage (not DONE; see done)")
    transition.add_argument("--actor", required=True, choices=identities)
    transition.add_argument("--id", required=True, dest="ticket_id")
    transition.add_argument("--stage", required=True, choices=[s for s in tickets_module.STAGES if s != "DONE"])
    transition.add_argument("--expected-revision", type=int)
    transition.add_argument("--summary", default="")

    comment = commands.add_parser("comment", help="attach a comment to a ticket")
    comment.add_argument("--actor", required=True)
    comment.add_argument("--id", required=True, dest="ticket_id")
    comment.add_argument("--summary", required=True)
    comment_body = comment.add_mutually_exclusive_group()
    comment_body.add_argument("--body")
    comment_body.add_argument("--body-file", type=Path)
    comment.add_argument("--expected-revision", type=int)

    worklog = commands.add_parser("worklog", help="record evidence pointers: commit, test, or artifact")
    worklog.add_argument("--actor", required=True)
    worklog.add_argument("--id", required=True, dest="ticket_id")
    worklog.add_argument("--summary", required=True)
    worklog.add_argument("--repo", dest="evidence_repo")
    worklog.add_argument("--sha")
    worklog.add_argument("--test")
    worklog.add_argument("--exit-code", type=int)
    worklog.add_argument("--artifact")
    worklog.add_argument("--content-hash")
    worklog.add_argument("--expected-revision", type=int)

    review = commands.add_parser("review", help="record an owning-master review verdict")
    review.add_argument("--actor", required=True)
    review.add_argument("--id", required=True, dest="ticket_id")
    review.add_argument("--verdict", required=True, choices=tickets_module.REVIEW_VERDICTS)
    review.add_argument("--summary", required=True)
    review.add_argument("--finding", action="append", default=[], dest="findings")
    review.add_argument("--expected-revision", type=int)

    done = commands.add_parser("done", help="mark a reviewed, unblocked ticket DONE")
    done.add_argument("--actor", required=True, choices=identities)
    done.add_argument("--id", required=True, dest="ticket_id")
    done.add_argument("--summary", default="")
    done.add_argument("--expected-revision", type=int)
    done.add_argument("--force", action="store_true", help="skip the review/blocker gate")

    dep_add = commands.add_parser("dep-add", help="add a typed dependency edge")
    dep_add.add_argument("--actor", required=True, choices=identities)
    dep_add.add_argument("--id", required=True, dest="ticket_id")
    dep_add.add_argument("--type", required=True, dest="dep_type", choices=tickets_module.DEP_TYPES)
    dep_add.add_argument("--target", required=True)

    list_cmd = commands.add_parser("list", help="list tickets")
    list_cmd.add_argument("--stage", choices=tickets_module.STAGES)
    list_cmd.add_argument("--assignee")
    list_cmd.add_argument("--parent")
    list_cmd.add_argument("--include-archived", action="store_true")

    get = commands.add_parser("get", help="print one ticket's current state")
    get.add_argument("id")

    commands.add_parser("tree", help="print the parent/child ticket tree")
    commands.add_parser("critical-path", help="print the longest open BLOCKED_BY chain")
    commands.add_parser("metrics", help="print throughput/time-in-stage/reopen metrics")
    commands.add_parser("wip", help="print work-in-progress by assignee")

    archive = commands.add_parser("archive", help="archive a DONE or CANCELLED ticket")
    archive.add_argument("--actor", required=True, choices=identities)
    archive.add_argument("--id", required=True, dest="ticket_id")

    export = commands.add_parser("export", help="render one ticket as standalone Markdown")
    export.add_argument("id")

    verify = commands.add_parser("verify", help="recompute one ticket's hash chain and report drift")
    verify.add_argument("id")


def _read_ticket_body(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        return Path(args.body_file).read_text(encoding="utf-8")
    return getattr(args, "body", None) or ""


def _ticket_evidence_from_args(args: argparse.Namespace) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    if args.evidence_repo is not None or args.sha is not None:
        evidence["repo"] = args.evidence_repo or ""
        evidence["sha"] = args.sha or ""
    if args.test is not None or args.exit_code is not None:
        evidence["test"] = args.test or ""
        if args.exit_code is not None:
            evidence["exit_code"] = args.exit_code
    if args.artifact is not None or args.content_hash is not None:
        evidence["artifact"] = args.artifact or ""
        evidence["content_hash"] = args.content_hash or ""
    return evidence


def _run_ticket_command(args: argparse.Namespace, root: Path) -> None:
    tickets_module = _tickets_module()
    command = args.ticket_command
    if command == "create":
        _print_json(
            tickets_module.create_ticket(
                root,
                actor=args.actor,
                ticket_id=args.ticket_id,
                title=args.title,
                kind=args.kind,
                summary=args.summary,
                body=_read_ticket_body(args),
                parent_id=args.parent,
                acceptance_criteria=args.acceptance_criteria,
                assignee=args.assignee,
                reviewer=args.reviewer,
                subagents=args.subagents,
            )
        )
    elif command == "upsert":
        fields: dict[str, Any] = {}
        if args.title is not None:
            fields["title"] = args.title
        if args.summary is not None:
            fields["summary"] = args.summary
        if args.body is not None or args.body_file is not None:
            fields["body"] = _read_ticket_body(args)
        if args.kind is not None:
            fields["kind"] = args.kind
        if args.acceptance_criteria is not None:
            fields["acceptance_criteria"] = args.acceptance_criteria
        if args.reviewer is not None:
            fields["reviewer"] = args.reviewer
        if args.subagents is not None:
            fields["subagents"] = args.subagents
        _print_json(tickets_module.upsert_ticket(root, args.ticket_id, actor=args.actor, expected_revision=args.expected_revision, **fields))
    elif command == "assign":
        _print_json(
            tickets_module.assign_ticket(
                root,
                args.ticket_id,
                actor=args.actor,
                assignee=args.assignee,
                expected_revision=args.expected_revision,
                ttl_sec=args.ttl_sec,
                reviewer=args.reviewer,
                subagents=args.subagents,
            )
        )
    elif command == "heartbeat":
        _print_json(tickets_module.heartbeat_ticket(root, args.ticket_id, actor=args.actor, ttl_sec=args.ttl_sec))
    elif command == "transition":
        _print_json(
            tickets_module.transition_ticket(
                root, args.ticket_id, actor=args.actor, stage=args.stage, expected_revision=args.expected_revision, summary=args.summary
            )
        )
    elif command == "comment":
        _print_json(
            tickets_module.comment_ticket(
                root, args.ticket_id, actor=args.actor, summary=args.summary, body=_read_ticket_body(args), expected_revision=args.expected_revision
            )
        )
    elif command == "worklog":
        _print_json(
            tickets_module.add_worklog(
                root,
                args.ticket_id,
                actor=args.actor,
                summary=args.summary,
                evidence=_ticket_evidence_from_args(args),
                expected_revision=args.expected_revision,
            )
        )
    elif command == "review":
        _print_json(
            tickets_module.review_ticket(
                root, args.ticket_id, actor=args.actor, verdict=args.verdict, summary=args.summary, findings=args.findings, expected_revision=args.expected_revision
            )
        )
    elif command == "done":
        _print_json(
            tickets_module.mark_ticket_done(
                root, args.ticket_id, actor=args.actor, summary=args.summary, expected_revision=args.expected_revision, force=args.force
            )
        )
    elif command == "dep-add":
        _print_json(tickets_module.add_dependency(root, args.ticket_id, actor=args.actor, dep_type=args.dep_type, target=args.target))
    elif command == "list":
        _print_json(tickets_module.list_tickets(root, stage=args.stage, assignee=args.assignee, parent_id=args.parent, include_archived=args.include_archived))
    elif command == "get":
        _print_json(tickets_module.get_ticket(root, args.id))
    elif command == "tree":
        _print_json(tickets_module.ticket_tree(root))
    elif command == "critical-path":
        _print_json(tickets_module.critical_path(root))
    elif command == "metrics":
        _print_json(tickets_module.metrics_digest(root))
    elif command == "wip":
        _print_json(tickets_module.wip_by_agent(root))
    elif command == "archive":
        _print_json(tickets_module.archive_ticket(root, args.ticket_id, actor=args.actor))
    elif command == "export":
        print(tickets_module.export_ticket_markdown(root, args.id), end="")
    elif command == "verify":
        ok, detail = tickets_module.verify_ticket_chain(root, args.id)
        _print_json({"ok": ok, "detail": detail})


def _project_hint(argv: Sequence[str] | None) -> dict[str, Any]:
    """Actors are per project, argparse choices are built once: resolve --repo first, then build the parser."""

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--repo", type=Path)
    pre.add_argument("--root", dest="maintenance_root", type=Path)
    pre.add_argument("command", nargs="?")
    pre.add_argument("arguments", nargs=argparse.REMAINDER)
    try:
        # Stop global parsing at the command: worklog --repo is evidence.
        known, _ = pre.parse_known_args(argv)
        if known.command == "maintenance":
            maintenance, _ = pre.parse_known_args(known.arguments)
            known.maintenance_root = maintenance.maintenance_root
        return project_config(known.maintenance_root or board_root(known.repo))
    except (BoardError, OSError, SystemExit, ValueError):
        return project_config(None)


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser(_project_hint(argv)).parse_args(argv)
    if args.command == "maintenance" and args.maintenance_root is not None:
        root = args.maintenance_root
    else:
        root = board_root(args.repo)
    if args.command == "init":
        print(initialize(root))
        seeded = seed_project_config(root)
        if seeded is not None:
            print(seeded)
    elif args.command == "maintenance":
        _print_json(remove_stale_locks(root))
    elif args.command == "post":
        _print_json(
            post_message(
                root,
                sender=args.sender,
                recipient=args.recipient,
                kind=args.kind,
                priority=args.priority,
                workstream=args.workstream,
                summary=args.summary,
                body=_read_body(args),
                reply_to=args.reply_to,
                ticket_id=args.ticket_id,
                requires_ack=args.requires_ack,
            )
        )
    elif args.command == "inbox":
        _print_json(inbox(root, actor=args.actor, pending_ack_only=args.pending_ack))
    elif args.command == "read":
        print(read_message(root, args.id)[2], end="")
    elif args.command == "ack":
        _print_json(acknowledge(root, actor=args.actor, message_id=args.id))
    elif args.command == "status":
        if args.state:
            if not args.actor or not args.summary:
                raise BoardError("status publication requires --actor, --state, and --summary")
            _print_json(
                publish_status(
                    root,
                    actor=args.actor,
                    state=args.state,
                    summary=args.summary,
                    workstream=args.workstream,
                    head=args.head,
                    paths=args.path,
                )
            )
        else:
            if args.summary or args.workstream or args.head or args.path:
                raise BoardError("status fields require --state")
            _print_json(list_status(root, args.actor))
    elif args.command == "ticket":
        initialize(root)
        _run_ticket_command(args, root)
    elif args.command == "actor":
        initialize(root)
        tickets_module = _tickets_module()
        if args.actor_command == "register":
            _print_json(
                tickets_module.register_actor(
                    root, args.name, role=args.role.upper(), display=args.display, master=args.master
                )
            )
        elif args.actor_command == "list":
            _print_json(tickets_module.list_actors(root, role=args.role.upper() if args.role else None))
    elif args.command == "roadmap":
        if args.roadmap_command == "list":
            _print_json(list_roadmap(root))
        elif args.roadmap_command == "get":
            _print_json(get_roadmap_item(root, args.id))
        elif args.roadmap_command == "upsert":
            item = upsert_roadmap_item(
                root,
                actor=args.actor,
                item_id=args.id,
                title=args.title,
                summary=args.summary,
                status=args.status,
                owner=args.owner,
                progress=args.progress,
                blocker=args.blocker,
                expected_revision=args.expected_revision,
            )
            extension = _roadmap_extension_kwargs(args)
            if extension:
                # The v1 item is written first; the sidecar annotation prints the merged superset.
                item = _tree_module().annotate_roadmap_item(root, actor=args.actor, item_id=args.id, **extension)
            _print_json(item)
        elif args.roadmap_command in ("tree", "annotate"):
            _run_roadmap_extension(args, root)
    return 0


def main() -> int:
    try:
        return run()
    except (BoardError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"agent-board: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
