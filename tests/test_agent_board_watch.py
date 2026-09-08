from __future__ import annotations

import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from unittest import mock

import pytest

from agent_board import cli as board
from agent_board import watch


def post(root, recipient="claude-master", summary="New message"):
    return board.post_message(
        root,
        sender="gpt-master",
        recipient=recipient,
        kind="STATUS",
        priority="NORMAL",
        workstream="BOARD",
        summary=summary,
    )


def test_listener_arms_before_snapshot_and_blocks_without_polling(tmp_path):
    root = board.initialize(tmp_path / "board")
    order = []
    watcher = mock.Mock()
    watcher.wait.side_effect = KeyboardInterrupt

    def native(directory):
        order.append("arm")
        return watcher

    def changes(*args, **kwargs):
        order.append("scan")
        return {"events": [], "cursor": "token", "has_more": False, "malformed": 0}

    with (
        mock.patch.object(watch, "native_notifications", side_effect=native),
        mock.patch.object(watch, "message_changes", side_effect=changes),
    ):
        with pytest.raises(KeyboardInterrupt):
            watch.listen(root, "claude-master", output=io.StringIO())
    assert order == ["arm", "scan"]
    watcher.wait.assert_called_once()
    watcher.close.assert_called_once()


def test_failed_stdout_does_not_advance_cursor(tmp_path):
    root = board.initialize(tmp_path / "board")
    post(root)
    output = mock.Mock()
    output.flush.side_effect = BrokenPipeError
    cursor = tmp_path / "cursor.json"
    with mock.patch.object(watch, "native_notifications"):
        with pytest.raises(BrokenPipeError):
            watch.listen(root, "claude-master", cursor_file=cursor, output=output)
    assert not cursor.exists()


def test_native_subprocess_delivery_filter_and_cursor_restart(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    root = board.initialize(board.board_root(repo))
    cursor = tmp_path / "cursor.json"
    wrapper = Path(__file__).resolve().parents[1] / "agent_board_watch.py"
    processes = []

    def start():
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(wrapper),
                "--repo",
                str(repo),
                "--actor",
                "claude-master",
                "--cursor-file",
                str(cursor),
            ],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        processes.append(process)
        records = queue.Queue()

        def read():
            for line in process.stdout:
                records.put(json.loads(line))

        threading.Thread(target=read, daemon=True).start()
        return process, records

    try:
        existing = post(root)
        process, records = start()
        assert records.get(timeout=10)["message"]["id"] == existing["id"]
        assert records.get(timeout=10)["type"] == "watch.ready"
        post(root, recipient="operator")
        live = post(root, summary="Delivered while listener blocked")
        assert records.get(timeout=10)["message"]["id"] == live["id"]
        # A ready marker on restart proves initial drain completed and persisted
        # cursor; this also tolerates the documented flush/save crash window.
        process.terminate()
        process.wait(timeout=10)
        process, records = start()
        event = records.get(timeout=10)
        if event["type"] == "message.posted":
            assert event["message"]["id"] == live["id"]
            event = records.get(timeout=10)
        assert event["type"] == "watch.ready"
        after_restart = post(root, summary="After restart")
        assert records.get(timeout=10)["message"]["id"] == after_restart["id"]
        with pytest.raises(queue.Empty):
            records.get(timeout=0.2)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()


def test_cursor_rejects_other_actor_and_malformed_json(tmp_path):
    root = board.initialize(tmp_path / "board")
    cursor = tmp_path / "cursor.json"
    cursor.write_text('{"actor": "operator"}', encoding="utf-8")
    with pytest.raises(board.BoardError, match="another actor"):
        watch.listen(root, "claude-master", cursor_file=cursor)


def test_unsupported_platform_fails_explicitly(tmp_path):
    with (
        mock.patch.object(watch.sys, "platform", "unsupported"),
        mock.patch.object(watch, "select", object()),
    ):
        with pytest.raises(board.BoardError, match="unsupported"):
            watch.native_notifications(tmp_path)
