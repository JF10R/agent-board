#!/usr/bin/env python3
"""Durable, project-agnostic roadmap storage for the agent board.

The public surface is :class:`RoadmapStore`.  Every write uses a fresh SQLite
connection and ``BEGIN IMMEDIATE`` so optimistic revision checks and writes are
one transaction across both threads and processes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid
from typing import Any
from datetime import datetime, timezone


SCHEMA_VERSION = 2
DATABASE_NAME = "roadmap.v2.sqlite3"
COMPACTION_SUPPORTED = False
SQLITE_MAX_INTEGER = 2**63 - 1
STATUSES = frozenset({"PENDING", "IN_PROGRESS", "BLOCKED", "COMPLETE"})
DEFAULT_ACTORS = frozenset({"sol-master", "claude-master"})
DEFAULT_OWNERS = frozenset({"sol-master", "claude-master", "shared", "unassigned"})
LINK_RELATIONS = frozenset(
    {"CONTEXT", "DECISION", "EVIDENCE", "HANDOFF", "BLOCKER"}
)
MIGRATED_COMPLETE_IMPACT = (
    "Imported completion; human-readable impact was not recorded in roadmap v1."
)

FIELD_CAPS = {
    "actor": 128,
    "item_id": 128,
    "title": 200,
    "summary": 2_000,
    "owner": 128,
    "blocker": 2_000,
    "completion_impact": 2_000,
    "change_summary": 1_000,
    "message_id": 200,
}

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_STATE_COLUMNS = (
    "title",
    "summary",
    "status",
    "owner",
    "progress",
    "blocker",
    "completion_impact",
)


class RoadmapError(Exception):
    """Base class for roadmap storage errors."""


class RoadmapValidationError(RoadmapError, ValueError):
    """An input violates the roadmap contract."""


class RoadmapNotFound(RoadmapError, LookupError):
    """A requested item or revision does not exist."""


class RoadmapNoOp(RoadmapError):
    """A write would add neither a state revision nor a new message link."""


class RoadmapConflict(RoadmapError):
    """Typed optimistic-concurrency conflict with exact revision values."""

    def __init__(self, item_id: str, expected_revision: int, current_revision: int):
        self.item_id = item_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"roadmap revision conflict for {item_id}: "
            f"expected {expected_revision}, current {current_revision}"
        )


class RoadmapSourceConflict(RoadmapError):
    """The locked v1 file no longer matches the caller's observed source hash."""

    def __init__(self, expected_sha256: str, current_sha256: str):
        self.expected_sha256 = expected_sha256
        self.current_sha256 = current_sha256
        super().__init__(
            "roadmap v1 source conflict: "
            f"expected {expected_sha256}, current {current_sha256}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def discover_git_common_dir(repo: Path | str | None = None) -> Path:
    """Resolve Git's shared common directory for a checkout or worktree."""

    cwd = Path(repo or ".").resolve()
    process = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "not a Git checkout"
        raise RoadmapError(f"cannot discover Git common directory: {detail}")
    value = Path(process.stdout.strip())
    return (value if value.is_absolute() else cwd / value).resolve()


# Same store-directory rule as agent_board.board_root.
def roadmap_runtime_root(repo: Path | str | None = None) -> Path:
    common = discover_git_common_dir(repo)
    return common / "agent-board"


def roadmap_database_path(repo: Path | str | None = None) -> Path:
    return roadmap_runtime_root(repo) / DATABASE_NAME


def _required_text(label: str, value: Any, cap: int) -> str:
    if not isinstance(value, str):
        raise RoadmapValidationError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise RoadmapValidationError(f"{label} must not be empty")
    if len(normalized) > cap:
        raise RoadmapValidationError(f"{label} exceeds {cap} characters")
    return normalized


def _optional_text(label: str, value: Any, cap: int) -> str:
    if not isinstance(value, str):
        raise RoadmapValidationError(f"{label} must be a string")
    normalized = value.strip()
    if len(normalized) > cap:
        raise RoadmapValidationError(f"{label} exceeds {cap} characters")
    return normalized


def _safe_token(label: str, value: Any, cap: int) -> str:
    normalized = _required_text(label, value, cap)
    if not _SAFE_TOKEN.fullmatch(normalized):
        raise RoadmapValidationError(f"{label} contains unsupported characters")
    return normalized


def _non_negative_revision(label: str, value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= SQLITE_MAX_INTEGER
    ):
        raise RoadmapValidationError(
            f"{label} must be an integer from 0 through {SQLITE_MAX_INTEGER}"
        )
    return value


def _positive_revision(label: str, value: Any) -> int:
    value = _non_negative_revision(label, value)
    if value == 0:
        raise RoadmapValidationError(f"{label} must be a positive integer")
    return value


def _validate_state(
    *,
    item_id: Any,
    title: Any,
    summary: Any,
    status: Any,
    owner: Any,
    progress: Any,
    blocker: Any,
    completion_impact: Any,
) -> dict[str, Any]:
    normalized_status = _required_text("status", status, 32).upper()
    if normalized_status not in STATUSES:
        raise RoadmapValidationError(f"invalid roadmap status {normalized_status!r}")
    if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
        raise RoadmapValidationError("progress must be an integer from 0 through 100")
    normalized_blocker = _optional_text("blocker", blocker, FIELD_CAPS["blocker"])
    impact = _optional_text(
        "completion impact", completion_impact, FIELD_CAPS["completion_impact"]
    )
    if normalized_status == "BLOCKED" and not normalized_blocker:
        raise RoadmapValidationError("BLOCKED roadmap items require a blocker")
    if normalized_status != "BLOCKED" and normalized_blocker:
        raise RoadmapValidationError("only BLOCKED roadmap items may have a blocker")
    if normalized_status == "COMPLETE":
        if progress != 100:
            raise RoadmapValidationError("COMPLETE roadmap items require progress 100")
        if not impact:
            raise RoadmapValidationError("COMPLETE roadmap items require completion impact")
    else:
        if progress == 100:
            raise RoadmapValidationError("roadmap progress 100 requires status COMPLETE")
        if impact:
            raise RoadmapValidationError(
                "only COMPLETE roadmap items may have completion impact"
            )
    return {
        "id": _safe_token("roadmap item id", item_id, FIELD_CAPS["item_id"]),
        "title": _required_text("roadmap title", title, FIELD_CAPS["title"]),
        "summary": _required_text("roadmap summary", summary, FIELD_CAPS["summary"]),
        "status": normalized_status,
        "owner": _required_text("roadmap owner", owner, FIELD_CAPS["owner"]),
        "progress": progress,
        "blocker": normalized_blocker,
        "completion_impact": impact,
    }


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


@contextmanager
def _cutover_lock(path: Path, timeout_seconds: float = 30.0) -> Iterable[None]:
    """Use the same one-byte lock protocol as the v1 roadmap implementation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as initializer:
            initializer.write(b"0")
            initializer.flush()
            os.fsync(initializer.fileno())
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
                    raise RoadmapError("timed out waiting for roadmap cutover lock") from exc
                time.sleep(0.01)
        yield
    finally:
        if locked and handle is not None:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if handle is not None:
            handle.close()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class RoadmapStore:
    """SQLite-backed current roadmap, immutable history, and message links."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        message_exists: Callable[[str], bool] | None = None,
        now: Callable[[], str] = utc_now,
        fault_injector: Callable[[str], None] | None = None,
        busy_timeout_seconds: float = 30.0,
        actors: Iterable[str] = DEFAULT_ACTORS,
        owners: Iterable[str] = DEFAULT_OWNERS,
        _database_path: Path | str | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.path = (
            Path(_database_path).resolve()
            if _database_path is not None
            else self.runtime_root / DATABASE_NAME
        )
        self._is_private_database = _database_path is not None
        self.message_exists = message_exists
        self._now = now
        self._fault_injector = fault_injector
        self._timeout = busy_timeout_seconds
        self._actors = frozenset(actors)
        self._owners = frozenset(owners)
        if not self._actors or not self._owners:
            raise RoadmapValidationError("actor and owner allowlists must not be empty")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if (
            not self._is_private_database
            and not self.path.exists()
            and (self.runtime_root / "roadmap.v1.json").exists()
        ):
            raise RoadmapError(
                "roadmap v1 exists; create v2 only through migrate_v1_atomic"
            )
        self._initialize()

    @classmethod
    def for_repo(cls, repo: Path | str | None = None, **kwargs: Any) -> "RoadmapStore":
        return cls(roadmap_runtime_root(repo), **kwargs)

    @classmethod
    def migrate_v1_atomic(
        cls,
        runtime_root: Path | str,
        *,
        actor: str,
        legacy_writes_quiesced: Callable[[], bool],
        expected_v1_source_sha256: str | None = None,
        message_exists: Callable[[str], bool] | None = None,
        now: Callable[[], str] = utc_now,
        fault_injector: Callable[[str], None] | None = None,
        busy_timeout_seconds: float = 30.0,
        actors: Iterable[str] = DEFAULT_ACTORS,
        owners: Iterable[str] = DEFAULT_OWNERS,
    ) -> "RoadmapStore":
        """Build a fully imported DB before atomically publishing the final path.

        Callers must make ``legacy_writes_quiesced`` fail closed using a cutover
        marker understood by every v1 writer.  This method also holds v1's
        ``.roadmap.lock`` during the build and publication, but does not mutate v1.
        A false guard aborts both before building and immediately before publish.
        """

        if not callable(legacy_writes_quiesced):
            raise RoadmapValidationError("legacy write quiescence guard is required")
        if expected_v1_source_sha256 is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", expected_v1_source_sha256
        ):
            raise RoadmapValidationError("expected roadmap v1 source hash is invalid")
        root = Path(runtime_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        final_path = root / DATABASE_NAME
        v1_path = root / "roadmap.v1.json"
        with _cutover_lock(root / ".roadmap.lock", busy_timeout_seconds):
            if not legacy_writes_quiesced():
                raise RoadmapError("legacy roadmap writers are not quiesced")
            try:
                source_bytes = v1_path.read_bytes()
            except FileNotFoundError as exc:
                raise RoadmapError("roadmap.v1.json is missing at cutover") from exc
            try:
                parsed_store = json.loads(source_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RoadmapValidationError(
                    "roadmap v1 source is not valid UTF-8 JSON"
                ) from exc
            source_sha = "sha256:" + sha256(source_bytes).hexdigest()
            if (
                expected_v1_source_sha256 is not None
                and source_sha != expected_v1_source_sha256
            ):
                raise RoadmapSourceConflict(
                    expected_v1_source_sha256, source_sha
                )
            if final_path.exists():
                existing = cls(
                    root,
                    message_exists=message_exists,
                    now=now,
                    fault_injector=fault_injector,
                    busy_timeout_seconds=busy_timeout_seconds,
                    actors=actors,
                    owners=owners,
                )
                meta = existing.get_meta()
                if meta.get("v1_source_sha256") != source_sha:
                    raise RoadmapError(
                        "existing roadmap v2 is not the completed import of this v1 source"
                    )
                existing.verify(expected_v1_source_sha256=source_sha)
                return existing

            temporary_path = root / f".{DATABASE_NAME}.{uuid.uuid4().hex}.tmp"
            try:
                temporary = cls(
                    root,
                    message_exists=message_exists,
                    now=now,
                    fault_injector=fault_injector,
                    busy_timeout_seconds=busy_timeout_seconds,
                    actors=actors,
                    owners=owners,
                    _database_path=temporary_path,
                )
                temporary.import_v1(
                    parsed_store,
                    actor=actor,
                    source_bytes=source_bytes,
                )
                temporary.verify(expected_v1_source_sha256=source_sha)
                _fsync_file(temporary_path)
                if fault_injector is not None:
                    fault_injector("before_publish")
                if not legacy_writes_quiesced():
                    raise RoadmapError("legacy roadmap writers resumed before publish")
                os.replace(temporary_path, final_path)
                _fsync_file(final_path)
                _fsync_directory(root)
                if fault_injector is not None:
                    fault_injector("after_publish")
                published = cls(
                    root,
                    message_exists=message_exists,
                    now=now,
                    fault_injector=fault_injector,
                    busy_timeout_seconds=busy_timeout_seconds,
                    actors=actors,
                    owners=owners,
                )
                published.verify(expected_v1_source_sha256=source_sha)
                return published
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self._timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {max(1, int(self._timeout * 1000))}")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) STRICT;

                CREATE TABLE IF NOT EXISTS changes (
                    change_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                ) STRICT;

                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    blocker TEXT NOT NULL,
                    completion_impact TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    change_seq INTEGER NOT NULL UNIQUE REFERENCES changes(change_seq)
                ) STRICT;

                CREATE TABLE IF NOT EXISTS item_revisions (
                    item_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    blocker TEXT NOT NULL,
                    completion_impact TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    change_summary TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    change_seq INTEGER NOT NULL UNIQUE REFERENCES changes(change_seq),
                    PRIMARY KEY (item_id, revision)
                ) STRICT;

                CREATE TABLE IF NOT EXISTS revision_message_links (
                    item_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    change_seq INTEGER NOT NULL UNIQUE REFERENCES changes(change_seq),
                    PRIMARY KEY (item_id, revision, message_id, relation),
                    FOREIGN KEY (item_id, revision)
                        REFERENCES item_revisions(item_id, revision)
                ) STRICT;

                CREATE INDEX IF NOT EXISTS revision_links_by_message
                ON revision_message_links(message_id, change_seq);

                CREATE TRIGGER IF NOT EXISTS immutable_item_revisions_update
                BEFORE UPDATE ON item_revisions BEGIN
                    SELECT RAISE(ABORT, 'item revisions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_item_revisions_delete
                BEFORE DELETE ON item_revisions BEGIN
                    SELECT RAISE(ABORT, 'item revisions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_links_update
                BEFORE UPDATE ON revision_message_links BEGIN
                    SELECT RAISE(ABORT, 'revision message links are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_links_delete
                BEFORE DELETE ON revision_message_links BEGIN
                    SELECT RAISE(ABORT, 'revision message links are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_changes_update
                BEFORE UPDATE ON changes BEGIN
                    SELECT RAISE(ABORT, 'change log is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_changes_delete
                BEFORE DELETE ON changes BEGIN
                    SELECT RAISE(ABORT, 'change log is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS items_no_delete
                BEFORE DELETE ON items BEGIN
                    SELECT RAISE(ABORT, 'roadmap items cannot be deleted');
                END;
                CREATE TRIGGER IF NOT EXISTS items_revision_backed_insert
                BEFORE INSERT ON items WHEN NEW.revision != coalesce((
                    SELECT max(revision) FROM item_revisions
                    WHERE item_id=NEW.item_id
                ), -1) OR NOT EXISTS (
                    SELECT 1 FROM item_revisions r
                    WHERE r.item_id=NEW.item_id AND r.revision=NEW.revision
                      AND r.title=NEW.title AND r.summary=NEW.summary
                      AND r.status=NEW.status AND r.owner=NEW.owner
                      AND r.progress=NEW.progress AND r.blocker=NEW.blocker
                      AND r.completion_impact=NEW.completion_impact
                      AND r.updated_at=NEW.updated_at AND r.change_seq=NEW.change_seq
                ) BEGIN
                    SELECT RAISE(ABORT, 'current item must match an immutable revision');
                END;
                CREATE TRIGGER IF NOT EXISTS items_revision_backed_update
                BEFORE UPDATE ON items WHEN NEW.revision != coalesce((
                    SELECT max(revision) FROM item_revisions
                    WHERE item_id=NEW.item_id
                ), -1) OR NOT EXISTS (
                    SELECT 1 FROM item_revisions r
                    WHERE r.item_id=NEW.item_id AND r.revision=NEW.revision
                      AND r.title=NEW.title AND r.summary=NEW.summary
                      AND r.status=NEW.status AND r.owner=NEW.owner
                      AND r.progress=NEW.progress AND r.blocker=NEW.blocker
                      AND r.completion_impact=NEW.completion_impact
                      AND r.updated_at=NEW.updated_at AND r.change_seq=NEW.change_seq
                ) BEGIN
                    SELECT RAISE(ABORT, 'current item must match an immutable revision');
                END;
                CREATE TRIGGER IF NOT EXISTS revision_link_limit
                BEFORE INSERT ON revision_message_links WHEN (
                    SELECT count(*) FROM revision_message_links
                    WHERE item_id=NEW.item_id AND revision=NEW.revision
                ) >= 20 BEGIN
                    SELECT RAISE(ABORT, 'a roadmap revision may link at most 20 messages');
                END;
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('feed_id', ?)",
                (uuid.uuid4().hex,),
            )
            version = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            if version != str(SCHEMA_VERSION):
                raise RoadmapError(f"unsupported roadmap schema version {version!r}")
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def _validate_actor(self, actor: Any) -> str:
        normalized = _safe_token("actor", actor, FIELD_CAPS["actor"])
        if normalized not in self._actors:
            raise RoadmapValidationError(f"unauthorized roadmap actor {normalized!r}")
        return normalized

    def _validate_link(self, link: Mapping[str, Any] | Sequence[str]) -> tuple[str, str]:
        if isinstance(link, Mapping):
            if set(link) != {"message_id", "relation"}:
                raise RoadmapValidationError(
                    "message links require exactly message_id and relation"
                )
            message_id, relation = link["message_id"], link["relation"]
        elif isinstance(link, Sequence) and not isinstance(link, (str, bytes)) and len(link) == 2:
            message_id, relation = link
        else:
            raise RoadmapValidationError("message link must be a pair or mapping")
        normalized_id = _safe_token(
            "message id", message_id, FIELD_CAPS["message_id"]
        )
        normalized_relation = _required_text("link relation", relation, 32).upper()
        if normalized_relation not in LINK_RELATIONS:
            raise RoadmapValidationError(
                f"invalid message link relation {normalized_relation!r}"
            )
        if self.message_exists is None:
            raise RoadmapValidationError(
                "message links require a fail-closed existence validator"
            )
        if not self.message_exists(normalized_id):
            raise RoadmapValidationError(f"unknown board message: {normalized_id}")
        return normalized_id, normalized_relation

    def _validate_owner(self, owner: str) -> str:
        if owner not in self._owners:
            raise RoadmapValidationError(f"invalid roadmap owner {owner!r}")
        return owner

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["item_id"],
            "title": row["title"],
            "summary": row["summary"],
            "status": row["status"],
            "owner": row["owner"],
            "progress": row["progress"],
            "blocker": row["blocker"],
            "completion_impact": row["completion_impact"],
            "updated_at": row["updated_at"],
            "revision": row["revision"],
            "change_seq": row["change_seq"],
        }

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> dict[str, Any]:
        result = RoadmapStore._row_to_item(row)
        result.update(
            actor=row["actor"],
            change_summary=row["change_summary"],
            source_kind=row["source_kind"],
        )
        return result

    def list_items(
        self, *, after_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit)
        after_id = "" if after_id is None else _safe_token(
            "roadmap item cursor", after_id, FIELD_CAPS["item_id"]
        )
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM items WHERE item_id > ? ORDER BY item_id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get_item(self, item_id: str) -> dict[str, Any]:
        item_id = _safe_token("roadmap item id", item_id, FIELD_CAPS["item_id"])
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise RoadmapNotFound(f"unknown roadmap item: {item_id}")
        return self._row_to_item(row)

    def list_revisions(
        self, item_id: str, *, after_revision: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        item_id = _safe_token("roadmap item id", item_id, FIELD_CAPS["item_id"])
        after_revision = _non_negative_revision("revision cursor", after_revision)
        limit = self._validate_limit(limit)
        with self._read() as connection:
            rows = connection.execute(
                """SELECT * FROM item_revisions
                   WHERE item_id = ? AND revision > ? ORDER BY revision LIMIT ?""",
                (item_id, after_revision, limit),
            ).fetchall()
            known = bool(rows) or connection.execute(
                "SELECT 1 FROM items WHERE item_id = ?", (item_id,)
            ).fetchone() is not None
        if not known:
            raise RoadmapNotFound(f"unknown roadmap item: {item_id}")
        return [self._row_to_revision(row) for row in rows]

    def get_revision(self, item_id: str, revision: int) -> dict[str, Any]:
        item_id = _safe_token("roadmap item id", item_id, FIELD_CAPS["item_id"])
        revision = _positive_revision("revision", revision)
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM item_revisions WHERE item_id = ? AND revision = ?",
                (item_id, revision),
            ).fetchone()
        if row is None:
            raise RoadmapNotFound(f"unknown roadmap revision: {item_id}@{revision}")
        return self._row_to_revision(row)

    def _next_change(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        item_id: str,
        revision: int,
        created_at: str,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO changes(kind, item_id, revision, created_at) VALUES (?, ?, ?, ?)",
            (kind, item_id, revision, created_at),
        )
        return int(cursor.lastrowid)

    def _insert_link(
        self,
        connection: sqlite3.Connection,
        *,
        actor: str,
        item_id: str,
        revision: int,
        message_id: str,
        relation: str,
        linked_at: str,
    ) -> int:
        exists = connection.execute(
            """SELECT 1 FROM revision_message_links
               WHERE item_id = ? AND revision = ? AND message_id = ? AND relation = ?""",
            (item_id, revision, message_id, relation),
        ).fetchone()
        if exists:
            return 0
        change_seq = self._next_change(
            connection,
            kind="MESSAGE_LINK",
            item_id=item_id,
            revision=revision,
            created_at=linked_at,
        )
        connection.execute(
            """INSERT INTO revision_message_links(
                   item_id, revision, message_id, relation, actor, linked_at, change_seq
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_id, revision, message_id, relation, actor, linked_at, change_seq),
        )
        return change_seq

    def upsert_item(
        self,
        *,
        actor: str,
        item_id: str,
        title: str,
        summary: str,
        status: str,
        owner: str,
        progress: int,
        change_summary: str,
        expected_revision: int,
        blocker: str = "",
        completion_impact: str = "",
        message_links: Sequence[Mapping[str, Any] | Sequence[str]] = (),
    ) -> dict[str, Any]:
        actor = self._validate_actor(actor)
        expected_revision = _non_negative_revision(
            "expected revision", expected_revision
        )
        change_summary = _required_text(
            "change summary", change_summary, FIELD_CAPS["change_summary"]
        )
        state = _validate_state(
            item_id=item_id,
            title=title,
            summary=summary,
            status=status,
            owner=owner,
            progress=progress,
            blocker=blocker,
            completion_impact=completion_impact,
        )
        state["owner"] = self._validate_owner(state["owner"])
        links = [self._validate_link(link) for link in message_links]
        if len(links) > 20:
            raise RoadmapValidationError("a roadmap revision may link at most 20 messages")
        if len(set(links)) != len(links):
            raise RoadmapValidationError("duplicate message link in request")

        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM items WHERE item_id = ?", (state["id"],)
            ).fetchone()
            current_revision = int(existing["revision"]) if existing else 0
            if expected_revision != current_revision:
                raise RoadmapConflict(
                    state["id"], expected_revision, current_revision
                )

            if existing and all(existing[column] == state[column] for column in _STATE_COLUMNS):
                current_link_count = connection.execute(
                    """SELECT count(*) FROM revision_message_links
                       WHERE item_id = ? AND revision = ?""",
                    (state["id"], current_revision),
                ).fetchone()[0]
                new_link_count = sum(
                    1 for message_id, relation in links
                    if connection.execute(
                        """SELECT 1 FROM revision_message_links
                           WHERE item_id=? AND revision=? AND message_id=? AND relation=?""",
                        (state["id"], current_revision, message_id, relation),
                    ).fetchone() is None
                )
                if current_link_count + new_link_count > 20:
                    raise RoadmapValidationError(
                        "a roadmap revision may link at most 20 messages"
                    )
                linked_at = self._now()
                added = [
                    self._insert_link(
                        connection,
                        actor=actor,
                        item_id=state["id"],
                        revision=current_revision,
                        message_id=message_id,
                        relation=relation,
                        linked_at=linked_at,
                    )
                    for message_id, relation in links
                ]
                if not any(added):
                    raise RoadmapNoOp(
                        f"roadmap update for {state['id']} changes no state or links"
                    )
                self._fault("after_link_insert")
                return self._row_to_item(existing)

            revision = current_revision + 1
            updated_at = self._now()
            change_seq = self._next_change(
                connection,
                kind="ITEM_REVISION",
                item_id=state["id"],
                revision=revision,
                created_at=updated_at,
            )
            connection.execute(
                """INSERT INTO item_revisions(
                       item_id, revision, title, summary, status, owner, progress,
                       blocker, completion_impact, updated_at, actor, change_summary,
                       source_kind, change_seq
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NATIVE', ?)""",
                (
                    state["id"], revision, state["title"], state["summary"],
                    state["status"], state["owner"], state["progress"],
                    state["blocker"], state["completion_impact"], updated_at,
                    actor, change_summary, change_seq,
                ),
            )
            self._fault("after_revision_insert")
            connection.execute(
                """INSERT INTO items(
                       item_id, title, summary, status, owner, progress, blocker,
                       completion_impact, updated_at, revision, change_seq
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                       title=excluded.title, summary=excluded.summary,
                       status=excluded.status, owner=excluded.owner,
                       progress=excluded.progress, blocker=excluded.blocker,
                       completion_impact=excluded.completion_impact,
                       updated_at=excluded.updated_at, revision=excluded.revision,
                       change_seq=excluded.change_seq""",
                (
                    state["id"], state["title"], state["summary"], state["status"],
                    state["owner"], state["progress"], state["blocker"],
                    state["completion_impact"], updated_at, revision, change_seq,
                ),
            )
            for message_id, relation in links:
                self._insert_link(
                    connection,
                    actor=actor,
                    item_id=state["id"],
                    revision=revision,
                    message_id=message_id,
                    relation=relation,
                    linked_at=updated_at,
                )
            self._fault("before_commit")
            row = connection.execute(
                "SELECT * FROM items WHERE item_id = ?", (state["id"],)
            ).fetchone()
            return self._row_to_item(row)

    def link_message(
        self,
        *,
        actor: str,
        item_id: str,
        revision: int,
        message_id: str,
        relation: str,
    ) -> dict[str, Any]:
        actor = self._validate_actor(actor)
        item_id = _safe_token("roadmap item id", item_id, FIELD_CAPS["item_id"])
        revision = _positive_revision("revision", revision)
        message_id, relation = self._validate_link((message_id, relation))
        linked_at = self._now()
        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM item_revisions WHERE item_id = ? AND revision = ?",
                (item_id, revision),
            ).fetchone() is None:
                raise RoadmapNotFound(f"unknown roadmap revision: {item_id}@{revision}")
            current_link_count = connection.execute(
                """SELECT count(*) FROM revision_message_links
                   WHERE item_id = ? AND revision = ?""",
                (item_id, revision),
            ).fetchone()[0]
            if current_link_count >= 20:
                raise RoadmapValidationError(
                    "a roadmap revision may link at most 20 messages"
                )
            change_seq = self._insert_link(
                connection,
                actor=actor,
                item_id=item_id,
                revision=revision,
                message_id=message_id,
                relation=relation,
                linked_at=linked_at,
            )
            if change_seq == 0:
                raise RoadmapNoOp(
                    f"message link already exists: {item_id}@{revision} "
                    f"{relation} {message_id}"
                )
            self._fault("after_link_insert")
        return {
            "item_id": item_id,
            "revision": revision,
            "message_id": message_id,
            "relation": relation,
            "actor": actor,
            "linked_at": linked_at,
            "change_seq": change_seq,
        }

    def list_links(
        self,
        item_id: str,
        revision: int | None = None,
        *,
        after_seq: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        item_id = _safe_token("roadmap item id", item_id, FIELD_CAPS["item_id"])
        after_seq = _non_negative_revision("after sequence", after_seq)
        limit = self._validate_limit(limit)
        parameters: tuple[Any, ...] = (item_id, after_seq)
        sql = (
            "SELECT * FROM revision_message_links "
            "WHERE item_id = ? AND change_seq > ?"
        )
        if revision is not None:
            revision = _positive_revision("revision", revision)
            sql += " AND revision = ?"
            parameters += (revision,)
        sql += " ORDER BY change_seq LIMIT ?"
        parameters += (limit,)
        with self._read() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def list_items_for_message(
        self,
        message_id: str,
        *,
        after_seq: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return revision links for a message in stable global-change order."""

        message_id = _safe_token(
            "message id", message_id, FIELD_CAPS["message_id"]
        )
        after_seq = _non_negative_revision("after sequence", after_seq)
        limit = self._validate_limit(limit)
        with self._read() as connection:
            rows = connection.execute(
                """SELECT item_id, revision, message_id, relation, actor,
                          linked_at, change_seq
                   FROM revision_message_links
                   WHERE message_id = ? AND change_seq > ?
                   ORDER BY change_seq LIMIT ?""",
                (message_id, after_seq, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_history(
        self,
        item_id: str,
        *,
        before_seq: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return immutable item snapshots newest-first for timeline rendering."""

        item_id = _safe_token("roadmap item id", item_id, FIELD_CAPS["item_id"])
        limit = self._validate_limit(limit)
        if before_seq is None:
            before_seq = 2**63 - 1
        else:
            before_seq = _positive_revision("before sequence", before_seq)
        with self._read() as connection:
            rows = connection.execute(
                """SELECT * FROM item_revisions
                   WHERE item_id = ? AND change_seq < ?
                   ORDER BY change_seq DESC LIMIT ?""",
                (item_id, before_seq, limit),
            ).fetchall()
            known = bool(rows) or connection.execute(
                "SELECT 1 FROM items WHERE item_id = ?", (item_id,)
            ).fetchone() is not None
        if not known:
            raise RoadmapNotFound(f"unknown roadmap item: {item_id}")
        return [self._row_to_revision(row) for row in rows]

    def list_changes(self, *, after_seq: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        after_seq = _non_negative_revision("after sequence", after_seq)
        limit = self._validate_limit(limit)
        with self._read() as connection:
            rows = connection.execute(
                """SELECT change_seq, kind, item_id, revision, created_at
                   FROM changes WHERE change_seq > ? ORDER BY change_seq LIMIT ?""",
                (after_seq, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_change_bounds(self) -> dict[str, int | str]:
        with self._read() as connection:
            row = connection.execute(
                "SELECT coalesce(min(change_seq), 0), coalesce(max(change_seq), 0) FROM changes"
            ).fetchone()
            feed_id = connection.execute(
                "SELECT value FROM meta WHERE key='feed_id'"
            ).fetchone()[0]
        return {
            "feed_id": feed_id,
            "first_seq": int(row[0]),
            "latest_seq": int(row[1]),
        }

    def get_feed_state(
        self,
        *,
        client_feed_id: str | None = None,
        after_seq: int = 0,
    ) -> dict[str, int | str | bool]:
        """Report whether a persisted cursor must reset after DB replacement."""

        after_seq = _non_negative_revision("after sequence", after_seq)
        bounds = self.get_change_bounds()
        reset = (
            (client_feed_id is not None and client_feed_id != bounds["feed_id"])
            or after_seq > bounds["latest_seq"]
            or (
                bounds["first_seq"] > 0
                and after_seq != 0
                and after_seq < bounds["first_seq"] - 1
            )
        )
        return {**bounds, "reset_required": reset}

    def get_change(self, change_seq: int) -> dict[str, Any]:
        """Hydrate one feed entry with its immutable revision or link payload."""

        change_seq = _positive_revision("change sequence", change_seq)
        with self._read() as connection:
            change = connection.execute(
                "SELECT * FROM changes WHERE change_seq = ?", (change_seq,)
            ).fetchone()
            if change is None:
                raise RoadmapNotFound(f"unknown roadmap change: {change_seq}")
            revision = connection.execute(
                "SELECT * FROM item_revisions WHERE change_seq = ?", (change_seq,)
            ).fetchone()
            link = connection.execute(
                "SELECT * FROM revision_message_links WHERE change_seq = ?", (change_seq,)
            ).fetchone()
        return {
            "change": dict(change),
            "revision": self._row_to_revision(revision) if revision is not None else None,
            "link": dict(link) if link is not None else None,
        }

    def get_meta(self) -> dict[str, str]:
        with self._read() as connection:
            rows = connection.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def verify(
        self, *, expected_v1_source_sha256: str | None = None
    ) -> dict[str, Any]:
        """Fail closed if schema, history, link, feed, or import integrity drifts.

        Compaction is intentionally unsupported in schema v2: immutable history,
        links, and the global change log must not be deleted.
        """

        with self._read() as connection:
            quick = [row[0] for row in connection.execute("PRAGMA quick_check")]
            if quick != ["ok"]:
                raise RoadmapError(f"SQLite quick_check failed: {quick!r}")
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign:
                raise RoadmapError("SQLite foreign_key_check failed")
            meta = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM meta")
            }
            if meta.get("schema_version") != str(SCHEMA_VERSION):
                raise RoadmapError("roadmap schema metadata mismatch")
            if not re.fullmatch(r"[0-9a-f]{32}", meta.get("feed_id", "")):
                raise RoadmapError("roadmap feed_id is missing or invalid")
            source_sha = meta.get("v1_source_sha256")
            if source_sha is not None and not re.fullmatch(
                r"sha256:[0-9a-f]{64}", source_sha
            ):
                raise RoadmapError("roadmap v1 source hash is invalid")
            if (
                expected_v1_source_sha256 is not None
                and source_sha != expected_v1_source_sha256
            ):
                raise RoadmapError("roadmap v1 source hash mismatch")

            bad_current = connection.execute(
                """SELECT i.item_id FROM items i
                   WHERE i.revision != (
                       SELECT max(r.revision) FROM item_revisions r
                       WHERE r.item_id=i.item_id
                   ) OR NOT EXISTS (
                       SELECT 1 FROM item_revisions r
                       WHERE r.item_id=i.item_id AND r.revision=i.revision
                         AND r.title=i.title AND r.summary=i.summary
                         AND r.status=i.status AND r.owner=i.owner
                         AND r.progress=i.progress AND r.blocker=i.blocker
                         AND r.completion_impact=i.completion_impact
                         AND r.updated_at=i.updated_at AND r.change_seq=i.change_seq
                   ) LIMIT 1"""
            ).fetchone()
            if bad_current is not None:
                raise RoadmapError(
                    f"current item does not match latest immutable revision: {bad_current[0]}"
                )
            bad_revision = connection.execute(
                """SELECT r.change_seq FROM item_revisions r
                   LEFT JOIN changes c ON c.change_seq=r.change_seq
                   WHERE c.change_seq IS NULL OR c.item_id!=r.item_id
                      OR c.revision!=r.revision
                      OR c.kind NOT IN ('ITEM_REVISION', 'V1_IMPORT') LIMIT 1"""
            ).fetchone()
            if bad_revision is not None:
                raise RoadmapError("immutable revision has invalid change-log entry")
            bad_link = connection.execute(
                """SELECT l.change_seq FROM revision_message_links l
                   LEFT JOIN changes c ON c.change_seq=l.change_seq
                   WHERE c.change_seq IS NULL OR c.item_id!=l.item_id
                      OR c.revision!=l.revision OR c.kind!='MESSAGE_LINK' LIMIT 1"""
            ).fetchone()
            if bad_link is not None:
                raise RoadmapError("message link has invalid change-log entry")
            orphan_change = connection.execute(
                """SELECT c.change_seq FROM changes c
                   LEFT JOIN item_revisions r ON r.change_seq=c.change_seq
                   LEFT JOIN revision_message_links l ON l.change_seq=c.change_seq
                   WHERE (c.kind IN ('ITEM_REVISION','V1_IMPORT') AND r.change_seq IS NULL)
                      OR (c.kind='MESSAGE_LINK' AND l.change_seq IS NULL)
                      OR c.kind NOT IN ('ITEM_REVISION','V1_IMPORT','MESSAGE_LINK')
                   LIMIT 1"""
            ).fetchone()
            if orphan_change is not None:
                raise RoadmapError("change log contains an orphan or unsupported kind")
            counts = connection.execute(
                """SELECT
                       (SELECT count(*) FROM items),
                       (SELECT count(*) FROM item_revisions),
                       (SELECT count(*) FROM revision_message_links),
                       (SELECT coalesce(max(change_seq), 0) FROM changes)"""
            ).fetchone()
        return {
            "ok": True,
            "feed_id": meta["feed_id"],
            "items": counts[0],
            "revisions": counts[1],
            "links": counts[2],
            "latest_seq": counts[3],
            "compaction_supported": COMPACTION_SUPPORTED,
        }

    def import_v1(
        self,
        parsed_store: Mapping[str, Any],
        *,
        actor: str,
        source_bytes: bytes,
    ) -> list[dict[str, Any]]:
        """Import one immutable V1_IMPORT snapshot per item, exactly once.

        ``parsed_store`` is never mutated.  It must exactly equal JSON decoded from
        ``source_bytes`` so provenance cannot be bound to different content.  The
        caller remains responsible for reading, not rewriting, v1.
        """

        if not self._is_private_database:
            raise RoadmapError(
                "low-level v1 import is restricted to unpublished migration databases; "
                "use migrate_v1_atomic"
            )

        actor = self._validate_actor(actor)
        if not isinstance(parsed_store, Mapping) or set(parsed_store) != {
            "schema_version",
            "items",
        }:
            raise RoadmapValidationError("invalid roadmap v1 store shape")
        if parsed_store["schema_version"] != 1 or not isinstance(
            parsed_store["items"], list
        ):
            raise RoadmapValidationError("invalid roadmap v1 schema")
        if not isinstance(source_bytes, bytes):
            raise RoadmapValidationError("roadmap v1 source bytes must be bytes")
        try:
            decoded_source = json.loads(source_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RoadmapValidationError("roadmap v1 source bytes are not valid UTF-8 JSON") from exc
        if decoded_source != parsed_store:
            raise RoadmapValidationError(
                "parsed roadmap v1 does not match the exact source bytes"
            )
        source_sha = "sha256:" + sha256(source_bytes).hexdigest()
        normalized: list[tuple[dict[str, Any], int, str]] = []
        seen: set[str] = set()
        required = {
            "id", "title", "summary", "status", "owner", "progress",
            "blocker", "updated_at", "revision",
        }
        for raw in parsed_store["items"]:
            if not isinstance(raw, Mapping) or set(raw) != required:
                raise RoadmapValidationError("invalid roadmap v1 item fields")
            raw_copy = dict(raw)
            revision = _positive_revision("roadmap v1 revision", raw_copy["revision"])
            status = _required_text("status", raw_copy["status"], 32).upper()
            blocker = _optional_text(
                "blocker", raw_copy["blocker"], FIELD_CAPS["blocker"]
            )
            if blocker and status != "BLOCKED":
                status = "BLOCKED"
                revision += 1
                revision = _positive_revision("roadmap v1 revision", revision)
            completion_impact = (
                MIGRATED_COMPLETE_IMPACT if status == "COMPLETE" else ""
            )
            state = _validate_state(
                item_id=raw_copy["id"], title=raw_copy["title"],
                summary=raw_copy["summary"], status=status,
                owner=raw_copy["owner"], progress=raw_copy["progress"],
                blocker=blocker, completion_impact=completion_impact,
            )
            state["owner"] = self._validate_owner(state["owner"])
            updated_at = _required_text("updated at", raw_copy["updated_at"], 100)
            if state["id"] in seen:
                raise RoadmapValidationError("roadmap v1 item ids must be unique")
            seen.add(state["id"])
            normalized.append((state, revision, updated_at))

        imported_at = self._now()
        with self._write() as connection:
            prior = connection.execute(
                "SELECT value FROM meta WHERE key = 'v1_source_sha256'"
            ).fetchone()
            if prior is not None:
                if prior["value"] != source_sha:
                    raise RoadmapConflict("V1_IMPORT", 0, 1)
                rows = connection.execute("SELECT * FROM items ORDER BY item_id").fetchall()
                return [self._row_to_item(row) for row in rows]
            if connection.execute("SELECT 1 FROM items LIMIT 1").fetchone() is not None:
                raise RoadmapError("cannot import roadmap v1 into a non-empty v2 store")

            for state, revision, updated_at in normalized:
                change_seq = self._next_change(
                    connection,
                    kind="V1_IMPORT",
                    item_id=state["id"],
                    revision=revision,
                    created_at=imported_at,
                )
                connection.execute(
                    """INSERT INTO item_revisions(
                           item_id, revision, title, summary, status, owner, progress,
                           blocker, completion_impact, updated_at, actor, change_summary,
                           source_kind, change_seq
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'V1_IMPORT', ?)""",
                    (
                        state["id"], revision, state["title"], state["summary"],
                        state["status"], state["owner"], state["progress"],
                        state["blocker"], state["completion_impact"], updated_at,
                        actor, "Imported from roadmap.v1.json", change_seq,
                    ),
                )
                connection.execute(
                    """INSERT INTO items(
                           item_id, title, summary, status, owner, progress, blocker,
                           completion_impact, updated_at, revision, change_seq
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        state["id"], state["title"], state["summary"], state["status"],
                        state["owner"], state["progress"], state["blocker"],
                        state["completion_impact"], updated_at, revision, change_seq,
                    ),
                )
            self._fault("after_v1_items")
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                (
                    ("v1_source_sha256", source_sha),
                    ("v1_imported_at", imported_at),
                ),
            )
            self._fault("before_commit")
            rows = connection.execute("SELECT * FROM items ORDER BY item_id").fetchall()
            return [self._row_to_item(row) for row in rows]

    @staticmethod
    def _validate_limit(limit: Any) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise RoadmapValidationError("limit must be from 1 through 200")
        return limit


__all__ = [
    "COMPACTION_SUPPORTED",
    "DATABASE_NAME",
    "FIELD_CAPS",
    "LINK_RELATIONS",
    "MIGRATED_COMPLETE_IMPACT",
    "RoadmapConflict",
    "RoadmapError",
    "RoadmapNoOp",
    "RoadmapNotFound",
    "RoadmapSourceConflict",
    "RoadmapStore",
    "RoadmapValidationError",
    "SQLITE_MAX_INTEGER",
    "roadmap_database_path",
    "roadmap_runtime_root",
]
