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
ROOM_CHANNELS_JSON='[{"id":"general","name":"General"},{"id":"media","name":"Media"}]'
ROOM_IMAGES_ENABLED=1
ROOM_MAX_IMAGE_BYTES=10485760
ROOM_IMAGE_RETENTION_DAYS=14
ROOM_MEDIA_CLEANUP_INTERVAL=3600
ROOM_SETTINGS_FILE=/path/to/settings.json
```

Generate a Fernet key with:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

The room server stores encrypted message history in `messages.sqlite3` by
default. The room server can decrypt its own history for authorized clients;
this is transport encryption plus encrypted-at-rest storage, not end-to-end
encryption. Images are stored as Fernet-encrypted files below the database
directory (or `ROOM_MEDIA_DIRECTORY`) on the room server, never on the master.
The cleanup loop removes image files, metadata, and message entries older than
`ROOM_IMAGE_RETENTION_DAYS` (14 days by default). Set `ROOM_IMAGES_ENABLED=0`
to disable image uploads for a room; the frontend hides its image controls.
Keep `ROOM_MESSAGE_KEY` backed up and rotate it deliberately.

Room owners control media server-side in `settings.json` (the default is
`room/settings.json`):

```json
{
  "images_enabled": true,
  "notification_sounds_enabled": true,
  "channels": {
    "general": {"images_enabled": true, "messaging_enabled": true},
    "media": {"images_enabled": false, "messaging_enabled": true},
    "lounge": {"name": "Lounge", "type": "voice", "messaging_enabled": false}
  },
  "categories": [
    {"id": "text", "name": "Text Channels", "channels": ["general", "media"]},
    {"id": "voice", "name": "Voice Channels", "channels": ["lounge"]}
  }
}
```

The global setting must be enabled and each channel may independently allow or
deny images. The browser has no user-controlled media toggle; it only shows the
upload button for channels enabled by the room server.

Set a channel's `messaging_enabled` to `false` to make it read-only. Notification
sounds are enabled globally by `notification_sounds_enabled` and can also be
overridden per channel. Users can mute sounds locally in the room composer.

Channels may use `type: "text"` or `type: "voice"`. Voice channels are exposed
as separate category entries and are read-only to the text-message protocol;
voice media transport still needs a WebRTC signaling layer before users can
talk in them.

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

The browser first sends `{"type":"authenticate","ticket":"...","channel_id":"general"}`.
The room server verifies the short-lived, room-scoped ticket and responds with
an `authenticated` event containing the configured channels and a bounded
history page. Send `{"type":"switch_channel","channel_id":"media"}` to
change channel; broadcasts are only delivered to clients in the same channel.
Messages use:

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

Image uploads use `POST /media/upload/<channel_id>` with the same ticket as a
Bearer token and a multipart `image` field plus `client_message_id`. Authorized
clients fetch an image through `GET /media/<media_id>` with that ticket.
