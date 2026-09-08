"""Blocking, native filesystem notifications bridged to flushed NDJSON.

The harness owns this long-lived subprocess and consumes stdout. Successful writes
are transport delivery, not acknowledgement by an agent. Deduplicate message IDs
when restarting: a crash between flush and cursor replacement can replay a page.
"""

from __future__ import annotations

from .runtime import write_atomic_replace

import argparse
import ctypes
import json
import os
from pathlib import Path
import select
import sys

from .changes import message_changes
from .cli import _runtime_directory, board_root
from .errors import BoardError
from .identity import require_message_recipient


class _LinuxNotifications:
    def __init__(self, directory: Path):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = libc.inotify_init1(os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        # Close-write, move-in, create, delete and watch-directory replacement.
        if libc.inotify_add_watch(self.fd, os.fsencode(directory), 0x00000FC8) < 0:
            self.close()
            raise OSError(ctypes.get_errno(), "inotify_add_watch failed")

    def wait(self):
        # Overflow also wakes this read: every notification triggers a full feed
        # rescan, so event names and individual event counts are unnecessary.
        os.read(self.fd, 65536)

    def close(self):
        os.close(self.fd)


class _KqueueNotifications:
    def __init__(self, directory: Path):
        self.fd = os.open(directory, os.O_RDONLY)
        self.queue = select.kqueue()
        event = select.kevent(
            self.fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME,
        )
        try:
            self.queue.control([event], 0, 0)
        except BaseException:
            self.close()
            raise

    def wait(self):
        self.queue.control(None, 1, None)

    def close(self):
        self.queue.close()
        os.close(self.fd)


class _WindowsNotifications:
    def __init__(self, directory: Path):
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ],
                wintypes.HANDLE,
            ),
            "CreateEventW": (
                [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR],
                wintypes.HANDLE,
            ),
            "ReadDirectoryChangesW": (
                [
                    wintypes.HANDLE,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    ctypes.POINTER(Overlapped),
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "GetOverlappedResult": (
                [
                    wintypes.HANDLE,
                    ctypes.POINTER(Overlapped),
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.BOOL,
                ],
                wintypes.BOOL,
            ),
            "CancelIoEx": (
                [wintypes.HANDLE, ctypes.POINTER(Overlapped)],
                wintypes.BOOL,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "ResetEvent": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, returns) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, returns
        self.handle = self.api.CreateFileW(
            str(directory), 1, 7, None, 3, 0x42000000, None
        )
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.event = self.api.CreateEventW(None, True, False, None)
        if not self.event:
            self.api.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())
        self.overlapped = Overlapped(hEvent=self.event)
        self.buffer = ctypes.create_string_buffer(65536)
        try:
            self._arm()
        except BaseException:
            self.close()
            raise

    def _arm(self):
        self.api.ResetEvent(self.event)
        if not self.api.ReadDirectoryChangesW(
            self.handle,
            self.buffer,
            len(self.buffer),
            False,
            0x1B,
            None,
            ctypes.byref(self.overlapped),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self):
        from ctypes import wintypes
        import _winapi

        # CPython's wait includes its SIGINT event on the main thread, permitting
        # Ctrl+C without a timeout or polling loop.
        _winapi.WaitForMultipleObjects([self.event], False, _winapi.INFINITE)
        count = wintypes.DWORD()
        if not self.api.GetOverlappedResult(
            self.handle, ctypes.byref(self.overlapped), ctypes.byref(count), False
        ):
            error = ctypes.get_last_error()
            if error != 1022:  # ERROR_NOTIFY_ENUM_DIR: overflow -> rescan.
                raise ctypes.WinError(error)
        # Arm before draining: notifications during the scan stay queued.
        self._arm()

    def close(self):
        from ctypes import wintypes

        self.api.CancelIoEx(self.handle, ctypes.byref(self.overlapped))
        count = wintypes.DWORD()
        self.api.GetOverlappedResult(
            self.handle, ctypes.byref(self.overlapped), ctypes.byref(count), True
        )
        self.api.CloseHandle(self.handle)
        self.api.CloseHandle(self.event)


def native_notifications(directory: Path):
    if sys.platform == "win32":
        return _WindowsNotifications(directory)
    if sys.platform.startswith("linux"):
        return _LinuxNotifications(directory)
    if hasattr(select, "kqueue"):
        return _KqueueNotifications(directory)
    raise BoardError(f"native filesystem notifications unsupported on {sys.platform}")


def _save_cursor(path: Path, actor: str, root: Path, cursor: str) -> None:
    payload = {"actor": actor, "root": str(root.resolve()), "cursor": cursor}
    write_atomic_replace(path, json.dumps(payload) + "\n")


def listen(
    root: Path, actor: str, *, cursor_file: Path | None = None, output=None, errors=None
):
    """Replay existing messages (or resume cursor), then block until OS changes."""
    output = sys.stdout if output is None else output
    errors = sys.stderr if errors is None else errors
    require_message_recipient(actor, root)
    directory = _runtime_directory(root / "messages")
    directory_identity = directory.stat()
    if cursor_file is not None and cursor_file.parent.resolve() == directory:
        raise BoardError("watch cursor file must be outside the messages directory")
    cursor = None
    if cursor_file is not None and cursor_file.exists():
        if cursor_file.stat().st_size > 4096:
            raise BoardError("watch cursor file is oversized")
        saved = json.loads(cursor_file.read_text(encoding="utf-8"))
        if (
            not isinstance(saved, dict)
            or saved.get("actor") != actor
            or saved.get("root") != str(root.resolve())
            or not isinstance(saved.get("cursor"), str)
        ):
            raise BoardError(
                "watch cursor file belongs to another actor/repository or is invalid"
            )
        cursor = saved["cursor"]
    watcher = native_notifications(directory)
    ready = False
    malformed = 0
    try:
        while True:
            page = message_changes(root, actor=actor, cursor=cursor)
            for event in page["events"]:
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()
            next_cursor = page["cursor"]
            if cursor_file is not None and next_cursor != cursor:
                _save_cursor(cursor_file, actor, root, next_cursor)
            cursor = next_cursor
            if page["malformed"] != malformed:
                malformed = page["malformed"]
                print(
                    f"watch: skipped {malformed} malformed message files",
                    file=errors,
                    flush=True,
                )
            if page["has_more"]:
                continue
            if not ready:
                output.write(
                    json.dumps(
                        {"type": "watch.ready", "actor": actor, "cursor": cursor}
                    )
                    + "\n"
                )
                output.flush()
                ready = True
            watcher.wait()
            # Fail explicitly if the watched directory was replaced or removed.
            if not os.path.samestat(directory_identity, (root / "messages").stat()):
                raise BoardError("message directory replaced; restart listener")
    finally:
        watcher.close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--cursor-file",
        type=Path,
        help="resume cursor; first run replays existing addressed messages",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        listen(board_root(args.repo), args.actor, cursor_file=args.cursor_file)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0
    except (BoardError, OSError, ValueError) as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 1
    return 0
