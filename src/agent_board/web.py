#!/usr/bin/env python3
"""Local web dashboard for the filesystem agent board."""

from __future__ import annotations

import argparse
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from agent_board import cli as board
    from agent_board import derive
    from agent_board import tree
    from agent_board import tickets
except ImportError:  # Direct execution from within the package directory.
    import cli as board  # type: ignore[no-redef]
    import derive  # type: ignore[no-redef]
    import tree  # type: ignore[no-redef]
    import tickets  # type: ignore[no-redef]


# A browser that navigates away, switches project or supersedes a poll drops the socket mid-response.
# That is normal traffic, not a server fault: never answer a dead socket, never log a traceback for it.
CLIENT_DISCONNECT = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)
MAX_REQUEST_BYTES = 64 * 1024
STATIC_ROOT = Path(__file__).parent / "web_static"
STATIC_FILES = {
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
    "/copy.js": ("copy.js", "text/javascript; charset=utf-8"),
    "/selection.js": ("selection.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
UI_FILES = ("index.html", *(name for name, _ in STATIC_FILES.values()))
DATA_FILES = ("roadmap.v1.json", "roadmap-ext.v1.json", "tickets.v1.json", "actors.v1.json", tickets.DISPLAY_IDS_FILE)
DATA_DIRECTORIES = ("messages", "acks", "status", "ticket-events", "leases")


def _digest(parts: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _stat_part(label: str, path: Path) -> str:
    """One path's identity for a change token; a missing path is a state of its own, never an error."""

    try:
        info = path.stat()
    except OSError:
        return f"{label}|absent"
    return f"{label}|{info.st_mtime_ns}|{info.st_size}"


def data_version(root: Path) -> str:
    """Change token over the whole store: the two roadmap files plus every entry of the four data dirs."""

    parts = [_stat_part(name, root / name) for name in DATA_FILES]
    for name in DATA_DIRECTORIES:
        try:
            entries = sorted(os.scandir(root / name), key=lambda entry: entry.name)
        except OSError:
            parts.append(f"{name}/|absent")
            continue
        newest, total = 0, 0
        for entry in entries:
            try:
                info = entry.stat()
            except OSError:
                continue
            newest = max(newest, info.st_mtime_ns)
            total += info.st_size
        parts.append(f"{name}/|{len(entries)}|{newest}|{total}")
    return _digest(parts)


def ui_version() -> str:
    """Change token over the served HTML/JS/CSS; a restart is only needed for the Python server itself."""

    return _digest([_stat_part(name, STATIC_ROOT / name) for name in UI_FILES])


FULL_SCAN_LIMIT = 100_000
ACK_BACKLOG_IDS = 50
THREAD_LIMIT = 50
THREAD_WALK_LIMIT = 200


def ack_backlog(root: Path) -> dict[str, dict[str, Any]]:
    """Per recipient: how many requires_ack messages are still unacknowledged (whole board, not one page)."""

    messages, _ = board.list_recent_messages(root, limit=FULL_SCAN_LIMIT)
    backlog: dict[str, dict[str, Any]] = {
        actor: {"count": 0, "ids": [], "oldest_created_at": None}
        for actor in sorted(board.project_config(root)["message_recipients"])
    }
    for message in messages:  # newest first
        if not message["requires_ack"] or message["acked"]:
            continue
        entry = backlog[message["to"]]
        entry["count"] += 1
        if len(entry["ids"]) < ACK_BACKLOG_IDS:
            entry["ids"].append(message["id"])
        entry["oldest_created_at"] = message["created_at"]
    return backlog


def message_thread(root: Path, message_id: str) -> dict[str, Any]:
    """Whole reply chain around one message: root, then every descendant, oldest first, with bodies."""

    message_id = board._require_safe_token("message id", message_id)
    messages, _ = board.list_recent_messages(root, limit=FULL_SCAN_LIMIT)
    by_id = {message["id"]: message for message in messages}
    if message_id not in by_id:
        raise board.BoardError(f"unknown or invalid message: {message_id}")
    root_id, seen = message_id, set()
    while by_id[root_id].get("reply_to") in by_id and root_id not in seen and len(seen) < THREAD_WALK_LIMIT:
        seen.add(root_id)
        root_id = by_id[root_id]["reply_to"]
    replies: dict[str, list[str]] = {}
    for message in messages:
        parent = message.get("reply_to")
        if parent in by_id:
            replies.setdefault(parent, []).append(message["id"])
    ordered, queue = [], [root_id]
    while queue and len(ordered) < THREAD_LIMIT:
        current = queue.pop(0)
        if current in ordered:
            continue
        ordered.append(current)
        queue.extend(sorted(replies.get(current, []), key=lambda child: by_id[child]["created_at"]))
    chain = []
    for chain_id in sorted(ordered, key=lambda item: (by_id[item]["created_at"], item)):
        try:
            _, body, _ = board.read_message(root, chain_id)
        except board.BoardError:
            body = None
        chain.append({**by_id[chain_id], "body": body})
    return {"root": root_id, "requested": message_id, "messages": chain, "truncated": len(ordered) >= THREAD_LIMIT}


def is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str, unsafe_allow_non_loopback: bool = False) -> None:
    if not is_loopback_host(host) and not unsafe_allow_non_loopback:
        raise board.BoardError(
            f"refusing non-loopback bind {host!r}; pass --unsafe-allow-non-loopback explicitly"
        )


def _literal_host(value: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    try:
        parsed = urlsplit(f"//{value}")
        if parsed.username is not None or parsed.password is not None or parsed.path:
            raise ValueError
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise PermissionError("Host must be a literal loopback IP and the server port") from exc
    if not address.is_loopback or port is None:
        raise PermissionError("Host must be a literal loopback IP and the server port")
    return address, port


def _exact_object(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise board.BoardError("JSON body must be an object")
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise board.BoardError(f"missing JSON fields: {', '.join(sorted(missing))}")
    if unknown:
        raise board.BoardError(f"unknown JSON fields: {', '.join(sorted(unknown))}")
    return value


def _string(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise board.BoardError(f"{label} must be a string")
    if len(value) > maximum:
        raise board.BoardError(f"{label} exceeds {maximum} characters")
    if not allow_empty and not value.strip():
        raise board.BoardError(f"{label} must not be empty")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise board.BoardError(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise board.BoardError(f"{label} must be an integer")
    return value


def send_messages(root: Path, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    value = _exact_object(
        dict(payload),
        {"actor", "to", "kind", "priority", "workstream", "summary", "body", "requires_ack"},
        {"reply_to", "ticket_id"},
    )
    actor = _string(value["actor"], "actor", 32)
    recipient = _string(value["to"], "to", 32)
    vocabulary = board.project_config(root)
    if recipient not in vocabulary["message_recipients"] and recipient != "BOTH":
        allowed = ", ".join(sorted(vocabulary["message_recipients"]))
        raise board.BoardError(f"to must be {allowed}, or BOTH")
    reply_to = value.get("reply_to")
    if reply_to is not None:
        reply_to = _string(reply_to, "reply_to", 128)
    common = {
        "sender": actor,
        "kind": _string(value["kind"], "kind", 32),
        "priority": _string(value["priority"], "priority", 32),
        "workstream": _string(value["workstream"], "workstream", 128),
        "summary": _string(value["summary"], "summary", 300),
        "body": _string(value["body"], "body", 32768, allow_empty=True),
        "requires_ack": _boolean(value["requires_ack"], "requires_ack"),
        "reply_to": reply_to,
        "ticket_id": _optional_string(value.get("ticket_id"), "ticket_id", 128, allow_empty=False),
    }
    recipients = sorted(vocabulary["identities"]) if recipient == "BOTH" else [recipient]
    results = []
    for target in recipients:
        try:
            message = board.post_message(root, recipient=target, **common)
            results.append({"recipient": target, "ok": True, "message": message})
        except (board.BoardError, OSError, ValueError) as exc:
            results.append({"recipient": target, "ok": False, "error": str(exc)})
    successes = sum(1 for result in results if result["ok"])
    status = HTTPStatus.CREATED if successes == len(results) else HTTPStatus.MULTI_STATUS
    return int(status), {"ok": successes == len(results), "results": results}


class Project:
    """One repo's board: its own store, its own actors, its own derived views. Projects never share state."""

    def __init__(self, name: str, board_root: Path, project_root: Path | None = None):
        self.name = name
        self.board_root = board.initialize(board_root)
        self.project_root = project_root.resolve() if project_root is not None else None

    def roadmap_tree(self, since_hours: float = tree.DEFAULT_SINCE_HOURS, standby_hours: float = derive.DEFAULT_STANDBY_HOURS) -> dict[str, Any]:
        return derive.derive_roadmap_views(tree.load_roadmap_tree(self.board_root, since_hours=since_hours), standby_hours=standby_hours)

    def roadmap_tree_or_error(self, standby_hours: float = derive.DEFAULT_STANDBY_HOURS) -> dict[str, Any]:
        """State must stay live even if the sidecar is corrupt: surface the error, never hide it."""

        try:
            return self.roadmap_tree(standby_hours=standby_hours)
        except (board.BoardError, OSError) as exc:
            return {"error": str(exc), "items": [], "roots": [], "moved": [], "counts": {}, "warnings": [], "views": None}


class AgentBoardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        root: Path,
        token: str,
        *,
        project_root: Path | None = None,
        projects: Sequence[Project] | None = None,
    ):
        members = list(projects) if projects else [Project("default", root, project_root)]
        self.projects = {member.name: member for member in members}
        if len(self.projects) != len(members):
            raise board.BoardError("project names must be unique")
        self.default_project = members[0]
        self.board_root = self.default_project.board_root
        self.project_root = self.default_project.project_root
        self.csrf_token = token
        super().__init__(address, AgentBoardHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A peer that vanished is not a server error: one line, no traceback, whichever syscall noticed."""

        error = sys.exc_info()[1]
        if isinstance(error, CLIENT_DISCONNECT):
            sys.stderr.write(f"agent-board-web: {client_address[0]} client closed the connection ({type(error).__name__})\n")
            return
        super().handle_error(request, client_address)

    def project(self, name: str | None) -> Project:
        if not name:
            return self.default_project
        try:
            return self.projects[name]
        except KeyError as exc:
            raise board.BoardError(f"unknown project {name!r}; known: {', '.join(sorted(self.projects))}") from exc

    def roadmap_tree(self, since_hours: float = tree.DEFAULT_SINCE_HOURS, standby_hours: float = derive.DEFAULT_STANDBY_HOURS) -> dict[str, Any]:
        return self.default_project.roadmap_tree(since_hours, standby_hours)

    def roadmap_tree_or_error(self, standby_hours: float = derive.DEFAULT_STANDBY_HOURS) -> dict[str, Any]:
        return self.default_project.roadmap_tree_or_error(standby_hours)


def _hours_query(query: Mapping[str, list[str]], name: str, default: float) -> float:
    raw = query.get(name, [str(default)])[0]
    try:
        return float(raw)
    except ValueError as exc:
        raise board.BoardError(f"{name} must be a number of hours") from exc


class AgentBoardHandler(BaseHTTPRequestHandler):
    server: AgentBoardServer
    protocol_version = "HTTP/1.1"
    client_gone = False

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"agent-board-web: {self.address_string()} {format % args}\n")

    def _note_disconnect(self, where: str) -> None:
        """One short line, no traceback: the peer is gone, there is nothing to report to it or about it."""

        self.client_gone = True
        self.close_connection = True
        sys.stderr.write(f"agent-board-web: {self.address_string()} client closed the connection during {where}\n")

    def _send(self, status: int, body: bytes, content_type: str, extra: Mapping[str, str] | None = None) -> None:
        if self.client_gone:
            return
        try:
            self._write_response(status, body, content_type, extra)
        except CLIENT_DISCONNECT:
            self._note_disconnect("the response")

    def _write_response(self, status: int, body: bytes, content_type: str, extra: Mapping[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if not (extra or {}).get("Cache-Control"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _not_modified(self, etag: str) -> None:
        if self.client_gone:
            return
        try:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
        except CLIENT_DISCONNECT:
            self._note_disconnect("the response")

    def _send_ui(self, body: bytes, content_type: str) -> None:
        """Static JS/CSS only (nothing token-bearing): no-cache + ETag so a revalidated, unedited
        file costs a 304, and an edited one lands on the next reload."""

        etag = f'"{ui_version()}"'
        if self.headers.get("If-None-Match") == etag:
            self._not_modified(etag)
            return
        self._send(HTTPStatus.OK, body, content_type, {"Cache-Control": "no-cache", "ETag": etag})

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        if self.client_gone:
            return  # the socket that would carry the error is the one that just died
        self._json(status, {"ok": False, "error": message})

    def _read_json(self) -> dict[str, Any]:
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise board.BoardError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise board.BoardError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise board.BoardError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise board.BoardError(f"request body exceeds {MAX_REQUEST_BYTES} bytes")
        raw = self.rfile.read(length)
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=lambda pairs: _reject_duplicate_keys(pairs),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise board.BoardError("request body is not valid UTF-8 JSON") from exc
        return _exact_object(value, set(), set(value) if isinstance(value, dict) else set())

    def _require_token(self) -> None:
        supplied = self.headers.get("X-Agent-Board-Token", "")
        if not secrets.compare_digest(supplied, self.server.csrf_token):
            raise PermissionError("missing or invalid board write token")

    def _validate_host(self) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
        address, port = _literal_host(self.headers.get("Host", ""))
        if port != self.server.server_port:
            raise PermissionError("Host port does not match the board server")
        bound = ipaddress.ip_address(self.server.server_address[0])
        if bound.is_loopback and address != bound:
            raise PermissionError("Host address does not match the board server")
        return address, port

    def _require_loopback_bound_request(self) -> None:
        """Extra guard for filesystem-touching routes (e.g. opening Explorer):

        refuse unless the server itself is bound to a loopback address AND
        the connecting client is loopback too, independent of the Host/Origin/
        token checks every POST already passes.
        """

        if not is_loopback_host(str(self.server.server_address[0])):
            raise PermissionError("server is not bound to a loopback address")
        if not is_loopback_host(str(self.client_address[0])):
            raise PermissionError("request did not originate from loopback")

    def _validate_origin(
        self, expected: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]
    ) -> None:
        origin = self.headers.get("Origin", "")
        try:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "http"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError as exc:
            raise PermissionError("POST Origin must exactly match the loopback board origin") from exc
        if (address, port) != expected:
            raise PermissionError("POST Origin must exactly match the loopback board origin")

    def _query_project(self) -> "Project":
        query = parse_qs(urlsplit(self.path).query)
        return self.server.project(query.get("project", [""])[0])

    def do_GET(self) -> None:
        try:
            self._validate_host()
            path = urlsplit(self.path).path
            project = self._query_project() if path.startswith("/api/") else self.server.default_project
            if path == "/":
                # Never conditionally cached: the body embeds this process's write token, which
                # ui_version() (file mtimes only) cannot see, so a 304 here would resurrect a
                # stale token after every server restart no browser reload could then clear.
                template = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
                page = template.replace("__BOARD_TOKEN__", self.server.csrf_token).encode("utf-8")
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            elif path in STATIC_FILES:
                filename, content_type = STATIC_FILES[path]
                self._send_ui((STATIC_ROOT / filename).read_bytes(), content_type)
            elif path == "/api/projects":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "default": self.server.default_project.name,
                        "projects": [
                            {"name": member.name, "board_root": str(member.board_root)}
                            for member in self.server.projects.values()
                        ],
                    },
                )
            elif path == "/api/version":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "data_version": data_version(project.board_root),
                        "ui_version": ui_version(),
                        "projects": {
                            member.name: data_version(member.board_root)
                            for member in self.server.projects.values()
                        },
                    },
                )
            elif path == "/api/state":
                query = parse_qs(urlsplit(self.path).query)
                standby_hours = derive.validate_standby_hours(_hours_query(query, "standby", derive.DEFAULT_STANDBY_HOURS))
                messages, messages_meta = board.list_recent_messages(
                    project.board_root, limit=board.STATE_MESSAGE_LIMIT
                )
                vocabulary = board.project_config(project.board_root)
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "project": project.name,
                        "messages": messages,
                        "messages_meta": messages_meta,
                        "status": board.list_status(project.board_root),
                        "roadmap": board.list_roadmap(project.board_root),
                        "roadmap_tree": project.roadmap_tree_or_error(standby_hours),
                        "ack_backlog": ack_backlog(project.board_root),
                        "tickets": ticket_list(project.board_root),
                        "leases": tickets.list_leases(project.board_root),
                        "actors": tickets.list_actors(project.board_root),
                        "choices": {
                            "identities": sorted(vocabulary["identities"]),
                            "message_senders": sorted(vocabulary["message_senders"]),
                            "message_recipients": sorted(vocabulary["message_recipients"]),
                            "message_actors": sorted(vocabulary["message_senders"]),
                            "kinds": sorted(board.KINDS),
                            "priorities": sorted(board.PRIORITIES),
                            "roadmap_statuses": sorted(board.ROADMAP_STATUSES),
                            "roadmap_owners": sorted(vocabulary["roadmap_owners"]),
                            "roadmap_kinds": sorted(tree.ITEM_KINDS),
                            "workstreams": list(vocabulary["workstreams"]),
                            "ticket_stages": list(tickets.STAGES),
                            "ticket_kinds": list(tickets.TICKET_KINDS),
                            "ticket_dep_types": list(tickets.DEP_TYPES),
                            "ticket_review_verdicts": list(tickets.REVIEW_VERDICTS),
                        },
                    },
                )
            elif path.startswith("/api/messages/") and path.endswith("/thread"):
                message_id = unquote(path.removeprefix("/api/messages/").removesuffix("/thread"))
                self._json(HTTPStatus.OK, {"ok": True, **message_thread(project.board_root, message_id)})
            elif path.startswith("/api/messages/"):
                message_id = unquote(path.removeprefix("/api/messages/"))
                metadata, body, raw = board.read_message(project.board_root, message_id)
                acked = board._ack_path(project.board_root, message_id, metadata["to"]).is_file()
                self._json(HTTPStatus.OK, {"ok": True, "message": metadata, "body": body, "raw": raw, "acked": acked})
            elif path == "/api/messages-folder":
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "path": str(project.board_root / "messages")},
                )
            elif path == "/api/ack-backlog":
                self._json(HTTPStatus.OK, {"ok": True, "ack_backlog": ack_backlog(project.board_root)})
            elif path == "/api/roadmap-tree":
                query = parse_qs(urlsplit(self.path).query)
                since_hours = _hours_query(query, "since", tree.DEFAULT_SINCE_HOURS)
                standby_hours = _hours_query(query, "standby", derive.DEFAULT_STANDBY_HOURS)
                self._json(HTTPStatus.OK, {"ok": True, "tree": project.roadmap_tree(since_hours, standby_hours)})
            elif path == "/api/roadmap":
                self._json(HTTPStatus.OK, {"ok": True, "items": board.list_roadmap(project.board_root)})
            elif path.startswith("/api/roadmap/"):
                item_id = unquote(path.removeprefix("/api/roadmap/"))
                self._json(HTTPStatus.OK, {"ok": True, "item": board.get_roadmap_item(project.board_root, item_id)})
            elif path == "/api/tickets/tree":
                self._json(HTTPStatus.OK, {"ok": True, "tree": tickets.ticket_tree(project.board_root)})
            elif path == "/api/tickets/critical-path":
                self._json(HTTPStatus.OK, {"ok": True, "critical_path": tickets.critical_path(project.board_root)})
            elif path == "/api/tickets/metrics":
                self._json(HTTPStatus.OK, {"ok": True, "metrics": tickets.metrics_digest(project.board_root)})
            elif path == "/api/tickets/wip":
                self._json(HTTPStatus.OK, {"ok": True, "wip": tickets.wip_by_agent(project.board_root)})
            elif path == "/api/tickets":
                query = parse_qs(urlsplit(self.path).query)
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "tickets": ticket_list(
                            project.board_root,
                            stage=query.get("stage", [None])[0],
                            assignee=query.get("assignee", [None])[0],
                            parent_id=query.get("parent", [None])[0],
                            include_archived=query.get("include_archived", ["false"])[0] == "true",
                        ),
                    },
                )
            elif path.startswith("/api/tickets/") and path.endswith("/export"):
                ticket_id = unquote(path.removeprefix("/api/tickets/").removesuffix("/export"))
                self._send(HTTPStatus.OK, tickets.export_ticket_markdown(project.board_root, ticket_id).encode("utf-8"), "text/markdown; charset=utf-8")
            elif path.startswith("/api/tickets/") and path.endswith("/verify"):
                ticket_id = unquote(path.removeprefix("/api/tickets/").removesuffix("/verify"))
                ok, detail = tickets.verify_ticket_chain(project.board_root, ticket_id)
                self._json(HTTPStatus.OK, {"ok": True, "chain_ok": ok, "detail": detail})
            elif path.startswith("/api/tickets/"):
                ticket_id = unquote(path.removeprefix("/api/tickets/"))
                self._json(HTTPStatus.OK, {"ok": True, "ticket": ticket_detail(project.board_root, ticket_id)})
            elif path == "/api/actors":
                query = parse_qs(urlsplit(self.path).query)
                self._json(HTTPStatus.OK, {"ok": True, "actors": tickets.list_actors(project.board_root, role=query.get("role", [None])[0])})
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except CLIENT_DISCONNECT:
            self._note_disconnect("the request")
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except board.BoardError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        try:
            expected_origin = self._validate_host()
            self._validate_origin(expected_origin)
            self._require_token()
            path = urlsplit(self.path).path
            project = self._query_project()
            if path == "/api/open-messages-folder":
                self._require_loopback_bound_request()
                os.startfile(str(project.board_root / "messages"))  # type: ignore[attr-defined]
                self._send(HTTPStatus.NO_CONTENT, b"", "application/octet-stream")
                return
            payload = self._read_json()
            if "project" in payload:  # a POST may name its project in the body as well as in the query
                project = self.server.project(_string(payload.pop("project"), "project", 64))
            if path == "/api/messages":
                status, value = send_messages(project.board_root, payload)
                self._json(status, value)
            elif path == "/api/acks":
                value = _exact_object(payload, {"actor", "message_id"})
                ack = board.acknowledge(
                    project.board_root,
                    actor=_string(value["actor"], "actor", 32),
                    message_id=_string(value["message_id"], "message_id", 128),
                )
                self._json(HTTPStatus.CREATED, {"ok": True, "ack": ack})
            elif path == "/api/roadmap":
                value = _exact_object(
                    payload,
                    {"actor", "id", "title", "summary", "status", "owner", "progress", "blocker", "expected_revision"},
                    {"depends_on", "kind", "impact", "standby"},
                )
                extension = _roadmap_extension(value)
                actor = _string(value["actor"], "actor", 32)
                item = board.upsert_roadmap_item(
                    project.board_root,
                    actor=actor,
                    item_id=_string(value["id"], "id", 128),
                    title=_string(value["title"], "title", 200),
                    summary=_string(value["summary"], "summary", 2000),
                    status=_string(value["status"], "status", 32),
                    owner=_string(value["owner"], "owner", 32),
                    progress=_integer(value["progress"], "progress"),
                    blocker=_string(value["blocker"], "blocker", 2000, allow_empty=True),
                    expected_revision=_integer(value["expected_revision"], "expected_revision"),
                )
                if extension:
                    item = tree.annotate_roadmap_item(project.board_root, actor=actor, item_id=item["id"], **extension)
                self._json(HTTPStatus.OK, {"ok": True, "item": item})
            elif path == "/api/tickets":
                value = _exact_object(
                    payload, {"actor", "title"},
                    {"id", "kind", "summary", "body", "parent", "acceptance_criteria", "assignee", "reviewer", "subagents"},
                )
                ticket = tickets.create_ticket(
                    project.board_root,
                    actor=_string(value["actor"], "actor", 128),
                    ticket_id=_string(value["id"], "id", 128) if "id" in value else "ticket-" + secrets.token_hex(16),
                    title=_string(value["title"], "title", 300),
                    kind=_string(value["kind"], "kind", 32) if "kind" in value else "ENGINEERING",
                    summary=_string(value["summary"], "summary", 300, allow_empty=True) if "summary" in value else "",
                    body=_string(value["body"], "body", 32768, allow_empty=True) if "body" in value else "",
                    parent_id=_optional_string(value.get("parent"), "parent", 128, allow_empty=False),
                    acceptance_criteria=_string_list(value["acceptance_criteria"], "acceptance_criteria", 300) if "acceptance_criteria" in value else None,
                    assignee=_optional_string(value.get("assignee"), "assignee", 128, allow_empty=False),
                    reviewer=_optional_string(value.get("reviewer"), "reviewer", 128, allow_empty=False),
                    subagents=_string_list(value["subagents"], "subagents", 128) if "subagents" in value else None,
                )
                self._json(HTTPStatus.CREATED, {"ok": True, "ticket": ticket})
            elif path.startswith("/api/tickets/"):
                remainder = unquote(path.removeprefix("/api/tickets/"))
                ticket_id, separator, action = remainder.rpartition("/")
                if not separator:
                    raise board.BoardError("ticket action path must be /api/tickets/<id>/<action>")
                self._json(HTTPStatus.OK, {"ok": True, "ticket": ticket_action(project.board_root, ticket_id, action, payload)})
            elif path == "/api/actors":
                value = _exact_object(payload, {"name", "role"}, {"display", "master"})
                actor = tickets.register_actor(
                    project.board_root,
                    _string(value["name"], "name", 128),
                    role=_string(value["role"], "role", 32),
                    display=_string(value["display"], "display", 128, allow_empty=True) if "display" in value else "",
                    master=_optional_string(value.get("master"), "master", 128, allow_empty=False),
                )
                self._json(HTTPStatus.CREATED, {"ok": True, "actor": actor})
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except CLIENT_DISCONNECT:
            self._note_disconnect("the request")
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except board.BoardError as exc:
            status = HTTPStatus.CONFLICT if "revision conflict" in str(exc) else HTTPStatus.BAD_REQUEST
            self._error(status, str(exc))
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))


def _roadmap_extension(value: Mapping[str, Any]) -> dict[str, Any]:
    """Optional sidecar fields of a roadmap POST; a key that is absent stays untouched."""

    extension: dict[str, Any] = {}
    if "depends_on" in value:
        deps = value["depends_on"]
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            raise board.BoardError("depends_on must be a list of item ids")
        extension["depends_on"] = [_string(dep, "dependency id", 128) for dep in deps]
    if "kind" in value:
        extension["kind"] = None if value["kind"] in (None, "") else _string(value["kind"], "kind", 32)
    if "impact" in value:
        extension["impact"] = None if value["impact"] is None else _string(value["impact"], "impact", 300, allow_empty=True)
    if "standby" in value:
        extension["standby"] = None if value["standby"] in (None, "") else _string(value["standby"], "standby", 300)
    return extension


def _optional_string(value: Any, label: str, maximum: int, *, allow_empty: bool = True) -> str | None:
    if value is None:
        return None
    return _string(value, label, maximum, allow_empty=allow_empty)


def _optional_int(value: Any, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _string_list(value: Any, label: str, item_cap: int) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise board.BoardError(f"{label} must be a list of strings")
    return [_string(item, f"{label} entry", item_cap) for item in value]


def ticket_list(root: Path, **filters: Any) -> list[dict[str, Any]]:
    items = tickets.list_tickets(root, **filters)
    for item in items:
        item["open_blockers"] = tickets._open_blockers(root, item)
    return items


def ticket_detail(root: Path, ticket_id: str) -> dict[str, Any]:
    ticket = tickets.get_ticket(root, ticket_id)
    ticket["open_blockers"] = tickets._open_blockers(root, ticket)
    messages = {}
    malformed = 0
    for path in (root / "messages").glob("*.md"):
        try:
            metadata = board._read_message_metadata(path)
            board._validate_state_message(path, metadata)
        except (board.BoardError, board._OversizedRuntimeFile, OSError, UnicodeError, ValueError, TypeError):
            malformed += 1
            continue
        metadata["acked"] = board._ack_path(root, metadata["id"], metadata["to"]).is_file()
        messages[metadata["id"]] = metadata
    children: dict[str, list[str]] = {}
    for message in messages.values():
        if message.get("reply_to") in messages:
            children.setdefault(message["reply_to"], []).append(message["id"])
    # Each message has one parent. Explicit references start independent roots;
    # inheritance stops at another explicit reference, even inside a cycle.
    effective_ticket = {}
    pending = [(message["id"], message["ticket_id"]) for message in messages.values() if message.get("ticket_id")]
    while pending:
        message_id, reference = pending.pop()
        if message_id in effective_ticket:
            continue
        effective_ticket[message_id] = reference
        pending.extend((child, reference) for child in children.get(message_id, []) if not messages[child].get("ticket_id"))

    def newest_first(message: Mapping[str, Any]) -> tuple[Any, str]:
        return (board.datetime.fromisoformat(message["created_at"].replace("Z", "+00:00")), message["id"])

    linked = []
    for message_id, reference in effective_ticket.items():
        if reference != ticket_id:
            continue
        message = {**messages[message_id], "linked_ticket_id": reference}
        replies = [{**messages[child], "linked_ticket_id": effective_ticket.get(child)} for child in children.get(message_id, [])]
        message["replies"] = sorted(replies, key=newest_first, reverse=True)
        message["answered"] = bool(replies)
        linked.append(message)
    linked.sort(key=newest_first, reverse=True)
    ticket["linked_messages"] = linked
    ticket["linked_messages_malformed"] = malformed
    return ticket


def ticket_action(root: Path, ticket_id: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Dispatch one POST /api/tickets/<id>/<action> body to the matching tickets.py call."""

    if action == "upsert":
        value = _exact_object(
            payload, {"actor", "expected_revision"},
            {"title", "summary", "body", "kind", "acceptance_criteria", "reviewer", "subagents"},
        )
        fields: dict[str, Any] = {}
        if "title" in value:
            fields["title"] = _string(value["title"], "title", 300)
        if "summary" in value:
            fields["summary"] = _string(value["summary"], "summary", 300, allow_empty=True)
        if "body" in value:
            fields["body"] = _string(value["body"], "body", 32768, allow_empty=True)
        if "kind" in value:
            fields["kind"] = _string(value["kind"], "kind", 32)
        if "acceptance_criteria" in value:
            fields["acceptance_criteria"] = _string_list(value["acceptance_criteria"], "acceptance_criteria", 300)
        if "reviewer" in value:
            fields["reviewer"] = _string(value["reviewer"], "reviewer", 128)
        if "subagents" in value:
            fields["subagents"] = _string_list(value["subagents"], "subagents", 128)
        return tickets.upsert_ticket(
            root, ticket_id, actor=_string(value["actor"], "actor", 128),
            expected_revision=_integer(value["expected_revision"], "expected_revision"), **fields,
        )
    if action == "assign":
        value = _exact_object(payload, {"actor", "assignee"}, {"expected_revision", "ttl_sec", "reviewer", "subagents"})
        return tickets.assign_ticket(
            root, ticket_id,
            actor=_string(value["actor"], "actor", 128),
            assignee=_string(value["assignee"], "assignee", 128),
            expected_revision=_optional_int(value.get("expected_revision"), "expected_revision"),
            ttl_sec=_integer(value["ttl_sec"], "ttl_sec") if "ttl_sec" in value else tickets.DEFAULT_LEASE_TTL_SEC,
            reviewer=_optional_string(value.get("reviewer"), "reviewer", 128, allow_empty=False),
            subagents=_string_list(value["subagents"], "subagents", 128) if "subagents" in value else None,
        )
    if action == "heartbeat":
        value = _exact_object(payload, {"actor"}, {"ttl_sec"})
        return tickets.heartbeat_ticket(
            root, ticket_id, actor=_string(value["actor"], "actor", 128),
            ttl_sec=_optional_int(value.get("ttl_sec"), "ttl_sec"),
        )
    if action == "transition":
        value = _exact_object(payload, {"actor", "stage"}, {"expected_revision", "summary"})
        return tickets.transition_ticket(
            root, ticket_id, actor=_string(value["actor"], "actor", 128), stage=_string(value["stage"], "stage", 32),
            expected_revision=_optional_int(value.get("expected_revision"), "expected_revision"),
            summary=_string(value["summary"], "summary", 300, allow_empty=True) if "summary" in value else "",
        )
    if action == "comment":
        value = _exact_object(payload, {"actor", "summary"}, {"body", "expected_revision"})
        return tickets.comment_ticket(
            root, ticket_id, actor=_string(value["actor"], "actor", 128), summary=_string(value["summary"], "summary", 300),
            body=_string(value["body"], "body", 32768, allow_empty=True) if "body" in value else "",
            expected_revision=_optional_int(value.get("expected_revision"), "expected_revision"),
        )
    if action == "worklog":
        value = _exact_object(payload, {"actor", "summary", "evidence"}, {"expected_revision"})
        evidence = value["evidence"]
        if not isinstance(evidence, dict):
            raise board.BoardError("evidence must be a JSON object")
        return tickets.add_worklog(
            root, ticket_id, actor=_string(value["actor"], "actor", 128), summary=_string(value["summary"], "summary", 300),
            evidence=evidence, expected_revision=_optional_int(value.get("expected_revision"), "expected_revision"),
        )
    if action == "review":
        value = _exact_object(payload, {"actor", "verdict", "summary"}, {"findings", "expected_revision"})
        return tickets.review_ticket(
            root, ticket_id, actor=_string(value["actor"], "actor", 128), verdict=_string(value["verdict"], "verdict", 32),
            summary=_string(value["summary"], "summary", 300),
            findings=_string_list(value["findings"], "findings", 300) if "findings" in value else None,
            expected_revision=_optional_int(value.get("expected_revision"), "expected_revision"),
        )
    if action == "done":
        value = _exact_object(payload, {"actor"}, {"summary", "expected_revision", "force"})
        return tickets.mark_ticket_done(
            root, ticket_id, actor=_string(value["actor"], "actor", 128),
            summary=_string(value["summary"], "summary", 300, allow_empty=True) if "summary" in value else "",
            expected_revision=_optional_int(value.get("expected_revision"), "expected_revision"),
            force=_boolean(value["force"], "force") if "force" in value else False,
        )
    if action == "dependencies":
        value = _exact_object(payload, {"actor", "dep_type", "target"})
        return tickets.add_dependency(
            root, ticket_id, actor=_string(value["actor"], "actor", 128),
            dep_type=_string(value["dep_type"], "dep_type", 32), target=_string(value["target"], "target", 128),
        )
    if action == "archive":
        value = _exact_object(payload, {"actor"})
        return tickets.archive_ticket(root, ticket_id, actor=_string(value["actor"], "actor", 128))
    raise board.BoardError(f"unknown ticket action: {action}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise board.BoardError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def create_server(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    token: str | None = None,
    project_root: Path | None = None,
    projects: Sequence[Project] | None = None,
) -> AgentBoardServer:
    return AgentBoardServer(
        (host, port),
        root,
        token or secrets.token_urlsafe(32),
        project_root=project_root,
        projects=projects,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        action="append",
        default=[],
        help="worktree whose board to serve; repeat for several projects (name = the worktree's directory name)",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="serve PATH's board under NAME; repeatable, combines with --repo",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--unsafe-allow-non-loopback", action="store_true")
    parser.add_argument(
        "--board-root",
        type=Path,
        help="serve this board directory (e.g. a copied snapshot) instead of the repo's shared runtime; single project only",
    )
    return parser


def _projects_from_args(args: argparse.Namespace) -> list[Project]:
    """--repo (repeatable) and --project NAME=PATH build the same list; the first entry is the default project."""

    specs: list[tuple[str, Path]] = [(Path(repo).resolve().name, Path(repo)) for repo in args.repo]
    for raw in args.project:
        name, separator, value = str(raw).partition("=")
        if not separator or not name.strip() or not value.strip():
            raise board.BoardError(f"--project must be NAME=PATH, got {raw!r}")
        specs.append((name.strip(), Path(value.strip())))
    if not specs:
        raise board.BoardError("pass at least one --repo or --project NAME=PATH")
    if args.board_root is not None and len(specs) > 1:
        raise board.BoardError("--board-root serves a single project; drop it or pass one repo")
    return [
        Project(
            name,
            args.board_root.resolve() if args.board_root is not None else board.board_root(repo),
            repo,
        )
        for name, repo in specs
    ]


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_bind_host(args.host, args.unsafe_allow_non_loopback)
    if not 0 <= args.port <= 65535:
        raise board.BoardError("port must be from 0 through 65535")
    projects = _projects_from_args(args)
    server = create_server(projects[0].board_root, args.host, args.port, projects=projects)
    host, port = server.server_address[:2]
    print(f"Agent board: http://{host}:{port}/")
    for member in projects:
        print(f"  project {member.name}: {member.board_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main() -> int:
    try:
        return run()
    except (board.BoardError, OSError, ValueError) as exc:
        print(f"agent-board-web: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
