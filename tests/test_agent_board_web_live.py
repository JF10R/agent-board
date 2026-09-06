"""Live refresh plumbing of the board web server: the change token and the UI cache headers.

The token must move when the store moves (data) or when a served file is edited (ui); the HTML/JS/CSS
must be revalidated on every load, so a UI edit needs a browser reload, never a server restart.
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import struct
import sys
import time
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_board import cli as board  # noqa: E402
from agent_board import web  # noqa: E402


@pytest.fixture()
def server(tmp_path: Path):
    instance = web.create_server(board.initialize(tmp_path / "board"), "127.0.0.1", 0, token="t0ken")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def _get(instance, path: str, headers: dict[str, str] | None = None):
    host, port = instance.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("GET", path, headers={"Host": f"{host}:{port}", **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_version_reports_both_tokens(server) -> None:
    status, _, body = _get(server, "/api/version")
    assert status == 200
    value = json.loads(body)
    assert value["ok"] is True
    assert value["data_version"] and value["ui_version"]


def test_data_version_moves_when_the_store_moves(server) -> None:
    first = json.loads(_get(server, "/api/version")[2])["data_version"]
    board.post_message(
        server.board_root,
        sender="lead",
        recipient="claude-master",
        kind="STATUS",
        priority="NORMAL",
        workstream="board",
        summary="a new message must move the token",
        body="",
        requires_ack=False,
        reply_to=None,
    )
    second = json.loads(_get(server, "/api/version")[2])
    assert second["data_version"] != first
    assert json.loads(_get(server, "/api/version")[2])["data_version"] == second["data_version"]


def test_ui_version_is_stable_until_a_file_is_touched(tmp_path: Path) -> None:
    assert web.ui_version() == web.ui_version()
    probe = tmp_path / "app.js"
    probe.write_text("one", encoding="utf-8")
    before = web._stat_part("app.js", probe)
    probe.write_text("two longer", encoding="utf-8")
    assert web._stat_part("app.js", probe) != before
    assert web._stat_part("app.js", tmp_path / "absent.js").endswith("|absent")


def test_static_ui_files_are_revalidated_not_cached(server) -> None:
    for path in ("/app.js", "/styles.css", "/selection.js"):
        status, headers, _ = _get(server, path)
        assert status == 200, path
        assert headers["Cache-Control"] == "no-cache", path
        etag = headers["ETag"]
        assert etag.strip('"') == web.ui_version()
        assert _get(server, path, {"If-None-Match": etag})[0] == 304, path


def test_index_page_is_never_conditionally_cached(server) -> None:
    """The page embeds this process's write token; ui_version() (file mtimes only) cannot see
    that, so honoring a conditional GET here would resurrect a stale token after every restart."""

    status, headers, body = _get(server, "/")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert "ETag" not in headers
    assert server.csrf_token in body.decode("utf-8")
    # A stale If-None-Match (as a browser would still send after a server restart) must not 304.
    status, _, body = _get(server, "/", {"If-None-Match": f'"{web.ui_version()}"'})
    assert status == 200
    assert server.csrf_token in body.decode("utf-8")


def test_api_responses_stay_uncached(server) -> None:
    _, headers, _ = _get(server, "/api/state")
    assert headers["Cache-Control"] == "no-store"
    assert "ETag" not in headers

# ---------------------------------------------------------------- aborted clients


class _DeadSocket(io.RawIOBase):
    """A browser that went away mid-response: every write raises, exactly as Windows does (WinError 10053)."""

    def __init__(self) -> None:
        self.writes = 0

    def write(self, data):  # noqa: D102
        self.writes += 1
        raise ConnectionAbortedError(10053, "An established connection was aborted")

    def writable(self) -> bool:
        return True


def _handler_on_a_dead_socket() -> web.AgentBoardHandler:
    handler = web.AgentBoardHandler.__new__(web.AgentBoardHandler)
    handler.wfile = _DeadSocket()
    handler.rfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 54321)
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.requestline = "GET /api/state HTTP/1.1"
    handler.close_connection = False
    handler.client_gone = False
    return handler


def test_a_write_to_a_dead_socket_is_swallowed_and_the_error_reply_is_not_attempted(capsys) -> None:
    handler = _handler_on_a_dead_socket()
    handler._send(200, b'{"ok": true}', "application/json")  # must not raise
    assert handler.client_gone is True
    assert handler.close_connection is True
    handler._error(500, "would go to a socket that is gone")
    handler._not_modified('"etag"')
    logged = capsys.readouterr().err
    assert "Traceback" not in logged
    assert logged.count("client closed the connection") == 1
    assert handler.wfile.writes == 1  # nothing was written after the first failure


ABORTS = 3


def test_the_server_survives_clients_that_abort_and_logs_no_traceback(server, capfd) -> None:
    for index in range(5):
        board.post_message(
            server.board_root, sender="lead", recipient="claude-master", kind="STATUS", priority="NORMAL",
            workstream="board", summary=f"message {index}", body="x" * 200, requires_ack=False, reply_to=None,
        )
    host, port = server.server_address[:2]
    for _ in range(ABORTS):
        raw = socket.create_connection((host, port), timeout=10)
        raw.sendall(f"GET /api/state HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # RST, not a clean close
        raw.close()

    logged, deadline = "", time.monotonic() + 5
    while logged.count("client closed the connection") < ABORTS and time.monotonic() < deadline:
        logged += capfd.readouterr().err
        time.sleep(0.05)
    logged += capfd.readouterr().err
    assert logged.count("client closed the connection") == ABORTS, logged
    assert "Traceback" not in logged, logged

    status, _, body = _get(server, "/api/version")  # the serving threads must still be there
    assert status == 200
    assert json.loads(body)["ok"] is True
