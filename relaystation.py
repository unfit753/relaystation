#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

VERSION = "0.6.0"
ROOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
MAX_TEXT_BYTES = int(os.getenv("RELAYSTATION_MAX_TEXT_BYTES", "65536"))
SESSION_ID_CACHE: str | None = None
ADDRESS_CACHE: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def load_json_value(value: str, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except json.JSONDecodeError:
        return default
    return parsed if isinstance(parsed, type(default)) else default


def clean_string_list(values: Any, name: str, max_items: int = 32, max_len: int = 80) -> list[str]:
    if values is None or values == "":
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list of strings")
    cleaned: list[str] = []
    for value in values:
        item = clean_string(value, name, max_len, "")
        if item and item not in cleaned:
            cleaned.append(item)
    if len(cleaned) > max_items:
        raise ValueError(f"{name} must contain at most {max_items} items")
    return cleaned


def validate_room(room: str) -> str:
    room = str(room or "").strip()
    if not ROOM_RE.fullmatch(room):
        raise ValueError("room must start with a letter/number and use only letters, numbers, dot, underscore, colon, or dash")
    return room


def validate_address(address: str) -> str:
    address = str(address or "").strip()
    if not ADDRESS_RE.fullmatch(address):
        raise ValueError("address must start with a letter/number and use only letters, numbers, dot, underscore, colon, or dash")
    return address


def slug(value: str, default: str = "x", max_len: int = 28) -> str:
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "").strip())
    value = value.strip("-_.:")
    if not value:
        value = default
    if not value[0].isalnum():
        value = f"{default}-{value}"
    return value[:max_len].strip("-_.:") or default


def direct_room_name(address_a: str, address_b: str, project: str = "", topic: str = "") -> str:
    participants = sorted([slug(address_a, "a"), slug(address_b, "b")])
    pieces = ["dm", slug(project, "general", 24), *participants]
    topic_slug = slug(topic, "", 20)
    if topic_slug:
        pieces.append(topic_slug)
    room = ":".join(pieces)
    if len(room) <= 96:
        return validate_room(room)
    digest = hashlib.sha1(room.encode("utf-8")).hexdigest()[:10]
    compact = ":".join([pieces[0], pieces[1][:16], participants[0][:18], participants[1][:18], digest])
    return validate_room(compact[:96])


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


def default_session_id() -> str:
    global SESSION_ID_CACHE
    configured = os.getenv("RELAYSTATION_SESSION_ID") or os.getenv("RELAYSTATION_SESSION")
    if configured:
        return clean_string(configured, "session_id", 128)
    if SESSION_ID_CACHE is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        SESSION_ID_CACHE = f"{stamp}-{os.getpid()}"
    return SESSION_ID_CACHE


def default_address() -> str:
    return ADDRESS_CACHE or os.getenv("RELAYSTATION_ADDRESS") or os.getenv("RELAYSTATION_CALLSIGN") or ""


def current_identity(
    *,
    agent: str | None = None,
    origin: str | None = None,
    session_id: str | None = None,
    sender: str | None = None,
    address: str | None = None,
) -> dict[str, str]:
    agent_value = clean_string(agent or os.getenv("RELAYSTATION_AGENT") or default_sender(), "agent", 128)
    origin_value = clean_string(origin or os.getenv("RELAYSTATION_ORIGIN") or socket.gethostname(), "origin", 128)
    session_value = clean_string(session_id or default_session_id(), "session_id", 128)
    sender_value = clean_string(sender or f"{agent_value}@{origin_value}/{session_value}", "sender", 256)
    address_value = str(address if address is not None else default_address()).strip()
    if address_value:
        address_value = validate_address(address_value)
    return {
        "agent": agent_value,
        "origin": origin_value,
        "session_id": session_value,
        "sender": sender_value,
        "address": address_value,
    }


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
                    sender_address TEXT NOT NULL DEFAULT '',
                    sender_agent TEXT NOT NULL DEFAULT '',
                    sender_origin TEXT NOT NULL DEFAULT '',
                    sender_session TEXT NOT NULL DEFAULT '',
                    recipient_address TEXT NOT NULL DEFAULT '',
                    recipient_agent TEXT NOT NULL DEFAULT '',
                    recipient_origin TEXT NOT NULL DEFAULT '',
                    recipient_session TEXT NOT NULL DEFAULT '',
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
                    sender_agent TEXT NOT NULL DEFAULT '',
                    sender_origin TEXT NOT NULL DEFAULT '',
                    sender_session TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'online',
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(room, sender)
                );

                CREATE TABLE IF NOT EXISTS directory (
                    address TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    room TEXT NOT NULL DEFAULT '',
                    project TEXT NOT NULL DEFAULT '',
                    chat_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    cwd TEXT NOT NULL DEFAULT '',
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    contact_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'online',
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_directory_updated ON directory(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_directory_room ON directory(room);
                CREATE INDEX IF NOT EXISTS idx_directory_project ON directory(project);
                """
            )
            self.ensure_columns(
                con,
                "messages",
                {
                    "sender_address": "TEXT NOT NULL DEFAULT ''",
                    "sender_agent": "TEXT NOT NULL DEFAULT ''",
                    "sender_origin": "TEXT NOT NULL DEFAULT ''",
                    "sender_session": "TEXT NOT NULL DEFAULT ''",
                    "recipient_address": "TEXT NOT NULL DEFAULT ''",
                    "recipient_agent": "TEXT NOT NULL DEFAULT ''",
                    "recipient_origin": "TEXT NOT NULL DEFAULT ''",
                    "recipient_session": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self.ensure_columns(
                con,
                "directory",
                {
                    "room": "TEXT NOT NULL DEFAULT ''",
                    "project": "TEXT NOT NULL DEFAULT ''",
                    "chat_name": "TEXT NOT NULL DEFAULT ''",
                    "role": "TEXT NOT NULL DEFAULT ''",
                    "cwd": "TEXT NOT NULL DEFAULT ''",
                    "capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
                    "tags_json": "TEXT NOT NULL DEFAULT '[]'",
                    "contact_json": "TEXT NOT NULL DEFAULT '{}'",
                    "status": "TEXT NOT NULL DEFAULT 'online'",
                    "note": "TEXT NOT NULL DEFAULT ''",
                },
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_directory_role ON directory(role)")
            self.ensure_columns(
                con,
                "presence",
                {
                    "sender_agent": "TEXT NOT NULL DEFAULT ''",
                    "sender_origin": "TEXT NOT NULL DEFAULT ''",
                    "sender_session": "TEXT NOT NULL DEFAULT ''",
                },
            )

    def ensure_columns(self, con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @staticmethod
    def message_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "room": row["room"],
            "sender": row["sender"],
            "sender_address": row["sender_address"],
            "sender_agent": row["sender_agent"],
            "sender_origin": row["sender_origin"],
            "sender_session": row["sender_session"],
            "recipient_address": row["recipient_address"],
            "recipient_agent": row["recipient_agent"],
            "recipient_origin": row["recipient_origin"],
            "recipient_session": row["recipient_session"],
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
            "sender_agent": row["sender_agent"],
            "sender_origin": row["sender_origin"],
            "sender_session": row["sender_session"],
            "status": row["status"],
            "note": row["note"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def directory_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "address": row["address"],
            "agent": row["agent"],
            "origin": row["origin"],
            "session_id": row["session_id"],
            "sender": row["sender"],
            "room": row["room"],
            "project": row["project"],
            "chat_name": row["chat_name"],
            "role": row["role"],
            "cwd": row["cwd"],
            "capabilities": load_json_value(row["capabilities_json"], []),
            "tags": load_json_value(row["tags_json"], []),
            "contact": load_json_value(row["contact_json"], {}),
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
        sender_address: str = "",
        sender_agent: str = "",
        sender_origin: str = "",
        sender_session: str = "",
        recipient_address: str = "",
        recipient_agent: str = "",
        recipient_origin: str = "",
        recipient_session: str = "",
    ) -> dict[str, Any]:
        room = validate_room(room)
        sender = clean_string(sender, "sender", 256)
        sender_address = validate_address(sender_address) if str(sender_address or "").strip() else ""
        sender_agent = clean_string(sender_agent, "sender_agent", 128, "")
        sender_origin = clean_string(sender_origin, "sender_origin", 128, "")
        sender_session = clean_string(sender_session, "sender_session", 128, "")
        recipient_address = validate_address(recipient_address) if str(recipient_address or "").strip() else ""
        recipient_agent = clean_string(recipient_agent, "recipient_agent", 128, "")
        recipient_origin = clean_string(recipient_origin, "recipient_origin", 128, "")
        recipient_session = clean_string(recipient_session, "recipient_session", 128, "")
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
                INSERT INTO messages(
                    room, sender, sender_address, sender_agent, sender_origin, sender_session,
                    recipient_address, recipient_agent, recipient_origin, recipient_session,
                    kind, text, reply_to, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room,
                    sender,
                    sender_address,
                    sender_agent,
                    sender_origin,
                    sender_session,
                    recipient_address,
                    recipient_agent,
                    recipient_origin,
                    recipient_session,
                    kind,
                    text,
                    reply_to,
                    metadata_json,
                    created_at,
                ),
            )
            row = con.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self.message_from_row(row)

    def list_messages(
        self,
        room: str,
        after_id: int = 0,
        limit: int = 50,
        recipient_agent: str = "",
        recipient_origin: str = "",
        recipient_session: str = "",
        recipient_address: str = "",
        include_broadcast: bool = True,
    ) -> list[dict[str, Any]]:
        room = validate_room(room)
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 50), 1), 200)
        clauses = ["room = ?", "id > ?"]
        values: list[Any] = [room, after_id]
        recipient_filters = [
            ("recipient_address", recipient_address),
            ("recipient_agent", recipient_agent),
            ("recipient_origin", recipient_origin),
            ("recipient_session", recipient_session),
        ]
        for column, value in recipient_filters:
            value = str(value or "").strip()
            if not value:
                continue
            if include_broadcast:
                clauses.append(f"({column} = ? OR {column} = '')")
            else:
                clauses.append(f"{column} = ?")
            values.append(value)
        values.append(limit)
        with self.session() as con:
            rows = con.execute(
                f"""
                SELECT * FROM messages
                WHERE {' AND '.join(clauses)}
                ORDER BY id ASC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [self.message_from_row(row) for row in rows]

    def list_rooms(self) -> list[str]:
        with self.session() as con:
            rows = con.execute(
                """
                SELECT room FROM messages
                UNION
                SELECT room FROM presence
                UNION
                SELECT room FROM directory WHERE room != ''
                ORDER BY room ASC
                """
            ).fetchall()
        return [row["room"] for row in rows]

    def latest_message_id(self) -> int:
        with self.session() as con:
            row = con.execute("SELECT COALESCE(MAX(id), 0) AS latest_id FROM messages").fetchone()
        return int(row["latest_id"] or 0)

    def list_recent_messages(
        self,
        after_id: int = 0,
        limit: int = 50,
        room: str = "",
        address: str = "",
        kind: str = "",
    ) -> list[dict[str, Any]]:
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 50), 1), 200)
        clauses: list[str] = []
        values: list[Any] = []
        if after_id:
            clauses.append("id > ?")
            values.append(after_id)
        room = str(room or "").strip()
        if room:
            clauses.append("room = ?")
            values.append(validate_room(room))
        address = str(address or "").strip()
        if address:
            address = validate_address(address)
            clauses.append("(sender_address = ? OR recipient_address = ?)")
            values.extend([address, address])
        kind = str(kind or "").strip()
        if kind:
            clauses.append("kind = ?")
            values.append(clean_string(kind, "kind", 48))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if after_id else "DESC"
        values.append(limit)
        with self.session() as con:
            rows = con.execute(
                f"""
                SELECT * FROM messages
                {where}
                ORDER BY id {order}
                LIMIT ?
                """,
                values,
            ).fetchall()
        if not after_id:
            rows = list(reversed(rows))
        return [self.message_from_row(row) for row in rows]

    def announce_address(
        self,
        address: str,
        agent: str,
        origin: str,
        session_id: str,
        sender: str,
        room: str = "",
        project: str = "",
        chat_name: str = "",
        role: str = "",
        cwd: str = "",
        capabilities: list[str] | None = None,
        tags: list[str] | None = None,
        contact: dict[str, Any] | None = None,
        status: str = "online",
        note: str = "",
    ) -> dict[str, Any]:
        address = validate_address(address)
        agent = clean_string(agent, "agent", 128)
        origin = clean_string(origin, "origin", 128)
        session_id = clean_string(session_id, "session_id", 128)
        sender = clean_string(sender, "sender", 256)
        room = validate_room(room) if str(room or "").strip() else ""
        project = clean_string(project, "project", 128, "")
        chat_name = clean_string(chat_name, "chat_name", 160, "")
        role = clean_string(role, "role", 80, "")
        cwd = clean_string(cwd, "cwd", 256, "")
        capabilities = clean_string_list(capabilities, "capabilities")
        tags = clean_string_list(tags, "tags")
        if contact is None:
            contact = {}
        if not isinstance(contact, dict):
            raise ValueError("contact must be an object")
        contact_json = json.dumps(contact, ensure_ascii=False, sort_keys=True)
        status = clean_string(status, "status", 48, "online")
        note = str(note or "")
        if len(note) > 512:
            raise ValueError("note must be at most 512 characters")
        updated_at = now_iso()
        with self.session() as con:
            con.execute(
                """
                INSERT INTO directory(
                    address, agent, origin, session_id, sender, room, project,
                    chat_name, role, cwd, capabilities_json, tags_json, contact_json,
                    status, note, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    agent = excluded.agent,
                    origin = excluded.origin,
                    session_id = excluded.session_id,
                    sender = excluded.sender,
                    room = excluded.room,
                    project = excluded.project,
                    chat_name = excluded.chat_name,
                    role = excluded.role,
                    cwd = excluded.cwd,
                    capabilities_json = excluded.capabilities_json,
                    tags_json = excluded.tags_json,
                    contact_json = excluded.contact_json,
                    status = excluded.status,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    address,
                    agent,
                    origin,
                    session_id,
                    sender,
                    room,
                    project,
                    chat_name,
                    role,
                    cwd,
                    json.dumps(capabilities, ensure_ascii=False, sort_keys=True),
                    json.dumps(tags, ensure_ascii=False, sort_keys=True),
                    contact_json,
                    status,
                    note,
                    updated_at,
                ),
            )
            row = con.execute("SELECT * FROM directory WHERE address = ?", (address,)).fetchone()
        return self.directory_from_row(row)

    def lookup_address(self, address: str) -> dict[str, Any] | None:
        address = validate_address(address)
        with self.session() as con:
            row = con.execute("SELECT * FROM directory WHERE address = ?", (address,)).fetchone()
        if row is None:
            return None
        return self.directory_from_row(row)

    def list_directory(
        self,
        room: str = "",
        project: str = "",
        role: str = "",
        chat_name: str = "",
        capability: str = "",
        tag: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        room = str(room or "").strip()
        if room:
            clauses.append("room = ?")
            values.append(validate_room(room))
        project = str(project or "").strip()
        if project:
            clauses.append("project = ?")
            values.append(clean_string(project, "project", 128))
        role = str(role or "").strip()
        if role:
            clauses.append("role = ?")
            values.append(clean_string(role, "role", 80))
        chat_name = str(chat_name or "").strip()
        if chat_name:
            clauses.append("chat_name = ?")
            values.append(clean_string(chat_name, "chat_name", 160))
        status = str(status or "").strip()
        if status:
            clauses.append("status = ?")
            values.append(clean_string(status, "status", 48))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.session() as con:
            rows = con.execute(
                f"""
                SELECT * FROM directory
                {where}
                ORDER BY updated_at DESC, address ASC
                """,
                values,
            ).fetchall()
        entries = [self.directory_from_row(row) for row in rows]
        capability = str(capability or "").strip()
        if capability:
            entries = [entry for entry in entries if capability in entry.get("capabilities", [])]
        tag = str(tag or "").strip()
        if tag:
            entries = [entry for entry in entries if tag in entry.get("tags", [])]
        return entries

    def list_address_messages(
        self,
        address: str,
        after_id: int = 0,
        limit: int = 50,
        kind: str = "",
        room: str = "",
    ) -> list[dict[str, Any]]:
        address = validate_address(address)
        after_id = max(int(after_id or 0), 0)
        limit = min(max(int(limit or 50), 1), 200)
        clauses = ["recipient_address = ?", "id > ?"]
        values: list[Any] = [address, after_id]
        kind = str(kind or "").strip()
        if kind:
            clauses.append("kind = ?")
            values.append(clean_string(kind, "kind", 48))
        room = str(room or "").strip()
        if room:
            clauses.append("room = ?")
            values.append(validate_room(room))
        values.append(limit)
        with self.session() as con:
            rows = con.execute(
                f"""
                SELECT * FROM messages
                WHERE {' AND '.join(clauses)}
                ORDER BY id ASC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [self.message_from_row(row) for row in rows]

    def set_presence(
        self,
        room: str,
        sender: str,
        status: str,
        note: str = "",
        sender_agent: str = "",
        sender_origin: str = "",
        sender_session: str = "",
    ) -> dict[str, Any]:
        room = validate_room(room)
        sender = clean_string(sender, "sender", 256)
        sender_agent = clean_string(sender_agent, "sender_agent", 128, "")
        sender_origin = clean_string(sender_origin, "sender_origin", 128, "")
        sender_session = clean_string(sender_session, "sender_session", 128, "")
        status = clean_string(status, "status", 48, "online")
        note = str(note or "")
        if len(note) > 512:
            raise ValueError("note must be at most 512 characters")
        updated_at = now_iso()
        with self.session() as con:
            con.execute(
                """
                INSERT INTO presence(room, sender, sender_agent, sender_origin, sender_session, status, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room, sender) DO UPDATE SET
                    sender_agent = excluded.sender_agent,
                    sender_origin = excluded.sender_origin,
                    sender_session = excluded.sender_session,
                    status = excluded.status,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (room, sender, sender_agent, sender_origin, sender_session, status, note, updated_at),
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

    @staticmethod
    def query_bool(query: dict[str, list[str]], name: str, default: bool = False) -> bool:
        value = (query.get(name) or [str(default).lower()])[0]
        return str(value).lower() in {"1", "true", "yes", "on"}

    def do_GET(self) -> None:
        try:
            parts, query = self.route_parts()
            if not parts:
                self.send_json(
                    200,
                    {
                        "name": "relaystation",
                        "version": VERSION,
                        "endpoints": [
                            "/health",
                            "/rooms",
                            "/rooms/{room}/messages",
                            "/rooms/{room}/presence",
                            "/directory",
                            "/directory/{address}",
                            "/route",
                            "/inbox/{address}",
                            "/messages",
                            "/messages/latest",
                        ],
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
            if parts == ["messages", "latest"]:
                self.send_json(200, {"latest_id": self.app.store.latest_message_id()})
                return
            if parts == ["messages"]:
                after_id = int((query.get("after_id") or query.get("since_id") or ["0"])[0] or "0")
                limit = int((query.get("limit") or ["50"])[0] or "50")
                messages = self.app.store.list_recent_messages(
                    after_id=after_id,
                    limit=limit,
                    room=(query.get("room") or [""])[0],
                    address=(query.get("address") or [""])[0],
                    kind=(query.get("kind") or [""])[0],
                )
                latest_id = messages[-1]["id"] if messages else after_id
                self.send_json(200, {"messages": messages, "latest_id": latest_id})
                return
            if parts == ["directory"] or parts == ["rolodex"]:
                self.send_json(
                    200,
                    {
                        "directory": self.app.store.list_directory(
                            room=(query.get("room") or [""])[0],
                            project=(query.get("project") or [""])[0],
                            role=(query.get("role") or [""])[0],
                            chat_name=(query.get("chat") or query.get("chat_name") or query.get("session_name") or [""])[0],
                            capability=(query.get("capability") or [""])[0],
                            tag=(query.get("tag") or [""])[0],
                            status=(query.get("status") or [""])[0],
                        )
                    },
                )
                return
            if parts == ["route"]:
                self.send_json(
                    200,
                    {
                        "routes": self.app.store.list_directory(
                            room=(query.get("room") or [""])[0],
                            project=(query.get("project") or [""])[0],
                            role=(query.get("role") or [""])[0],
                            chat_name=(query.get("chat") or query.get("chat_name") or query.get("session_name") or [""])[0],
                            capability=(query.get("capability") or [""])[0],
                            tag=(query.get("tag") or [""])[0],
                            status=(query.get("status") or [""])[0],
                        )
                    },
                )
                return
            if len(parts) == 2 and parts[0] in {"directory", "rolodex"}:
                entry = self.app.store.lookup_address(parts[1])
                if entry is None:
                    self.send_problem(404, f"unknown address: {parts[1]}")
                    return
                self.send_json(200, {"entry": entry})
                return
            if len(parts) == 2 and parts[0] == "inbox":
                after_id = int((query.get("after_id") or query.get("since_id") or ["0"])[0] or "0")
                limit = int((query.get("limit") or ["50"])[0] or "50")
                wait_seconds = min(max(float((query.get("wait") or ["0"])[0] or "0"), 0.0), 55.0)
                deadline = time.monotonic() + wait_seconds
                messages: list[dict[str, Any]] = []
                while True:
                    messages = self.app.store.list_address_messages(
                        parts[1],
                        after_id=after_id,
                        limit=limit,
                        kind=(query.get("kind") or [""])[0],
                        room=(query.get("room") or [""])[0],
                    )
                    if messages or time.monotonic() >= deadline:
                        break
                    time.sleep(0.5)
                latest_id = messages[-1]["id"] if messages else after_id
                self.send_json(200, {"messages": messages, "latest_id": latest_id})
                return
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "messages":
                after_id = int((query.get("after_id") or query.get("since_id") or ["0"])[0] or "0")
                limit = int((query.get("limit") or ["50"])[0] or "50")
                wait_seconds = min(max(float((query.get("wait") or ["0"])[0] or "0"), 0.0), 55.0)
                deadline = time.monotonic() + wait_seconds
                messages: list[dict[str, Any]] = []
                include_broadcast = self.query_bool(query, "include_broadcast", True)
                while True:
                    messages = self.app.store.list_messages(
                        parts[1],
                        after_id=after_id,
                        limit=limit,
                        recipient_agent=(query.get("to_agent") or query.get("recipient_agent") or [""])[0],
                        recipient_origin=(query.get("to_origin") or query.get("recipient_origin") or [""])[0],
                        recipient_session=(query.get("to_session") or query.get("recipient_session") or [""])[0],
                        recipient_address=(query.get("to_address") or query.get("recipient_address") or [""])[0],
                        include_broadcast=include_broadcast,
                    )
                    if messages or time.monotonic() >= deadline:
                        break
                    time.sleep(0.5)
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
            if len(parts) == 2 and parts[0] in {"directory", "rolodex"}:
                body = self.read_json_body()
                address = validate_address(parts[1])
                agent = body.get("agent") or body.get("sender_agent") or ""
                origin = body.get("origin") or body.get("sender_origin") or ""
                session_id = body.get("session_id") or body.get("sender_session") or body.get("session") or ""
                sender = body.get("sender") or f"{agent}@{origin}/{session_id}"
                entry = self.app.store.announce_address(
                    address=address,
                    agent=agent,
                    origin=origin,
                    session_id=session_id,
                    sender=sender,
                    room=body.get("room") or "",
                    project=body.get("project") or "",
                    chat_name=body.get("chat_name") or body.get("chat") or body.get("session_name") or "",
                    role=body.get("role") or "",
                    cwd=body.get("cwd") or body.get("workdir") or "",
                    capabilities=body.get("capabilities") or body.get("capability") or [],
                    tags=body.get("tags") or body.get("tag") or [],
                    contact=body.get("contact") or {},
                    status=body.get("status") or "online",
                    note=body.get("note") or "",
                )
                self.send_json(200, {"entry": entry})
                return
            if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "messages":
                body = self.read_json_body()
                recipient_address = body.get("recipient_address") or body.get("to_address") or ""
                recipient_entry = None
                if str(recipient_address or "").strip():
                    recipient_entry = self.app.store.lookup_address(recipient_address)
                    if recipient_entry is None:
                        raise ValueError(f"unknown address: {recipient_address}")
                message = self.app.store.create_message(
                    room=parts[1],
                    sender=body.get("sender") or "anonymous",
                    sender_address=body.get("sender_address") or body.get("from_address") or "",
                    sender_agent=body.get("sender_agent") or body.get("agent") or "",
                    sender_origin=body.get("sender_origin") or body.get("origin") or "",
                    sender_session=body.get("sender_session") or body.get("session_id") or body.get("session") or "",
                    recipient_address=recipient_address,
                    recipient_agent=body.get("recipient_agent")
                    or body.get("to_agent")
                    or (recipient_entry or {}).get("agent", ""),
                    recipient_origin=body.get("recipient_origin")
                    or body.get("to_origin")
                    or (recipient_entry or {}).get("origin", ""),
                    recipient_session=body.get("recipient_session")
                    or body.get("to_session")
                    or (recipient_entry or {}).get("session_id", ""),
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
                    sender_agent=body.get("sender_agent") or body.get("agent") or "",
                    sender_origin=body.get("sender_origin") or body.get("origin") or "",
                    sender_session=body.get("sender_session") or body.get("session_id") or body.get("session") or "",
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
    timeout: float = 20.0,
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
        with urlopen(req, timeout=timeout) as response:
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
                "sender": {"type": "string", "description": "Override sender display name."},
                "sender_address": {"type": "string", "description": "Optional sender callsign/address announced in the directory."},
                "agent": {"type": "string", "description": "Override sender agent, for example codex or claude."},
                "origin": {"type": "string", "description": "Override sender origin, for example laptop or pc."},
                "session_id": {"type": "string", "description": "Override sender session id."},
                "to_address": {"type": "string", "description": "Optional destination callsign/address from the directory."},
                "to_agent": {"type": "string", "description": "Optional destination agent."},
                "to_origin": {"type": "string", "description": "Optional destination origin."},
                "to_session": {"type": "string", "description": "Optional destination session id."},
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
                "wait": {"type": "number", "description": "Optional long-poll wait in seconds, max 55."},
                "to_address": {"type": "string", "description": "Filter destination callsign/address."},
                "to_agent": {"type": "string", "description": "Filter destination agent."},
                "to_origin": {"type": "string", "description": "Filter destination origin."},
                "to_session": {"type": "string", "description": "Filter destination session id."},
                "include_broadcast": {"type": "boolean", "description": "Include messages without a destination while filtering."},
            },
        },
    },
    {
        "name": "relay_inbox",
        "description": "Read messages addressed to this MCP client's configured agent/origin/session.",
        "inputSchema": {
            "type": "object",
            "required": ["room"],
            "properties": {
                "room": {"type": "string"},
                "after_id": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "wait": {"type": "number", "description": "Optional long-poll wait in seconds, max 55."},
                "include_broadcast": {"type": "boolean"},
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
                "agent": {"type": "string"},
                "origin": {"type": "string"},
                "session_id": {"type": "string"},
                "status": {"type": "string", "description": "online, working, waiting, offline, or any short custom status."},
                "note": {"type": "string"},
            },
        },
    },
    {
        "name": "relay_announce",
        "description": "Announce this live session under a short callsign/address in the Relaystation directory.",
        "inputSchema": {
            "type": "object",
            "required": ["address"],
            "properties": {
                "address": {"type": "string", "description": "Short human address, for example relay-laptop or parser-pc."},
                "agent": {"type": "string"},
                "origin": {"type": "string"},
                "session_id": {"type": "string"},
                "sender": {"type": "string"},
                "room": {"type": "string", "description": "Optional room this session is working in."},
                "project": {"type": "string", "description": "Optional project/context label."},
                "chat_name": {"type": "string", "description": "Optional human name for this specific chat/session."},
                "session_name": {"type": "string", "description": "Alias for chat_name."},
                "role": {"type": "string", "description": "Optional role, for example backend, frontend, review, docs."},
                "cwd": {"type": "string", "description": "Optional working directory or repo path."},
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional things this session can help with, for example api, ui, tests.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional free-form route tags.",
                },
                "contact": {"type": "object", "description": "Optional structured contact details for external watchers."},
                "status": {"type": "string", "description": "online, working, waiting, offline, or any short custom status."},
                "note": {"type": "string"},
            },
        },
    },
    {
        "name": "relay_directory",
        "description": "List announced Relaystation callsigns/addresses.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "Optional room filter."},
                "project": {"type": "string", "description": "Optional project filter."},
                "role": {"type": "string", "description": "Optional role filter."},
                "chat_name": {"type": "string", "description": "Optional chat/session name filter."},
                "session_name": {"type": "string", "description": "Alias for chat_name."},
                "capability": {"type": "string", "description": "Optional capability filter."},
                "tag": {"type": "string", "description": "Optional tag filter."},
                "status": {"type": "string", "description": "Optional status filter."},
            },
        },
    },
    {
        "name": "relay_route",
        "description": "Ask the Relaystation router for matching sessions by project, role, capability, tag, room, or status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "role": {"type": "string"},
                "capability": {"type": "string"},
                "tag": {"type": "string"},
                "room": {"type": "string"},
                "status": {"type": "string"},
                "chat_name": {"type": "string"},
                "session_name": {"type": "string"},
            },
        },
    },
    {
        "name": "relay_address_inbox",
        "description": "Read messages addressed to a short callsign/address across rooms.",
        "inputSchema": {
            "type": "object",
            "required": ["address"],
            "properties": {
                "address": {"type": "string"},
                "after_id": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "wait": {"type": "number", "description": "Optional long-poll wait in seconds, max 55."},
                "kind": {"type": "string"},
                "room": {"type": "string"},
            },
        },
    },
    {
        "name": "relay_connect",
        "description": "Open or continue a direct address-to-address thread and send an initial message.",
        "inputSchema": {
            "type": "object",
            "required": ["to_address", "text"],
            "properties": {
                "to_address": {"type": "string"},
                "from_address": {"type": "string"},
                "room": {"type": "string", "description": "Optional explicit thread room."},
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "text": {"type": "string"},
                "kind": {"type": "string", "description": "Defaults to question."},
                "metadata": {"type": "object"},
            },
        },
    },
    {
        "name": "relay_wake",
        "description": "Send a high-priority wake packet to an address for watchers/automations to notice.",
        "inputSchema": {
            "type": "object",
            "required": ["to_address"],
            "properties": {
                "to_address": {"type": "string"},
                "from_address": {"type": "string"},
                "room": {"type": "string"},
                "project": {"type": "string"},
                "topic": {"type": "string"},
                "reason": {"type": "string"},
                "text": {"type": "string"},
            },
        },
    },
    {
        "name": "relay_lookup",
        "description": "Look up one announced Relaystation callsign/address.",
        "inputSchema": {
            "type": "object",
            "required": ["address"],
            "properties": {"address": {"type": "string"}},
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
    {
        "name": "relay_identity",
        "description": "Show this MCP client's Relaystation identity.",
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
    global ADDRESS_CACHE
    args = args or {}
    identity = current_identity(
        agent=args.get("agent"),
        origin=args.get("origin"),
        session_id=args.get("session_id"),
        sender=args.get("sender"),
        address=args.get("sender_address") or args.get("from_address"),
    )
    if name == "relay_send":
        room = validate_room(args.get("room") or "")
        data = api_request(
            "POST",
            f"/rooms/{quote(room)}/messages",
            body={
                "sender": identity["sender"],
                "sender_address": identity["address"],
                "sender_agent": identity["agent"],
                "sender_origin": identity["origin"],
                "sender_session": identity["session_id"],
                "recipient_address": args.get("to_address") or args.get("recipient_address") or "",
                "recipient_agent": args.get("to_agent") or args.get("recipient_agent") or "",
                "recipient_origin": args.get("to_origin") or args.get("recipient_origin") or "",
                "recipient_session": args.get("to_session") or args.get("recipient_session") or "",
                "text": args.get("text") or "",
                "kind": args.get("kind") or "message",
                "reply_to": args.get("reply_to"),
                "metadata": args.get("metadata") or {},
            },
        )
        return mcp_text_result(data)
    if name == "relay_read":
        room = validate_room(args.get("room") or "")
        wait = min(max(float(args.get("wait") or 0), 0.0), 55.0)
        data = api_request(
            "GET",
            f"/rooms/{quote(room)}/messages",
            query={
                "after_id": args.get("after_id") or 0,
                "limit": args.get("limit") or 50,
                "wait": wait,
                "to_address": args.get("to_address") or args.get("recipient_address"),
                "to_agent": args.get("to_agent") or args.get("recipient_agent"),
                "to_origin": args.get("to_origin") or args.get("recipient_origin"),
                "to_session": args.get("to_session") or args.get("recipient_session"),
                "include_broadcast": str(args.get("include_broadcast", True)).lower(),
            },
            timeout=wait + 5,
        )
        return mcp_text_result(data)
    if name == "relay_inbox":
        room = validate_room(args.get("room") or "")
        wait = min(max(float(args.get("wait") or 0), 0.0), 55.0)
        query: dict[str, Any] = {
            "after_id": args.get("after_id") or 0,
            "limit": args.get("limit") or 50,
            "wait": wait,
            "include_broadcast": str(args.get("include_broadcast", True)).lower(),
        }
        if identity["address"]:
            query["to_address"] = identity["address"]
        else:
            query.update(
                {
                    "to_agent": identity["agent"],
                    "to_origin": identity["origin"],
                    "to_session": identity["session_id"],
                }
            )
        data = api_request(
            "GET",
            f"/rooms/{quote(room)}/messages",
            query=query,
            timeout=wait + 5,
        )
        return mcp_text_result(data)
    if name == "relay_presence":
        room = validate_room(args.get("room") or "")
        data = api_request(
            "POST",
            f"/rooms/{quote(room)}/presence",
            body={
                "sender": identity["sender"],
                "sender_agent": identity["agent"],
                "sender_origin": identity["origin"],
                "sender_session": identity["session_id"],
                "status": args.get("status") or "online",
                "note": args.get("note") or "",
            },
        )
        return mcp_text_result(data)
    if name == "relay_announce":
        address = validate_address(args.get("address") or "")
        identity = current_identity(
            agent=args.get("agent"),
            origin=args.get("origin"),
            session_id=args.get("session_id"),
            sender=args.get("sender"),
            address=address,
        )
        data = api_request(
            "POST",
            f"/directory/{quote(address)}",
            body={
                "sender": identity["sender"],
                "agent": identity["agent"],
                "origin": identity["origin"],
                "session_id": identity["session_id"],
                "room": args.get("room") or "",
                "project": args.get("project") or "",
                "chat_name": args.get("chat_name") or args.get("chat") or args.get("session_name") or "",
                "role": args.get("role") or "",
                "cwd": args.get("cwd") or args.get("workdir") or "",
                "capabilities": args.get("capabilities") or args.get("capability") or [],
                "tags": args.get("tags") or args.get("tag") or [],
                "contact": args.get("contact") or {},
                "status": args.get("status") or "online",
                "note": args.get("note") or "",
            },
        )
        ADDRESS_CACHE = address
        return mcp_text_result(data)
    if name == "relay_directory":
        data = api_request(
            "GET",
            "/directory",
            query={
                "room": args.get("room"),
                "project": args.get("project"),
                "role": args.get("role"),
                "chat": args.get("chat_name") or args.get("session_name"),
                "capability": args.get("capability"),
                "tag": args.get("tag"),
                "status": args.get("status"),
            },
        )
        return mcp_text_result(data)
    if name == "relay_route":
        data = api_request(
            "GET",
            "/route",
            query={
                "room": args.get("room"),
                "project": args.get("project"),
                "role": args.get("role"),
                "capability": args.get("capability"),
                "tag": args.get("tag"),
                "status": args.get("status"),
                "chat": args.get("chat_name") or args.get("session_name"),
            },
        )
        return mcp_text_result(data)
    if name == "relay_address_inbox":
        address = validate_address(args.get("address") or "")
        wait = min(max(float(args.get("wait") or 0), 0.0), 55.0)
        data = api_request(
            "GET",
            f"/inbox/{quote(address)}",
            query={
                "after_id": args.get("after_id") or 0,
                "limit": args.get("limit") or 50,
                "wait": wait,
                "kind": args.get("kind"),
                "room": args.get("room"),
            },
            timeout=wait + 5,
        )
        return mcp_text_result(data)
    if name in {"relay_connect", "relay_wake"}:
        to_address = validate_address(args.get("to_address") or args.get("recipient_address") or "")
        from_address = args.get("from_address") or args.get("sender_address") or identity["address"] or identity["agent"]
        room = args.get("room") or direct_room_name(from_address, to_address, args.get("project") or "", args.get("topic") or name)
        text = args.get("text") or ""
        kind = args.get("kind") or "question"
        metadata = args.get("metadata") or {}
        if name == "relay_wake":
            kind = "wake"
            reason = args.get("reason") or ""
            if not text:
                text = f"Wake request from {from_address}"
                if reason:
                    text += f": {reason}"
            metadata = {
                **(metadata if isinstance(metadata, dict) else {}),
                "wake": True,
                "project": args.get("project") or "",
                "topic": args.get("topic") or "",
                "reason": reason,
            }
        data = api_request(
            "POST",
            f"/rooms/{quote(validate_room(room))}/messages",
            body={
                "sender": identity["sender"],
                "sender_address": from_address,
                "sender_agent": identity["agent"],
                "sender_origin": identity["origin"],
                "sender_session": identity["session_id"],
                "recipient_address": to_address,
                "text": text,
                "kind": kind,
                "metadata": metadata if isinstance(metadata, dict) else {},
            },
        )
        data["thread_room"] = room
        return mcp_text_result(data)
    if name == "relay_lookup":
        address = validate_address(args.get("address") or "")
        data = api_request("GET", f"/directory/{quote(address)}")
        return mcp_text_result(data)
    if name == "relay_roster":
        room = validate_room(args.get("room") or "")
        data = api_request("GET", f"/rooms/{quote(room)}/presence")
        return mcp_text_result(data)
    if name == "relay_rooms":
        data = api_request("GET", "/rooms")
        return mcp_text_result(data)
    if name == "relay_identity":
        return mcp_text_result(identity)
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


def xdg_state_home() -> str:
    return os.getenv("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")


def default_watch_state_file(address: str) -> str:
    return os.path.join(xdg_state_home(), "relaystation", f"{validate_address(address)}.state.json")


def default_watch_log_file(address: str) -> str:
    return os.path.join(xdg_state_home(), "relaystation", f"{validate_address(address)}.log")


def read_watch_state(path: str) -> int:
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return 0
    if not isinstance(data, dict):
        return 0
    return int(data.get("latest_id") or 0)


def write_watch_state(path: str, address: str, latest_id: int) -> None:
    path = os.path.expanduser(path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"address": address, "latest_id": latest_id, "updated_at": now_iso()}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def sender_label(message: dict[str, Any]) -> str:
    return message.get("sender_address") or message.get("sender") or "unknown"


def destination_label(message: dict[str, Any]) -> str:
    if message.get("recipient_address"):
        return message["recipient_address"]
    if message.get("recipient_agent") or message.get("recipient_origin") or message.get("recipient_session"):
        destination = f"{message.get('recipient_agent') or '*'}@{message.get('recipient_origin') or '*'}"
        if message.get("recipient_session"):
            destination += f"/{message['recipient_session']}"
        return destination
    return "*"


def format_message(message: dict[str, Any], include_room: bool = False) -> str:
    reply = f" reply_to={message['reply_to']}" if message.get("reply_to") else ""
    room = f" room={message['room']}" if include_room else ""
    header = (
        f"[{message['id']}{reply}] {message['created_at']}{room} "
        f"{sender_label(message)} -> {destination_label(message)} ({message['kind']})"
    )
    return f"{header}:\n{message['text']}"


def append_watch_log(path: str, message: dict[str, Any]) -> None:
    path = os.path.expanduser(path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(format_message(message, include_room=True))
        handle.write("\n\n")


def desktop_notify(address: str, message: dict[str, Any]) -> None:
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return
    title = f"Relaystation {address}: {message.get('kind', 'message')}"
    body = f"{sender_label(message)}: {str(message.get('text') or '').strip()[:240]}"
    try:
        subprocess.run(
            [notify_send, title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return


def run_watch_hook(command: str, message: dict[str, Any], timeout: float) -> None:
    env = os.environ.copy()
    payload = dumps(message)
    env.update(
        {
            "RELAYSTATION_MESSAGE_JSON": payload,
            "RELAYSTATION_MESSAGE_ID": str(message.get("id") or ""),
            "RELAYSTATION_MESSAGE_KIND": str(message.get("kind") or ""),
            "RELAYSTATION_MESSAGE_ROOM": str(message.get("room") or ""),
            "RELAYSTATION_SENDER_ADDRESS": str(message.get("sender_address") or ""),
            "RELAYSTATION_RECIPIENT_ADDRESS": str(message.get("recipient_address") or ""),
        }
    )
    try:
        subprocess.run(command, input=payload, text=True, shell=True, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        print(f"hook timed out after {timeout:g}s: {command}", file=sys.stderr)


def command_send(args: argparse.Namespace) -> None:
    text = " ".join(args.text)
    identity = current_identity(
        agent=args.agent,
        origin=args.origin,
        session_id=args.session_id,
        sender=args.sender,
        address=args.sender_address,
    )
    data = api_request(
        "POST",
        f"/rooms/{quote(validate_room(args.room))}/messages",
        base_url=args.url,
        token=args.token,
        body={
            "sender": identity["sender"],
            "sender_address": identity["address"],
            "sender_agent": identity["agent"],
            "sender_origin": identity["origin"],
            "sender_session": identity["session_id"],
            "recipient_address": args.to_address or "",
            "recipient_agent": args.to_agent or "",
            "recipient_origin": args.to_origin or "",
            "recipient_session": args.to_session or "",
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
        query={
            "after_id": args.after_id,
            "limit": args.limit,
            "wait": args.wait,
            "to_address": args.to_address,
            "to_agent": args.to_agent,
            "to_origin": args.to_origin,
            "to_session": args.to_session,
            "include_broadcast": str(args.include_broadcast).lower(),
        },
        timeout=float(args.wait or 0) + 5,
    )
    if args.json:
        print_json(data)
        return
    for message in data.get("messages", []):
        reply = f" -> {message['reply_to']}" if message.get("reply_to") else ""
        destination = ""
        if message.get("recipient_address"):
            destination = f" -> {message['recipient_address']}"
        elif message.get("recipient_agent") or message.get("recipient_origin") or message.get("recipient_session"):
            destination = f" -> {message.get('recipient_agent') or '*'}@{message.get('recipient_origin') or '*'}"
            if message.get("recipient_session"):
                destination += f"/{message['recipient_session']}"
        sender = message.get("sender_address") or message["sender"]
        print(f"[{message['id']}{reply}] {message['created_at']} {sender}{destination} ({message['kind']}):")
        print(message["text"])
        print()


def command_watch(args: argparse.Namespace) -> None:
    after_id = args.after_id
    while True:
        data = api_request(
            "GET",
            f"/rooms/{quote(validate_room(args.room))}/messages",
            base_url=args.url,
            token=args.token,
            query={
                "after_id": after_id,
                "limit": args.limit,
                "wait": args.wait,
                "to_address": args.to_address,
                "to_agent": args.to_agent,
                "to_origin": args.to_origin,
                "to_session": args.to_session,
                "include_broadcast": str(args.include_broadcast).lower(),
            },
            timeout=float(args.wait or 0) + 5,
        )
        for message in data.get("messages", []):
            reply = f" -> {message['reply_to']}" if message.get("reply_to") else ""
            destination = ""
            if message.get("recipient_address"):
                destination = f" -> {message['recipient_address']}"
            elif message.get("recipient_agent") or message.get("recipient_origin") or message.get("recipient_session"):
                destination = f" -> {message.get('recipient_agent') or '*'}@{message.get('recipient_origin') or '*'}"
                if message.get("recipient_session"):
                    destination += f"/{message['recipient_session']}"
            sender = message.get("sender_address") or message["sender"]
            print(f"[{message['id']}{reply}] {message['created_at']} {sender}{destination} ({message['kind']}):")
            print(message["text"])
            print()
            after_id = max(after_id, int(message["id"]))
        sys.stdout.flush()


def command_rooms(args: argparse.Namespace) -> None:
    data = api_request("GET", "/rooms", base_url=args.url, token=args.token)
    print_json(data)


def command_board(args: argparse.Namespace) -> None:
    data = api_request(
        "GET",
        "/messages",
        base_url=args.url,
        token=args.token,
        query={
            "after_id": args.after_id,
            "limit": args.limit,
            "room": args.room,
            "address": args.address,
            "kind": args.kind,
        },
    )
    if args.json:
        print_json(data)
        return
    for message in data.get("messages", []):
        print(format_message(message, include_room=True))
        print()


def command_watch_address(args: argparse.Namespace) -> None:
    address = validate_address(args.address)
    state_file = args.state_file or default_watch_state_file(address)
    log_file = args.log_file or default_watch_log_file(address)
    if args.after_id is not None:
        after_id = max(int(args.after_id), 0)
    elif args.from_beginning:
        after_id = 0
    elif not args.no_state:
        after_id = read_watch_state(state_file)
    else:
        after_id = 0
    if after_id == 0 and not args.from_beginning and not args.replay:
        latest = api_request("GET", "/messages/latest", base_url=args.url, token=args.token)
        after_id = int(latest.get("latest_id") or 0)
        if not args.no_state:
            write_watch_state(state_file, address, after_id)

    print(f"Relaystation watcher listening on {address} after_id={after_id}", file=sys.stderr)
    if not args.no_state:
        print(f"state: {os.path.expanduser(state_file)}", file=sys.stderr)
    if not args.no_log:
        print(f"log: {os.path.expanduser(log_file)}", file=sys.stderr)

    try:
        while True:
            data = api_request(
                "GET",
                f"/inbox/{quote(address)}",
                base_url=args.url,
                token=args.token,
                query={
                    "after_id": after_id,
                    "limit": args.limit,
                    "wait": args.wait,
                    "kind": args.kind,
                    "room": args.room,
                },
                timeout=float(args.wait or 0) + 5,
            )
            messages = data.get("messages", [])
            for message in messages:
                print(format_message(message, include_room=True))
                print()
                if args.bell:
                    print("\a", end="", flush=True)
                if args.notify:
                    desktop_notify(address, message)
                if not args.no_log:
                    append_watch_log(log_file, message)
                if args.hook:
                    run_watch_hook(args.hook, message, args.hook_timeout)
                if args.wake_hook and message.get("kind") == "wake":
                    run_watch_hook(args.wake_hook, message, args.hook_timeout)
                after_id = max(after_id, int(message["id"]))
                if not args.no_state:
                    write_watch_state(state_file, address, after_id)
            sys.stdout.flush()
            if args.once:
                return
    except KeyboardInterrupt:
        print("\nwatcher stopped", file=sys.stderr)


def command_presence(args: argparse.Namespace) -> None:
    identity = current_identity(
        agent=args.agent,
        origin=args.origin,
        session_id=args.session_id,
        sender=args.sender,
    )
    data = api_request(
        "POST",
        f"/rooms/{quote(validate_room(args.room))}/presence",
        base_url=args.url,
        token=args.token,
        body={
            "sender": identity["sender"],
            "sender_agent": identity["agent"],
            "sender_origin": identity["origin"],
            "sender_session": identity["session_id"],
            "status": args.status,
            "note": args.note or "",
        },
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


def command_announce(args: argparse.Namespace) -> None:
    identity = current_identity(
        agent=args.agent,
        origin=args.origin,
        session_id=args.session_id,
        sender=args.sender,
        address=args.address,
    )
    data = api_request(
        "POST",
        f"/directory/{quote(validate_address(args.address))}",
        base_url=args.url,
        token=args.token,
        body={
            "sender": identity["sender"],
            "agent": identity["agent"],
            "origin": identity["origin"],
            "session_id": identity["session_id"],
            "room": args.room or "",
            "project": args.project or "",
            "chat_name": args.chat_name or args.session_name or "",
            "role": args.role or "",
            "cwd": args.cwd or args.workdir or "",
            "capabilities": args.capability or [],
            "tags": args.tag or [],
            "contact": json.loads(args.contact_json) if args.contact_json else {},
            "status": args.status,
            "note": args.note or "",
        },
    )
    print_json(data)


def command_directory(args: argparse.Namespace) -> None:
    data = api_request(
        "GET",
        "/directory",
        base_url=args.url,
        token=args.token,
        query={
            "room": args.room,
            "project": args.project,
            "role": args.role,
            "chat": args.chat_name or args.session_name,
            "capability": args.capability,
            "tag": args.tag,
            "status": args.status,
        },
    )
    print_json(data)


def command_route(args: argparse.Namespace) -> None:
    data = api_request(
        "GET",
        "/route",
        base_url=args.url,
        token=args.token,
        query={
            "room": args.room,
            "project": args.project,
            "role": args.role,
            "chat": args.chat_name or args.session_name,
            "capability": args.capability,
            "tag": args.tag,
            "status": args.status,
        },
    )
    print_json(data)


def command_lookup(args: argparse.Namespace) -> None:
    data = api_request(
        "GET",
        f"/directory/{quote(validate_address(args.address))}",
        base_url=args.url,
        token=args.token,
    )
    print_json(data)


def command_address_inbox(args: argparse.Namespace) -> None:
    data = api_request(
        "GET",
        f"/inbox/{quote(validate_address(args.address))}",
        base_url=args.url,
        token=args.token,
        query={
            "after_id": args.after_id,
            "limit": args.limit,
            "wait": args.wait,
            "kind": args.kind,
            "room": args.room,
        },
        timeout=float(args.wait or 0) + 5,
    )
    if args.json:
        print_json(data)
        return
    for message in data.get("messages", []):
        reply = f" -> {message['reply_to']}" if message.get("reply_to") else ""
        sender = message.get("sender_address") or message["sender"]
        print(f"[{message['id']}{reply}] {message['created_at']} {message['room']} {sender} ({message['kind']}):")
        print(message["text"])
        print()


def command_connect(args: argparse.Namespace) -> None:
    text = " ".join(args.text)
    identity = current_identity(
        agent=args.agent,
        origin=args.origin,
        session_id=args.session_id,
        sender=args.sender,
        address=args.from_address,
    )
    to_address = validate_address(args.to_address)
    from_address = args.from_address or identity["address"] or identity["agent"]
    room = args.room or direct_room_name(from_address, to_address, args.project or "", args.topic or "connect")
    metadata = json.loads(args.metadata_json) if args.metadata_json else {}
    data = api_request(
        "POST",
        f"/rooms/{quote(validate_room(room))}/messages",
        base_url=args.url,
        token=args.token,
        body={
            "sender": identity["sender"],
            "sender_address": from_address,
            "sender_agent": identity["agent"],
            "sender_origin": identity["origin"],
            "sender_session": identity["session_id"],
            "recipient_address": to_address,
            "kind": args.kind,
            "text": text,
            "metadata": {
                **metadata,
                "project": args.project or "",
                "topic": args.topic or "",
                "thread_room": room,
            },
        },
    )
    data["thread_room"] = room
    print_json(data)


def command_wake(args: argparse.Namespace) -> None:
    identity = current_identity(
        agent=args.agent,
        origin=args.origin,
        session_id=args.session_id,
        sender=args.sender,
        address=args.from_address,
    )
    to_address = validate_address(args.to_address)
    from_address = args.from_address or identity["address"] or identity["agent"]
    room = args.room or direct_room_name(from_address, to_address, args.project or "", args.topic or "wake")
    text = " ".join(args.text).strip()
    if not text:
        text = f"Wake request from {from_address}"
        if args.reason:
            text += f": {args.reason}"
    data = api_request(
        "POST",
        f"/rooms/{quote(validate_room(room))}/messages",
        base_url=args.url,
        token=args.token,
        body={
            "sender": identity["sender"],
            "sender_address": from_address,
            "sender_agent": identity["agent"],
            "sender_origin": identity["origin"],
            "sender_session": identity["session_id"],
            "recipient_address": to_address,
            "kind": "wake",
            "text": text,
            "metadata": {
                "wake": True,
                "project": args.project or "",
                "topic": args.topic or "",
                "reason": args.reason or "",
                "thread_room": room,
            },
        },
    )
    data["thread_room"] = room
    print_json(data)


def command_mcp_config(args: argparse.Namespace) -> None:
    script = os.path.abspath(__file__)
    agent = clean_string(args.agent or os.getenv("RELAYSTATION_AGENT") or default_sender(), "agent", 128)
    origin = clean_string(args.origin or os.getenv("RELAYSTATION_ORIGIN") or socket.gethostname(), "origin", 128)
    session_id = args.session_id or os.getenv("RELAYSTATION_SESSION_ID") or os.getenv("RELAYSTATION_SESSION")
    token_value = os.getenv("RELAYSTATION_TOKEN") if args.include_token else "<relaystation-token>"
    env = {
        "RELAYSTATION_URL": args.url or os.getenv("RELAYSTATION_URL") or "http://127.0.0.1:8787",
        "RELAYSTATION_TOKEN": token_value,
        "RELAYSTATION_AGENT": agent,
        "RELAYSTATION_ORIGIN": origin,
    }
    if session_id:
        env["RELAYSTATION_SESSION_ID"] = clean_string(session_id, "session_id", 128)
    config = {
        "mcpServers": {
            args.name: {
                "command": sys.executable,
                "args": [script, "mcp"],
                "env": env,
            }
        }
    }
    print_json(config)
    print()
    print("Codex TOML:")
    print(f'[mcp_servers.{args.name}]')
    print(f'command = "{sys.executable}"')
    print(f'args = ["{script}", "mcp"]')
    env_text = ", ".join(f'{key} = "{value}"' for key, value in env.items())
    print(f"env = {{ {env_text} }}")


def command_identity(_args: argparse.Namespace) -> None:
    print_json(current_identity())


def add_client_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=os.getenv("RELAYSTATION_URL") or "http://127.0.0.1:8787")
    parser.add_argument("--token", default=os.getenv("RELAYSTATION_TOKEN") or "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Relaystation server, CLI, and MCP radio adapter.")
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
    send.add_argument("--sender-address")
    send.add_argument("--agent")
    send.add_argument("--origin")
    send.add_argument("--session-id")
    send.add_argument("--to-address")
    send.add_argument("--to-agent")
    send.add_argument("--to-origin")
    send.add_argument("--to-session")
    send.add_argument("--kind", default="message")
    send.add_argument("--reply-to", type=int)
    send.add_argument("--metadata-json")
    send.set_defaults(func=command_send)

    read = sub.add_parser("read", help="Read room messages.")
    add_client_args(read)
    read.add_argument("room")
    read.add_argument("--after-id", type=int, default=0)
    read.add_argument("--limit", type=int, default=50)
    read.add_argument("--wait", type=float, default=0.0)
    read.add_argument("--to-address")
    read.add_argument("--to-agent")
    read.add_argument("--to-origin")
    read.add_argument("--to-session")
    read.add_argument("--include-broadcast", action=argparse.BooleanOptionalAction, default=True)
    read.add_argument("--json", action="store_true")
    read.set_defaults(func=command_read)

    watch = sub.add_parser("watch", help="Continuously watch a room like a radio/text channel.")
    add_client_args(watch)
    watch.add_argument("room")
    watch.add_argument("--after-id", type=int, default=0)
    watch.add_argument("--limit", type=int, default=50)
    watch.add_argument("--wait", type=float, default=30.0)
    watch.add_argument("--to-address")
    watch.add_argument("--to-agent")
    watch.add_argument("--to-origin")
    watch.add_argument("--to-session")
    watch.add_argument("--include-broadcast", action=argparse.BooleanOptionalAction, default=True)
    watch.set_defaults(func=command_watch)

    rooms = sub.add_parser("rooms", help="List rooms.")
    add_client_args(rooms)
    rooms.set_defaults(func=command_rooms)

    board = sub.add_parser("board", aliases=["messages"], help="Show recent saved messages across the messageboard.")
    add_client_args(board)
    board.add_argument("--after-id", type=int, default=0)
    board.add_argument("--limit", type=int, default=50)
    board.add_argument("--room")
    board.add_argument("--address")
    board.add_argument("--kind")
    board.add_argument("--json", action="store_true")
    board.set_defaults(func=command_board)

    watch_address = sub.add_parser("watch-address", aliases=["watcher", "awatch"], help="Watch one callsign across rooms, ping locally, log, and run optional hooks.")
    add_client_args(watch_address)
    watch_address.add_argument("address")
    watch_address.add_argument("--after-id", type=int)
    watch_address.add_argument("--from-beginning", action="store_true")
    watch_address.add_argument("--replay", action="store_true", help="Replay from state/after-id instead of starting at latest when no state exists.")
    watch_address.add_argument("--limit", type=int, default=50)
    watch_address.add_argument("--wait", type=float, default=55.0)
    watch_address.add_argument("--kind")
    watch_address.add_argument("--room")
    watch_address.add_argument("--once", action="store_true")
    watch_address.add_argument("--state-file")
    watch_address.add_argument("--no-state", action="store_true")
    watch_address.add_argument("--log-file")
    watch_address.add_argument("--no-log", action="store_true")
    watch_address.add_argument("--notify", action=argparse.BooleanOptionalAction, default=True)
    watch_address.add_argument("--bell", action=argparse.BooleanOptionalAction, default=True)
    watch_address.add_argument("--hook", help="Shell command to run for every message. Message JSON is passed on stdin and in RELAYSTATION_MESSAGE_JSON.")
    watch_address.add_argument("--wake-hook", help="Shell command to run for kind=wake messages.")
    watch_address.add_argument("--hook-timeout", type=float, default=30.0)
    watch_address.set_defaults(func=command_watch_address)

    presence = sub.add_parser("presence", help="Set presence in a room.")
    add_client_args(presence)
    presence.add_argument("room")
    presence.add_argument("--sender")
    presence.add_argument("--agent")
    presence.add_argument("--origin")
    presence.add_argument("--session-id")
    presence.add_argument("--status", default="online")
    presence.add_argument("--note")
    presence.set_defaults(func=command_presence)

    roster = sub.add_parser("roster", help="List room presence.")
    add_client_args(roster)
    roster.add_argument("room")
    roster.set_defaults(func=command_roster)

    announce = sub.add_parser("announce", help="Announce this live session under a short address/callsign.")
    add_client_args(announce)
    announce.add_argument("address")
    announce.add_argument("--sender")
    announce.add_argument("--agent")
    announce.add_argument("--origin")
    announce.add_argument("--session-id")
    announce.add_argument("--room")
    announce.add_argument("--project")
    announce.add_argument("--chat-name")
    announce.add_argument("--session-name")
    announce.add_argument("--role")
    announce.add_argument("--cwd")
    announce.add_argument("--workdir")
    announce.add_argument("--capability", action="append")
    announce.add_argument("--tag", action="append")
    announce.add_argument("--contact-json")
    announce.add_argument("--status", default="online")
    announce.add_argument("--note")
    announce.set_defaults(func=command_announce)

    directory = sub.add_parser("directory", aliases=["rolodex"], help="List announced addresses/callsigns.")
    add_client_args(directory)
    directory.add_argument("--room")
    directory.add_argument("--project")
    directory.add_argument("--role")
    directory.add_argument("--chat-name")
    directory.add_argument("--session-name")
    directory.add_argument("--capability")
    directory.add_argument("--tag")
    directory.add_argument("--status")
    directory.set_defaults(func=command_directory)

    route = sub.add_parser("route", help="Find matching sessions in the router directory.")
    add_client_args(route)
    route.add_argument("--room")
    route.add_argument("--project")
    route.add_argument("--role")
    route.add_argument("--chat-name")
    route.add_argument("--session-name")
    route.add_argument("--capability")
    route.add_argument("--tag")
    route.add_argument("--status")
    route.set_defaults(func=command_route)

    lookup = sub.add_parser("lookup", help="Look up one announced address/callsign.")
    add_client_args(lookup)
    lookup.add_argument("address")
    lookup.set_defaults(func=command_lookup)

    address_inbox = sub.add_parser("address-inbox", aliases=["ainbox"], help="Read messages addressed to one callsign across rooms.")
    add_client_args(address_inbox)
    address_inbox.add_argument("address")
    address_inbox.add_argument("--after-id", type=int, default=0)
    address_inbox.add_argument("--limit", type=int, default=50)
    address_inbox.add_argument("--wait", type=float, default=0.0)
    address_inbox.add_argument("--kind")
    address_inbox.add_argument("--room")
    address_inbox.add_argument("--json", action="store_true")
    address_inbox.set_defaults(func=command_address_inbox)

    connect = sub.add_parser("connect", help="Send a direct message to an address, using a generated thread room if needed.")
    add_client_args(connect)
    connect.add_argument("to_address")
    connect.add_argument("text", nargs="+")
    connect.add_argument("--sender")
    connect.add_argument("--agent")
    connect.add_argument("--origin")
    connect.add_argument("--session-id")
    connect.add_argument("--from-address")
    connect.add_argument("--room")
    connect.add_argument("--project")
    connect.add_argument("--topic")
    connect.add_argument("--kind", default="question")
    connect.add_argument("--metadata-json")
    connect.set_defaults(func=command_connect)

    wake = sub.add_parser("wake", help="Send a wake packet to an address for watchers/automations.")
    add_client_args(wake)
    wake.add_argument("to_address")
    wake.add_argument("text", nargs="*")
    wake.add_argument("--sender")
    wake.add_argument("--agent")
    wake.add_argument("--origin")
    wake.add_argument("--session-id")
    wake.add_argument("--from-address")
    wake.add_argument("--room")
    wake.add_argument("--project")
    wake.add_argument("--topic")
    wake.add_argument("--reason")
    wake.set_defaults(func=command_wake)

    config = sub.add_parser("mcp-config", help="Print MCP config snippets.")
    config.add_argument("--name", default="relaystation")
    config.add_argument("--url", default=os.getenv("RELAYSTATION_URL") or "http://127.0.0.1:8787")
    config.add_argument("--agent")
    config.add_argument("--origin")
    config.add_argument("--session-id")
    config.add_argument("--include-token", action="store_true")
    config.set_defaults(func=command_mcp_config)

    identity = sub.add_parser("identity", help="Print this client's Relaystation identity.")
    identity.set_defaults(func=command_identity)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
