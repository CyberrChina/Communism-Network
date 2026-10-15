# Master Backend

Routing receiver for the Communism Network master server.

This backend also provides MariaDB-backed account registration, login, server
registration, and authenticated user/server memberships.

Expected source/deployment location:

```text
/master/backend/app.py
```

`app.py` is a filesystem source path, not a URL that browsers should request
directly. Serve it through a WSGI server and reverse proxy the public API to a
stable base such as `/master/backend/api`.

Room servers register themselves by sending only their backend directory info:

```json
{
  "room_name": "General",
  "server_address": "https://room-server.example.org"
}
```

Routes exposed by this Flask app:

- `GET /health`
- `POST /routing/rooms`
- `GET /routing/rooms`
- `GET /routing/rooms/<room_id>`
- `POST /auth/register`
- `POST /api/auth/signup`
- `POST /auth/login`
- `POST /api/auth/login`
- `POST /auth/logout`
- `GET /auth/me`
- `POST /servers`
- `POST /servers/<id>/join`
- `GET /servers`
- `GET /api/servers/joined`
- `GET /api/servers/discover`
- `POST /api/servers/join`
- `DELETE /servers/<id>/join`

The frontend in `../frontend` uses those `/api` aliases. Joined rooms are
stored in `user_servers`; room messages are deliberately not handled by this
master backend.

## Database setup

Run `schema.sql` as an administrator, then create a restricted application
account and grant it only CRUD access to `chat_app`:

```sql
CREATE USER 'chat_app'@'localhost' IDENTIFIED BY 'use-a-secret-manager';
GRANT SELECT, INSERT, UPDATE, DELETE ON chat_app.* TO 'chat_app'@'localhost';
FLUSH PRIVILEGES;
```

Configure `CHAT_DB_HOST`, `CHAT_DB_PORT`, `CHAT_DB_NAME`, `CHAT_DB_USER`,
`CHAT_DB_PASSWORD`, and a high-entropy `CHAT_APP_SECRET_KEY`. Never put those
secrets in frontend code or commit them.

Passwords are hashed with Argon2id by `argon2-cffi`; plaintext passwords are
never sent to MariaDB. Flask's signed, HttpOnly session cookie stores only the
authenticated numeric user ID. All database queries use placeholders.

If `servers` already exists from an earlier schema, add the `name`,
`server_address`, and nullable `room_id` columns plus the unique room index
from `schema.sql` before enabling room registration syncing.

Run locally:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```
