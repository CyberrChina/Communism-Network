from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify, request, send_file
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
DATABASE_PATH = Path(os.environ.get("ROOM_DATABASE_PATH", Path(__file__).with_name("messages.sqlite3")))
SETTINGS_PATH = Path(os.environ.get("ROOM_SETTINGS_FILE", Path(__file__).with_name("settings.json")))
MAX_IMAGE_BYTES = int(os.environ.get("ROOM_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
IMAGE_RETENTION_DAYS = int(os.environ.get("ROOM_IMAGE_RETENTION_DAYS", "14"))
MEDIA_DIRECTORY = Path(os.environ.get("ROOM_MEDIA_DIRECTORY", str(DATABASE_PATH.parent / "media")))
MEDIA_CLEANUP_INTERVAL = int(os.environ.get("ROOM_MEDIA_CLEANUP_INTERVAL", "3600"))
RATE_LIMIT_COUNT = int(os.environ.get("ROOM_MESSAGE_RATE_COUNT", "10"))
RATE_LIMIT_WINDOW = float(os.environ.get("ROOM_MESSAGE_RATE_WINDOW", "10"))
ROOM_ID = os.environ.get("ROOM_ID", "").strip() or hashlib.sha256(f"{ROOM_NAME}\0{SERVER_ADDRESS}".encode()).hexdigest()[:16]

message_key = os.environ.get("ROOM_MESSAGE_KEY", "")
EPHEMERAL_MESSAGE_KEY = not bool(message_key)
if not message_key:
    message_key = Fernet.generate_key().decode()
cipher = Fernet(message_key.encode())
ticket_serializer = URLSafeTimedSerializer(ROOM_TICKET_SECRET, salt="communism-network-room-ticket-v1")

clients: dict[object, dict[str, object]] = {}
clients_lock = threading.Lock()
CHANNEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def load_settings() -> dict[str, object]:
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {}
    return settings if isinstance(settings, dict) else {}


ROOM_SETTINGS = load_settings()
_env_images_enabled = os.environ.get("ROOM_IMAGES_ENABLED")
IMAGES_ENABLED = bool(ROOM_SETTINGS.get("images_enabled", _env_images_enabled != "0" if _env_images_enabled is not None else True))


def configured_channels() -> list[dict[str, str]]:
    raw = os.environ.get("ROOM_CHANNELS_JSON", "")
    if raw:
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            values = []
    else:
        values = [{"id": "general", "name": "General"}]
    channels: list[dict[str, str]] = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        channel_id = str(value.get("id") or "").strip().lower()
        name = str(value.get("name") or "").strip()
        if CHANNEL_ID_RE.fullmatch(channel_id) and name and len(name) <= 64:
            channels.append({"id": channel_id, "name": name})
    return channels or [{"id": "general", "name": "General"}]


CHANNELS = configured_channels()
CHANNEL_MAP = {channel["id"]: channel for channel in CHANNELS}


def channel_images_enabled(channel_id: str) -> bool:
    channel_settings = ROOM_SETTINGS.get("channels", {})
    if isinstance(channel_settings, dict):
        value = channel_settings.get(channel_id)
        if isinstance(value, dict) and "images_enabled" in value:
            return IMAGES_ENABLED and bool(value["images_enabled"])
    return IMAGES_ENABLED


def channel_messaging_enabled(channel_id: str) -> bool:
    channel_settings = ROOM_SETTINGS.get("channels", {})
    if isinstance(channel_settings, dict):
        value = channel_settings.get(channel_id)
        if isinstance(value, dict) and "messaging_enabled" in value:
            return bool(value["messaging_enabled"])
    return True


def channel_notifications_enabled(channel_id: str) -> bool:
    enabled = bool(ROOM_SETTINGS.get("notification_sounds_enabled", True))
    channel_settings = ROOM_SETTINGS.get("channels", {})
    if isinstance(channel_settings, dict):
        value = channel_settings.get(channel_id)
        if isinstance(value, dict) and "notification_sounds_enabled" in value:
            enabled = bool(value["notification_sounds_enabled"])
    return enabled


def channel_payload() -> list[dict[str, object]]:
    return [dict(channel, images_enabled=channel_images_enabled(channel["id"]), messaging_enabled=channel_messaging_enabled(channel["id"]), notification_sounds_enabled=channel_notifications_enabled(channel["id"])) for channel in CHANNELS]


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
                channel_id TEXT NOT NULL DEFAULT 'general',
                message_type TEXT NOT NULL DEFAULT 'text',
                media_id TEXT,
                media_name TEXT,
                media_mime TEXT,
                media_size INTEGER,
                ciphertext BLOB NOT NULL,
                sent_at TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                UNIQUE(sender_id, client_message_id)
            );
            CREATE TABLE IF NOT EXISTS media (
                media_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_room_time
                ON messages(room_id, channel_id, sent_at, message_id);
            CREATE INDEX IF NOT EXISTS idx_media_uploaded_at
                ON media(uploaded_at);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
        if "channel_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN channel_id TEXT NOT NULL DEFAULT 'general'")
        if "message_type" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'")
        if "media_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_id TEXT")
        if "media_name" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_name TEXT")
        if "media_mime" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_mime TEXT")
        if "media_size" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_size INTEGER")
    MEDIA_DIRECTORY.mkdir(parents=True, exist_ok=True)


def registration_payload() -> dict[str, object]:
    return {
        "room_id": ROOM_ID,
        "room_name": ROOM_NAME,
        "display_name": ROOM_NAME,
        "server_address": SERVER_ADDRESS,
        "ws_address": f"{WS_ADDRESS}/ws/{ROOM_ID}",
        "channels": channel_payload(),
        "images_enabled": IMAGES_ENABLED,
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
        body, status = master_request("/api/rooms/heartbeat", {"room_id": ROOM_ID})
        if not (200 <= status < 300 and body.get("ok")):
            register_with_master(retries=3)


def runtime_loop() -> None:
    while not register_with_master(retries=3):
        time.sleep(30)
    heartbeat_loop()


def channel_exists(channel_id: str) -> bool:
    return channel_id in CHANNEL_MAP


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
        "channel_id": row["channel_id"],
        "message_type": row["message_type"],
        "sent_at": row["sent_at"],
        "protocol_version": row["protocol_version"],
    }


def media_message_from_row(row: sqlite3.Row) -> dict[str, object]:
    message = message_from_row(row)
    message["media_id"] = row["media_id"]
    message["media_name"] = row["media_name"]
    message["media_mime"] = row["media_mime"]
    message["media_size"] = row["media_size"]
    return message


def history(channel_id: str = "general") -> list[dict[str, object]]:
    with db_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM messages WHERE room_id = ? AND channel_id = ? ORDER BY sent_at DESC, message_id DESC LIMIT ?",
            (ROOM_ID, channel_id, MAX_HISTORY),
        ).fetchall()
    return [media_message_from_row(row) if row["message_type"] == "image" else message_from_row(row) for row in reversed(rows)]


def persist_message(user_id: int, username: str, client_message_id: str, body: str, channel_id: str = "general") -> tuple[dict[str, object], bool]:
    message_id = uuid.uuid4().hex
    sent_at = utc_now()
    encrypted = cipher.encrypt(body.encode("utf-8"))
    with db_connection() as connection:
        try:
            connection.execute(
                """INSERT INTO messages
                   (message_id, room_id, sender_id, sender_name, client_message_id,
                    channel_id, message_type, ciphertext, sent_at, protocol_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, ROOM_ID, user_id, username, client_message_id, channel_id, "text", encrypted, sent_at, PROTOCOL_VERSION),
            )
            row = connection.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
            return message_from_row(row), True
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT * FROM messages WHERE sender_id = ? AND client_message_id = ?",
                (user_id, client_message_id),
            ).fetchone()
            return message_from_row(row), False


def auth_from_request() -> dict[str, object] | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return parse_ticket(header[7:])


def parse_ticket(ticket: str) -> dict[str, object] | None:
    try:
        claims = ticket_serializer.loads(ticket, max_age=TICKET_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError):
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


def persist_image(user_id: int, username: str, client_message_id: str, channel_id: str, original_name: str, mime_type: str, data: bytes) -> tuple[dict[str, object], bool]:
    media_id = uuid.uuid4().hex
    message_id = uuid.uuid4().hex
    sent_at = utc_now()
    storage_path = MEDIA_DIRECTORY / f"{media_id}.bin"
    encrypted = cipher.encrypt(data)
    storage_path.write_bytes(encrypted)
    with db_connection() as connection:
        try:
            connection.execute(
                "INSERT INTO media (media_id, room_id, channel_id, storage_path, original_name, mime_type, size_bytes, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (media_id, ROOM_ID, channel_id, str(storage_path), original_name, mime_type, len(data), sent_at),
            )
            connection.execute(
                """INSERT INTO messages
                   (message_id, room_id, sender_id, sender_name, client_message_id, channel_id,
                    message_type, ciphertext, media_id, media_name, media_mime, media_size, sent_at, protocol_version)
                   VALUES (?, ?, ?, ?, ?, ?, 'image', ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, ROOM_ID, user_id, username, client_message_id, channel_id, cipher.encrypt(b""), media_id, original_name, mime_type, len(data), sent_at, PROTOCOL_VERSION),
            )
            row = connection.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
            return media_message_from_row(row), True
        except sqlite3.IntegrityError:
            connection.rollback()
            storage_path.unlink(missing_ok=True)
            row = connection.execute("SELECT * FROM messages WHERE sender_id = ? AND client_message_id = ?", (user_id, client_message_id)).fetchone()
            if row is None:
                raise
            return (media_message_from_row(row) if row["message_type"] == "image" else message_from_row(row)), False


def cleanup_expired_media() -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - IMAGE_RETENTION_DAYS * 86400
    cutoff_text = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    removed = 0
    with db_connection() as connection:
        rows = connection.execute("SELECT media_id, storage_path FROM media WHERE uploaded_at < ?", (cutoff_text,)).fetchall()
        for row in rows:
            Path(row["storage_path"]).unlink(missing_ok=True)
            connection.execute("DELETE FROM media WHERE media_id = ?", (row["media_id"],))
            connection.execute("DELETE FROM messages WHERE media_id = ?", (row["media_id"],))
            removed += 1
    return removed


def media_cleanup_loop() -> None:
    while True:
        time.sleep(MEDIA_CLEANUP_INTERVAL)
        try:
            cleanup_expired_media()
        except (OSError, sqlite3.Error):
            pass


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


def broadcast(payload: dict[str, object], channel_id: str | None = None) -> None:
    with clients_lock:
        connected = [(ws, client) for ws, client in clients.items() if channel_id is None or client.get("channel_id") == channel_id]
    for ws, _client in connected:
        if not send_json(ws, payload):
            remove_client(ws)


def remove_client(ws: object) -> None:
    with clients_lock:
        clients.pop(ws, None)


def parse_auth(raw: str) -> dict[str, object] | None:
    try:
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("type") != "authenticate" or not isinstance(event.get("ticket"), str):
            return None
    except (json.JSONDecodeError, TypeError):
        return None
    claims = parse_ticket(event["ticket"])
    if claims is not None:
        requested_channel = str(event.get("channel_id") or "general").strip().lower()
        claims["channel_id"] = requested_channel if channel_exists(requested_channel) else "general"
    return claims


def handle_message(ws: object, client: dict[str, object], event: dict[str, object]) -> bool:
    if event.get("type") == "switch_channel":
        channel_id = str(event.get("channel_id") or "").strip().lower()
        if not channel_exists(channel_id):
            return send_json(ws, {"type": "error", "code": "unknown_channel", "message": "unknown channel"})
        client["channel_id"] = channel_id
        return send_json(ws, {"type": "channel_changed", "channel_id": channel_id, "images_enabled": channel_images_enabled(channel_id), "messaging_enabled": channel_messaging_enabled(channel_id), "notification_sounds_enabled": channel_notifications_enabled(channel_id), "history": history(channel_id)})
    if event.get("type") != "send_message":
        return send_json(ws, {"type": "error", "code": "unsupported_event", "message": "unsupported event"})
    if event.get("room_id", ROOM_ID) != ROOM_ID:
        return send_json(ws, {"type": "error", "code": "wrong_room", "message": "wrong room"})
    channel_id = str(client.get("channel_id") or "general")
    if not channel_messaging_enabled(channel_id):
        return send_json(ws, {"type": "error", "code": "messaging_disabled", "message": "messaging is disabled for this channel"})
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
    message, created = persist_message(int(client["user_id"]), str(client["username"]), client_message_id, body.strip(), channel_id)
    if not send_json(ws, {"type": "message_ack", "client_message_id": client_message_id, "created": created, "message": message}):
        return False
    if created:
        broadcast({"type": "new_message", "message": message}, channel_id)
    return True


@app.get("/health")
def health():
    with clients_lock:
        connected = len(clients)
    return jsonify({"ok": True, "room_id": ROOM_ID, "room_name": ROOM_NAME, "protocol_version": PROTOCOL_VERSION, "connected_clients": connected, "ephemeral_message_key": EPHEMERAL_MESSAGE_KEY, "images_enabled": IMAGES_ENABLED, "image_retention_days": IMAGE_RETENTION_DAYS, "channels": channel_payload()})


@app.get("/channels")
def list_channels():
    if auth_from_request() is None:
        return jsonify({"ok": False, "error": "authentication required"}), 401
    return jsonify({"ok": True, "channels": channel_payload(), "images_enabled": IMAGES_ENABLED})


@app.post("/media/upload/<channel_id>")
def upload_media(channel_id: str):
    claims = auth_from_request()
    channel_id = channel_id.strip().lower()
    if claims is None:
        return jsonify({"ok": False, "error": "authentication required"}), 401
    if not IMAGES_ENABLED:
        return jsonify({"ok": False, "error": "image uploads are disabled"}), 403
    if not channel_exists(channel_id):
        return jsonify({"ok": False, "error": "unknown channel"}), 404
    if not channel_images_enabled(channel_id):
        return jsonify({"ok": False, "error": "image uploads are disabled for this channel"}), 403
    upload = request.files.get("image")
    client_message_id = request.form.get("client_message_id", "")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "image is required"}), 400
    if not isinstance(client_message_id, str) or not 1 <= len(client_message_id) <= 128:
        return jsonify({"ok": False, "error": "invalid client message id"}), 400
    mime_type = (upload.mimetype or "").lower()
    if mime_type not in ALLOWED_IMAGE_TYPES:
        return jsonify({"ok": False, "error": "unsupported image type"}), 415
    data = upload.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify({"ok": False, "error": "image is too large"}), 413
    original_name = Path(upload.filename).name[:128] or "image"
    message, created = persist_image(int(claims["user_id"]), str(claims["username"]), client_message_id, channel_id, original_name, mime_type, data)
    if created:
        broadcast({"type": "new_message", "message": message}, channel_id)
    return jsonify({"ok": True, "created": created, "message": message}), 201 if created else 200


@app.get("/media/<media_id>")
def download_media(media_id: str):
    if auth_from_request() is None:
        return jsonify({"ok": False, "error": "authentication required"}), 401
    with db_connection() as connection:
        row = connection.execute("SELECT media_id, storage_path, mime_type, original_name, room_id FROM media WHERE media_id = ?", (media_id,)).fetchone()
    if row is None or row["room_id"] != ROOM_ID:
        return jsonify({"ok": False, "error": "media not found"}), 404
    try:
        data = cipher.decrypt(Path(row["storage_path"]).read_bytes())
    except (OSError, InvalidToken):
        return jsonify({"ok": False, "error": "media unavailable"}), 404
    return send_file(BytesIO(data), mimetype=row["mime_type"], download_name=row["original_name"])


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
    client = {"user_id": claims["user_id"], "username": claims["username"], "channel_id": claims.get("channel_id", "general"), "recent": deque(), "send_lock": threading.Lock()}
    with clients_lock:
        clients[ws] = client
    if not send_json(ws, {"type": "authenticated", "room_id": ROOM_ID, "channel_id": client["channel_id"], "channels": channel_payload(), "images_enabled": channel_images_enabled(str(client["channel_id"])), "messaging_enabled": channel_messaging_enabled(str(client["channel_id"])), "notification_sounds_enabled": channel_notifications_enabled(str(client["channel_id"])), "history": history(str(client["channel_id"]))}):
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

if os.environ.get("ROOM_AUTOSTART_REGISTRATION") == "1":
    threading.Thread(target=runtime_loop, name="master-registration", daemon=True).start()
    threading.Thread(target=media_cleanup_loop, name="media-cleanup", daemon=True).start()


if __name__ == "__main__":
    registered = register_with_master()
    if not registered:
        print("warning: initial master registration failed; use /register-with-master to retry")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=media_cleanup_loop, daemon=True).start()
    app.run(host=os.environ.get("ROOM_BIND_HOST", "127.0.0.1"), port=int(os.environ.get("ROOM_PORT", "5001")), debug=False)
