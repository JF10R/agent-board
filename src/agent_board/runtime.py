"""Filesystem primitives shared by board domains and transport adapters."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Iterable, Mapping

from .errors import BoardError

STORE_DIRECTORY = "agent-board"
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_SUMMARY_CHARS = 300


def utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def require_text(label: str, value: str) -> str:
    if not isinstance(value, str):
        raise BoardError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise BoardError(f"{label} must not be empty")
    if "\n" in value or "\r" in value:
        raise BoardError(f"{label} must be one line")
    return value


def require_summary(value: str) -> str:
    summary = require_text("summary", value)
    if len(summary) > MAX_SUMMARY_CHARS:
        raise BoardError(
            f"summary is {len(summary)} characters, over the "
            f"{MAX_SUMMARY_CHARS}-character cap by {len(summary) - MAX_SUMMARY_CHARS}. "
            "Put the detail in --body-file and keep the summary to the one line a "
            "reader needs to decide whether to open the message."
        )
    return summary


def require_safe_token(label: str, value: str) -> str:
    if not isinstance(value, str) or not SAFE_TOKEN.fullmatch(value):
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
        detail = (
            completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        )
        raise BoardError(f"cannot resolve git common dir from {cwd}: {detail}")
    raw = completed.stdout.strip()
    if not raw:
        raise BoardError("git returned an empty common directory")
    common = Path(raw)
    if not common.is_absolute():
        common = cwd / common
    return common.resolve()


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@contextmanager
def file_lock(path: Path, timeout_seconds: float = 5.0) -> Iterable[None]:
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
                try:
                    candidate = path.open("r+b")
                except OSError as exc:
                    raise BoardError(
                        f"cannot open filesystem lock for writing: {path}; "
                        f"errno={exc.errno}, winerror={getattr(exc, 'winerror', None)}; "
                        "check filesystem permissions and execution context"
                    ) from exc
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
                    raise BoardError(
                        f"timed out waiting for filesystem lock: {path.name}"
                    ) from exc
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


def exclusive_lock_path(path: Path) -> Path:
    """Map a runtime target (``<root>/<subdir>/<name>``) to its lock file.

    Locks live in a dedicated ``<root>/locks/`` directory instead of beside
    their target so ``messages/`` (opened directly in Explorer) never
    accumulates one dotfile per message ever written.
    """

    root = path.parent.parent
    flattened = f"{path.parent.name}--{path.name}.lock"
    return root / "locks" / flattened


def write_exclusive(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(exclusive_lock_path(path)):
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


def write_atomic_replace(path: Path, content: str) -> None:
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
