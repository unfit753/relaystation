#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hmac
import json
import os
import re
import socket
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

VERSION = "0.1.0"
ROOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
MAX_TEXT_BYTES = int(os.getenv("RELAYSTATION_MAX_TEXT_BYTES", "65536"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def validate_room(room: str) -> str:
    room = str(room or "").strip()
    if not ROOM_RE.fullmatch(room):
        raise ValueError("room must start with a letter/number and use only letters, numbers, dot, underscore, colon, or dash")
    return room


def clean_string(value: Any, name: str, max_len: int, default: str | None = None) -> str:
    if value is None:
        if default is None:
            raise ValueError(f"{name} is required")
        value = default
    value = str(value).strip()
    if not value:
        if default is None:
            raise ValueError(f"{name} is required")
        value = default
    if len(value) > max_len:
        raise ValueError(f"{name} must be at most {max_len} characters")
    return value


def default_sender() -> str:
    env_sender = os.getenv("RELAYSTATION_AGENT")
    if env_sender:
        return env_sender
    return f"{getpass.getuser()}@{socket.gethostname()}"


class RelayStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        if self.path != ":memory:":
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @contextmanager
    def session(self) -> sqlite3.Connection:
        con = self.connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def init_db(self) -> None:
        with self.session() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'message',
                    text TEXT NOT NULL,
                    reply_to INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages(room, id);

                CREATE TABLE IF NOT EXISTS presence (
                    room TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'online',
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(room, sender)
                );
                """
            )

    @staticmethod
    def message_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "room": row["room"],
            "sender": row["sender"],
            "kind": row["kind"],
            "text": row["text"],
            "reply_to": row["reply_to"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": row["created_at"],
        }

    @staticmethod
    def presence_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "room": row["room"],
            "sender": row["sender"],
            "status": row["status"],
            "note": row["note"],
            "updated_at": row["updated_at"],
        }

    def create_message(
        self,
        room: str,
        sender: str,
        text: str,
        kind: str = "message",
        reply_to: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        room = validate_room(room)
        sender = clean_string(sender, "sender", 128)
        kind = clean_string(kind, "kind", 48, "message")
        text = str(text or "")
        if not text.strip():
            raise ValueError("text is required")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError(f"text must be at most {MAX_TEXT_BYTES} bytes")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        created_at = now_iso()
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        with self.session() as con:
            cur = con.execute(
                """
                INSERT INTO messages(room, sender, kind, text, reply_to, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (room, sender, kind, text, reply_to, metadata_json, created_at),
            )
            row = con.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self.message_from_row(row)

    def list_messages(self, room: str, after_id: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        room = validate_room(room)
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 50), 1), 200)
        with self.session() as con:
            rows = con.execute(
                """
                SELECT * FROM messages
                WHERE room = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (room, after_id, limit),
            ).fetchall()
        return [self.message_from_row(row) for row in rows]

    def list_rooms(self) -> list[str]:
        with self.session() as con:
            rows = con.execute(
                """
                SELECT room FROM messages
                UNION
                SELECT room FROM presence
                ORDER BY room ASC
                """
            ).fetchall()
        return [row["room"] for row in rows]

    def set_presence(self, room: str, sender: str, status: str, note: str = "") -> dict[str, Any]:
        room = validate_room(room)
        sender = clean_string(sender, "sender", 128)
        status = clean_string(status, "status", 48, "online")
        note = str(note or "")
        if len(note) > 512:
            raise ValueError("note must be at most 512 characters")
        updated_at = now_iso()
        with self.session() as con:
            con.execute(
                """
                INSERT INTO presence(room, sender, status, note, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(room, sender) DO UPDATE SET
                    status = excluded.status,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (room, sender, status, note, updated_at),
            )
            row = con.execute(
                "SELECT * FROM presence WHERE room = ? AND sender = ?",
                (room, sender),
            ).fetchone()
        return self.presence_from_row(row)

    def list_presence(self, room: str) -> list[dict[str, Any]]:
        room = validate_room(room)
        with self.session() as con:
            rows = con.execute(
                "SELECT * FROM presence WHERE room = ? ORDER BY updated_at DESC, sender ASC",
                (room,),
            ).fetchall()
        return [self.presence_from_row(row) for row in rows]


class RelayApp:
    def __init__(self, store: RelayStore, token: str = "", log_requests: bool = True):
        self.store = store
        self.token = token
        self.log_requests = log_requests


class RelayHandler(BaseHTTPRequestHandler):
    server_version = f"Relaystation/{VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> RelayApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.app.log_requests:
            super().log_message(fmt, *args)

    def send_json(self, status: int, data: Any) -> None:
        payload = dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_problem(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length > 1024 * 1024:
            raise ValueError("request body too large")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def is_authorized(self) -> bool:
        token = self.app.token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        provided = ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if not provided:
            provided = self.headers.get("X-Relaystation-Token", "").strip()
        return bool(provided) and hmac.compare_digest(provided, token)

    def route_parts(self) -> tuple[list[str], dict[str, list[str]]]:
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        return parts, parse_qs(parsed.query)

    def do_GET(self) -> None:
        try:
            parts, query = self.route_parts()
            if not parts:
                self.send_json(
                    200,
                    {
                        "name": "relaystation",
                        "version": VERSION,
                        "endpoints": ["/health", "/rooms", "/rooms/{room}/messages", "/rooms/{room}/presence"],
                    },
                )
                return
            if parts == ["health"]:
                self.send_json(200, {"ok": True, "version": VERSION})
                return
            if not self.is_authorized():
                self.send_problem(401, "missing or invalid bearer token")
                return
            if parts == ["rooms"]:
                self.send_json(200, {"rooms": self.app.store.list_rooms()})
                return
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "messages":
                after_id = int((query.get("after_id") or query.get("since_id") or ["0"])[0] or "0")
                limit = int((query.get("limit") or ["50"])[0] or "50")
                messages = self.app.store.list_messages(parts[1], after_id=after_id, limit=limit)
                latest_id = messages[-1]["id"] if messages else after_id
                self.send_json(200, {"messages": messages, "latest_id": latest_id})
                return
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "presence":
                self.send_json(200, {"presence": self.app.store.list_presence(parts[1])})
                return
            self.send_problem(404, "not found")
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_problem(400, str(exc))
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            self.send_problem(500, str(exc))

    def do_POST(self) -> None:
        try:
            parts, _query = self.route_parts()
            if not self.is_authorized():
                self.send_problem(401, "missing or invalid bearer token")
                return
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "messages":
                body = self.read_json_body()
                message = self.app.store.create_message(
                    room=parts[1],
                    sender=body.get("sender") or "anonymous",
                    kind=body.get("kind") or "message",
                    text=body.get("text") or "",
                    reply_to=body.get("reply_to"),
                    metadata=body.get("metadata") or {},
                )
                self.send_json(201, {"message": message})
                return
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "presence":
                body = self.read_json_body()
                presence = self.app.store.set_presence(
                    room=parts[1],
                    sender=body.get("sender") or "anonymous",
                    status=body.get("status") or "online",
                    note=body.get("note") or "",
                )
                self.send_json(200, {"presence": presence})
                return
            self.send_problem(404, "not found")
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_problem(400, str(exc))
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            self.send_problem(500, str(exc))


def run_server(bind: str, port: int, db_path: str, token: str, log_requests: bool) -> None:
    if not token and bind not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("RELAYSTATION_TOKEN is required when listening beyond localhost")
    store = RelayStore(db_path)
    server = ThreadingHTTPServer((bind, port), RelayHandler)
    server.app = RelayApp(store=store, token=token, log_requests=log_requests)  # type: ignore[attr-defined]
    auth_state = "token required" if token else "no token configured"
    print(f"Relaystation {VERSION} listening on http://{bind}:{port} ({auth_state})", file=sys.stderr)
    server.serve_forever()


def api_request(
    method: str,
    path: str,
    *,
    base_url: str | None = None,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    base_url = (base_url or os.getenv("RELAYSTATION_URL") or "http://127.0.0.1:8787").rstrip("/")
    token = token if token is not None else os.getenv("RELAYSTATION_TOKEN", "")
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urlencode({key: value for key, value in query.items() if value is not None})}"
    payload = None
    headers = {"Accept": "application/json"}
    if body is not None:
        payload = dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = raw
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"could not reach relaystation: {exc}") from exc


MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "relay_send",
        "description": "Send a message to a Relaystation room.",
        "inputSchema": {
            "type": "object",
            "required": ["room", "text"],
            "properties": {
                "room": {"type": "string", "description": "Shared room name, for example codex-claude."},
                "text": {"type": "string", "description": "Message body."},
                "sender": {"type": "string", "description": "Override sender name. Defaults to RELAYSTATION_AGENT."},
                "kind": {"type": "string", "description": "Message kind, for example message, task, status, question."},
                "reply_to": {"type": "integer", "description": "Optional message id being replied to."},
                "metadata": {"type": "object", "description": "Optional structured metadata."},
            },
        },
    },
    {
        "name": "relay_read",
        "description": "Read messages from a Relaystation room after a message id.",
        "inputSchema": {
            "type": "object",
            "required": ["room"],
            "properties": {
                "room": {"type": "string"},
                "after_id": {"type": "integer", "description": "Only return messages with id greater than this value."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "relay_presence",
        "description": "Set this agent's status in a Relaystation room.",
        "inputSchema": {
            "type": "object",
            "required": ["room"],
            "properties": {
                "room": {"type": "string"},
                "sender": {"type": "string"},
                "status": {"type": "string", "description": "online, working, waiting, offline, or any short custom status."},
                "note": {"type": "string"},
            },
        },
    },
    {
        "name": "relay_roster",
        "description": "List recent agent presence entries for a Relaystation room.",
        "inputSchema": {
            "type": "object",
            "required": ["room"],
            "properties": {"room": {"type": "string"}},
        },
    },
    {
        "name": "relay_rooms",
        "description": "List rooms known to Relaystation.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def read_mcp_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        line_text = line.decode("ascii", errors="replace").strip()
        if not line_text:
            break
        key, sep, value = line_text.partition(":")
        if sep:
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length") or "0")
    if length <= 0:
        return None
    raw = sys.stdin.buffer.read(length)
    return json.loads(raw.decode("utf-8"))


def write_mcp_message(message: dict[str, Any]) -> None:
    payload = dumps(message).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def mcp_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def mcp_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def mcp_text_result(data: Any, is_error: bool = False) -> dict[str, Any]:
    text = data if isinstance(data, str) else pretty(data)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_mcp_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    args = args or {}
    sender = args.get("sender") or default_sender()
    if name == "relay_send":
        room = validate_room(args.get("room") or "")
        data = api_request(
            "POST",
            f"/rooms/{quote(room)}/messages",
            body={
                "sender": sender,
                "text": args.get("text") or "",
                "kind": args.get("kind") or "message",
                "reply_to": args.get("reply_to"),
                "metadata": args.get("metadata") or {},
            },
        )
        return mcp_text_result(data)
    if name == "relay_read":
        room = validate_room(args.get("room") or "")
        data = api_request(
            "GET",
            f"/rooms/{quote(room)}/messages",
            query={"after_id": args.get("after_id") or 0, "limit": args.get("limit") or 50},
        )
        return mcp_text_result(data)
    if name == "relay_presence":
        room = validate_room(args.get("room") or "")
        data = api_request(
            "POST",
            f"/rooms/{quote(room)}/presence",
            body={"sender": sender, "status": args.get("status") or "online", "note": args.get("note") or ""},
        )
        return mcp_text_result(data)
    if name == "relay_roster":
        room = validate_room(args.get("room") or "")
        data = api_request("GET", f"/rooms/{quote(room)}/presence")
        return mcp_text_result(data)
    if name == "relay_rooms":
        data = api_request("GET", "/rooms")
        return mcp_text_result(data)
    raise ValueError(f"unknown tool: {name}")


def handle_mcp_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if request_id is None and method and method.startswith("notifications/"):
        return None
    try:
        if method == "initialize":
            protocol_version = params.get("protocolVersion") or "2024-11-05"
            return mcp_result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "relaystation", "version": VERSION},
                },
            )
        if method == "ping":
            return mcp_result(request_id, {})
        if method == "tools/list":
            return mcp_result(request_id, {"tools": MCP_TOOLS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                return mcp_result(request_id, call_mcp_tool(name, args))
            except Exception as exc:
                return mcp_result(request_id, mcp_text_result(str(exc), is_error=True))
        if method == "resources/list":
            return mcp_result(request_id, {"resources": []})
        if method == "prompts/list":
            return mcp_result(request_id, {"prompts": []})
        if method and method.startswith("notifications/"):
            return None
        return mcp_error(request_id, -32601, f"method not found: {method}")
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return mcp_error(request_id, -32603, str(exc))


def run_mcp() -> None:
    while True:
        message = read_mcp_message()
        if message is None:
            return
        response = handle_mcp_request(message)
        if response is not None:
            write_mcp_message(response)


def print_json(data: Any) -> None:
    print(pretty(data))


def command_send(args: argparse.Namespace) -> None:
    text = " ".join(args.text)
    data = api_request(
        "POST",
        f"/rooms/{quote(validate_room(args.room))}/messages",
        base_url=args.url,
        token=args.token,
        body={
            "sender": args.sender or default_sender(),
            "kind": args.kind,
            "text": text,
            "reply_to": args.reply_to,
            "metadata": json.loads(args.metadata_json) if args.metadata_json else {},
        },
    )
    print_json(data)


def command_read(args: argparse.Namespace) -> None:
    data = api_request(
        "GET",
        f"/rooms/{quote(validate_room(args.room))}/messages",
        base_url=args.url,
        token=args.token,
        query={"after_id": args.after_id, "limit": args.limit},
    )
    if args.json:
        print_json(data)
        return
    for message in data.get("messages", []):
        reply = f" -> {message['reply_to']}" if message.get("reply_to") else ""
        print(f"[{message['id']}{reply}] {message['created_at']} {message['sender']} ({message['kind']}):")
        print(message["text"])
        print()


def command_rooms(args: argparse.Namespace) -> None:
    data = api_request("GET", "/rooms", base_url=args.url, token=args.token)
    print_json(data)


def command_presence(args: argparse.Namespace) -> None:
    data = api_request(
        "POST",
        f"/rooms/{quote(validate_room(args.room))}/presence",
        base_url=args.url,
        token=args.token,
        body={"sender": args.sender or default_sender(), "status": args.status, "note": args.note or ""},
    )
    print_json(data)


def command_roster(args: argparse.Namespace) -> None:
    data = api_request(
        "GET",
        f"/rooms/{quote(validate_room(args.room))}/presence",
        base_url=args.url,
        token=args.token,
    )
    print_json(data)


def command_mcp_config(args: argparse.Namespace) -> None:
    script = os.path.abspath(__file__)
    agent = args.agent or default_sender()
    token_value = os.getenv("RELAYSTATION_TOKEN") if args.include_token else "<relaystation-token>"
    config = {
        "mcpServers": {
            args.name: {
                "command": sys.executable,
                "args": [script, "mcp"],
                "env": {
                    "RELAYSTATION_URL": args.url or os.getenv("RELAYSTATION_URL") or "http://127.0.0.1:8787",
                    "RELAYSTATION_TOKEN": token_value,
                    "RELAYSTATION_AGENT": agent,
                },
            }
        }
    }
    print_json(config)
    print()
    print("Codex TOML:")
    print(f'[mcp_servers.{args.name}]')
    print(f'command = "{sys.executable}"')
    print(f'args = ["{script}", "mcp"]')
    print(
        'env = { RELAYSTATION_URL = "'
        + (args.url or os.getenv("RELAYSTATION_URL") or "http://127.0.0.1:8787")
        + '", RELAYSTATION_TOKEN = "'
        + token_value
        + '", RELAYSTATION_AGENT = "'
        + agent
        + '" }'
    )


def add_client_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=os.getenv("RELAYSTATION_URL") or "http://127.0.0.1:8787")
    parser.add_argument("--token", default=os.getenv("RELAYSTATION_TOKEN") or "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Relaystation server, CLI, and MCP adapter.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("server", aliases=["serve"], help="Run the HTTP relay server.")
    serve.add_argument("--bind", default=os.getenv("RELAYSTATION_BIND") or "127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.getenv("RELAYSTATION_PORT") or "8787"))
    serve.add_argument("--db", default=os.getenv("RELAYSTATION_DB") or "./data/relaystation.sqlite")
    serve.add_argument("--token", default=os.getenv("RELAYSTATION_TOKEN") or "")
    serve.add_argument("--quiet", action="store_true", help="Disable per-request HTTP logging.")
    serve.set_defaults(func=lambda args: run_server(args.bind, args.port, args.db, args.token, not args.quiet))

    mcp = sub.add_parser("mcp", help="Run the MCP stdio adapter.")
    mcp.set_defaults(func=lambda _args: run_mcp())

    send = sub.add_parser("send", help="Send a message.")
    add_client_args(send)
    send.add_argument("room")
    send.add_argument("text", nargs="+")
    send.add_argument("--sender")
    send.add_argument("--kind", default="message")
    send.add_argument("--reply-to", type=int)
    send.add_argument("--metadata-json")
    send.set_defaults(func=command_send)

    read = sub.add_parser("read", help="Read room messages.")
    add_client_args(read)
    read.add_argument("room")
    read.add_argument("--after-id", type=int, default=0)
    read.add_argument("--limit", type=int, default=50)
    read.add_argument("--json", action="store_true")
    read.set_defaults(func=command_read)

    rooms = sub.add_parser("rooms", help="List rooms.")
    add_client_args(rooms)
    rooms.set_defaults(func=command_rooms)

    presence = sub.add_parser("presence", help="Set presence in a room.")
    add_client_args(presence)
    presence.add_argument("room")
    presence.add_argument("--sender")
    presence.add_argument("--status", default="online")
    presence.add_argument("--note")
    presence.set_defaults(func=command_presence)

    roster = sub.add_parser("roster", help="List room presence.")
    add_client_args(roster)
    roster.add_argument("room")
    roster.set_defaults(func=command_roster)

    config = sub.add_parser("mcp-config", help="Print MCP config snippets.")
    config.add_argument("--name", default="relaystation")
    config.add_argument("--url", default=os.getenv("RELAYSTATION_URL") or "http://127.0.0.1:8787")
    config.add_argument("--agent")
    config.add_argument("--include-token", action="store_true")
    config.set_defaults(func=command_mcp_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
