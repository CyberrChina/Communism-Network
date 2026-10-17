# Communism Network Room Server

This is the authoritative backend for one chat room. Clients connect directly
to its WebSocket; the master server only handles registration, membership
authorization, and short-lived connection tickets.

## Configuration

Set these in the room server environment:

```bash
ROOM_ID=general-01
ROOM_NAME=General
ROOM_SERVER_ADDRESS=https://room.example.org
ROOM_WS_ADDRESS=wss://room.example.org
MASTER_SERVER_URL=https://communism-network.calvinthegreat.org/master/backend
MASTER_REGISTRATION_TOKEN=replace-with-a-server-secret
CHAT_ROOM_TICKET_SECRET=the-same-secret-used-by-the-master
ROOM_MESSAGE_KEY=base64-fernet-key-kept-outside-the-repository
```

Generate a Fernet key with:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

The room server stores encrypted message history in `messages.sqlite3` by
default. The room server can decrypt its own history for authorized clients;
this is transport encryption plus encrypted-at-rest storage, not end-to-end
encryption. Keep `ROOM_MESSAGE_KEY` backed up and rotate it deliberately.

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Use HTTPS/WSS through the production reverse proxy. The master receives an
authenticated registration and heartbeats at `/api/rooms/register` and
`/api/rooms/heartbeat`.

## WebSocket protocol

The browser first sends `{"type":"authenticate","ticket":"..."}`. The
room server verifies the short-lived, room-scoped ticket and responds with an
`authenticated` event containing a bounded history page. Messages use:

```json
{
  "type": "send_message",
  "client_message_id": "client-generated-id",
  "room_id": "general-01",
  "body": "hello"
}
```

The server returns `message_ack` and broadcasts `new_message` only after the
encrypted message has been committed to SQLite. Duplicate client IDs are
acknowledged without inserting or broadcasting a second message.
