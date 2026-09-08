from pathlib import Path

import pytest

from agent_board import runtime
from agent_board.errors import BoardError


def test_lock_open_permission_denied_is_not_reported_as_contention(tmp_path, monkeypatch):
    lock = tmp_path / ".tickets.lock"
    lock.write_bytes(b"0")
    original_open = Path.open

    def denied_open(path, mode="r", *args, **kwargs):
        if path == lock and mode == "r+b":
            raise PermissionError(13, "Access denied", str(path))
        return original_open(path, mode, *args, **kwargs)

    def unexpected_sleep(seconds):
        pytest.fail("Permission errors must not enter the contention retry loop")

    monkeypatch.setattr(Path, "open", denied_open)
    monkeypatch.setattr(runtime.time, "sleep", unexpected_sleep)
    with pytest.raises(BoardError, match="cannot open filesystem lock for writing"):
        with runtime.file_lock(lock):
            pytest.fail("Denied lock cannot be acquired")
    assert lock.read_bytes() == b"0"
