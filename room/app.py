from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify
from flask_sock import Sock
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


app = Flask(__name__)
sock = Sock(app)

ROOM_NAME = os.environ.get("ROOM_NAME", "General").strip()
SERVER_ADDRESS = os.environ.get("ROOM_SERVER_ADDRESS", "http://127.0.0.1:5001").strip().rstrip("/")
WS_ADDRESS = os.environ.get("ROOM_WS_ADDRESS", SERVER_ADDRESS.replace("https://", "wss://").replace("http://", "ws://")).strip().rstrip("/")
MASTER_URL = os.environ.get("MASTER_SERVER_URL", "https://communism-network.calvinthegreat.org/master/backend").strip().rstrip("/")
REGISTRATION_TOKEN = os.environ.get("MASTER_REGISTRATION_TOKEN", "")
ROOM_TICKET_SECRET = os.environ.get("CHAT_ROOM_TICKET_SECRET", os.environ.get("ROOM_TICKET_SECRET", "dev-only-change-me"))
TICKET_MAX_AGE = int(os.environ.get("ROOM_TICKET_MAX_AGE", "120"))
PROTOCOL_VERSION = "1"
MAX_MESSAGE_LENGTH = int(os.environ.get("ROOM_MAX_MESSAGE_LENGTH", "2000"))
MAX_HISTORY = int(os.environ.get("ROOM_HISTORY_PAGE_SIZE", "100"))
RATE_LIMIT_COUNT = int(os.environ.get("ROOM_MESSAGE_RATE_COUNT", "10"))
RATE_LIMIT_WINDOW = float(os.environ.get("ROOM_MESSAGE_RATE_WINDOW", "10"))
DATABASE_PATH = Path(os.environ.get("ROOM_DATABASE_PATH", Path(__file__).with_name("messages.sqlite3")))
ROOM_ID = os.environ.get("ROOM_ID", "").strip() or hashlib.sha256(f"{ROOM_NAME}\0{SERVER_ADDRESS}".encode()).hexdigest()[:16]

message_key = os.environ.get("ROOM_MESSAGE_KEY", "")
EPHEMERAL_MESSAGE_KEY = not bool(message_key)
if not message_key:
    message_key = Fernet.generate_key().decode()
cipher = Fernet(message_key.encode())
ticket_serializer = URLSafeTimedSerializer(ROOM_TICKET_SECRET, salt="communism-network-room-ticket-v1")

clients: dict[object, dict[str, object]] = {}
clients_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connection() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                client_message_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                sent_at TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                UNIQUE(sender_id, client_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_room_time
                ON messages(room_id, sent_at, message_id);
            """
        )


def registration_payload() -> dict[str, object]:
    return {
        "room_id": ROOM_ID,
        "room_name": ROOM_NAME,
        "display_name": ROOM_NAME,
        "server_address": SERVER_ADDRESS,
        "ws_address": f"{WS_ADDRESS}/ws/{ROOM_ID}",
        "protocol_version": PROTOCOL_VERSION,
    }


def master_request(path: str, payload: dict[str, object]) -> tuple[dict[str, object], int]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"content-type": "application/json"}
    if REGISTRATION_TOKEN:
        headers["authorization"] = f"Bearer {REGISTRATION_TOKEN}"
    request = Request(f"{MASTER_URL}{path}", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8")), response.status
    except HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            body = {"ok": False, "error": "master server rejected request"}
        return body, error.code
    except (OSError, URLError) as error:
        return {"ok": False, "error": str(error)}, 502


def register_with_master(retries: int = 3) -> bool:
    for attempt in range(retries):
        body, status = master_request("/api/rooms/register", registration_payload())
        if 200 <= status < 300 and body.get("ok"):
            return True
        if attempt + 1 < retries:
            time.sleep(2**attempt)
    return False


def heartbeat_loop() -> None:
    while True:
        time.sleep(30)
        master_request("/api/rooms/heartbeat", {"room_id": ROOM_ID})


def message_from_row(row: sqlite3.Row) -> dict[str, object]:
    try:
        body = cipher.decrypt(bytes(row["ciphertext"])).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError):
        body = "[message unavailable]"
    return {
        "message_id": row["message_id"],
        "room_id": row["room_id"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "body": body,
        "sent_at": row["sent_at"],
        "protocol_version": row["protocol_version"],
    }


def history() -> list[dict[str, object]]:
    with db_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM messages WHERE room_id = ? ORDER BY sent_at DESC, message_id DESC LIMIT ?",
            (ROOM_ID, MAX_HISTORY),
        ).fetchall()
    return [message_from_row(row) for row in reversed(rows)]


def persist_message(user_id: int, username: str, client_message_id: str, body: str) -> tuple[dict[str, object], bool]:
    message_id = uuid.uuid4().hex
    sent_at = utc_now()
    encrypted = cipher.encrypt(body.encode("utf-8"))
    with db_connection() as connection:
        try:
            connection.execute(
                """INSERT INTO messages
                   (message_id, room_id, sender_id, sender_name, client_message_id,
                    ciphertext, sent_at, protocol_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, ROOM_ID, user_id, username, client_message_id, encrypted, sent_at, PROTOCOL_VERSION),
            )
            row = connection.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
            return message_from_row(row), True
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT * FROM messages WHERE sender_id = ? AND client_message_id = ?",
                (user_id, client_message_id),
            ).fetchone()
            return message_from_row(row), False


def send_json(ws: object, payload: dict[str, object]) -> bool:
    with clients_lock:
        client = clients.get(ws)
    send_lock = client.get("send_lock") if client else None
    try:
        if send_lock:
            with send_lock:  # type: ignore[union-attr]
                ws.send(json.dumps(payload, separators=(",", ":")))
        else:
            ws.send(json.dumps(payload, separators=(",", ":")))
        return True
    except Exception:
        return False


def broadcast(payload: dict[str, object]) -> None:
    with clients_lock:
        connected = list(clients)
    for ws in connected:
        if not send_json(ws, payload):
            remove_client(ws)


def remove_client(ws: object) -> None:
    with clients_lock:
        clients.pop(ws, None)


def parse_auth(raw: str) -> dict[str, object] | None:
    try:
        event = json.loads(raw)
        if event.get("type") != "authenticate" or not isinstance(event.get("ticket"), str):
            return None
        claims = ticket_serializer.loads(event["ticket"], max_age=TICKET_MAX_AGE)
    except (json.JSONDecodeError, BadSignature, SignatureExpired, TypeError):
        return None
    if claims.get("aud") != "room-server" or claims.get("room_id") != ROOM_ID:
        return None
    try:
        claims["user_id"] = int(claims["user_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(claims.get("username"), str) or not claims["username"]:
        return None
    return claims


def handle_message(ws: object, client: dict[str, object], event: dict[str, object]) -> bool:
    if event.get("type") != "send_message":
        return send_json(ws, {"type": "error", "code": "unsupported_event", "message": "unsupported event"})
    if event.get("room_id", ROOM_ID) != ROOM_ID:
        return send_json(ws, {"type": "error", "code": "wrong_room", "message": "wrong room"})
    body = event.get("body")
    client_message_id = event.get("client_message_id")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_MESSAGE_LENGTH:
        return send_json(ws, {"type": "error", "code": "invalid_message", "message": "message must be 1-2000 characters"})
    if not isinstance(client_message_id, str) or not 1 <= len(client_message_id) <= 128:
        return send_json(ws, {"type": "error", "code": "invalid_client_message_id", "message": "invalid client message id"})
    now = time.monotonic()
    recent: deque[float] = client["recent"]  # type: ignore[assignment]
    while recent and now - recent[0] > RATE_LIMIT_WINDOW:
        recent.popleft()
    if len(recent) >= RATE_LIMIT_COUNT:
        return send_json(ws, {"type": "error", "code": "rate_limited", "message": "too many messages"})
    recent.append(now)
    message, created = persist_message(int(client["user_id"]), str(client["username"]), client_message_id, body.strip())
    if not send_json(ws, {"type": "message_ack", "client_message_id": client_message_id, "created": created, "message": message}):
        return False
    if created:
        broadcast({"type": "new_message", "message": message})
    return True


@app.get("/health")
def health():
    with clients_lock:
        connected = len(clients)
    return jsonify({"ok": True, "room_id": ROOM_ID, "room_name": ROOM_NAME, "protocol_version": PROTOCOL_VERSION, "connected_clients": connected, "ephemeral_message_key": EPHEMERAL_MESSAGE_KEY})


@app.post("/register-with-master")
def register_route():
    return jsonify({"ok": register_with_master(), "room": registration_payload()})


@sock.route("/ws/<room_id>")
def websocket_room(ws: object, room_id: str):
    if room_id != ROOM_ID:
        ws.close()
        return
    raw = ws.receive()
    claims = parse_auth(raw or "")
    if claims is None:
        send_json(ws, {"type": "error", "code": "unauthorized", "message": "invalid or expired room ticket"})
        ws.close()
        return
    client = {"user_id": claims["user_id"], "username": claims["username"], "recent": deque(), "send_lock": threading.Lock()}
    with clients_lock:
        clients[ws] = client
    if not send_json(ws, {"type": "authenticated", "room_id": ROOM_ID, "history": history()}):
        remove_client(ws)
        return
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            if len(raw.encode("utf-8")) > 8192:
                send_json(ws, {"type": "error", "code": "frame_too_large", "message": "frame too large"})
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                send_json(ws, {"type": "error", "code": "invalid_json", "message": "invalid JSON"})
                continue
            if not isinstance(event, dict) or not handle_message(ws, client, event):
                break
    finally:
        remove_client(ws)


init_db()


if __name__ == "__main__":
    registered = register_with_master()
    if not registered:
        print("warning: initial master registration failed; use /register-with-master to retry")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    app.run(host=os.environ.get("ROOM_BIND_HOST", "127.0.0.1"), port=int(os.environ.get("ROOM_PORT", "5001")), debug=False)
