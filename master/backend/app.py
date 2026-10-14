from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from flask import Flask, jsonify, request, send_from_directory


app = Flask(__name__)
FRONTEND_DIRECTORY = Path(__file__).resolve().parent.parent / "frontend"
DATA_FILE = Path(__file__).with_name("serverlist.txt")
data_lock = Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_data() -> dict[str, list[dict[str, Any]]]:
    if not DATA_FILE.exists() or not DATA_FILE.read_text(encoding="utf-8").strip():
        return {"servers": [], "requests": []}
    with DATA_FILE.open(encoding="utf-8") as server_list:
        data = json.load(server_list)
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list) or not isinstance(
        data.get("requests"), list
    ):
        raise ValueError(f"Invalid server list data in {DATA_FILE}")
    return data


def save_data(data: dict[str, list[dict[str, Any]]]) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as server_list:
        json.dump(data, server_list, indent=2)
        server_list.write("\n")


def clean_room_payload(payload: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    room_name = str(payload.get("room_name") or payload.get("name") or "").strip()
    server_address = str(
        payload.get("server_address") or payload.get("server_url") or payload.get("url") or ""
    ).strip()

    if not room_name:
        return None, "missing room_name"
    if not server_address:
        return None, "missing server_address"
    parsed_address = urlparse(server_address)
    if parsed_address.scheme not in {"http", "https"} or not parsed_address.netloc:
        return None, "server_address must be an http or https URL"

    room_id = sha256(f"{room_name}\0{server_address}".encode("utf-8")).hexdigest()[:16]
    return {
        "room_id": room_id,
        "room_name": room_name,
        "server_address": server_address,
    }, None


@app.get("/")
def discover_page():
    return send_from_directory(FRONTEND_DIRECTORY, "discover.html")


@app.get("/room")
def room_page():
    return send_from_directory(FRONTEND_DIRECTORY, "room.html")


@app.get("/health")
def health():
    with data_lock:
        data = load_data()
        server_count = len(data["servers"])
    return jsonify({"ok": True, "servers": server_count})


@app.post("/routing/requests")
@app.post("/api/request-listing")
def receive_room_listing_request():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected json object"}), 400

    room, error = clean_room_payload(payload)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    if room is None:
        return jsonify({"ok": False, "error": "invalid room details"}), 400

    listing_request = {
        "request_id": uuid4().hex,
        **room,
        "message": "Hey, I want to be added to the server list.",
        "requested_at": utc_now(),
    }
    with data_lock:
        data = load_data()
        data["requests"].append(listing_request)
        save_data(data)
    return jsonify({"ok": True, "request": listing_request}), 202


@app.get("/routing/requests")
def list_room_listing_requests():
    with data_lock:
        data = load_data()
    return jsonify({"ok": True, "requests": data["requests"]})


@app.post("/routing/requests/<request_id>/approve")
def approve_room_listing_request(request_id: str):
    with data_lock:
        data = load_data()
        listing_request = next(
            (item for item in data["requests"] if item["request_id"] == request_id), None
        )
        if listing_request is None:
            return jsonify({"ok": False, "error": "request not found"}), 404

        server = {
            key: listing_request[key]
            for key in ("room_id", "room_name", "server_address")
        }
        server["added_at"] = utc_now()
        existing = next(
            (item for item in data["servers"] if item["room_id"] == server["room_id"]), None
        )
        if existing is None:
            data["servers"].append(server)
        else:
            server = existing
        data["requests"] = [
            item for item in data["requests"] if item["request_id"] != request_id
        ]
        save_data(data)
    return jsonify({"ok": True, "server": server})


@app.get("/routing/rooms")
@app.get("/api/servers")
def list_room_servers():
    with data_lock:
        data = load_data()
    servers = sorted(data["servers"], key=lambda server: server["room_name"].lower())
    return jsonify({"ok": True, "rooms": servers})


@app.get("/routing/rooms/<room_id>")
def get_room_server(room_id: str):
    with data_lock:
        data = load_data()
        server = next((item for item in data["servers"] if item["room_id"] == room_id), None)
    if server is None:
        return jsonify({"ok": False, "error": "room not found"}), 404
    return jsonify({"ok": True, "room": server})


if __name__ == "__main__":
    app.run(host=os.environ.get("MASTER_HOST", "127.0.0.1"), port=5000)
