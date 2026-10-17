from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import hmac
from pathlib import Path
from urllib.parse import urlparse
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
from threading import Lock
from typing import Any, Iterator

import pymysql
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import Flask, jsonify, request, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("CHAT_APP_SECRET_KEY", "dev-only-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("CHAT_APP_COOKIE_SECURE", "1") != "0",
)

rooms: dict[str, dict[str, Any]] = {}
rooms_lock = Lock()
ROOMS_FILE = Path(os.environ.get("CHAT_ROOMS_FILE", Path(__file__).with_name("serverlist.json")))
ROOM_HEARTBEAT_TIMEOUT = int(os.environ.get("CHAT_ROOM_HEARTBEAT_TIMEOUT", "90"))
ROOM_REGISTRATION_SECRET = os.environ.get("CHAT_ROOM_REGISTRATION_SECRET", "")
ROOM_TICKET_SECRET = os.environ.get("CHAT_ROOM_TICKET_SECRET", app.config["SECRET_KEY"])
ROOM_TICKET_MAX_AGE = int(os.environ.get("CHAT_ROOM_TICKET_MAX_AGE", "120"))
ticket_serializer = URLSafeTimedSerializer(ROOM_TICKET_SECRET, salt="communism-network-room-ticket-v1")
password_hasher = PasswordHasher()
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_rooms() -> None:
    if not ROOMS_FILE.exists():
        return
    try:
        saved = json.loads(ROOMS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(saved, list):
        with rooms_lock:
            rooms.update({room["room_id"]: room for room in saved if isinstance(room, dict) and room.get("room_id")})


def save_rooms() -> None:
    ROOMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ROOMS_FILE.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(sorted(rooms.values(), key=lambda room: room["room_name"].lower()), handle, indent=2)
        handle.write("\n")
    temporary.replace(ROOMS_FILE)


def db_config() -> dict[str, Any]:
    config = {
        "host": os.environ.get("CHAT_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("CHAT_DB_PORT", "3306")),
        "user": os.environ.get("CHAT_DB_USER", "chat_app"),
        "password": os.environ.get("CHAT_DB_PASSWORD", ""),
        "database": os.environ.get("CHAT_DB_NAME", "chat_app"),
        "charset": "utf8mb4", "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
    }
    socket_path = os.environ.get("CHAT_DB_SOCKET")
    if socket_path:
        config.pop("host")
        config.pop("port")
        config["unix_socket"] = socket_path
    return config


@contextmanager
def db_connection() -> Iterator[pymysql.connections.Connection]:
    connection = pymysql.connect(**db_config())
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def error(message: str, status: int = 503):
    return jsonify({"ok": False, "error": message}), status


@app.before_request
def csrf_protection():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request.path in {"/auth/register", "/auth/login", "/api/auth/signup", "/api/auth/login", "/routing/rooms"}:
        return None
    if session.get("user_id") is None:
        return None
    expected = session.get("csrf_token")
    provided = request.headers.get("X-CSRF-Token")
    if not expected or not secrets.compare_digest(str(expected), str(provided or "")):
        return error("csrf validation failed", 403)
    return None


def clean_registration_payload(payload: dict[str, Any]):
    username = str(payload.get("username") or "").strip()
    password = payload.get("password")
    if not USERNAME_RE.fullmatch(username):
        return None, "username must be 3-32 letters, numbers, or underscores"
    if not isinstance(password, str) or len(password) < 12:
        return None, "password must be at least 12 characters"
    if len(password) > 1024:
        return None, "password is too long"
    return {"username": username, "password": password}, None


def clean_server_payload(payload: dict[str, Any]):
    raw_address = str(payload.get("ip_address") or payload.get("address") or "").strip()
    parsed_endpoint = room_endpoint(raw_address) if "://" in raw_address else None
    if parsed_endpoint:
        address, inferred_port = parsed_endpoint
        server_address = raw_address if "://" in raw_address else f"https://{raw_address}"
    else:
        try:
            address = str(ipaddress.ip_address(raw_address))
        except ValueError:
            return None, "ip_address must be a valid IP address or server URL"
        inferred_port = None
        server_address = None
    try:
        port = int(payload.get("port") or inferred_port)
    except (TypeError, ValueError):
        return None, "port must be an integer"
    if not 1 <= port <= 65535:
        return None, "port must be between 1 and 65535"
    result = {"ip_address": address, "port": port}
    if server_address:
        result.update({"name": str(payload.get("name") or "").strip() or None, "server_address": server_address})
    return result, None


def current_user_id() -> int | None:
    value = session.get("user_id")
    return int(value) if value is not None else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user_id() is None:
            return error("authentication required", 401)
        return view(*args, **kwargs)
    return wrapped


def clean_room_payload(payload: dict[str, Any]):
    room_name = str(payload.get("room_name") or payload.get("name") or "").strip()
    server_address = str(payload.get("server_address") or payload.get("server_url") or payload.get("url") or "").strip()
    ws_address = str(payload.get("ws_address") or "").strip()
    room_id = str(payload.get("room_id") or "").strip()
    protocol_version = str(payload.get("protocol_version") or "1").strip()
    if not room_name:
        return None, "missing room_name"
    if not server_address:
        return None, "missing server_address"
    if not room_id:
        room_id = sha256(f"{room_name}\0{server_address}".encode()).hexdigest()[:16]
    parsed = room_endpoint(ws_address or server_address)
    if parsed is None:
        return None, "server_address must be a valid http(s) or ws(s) URL"
    return {
        "room_id": room_id,
        "room_name": room_name,
        "server_address": server_address,
        "ws_address": ws_address or server_address.replace("https://", "wss://").replace("http://", "ws://"),
        "protocol_version": protocol_version,
        "last_heartbeat": utc_now(),
        "status": "online",
    }, None


def room_is_online(room: dict[str, Any]) -> bool:
    try:
        last_heartbeat = datetime.fromisoformat(str(room.get("last_heartbeat") or room.get("last_seen")))
        if last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_heartbeat).total_seconds() <= ROOM_HEARTBEAT_TIMEOUT
    except (TypeError, ValueError):
        return False


def require_room_server_auth() -> bool:
    if not ROOM_REGISTRATION_SECRET:
        return True
    provided = request.headers.get("Authorization", "")
    expected = f"Bearer {ROOM_REGISTRATION_SECRET}"
    return hmac.compare_digest(provided, expected)


def room_endpoint(address: str) -> tuple[str, int] | None:
    parsed = urlparse(address if "://" in address else f"https://{address}")
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.hostname, port


def sync_room_server(room: dict[str, Any]) -> int | None:
    endpoint = room_endpoint(room["server_address"])
    if endpoint is None:
        return None
    host, port = endpoint
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO servers (ip_address, port, name, server_address, room_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE ip_address = VALUES(ip_address), port = VALUES(port),
                       name = VALUES(name), server_address = VALUES(server_address)""",
                (host, port, room["room_name"], room["server_address"], room["room_id"]),
            )
            cursor.execute("SELECT id FROM servers WHERE room_id = %s", (room["room_id"],))
            row = cursor.fetchone()
            return int(row["id"]) if row else None
    except pymysql.MySQLError:
        return None


@app.get("/health")
def health():
    return jsonify({"ok": True, "rooms": len(rooms)})


@app.post("/auth/register")
def register():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error("expected json object", 400)
    clean, message = clean_registration_payload(payload)
    if message:
        return error(message, 400)
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (clean["username"], password_hasher.hash(clean["password"])))
            user_id = cursor.lastrowid
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            return error("username already exists", 409)
        return error("database constraint error")
    except pymysql.MySQLError:
        return error("database unavailable")
    session.clear()
    session["user_id"] = int(user_id)
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify({"ok": True, "user": {"id": int(user_id), "username": clean["username"]}, "csrf_token": session["csrf_token"]}), 201


@app.post("/auth/login")
def login():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("username"), str) or not isinstance(payload.get("password"), str):
        return error("username and password are required", 400)
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (payload["username"].strip(),))
            user = cursor.fetchone()
    except pymysql.MySQLError:
        return error("database unavailable")
    if not user:
        return error("invalid username or password", 401)
    try:
        password_hasher.verify(user["password_hash"], payload["password"])
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return error("invalid username or password", 401)
    session.clear()
    session["user_id"] = int(user["id"])
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify({"ok": True, "user": {"id": int(user["id"]), "username": user["username"]}, "csrf_token": session["csrf_token"]})


@app.post("/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/auth/me")
@login_required
def me():
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id, username, created_at FROM users WHERE id = %s", (current_user_id(),))
            user = cursor.fetchone()
    except pymysql.MySQLError:
        return error("database unavailable")
    if not user:
        session.clear()
        return error("user not found", 401)
    return jsonify({"ok": True, "user": user, "csrf_token": session.get("csrf_token")})


@app.post("/servers")
@login_required
def register_server():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error("expected json object", 400)
    clean, message = clean_server_payload(payload)
    if message:
        return error(message, 400)
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO servers (ip_address, port, name, server_address) VALUES (%s, %s, %s, %s)", (clean["ip_address"], clean["port"], clean.get("name"), clean.get("server_address")))
            server_id = cursor.lastrowid
            cursor.execute("SELECT id, ip_address, port, created_at FROM servers WHERE id = %s", (server_id,))
            server = cursor.fetchone()
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            return error("server endpoint already exists", 409)
        return error("database constraint error")
    except pymysql.MySQLError:
        return error("database unavailable")
    return jsonify({"ok": True, "server": server}), 201


@app.post("/servers/<int:server_id>/join")
@login_required
def join_server(server_id: int):
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM servers WHERE id = %s", (server_id,))
            if not cursor.fetchone():
                return error("server not found", 404)
            cursor.execute("INSERT INTO user_servers (user_id, server_id) VALUES (%s, %s)", (current_user_id(), server_id))
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            return error("already joined server", 409)
        return error("database constraint error")
    except pymysql.MySQLError:
        return error("database unavailable")
    return jsonify({"ok": True, "server_id": server_id}), 201


@app.get("/servers")
@login_required
def list_servers():
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT s.id, s.room_id, s.ip_address, s.port, s.name, s.server_address, us.joined_at
                              FROM user_servers AS us
                              INNER JOIN servers AS s ON s.id = us.server_id
                              WHERE us.user_id = %s ORDER BY us.joined_at DESC""", (current_user_id(),))
            servers = cursor.fetchall()
    except pymysql.MySQLError:
        return error("database unavailable")
    return jsonify({"ok": True, "servers": servers})


@app.get("/servers/discover")
def discover_servers():
    user_id = current_user_id()
    with rooms_lock:
        discovered = [dict(room) for room in rooms.values() if room_is_online(room)]
    joined_ids: set[int] = set()
    if user_id is not None:
        try:
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT server_id FROM user_servers WHERE user_id = %s", (user_id,))
                joined_ids = {int(row["server_id"]) for row in cursor.fetchall()}
                for room in discovered:
                    server_id = room.get("server_id") or sync_room_server(room)
                    if server_id:
                        room["server_id"] = server_id
                        room["joined"] = server_id in joined_ids
        except pymysql.MySQLError:
            return error("database unavailable")
    return jsonify({"ok": True, "rooms": sorted(discovered, key=lambda room: room["room_name"].lower())})


@app.get("/rooms/<room_id>/connection")
@login_required
def room_connection(room_id: str):
    with rooms_lock:
        room = dict(rooms.get(room_id) or {})
    if not room:
        return error("room not found", 404)
    if not room_is_online(room):
        return error("room server is offline", 503)
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT u.id, u.username, s.id AS server_id
                   FROM users AS u
                   INNER JOIN user_servers AS us ON us.user_id = u.id
                   INNER JOIN servers AS s ON s.id = us.server_id
                   WHERE u.id = %s AND s.room_id = %s""",
                (current_user_id(), room_id),
            )
            membership = cursor.fetchone()
    except pymysql.MySQLError:
        return error("database unavailable")
    if not membership:
        return error("room membership required", 403)
    ticket = ticket_serializer.dumps({
        "aud": "room-server",
        "room_id": room_id,
        "user_id": int(membership["id"]),
        "username": membership["username"],
        "server_id": int(membership["server_id"]),
    })
    return jsonify({
        "ok": True,
        "room_id": room_id,
        "room_name": room["room_name"],
        "ws_address": room["ws_address"],
        "protocol_version": room.get("protocol_version", "1"),
        "connection_ticket": ticket,
        "expires_in": ROOM_TICKET_MAX_AGE,
    })


@app.post("/servers/join")
@login_required
def join_discovered_server():
    payload = request.get_json(silent=True)
    room_id = payload.get("room_id") if isinstance(payload, dict) else None
    with rooms_lock:
        room = rooms.get(str(room_id))
    if room is None:
        return error("room not found", 404)
    server_id = room.get("server_id") or sync_room_server(room)
    if server_id is None:
        return error("room server is not available", 503)
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO user_servers (user_id, server_id) VALUES (%s, %s)", (current_user_id(), server_id))
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            return error("already joined server", 409)
        return error("database constraint error")
    except pymysql.MySQLError:
        return error("database unavailable")
    room["server_id"] = server_id
    return jsonify({"ok": True, "server_id": server_id, "room": room}), 201


@app.delete("/servers/<int:server_id>/join")
@login_required
def leave_server(server_id: int):
    try:
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_servers WHERE user_id = %s AND server_id = %s", (current_user_id(), server_id))
            removed = cursor.rowcount
    except pymysql.MySQLError:
        return error("database unavailable")
    if not removed:
        return error("server membership not found", 404)
    return jsonify({"ok": True})


@app.post("/api/rooms/register")
@app.post("/routing/rooms")
def register_room_server():
    if not require_room_server_auth():
        return error("room server authentication failed", 401)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error("expected json object", 400)
    room, message = clean_room_payload(payload)
    if message:
        return error(message, 400)
    with rooms_lock:
        already_known = room["room_id"] in rooms
        existing = rooms.get(room["room_id"], {})
        room["server_id"] = existing.get("server_id")
        rooms[room["room_id"]] = room
        save_rooms()
    server_id = room.get("server_id") or sync_room_server(room)
    if server_id:
        with rooms_lock:
            rooms[room["room_id"]]["server_id"] = server_id
            save_rooms()
    return jsonify({"ok": True, "created": not already_known, "room": room}), 201 if not already_known else 200


@app.post("/api/rooms/heartbeat")
@app.post("/routing/rooms/<room_id>/heartbeat")
def room_heartbeat(room_id: str | None = None):
    if not require_room_server_auth():
        return error("room server authentication failed", 401)
    payload = request.get_json(silent=True) or {}
    room_id = room_id or str(payload.get("room_id") or "").strip()
    if not room_id:
        return error("missing room_id", 400)
    with rooms_lock:
        room = rooms.get(room_id)
        if room is None:
            return error("room not found", 404)
        room["last_heartbeat"] = utc_now()
        room["status"] = "online"
        save_rooms()
    return jsonify({"ok": True, "room_id": room_id, "status": "online", "last_heartbeat": room["last_heartbeat"]})


@app.post("/api/rooms/<room_id>/events")
def room_event(room_id: str):
    if not require_room_server_auth():
        return error("room server authentication failed", 401)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error("expected json object", 400)
    if payload.get("event") not in {"status", "occupancy", "sync_metadata"}:
        return error("unsupported room event", 400)
    with rooms_lock:
        room = rooms.get(room_id)
        if room is None:
            return error("room not found", 404)
        room["last_heartbeat"] = utc_now()
        room["status"] = "online"
        if "occupancy" in payload:
            try:
                room["occupancy"] = max(0, int(payload["occupancy"]))
            except (TypeError, ValueError):
                return error("occupancy must be an integer", 400)
        save_rooms()
    return jsonify({"ok": True})


@app.get("/routing/rooms")
def list_room_servers():
    with rooms_lock:
        room_list = sorted(rooms.values(), key=lambda room: room["room_name"].lower())
    return jsonify({"ok": True, "rooms": room_list})


@app.get("/routing/rooms/<room_id>")
def get_room_server(room_id: str):
    with rooms_lock:
        room = rooms.get(room_id)
    if room is None:
        return error("room not found", 404)
    return jsonify({"ok": True, "room": room})


# Keep the browser-facing API names predictable while retaining the original
# routes used by room backends and earlier prototypes.
app.add_url_rule("/api/auth/signup", endpoint="api_signup", view_func=register, methods=["POST"])
app.add_url_rule("/api/auth/login", endpoint="api_login", view_func=login, methods=["POST"])
app.add_url_rule("/api/auth/logout", endpoint="api_logout", view_func=logout, methods=["POST"])
app.add_url_rule("/api/auth/me", endpoint="api_me", view_func=me, methods=["GET"])
app.add_url_rule("/api/servers/joined", endpoint="api_joined", view_func=list_servers, methods=["GET"])
app.add_url_rule("/api/servers/discover", endpoint="api_discover", view_func=discover_servers, methods=["GET"])
app.add_url_rule("/api/servers/join", endpoint="api_join", view_func=join_discovered_server, methods=["POST"])
app.add_url_rule("/api/servers/<int:server_id>/join", endpoint="api_leave", view_func=leave_server, methods=["DELETE"])
app.add_url_rule("/api/rooms/<room_id>/connection", endpoint="api_room_connection", view_func=room_connection, methods=["GET"])


load_rooms()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
