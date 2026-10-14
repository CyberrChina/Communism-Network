from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any

from flask import Flask, jsonify, request, send_from_directory


app = Flask(__name__)
MASTER_URL = os.environ.get("MASTER_URL", "http://127.0.0.1:5000").rstrip("/")
ROOM_PAGE_DIRECTORY = Path(__file__).resolve().parent.parent / "master" / "frontend"


def request_master(
    path: str, payload: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, int]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    master_request = Request(
        f"{MASTER_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(master_request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                return {"ok": False, "error": "master returned an invalid response"}, 502
            return result, response.status
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        return {
            "ok": False,
            "error": f"master returned HTTP {error.code}",
            "details": detail,
        }, error.code
    except URLError as error:
        return {"ok": False, "error": f"could not reach master: {error.reason}"}, 502
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return {"ok": False, "error": f"master returned invalid JSON: {error}"}, 502


@app.get("/")
def room_page():
    return send_from_directory(ROOM_PAGE_DIRECTORY, "room.html")


@app.get("/api/servers")
def list_servers():
    result, status = request_master("/routing/rooms")
    return jsonify(result), status


@app.post("/api/request-listing")
def request_listing():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected json object"}), 400

    result, status = request_master("/routing/requests", payload)
    return jsonify(result), status


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001)
