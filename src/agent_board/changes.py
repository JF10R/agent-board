"""Resumable message discovery without relying on timestamp ordering."""

from __future__ import annotations

import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import uuid

from .errors import BoardError
from .identity import require_message_recipient


def message_changes(
    root: Path, *, cursor: str | None = None, limit: int = 100, actor: str | None = None
) -> dict:
    """Index immutable messages, then return a bounded page scoped to one actor.

    The discovery projection is disposable. Its epoch makes loss or replacement
    explicit to consumers rather than silently reusing sequence numbers.
    """
    from .cli import read_message

    if type(limit) is not int or not 1 <= limit <= 500:
        raise BoardError("message change limit must be between 1 and 500")
    if actor is not None:
        require_message_recipient(actor, root)
    decoded = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 1000:
                raise ValueError
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            if (
                not isinstance(decoded, list)
                or len(decoded) != 3
                or not isinstance(decoded[0], str)
                or type(decoded[1]) is not int
                or decoded[1] < 0
                or decoded[2] != actor
            ):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise BoardError("invalid message cursor or actor scope") from exc
    root.mkdir(parents=True, exist_ok=True)
    try:
        with (
            closing(
                sqlite3.connect(root / "message-feed.sqlite3", timeout=5)
            ) as connection,
            connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS meta (epoch TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS messages (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, recipient TEXT NOT NULL, metadata TEXT NOT NULL)"
            )
            row = connection.execute("SELECT epoch FROM meta").fetchone()
            epoch = row[0] if row else uuid.uuid4().hex
            if row is None:
                connection.execute("INSERT INTO meta VALUES (?)", (epoch,))
            if decoded is not None and decoded[0] != epoch:
                raise BoardError(
                    "message feed was rebuilt; restart without a cursor and deduplicate by message id"
                )
            after = decoded[1] if decoded else 0
            maximum = connection.execute(
                "SELECT coalesce(max(sequence), 0) FROM messages"
            ).fetchone()[0]
            if after > maximum:
                raise BoardError("message cursor is ahead of this feed")
            known = {row[0] for row in connection.execute("SELECT id FROM messages")}
            malformed = 0
            for path in sorted((root / "messages").glob("*.md")):
                if path.stem in known:
                    continue
                try:
                    metadata, _, _ = read_message(root, path.stem)
                except (BoardError, OSError):
                    malformed += 1
                    continue
                connection.execute(
                    "INSERT INTO messages (id, recipient, metadata) VALUES (?, ?, ?)",
                    (
                        path.stem,
                        metadata["to"],
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
            rows = connection.execute(
                "SELECT sequence, metadata FROM messages WHERE sequence > ? AND (? IS NULL OR recipient = ?) ORDER BY sequence LIMIT ?",
                (after, actor, actor, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            last = page[-1][0] if page else after
            if not has_more:
                last = connection.execute(
                    "SELECT coalesce(max(sequence), 0) FROM messages"
                ).fetchone()[0]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([epoch, last, actor]).encode("utf-8")
            ).decode("ascii")
            return {
                "events": [
                    {
                        "sequence": sequence,
                        "type": "message.posted",
                        "message": json.loads(metadata),
                    }
                    for sequence, metadata in page
                ],
                "cursor": next_cursor,
                "has_more": has_more,
                "malformed": malformed,
            }
    except sqlite3.Error as exc:
        raise BoardError(f"message feed projection unavailable: {exc}") from exc
