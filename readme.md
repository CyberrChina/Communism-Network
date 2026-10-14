# Communism Network
A super secure (probably) chat app for people in communist china.

## Run the master and room apps

Install Flask with `python -m pip install flask`, then start the master in one
terminal:

```powershell
python master\backend\app.py
```

Start the room app in a second terminal:

```powershell
python room\app.py
```

The master review page is at `http://127.0.0.1:5000/`. The room list and
request form are at `http://127.0.0.1:5001/`. The room app forwards listing requests to the master at
`http://127.0.0.1:5000` by default. For a room on another machine, bind the
master to an appropriate trusted interface with `MASTER_HOST` and set
`MASTER_URL` on the room process to the master's reachable base URL. The
approval endpoints are unauthenticated in this prototype, so do not expose the
master to an untrusted network.

Room listing requests are stored as pending until approved from the master's
review page. Approved rooms are shown in the room list, and the Join action
opens the room's server URL as a basic placeholder for a future in-app join
flow. Master data is persisted in `master/backend/serverlist.txt` as JSON.