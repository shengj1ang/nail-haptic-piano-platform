# Remote Guidance relay server

Routes key/finger guidance between a teacher client and a student client,
stores what happened, and answers latency probes. It is the only part of
the tele-training module that is not on a desk with hardware attached.

**Nothing here touches hardware.** This package imports no camera, MIDI,
MediaPipe, LED or haptic module - all of that runs on a client machine,
because the point of the relay is to move small JSON events, not to hold
devices. It also never re-runs finger matching and never computes its own
accuracy: the numbers it stores are the ones the student's own scoring
(`app.finger_matching`, `app.quiz.summarize`) produced, so the system has
exactly one definition of "correct".

---

## 1. Run it locally

From `main/` (the folder that contains `server/`):

```bash
python -m server --init
```

That writes `server/server_config.json`, creates the SQLite database, and
generates the JWT signing key pair. Then:

```bash
python -m server
```

The relay listens on `http://127.0.0.1:18765` by default (the clients
default to the same address; changing one means changing
`remote_guidance.config.NetworkConfig.server_url` and
`LocalServerConfig.port` too - a test asserts the three agree). Check it:

```bash
curl http://127.0.0.1:18765/api/v1/health
```

Other forms:

```bash
python -m server --gui
```

```bash
python -m server --host 0.0.0.0 --port 18765
```

Interactive API docs (FastAPI's own OpenAPI page) are at `/docs`.

### GUI vs CLI

`--gui` opens a Qt window (the launcher calls it simply **Relay Server**)
showing the bind host, port, registration state, database path, the HTTP
and WebSocket URLs, TLS on/off and run state, plus Start/Stop, the rooms
and clients currently connected, and the live server log.

Everything on that page is read-only. The editable settings - bind host,
port, whether registration is open, and the two token lifetimes - are
behind one **Settings** button, which writes `server_config.json`. It is
refused while the server is running, because the child process read its
configuration when it started; stop it first. TLS certificates and the
JWT signing keys stay out of that form deliberately: they are files, set
up once with `server/tools/`, and a text box inviting them to be retyped
would mostly produce a relay that cannot sign a token.

The panel does **not** run the server inside itself. It launches
`python -m server` as a separate process through `QProcess`, so Stop can
actually end it (terminate, then kill if it ignores that) and the port is
reliably released. Room/client rows come from the child process's own log
lines, which this window owns - no extra unauthenticated status endpoint
is exposed just to fill a dashboard.

The main panel opens at a compact **440 × 360 px**, also its minimum
size. The complete dashboard is wrapped in a resizable scroll area,
so server details, every button, the rooms/client tree and the server log
remain available without the window occupying most of a laptop screen.
The tree and log keep their own scroll bars as well.

The CLI form is what a headless deployment uses; the GUI is a convenience
for running the whole stack on one desk.

---

## 2. Accounts, rooms, joining

```bash
# teacher account
curl -X POST http://127.0.0.1:18765/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"teacher1","password":"choose-a-long-one","role":"teacher"}'
```

```bash
# student account
curl -X POST http://127.0.0.1:18765/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"student1","password":"choose-a-long-one","role":"student"}'
```

The response carries an access token and a refresh token.

A **teacher** creates a room and gets back a short join code:

```bash
curl -X POST http://127.0.0.1:18765/api/v1/rooms \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"name":"Tuesday lesson"}'
```

A **student** joins with that code:

```bash
curl -X POST http://127.0.0.1:18765/api/v1/rooms/join \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"join_code":"K7M4PQ"}'
```

Both clients have a form for this, so the curl calls above are only
needed for scripting or first-time setup. That form asks for a **join
code and nothing else** - no room id is ever typed into a client - and
the teacher reopens an existing room through this same `/rooms/join`
endpoint, which returns a room to its own owner without adding a
membership row. Keep that behaviour: it is what lets one field serve
both roles.

### The demo accounts

For development on one machine the clients' sign-in form arrives
pre-filled with `demoteacher` / `demostudent`. They are ordinary
accounts - register them once, on this relay, exactly as above:

```bash
curl -X POST http://127.0.0.1:18765/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"demoteacher","password":"<the demo password>","role":"teacher"}'
```

The password each client fills in is the constant `DEMO_ACCOUNTS` in
`remote_guidance/gui_common.py`; nothing on this side knows or special-
cases those names, and the relay treats them like any other account. For
a real deployment, register proper accounts, set
`"allow_registration": false`, and empty that mapping so no password is
pre-filled.

Set `"allow_registration": false` once the accounts exist. The very first
account on an empty database is always allowed through, so that setting
cannot lock an operator out of a fresh install.

### Who may do what

Every REST call and every WebSocket frame is checked against the caller's
role *and* their membership row for that room:

- a teacher can only drive rooms they own - not another teacher's;
- a student can never send a teacher-only control
  (`guidance.live`, `session.*`, `recording.start/pause/stop`,
  `latency.probe`); the frame is refused and not relayed;
- a non-member gets 403 on the room's REST endpoints and is disconnected
  from its WebSocket before being registered.

The first version routes one teacher plus one active student per room.
The `room_members` table is already keyed by `(room_id, user_id)` with a
per-row role, so adding more students later is a routing change, not a
schema migration.

---

## 3. Configuring the clients

The clients are configured from the *root* `config.json` of the platform
(`main/config.json`), under a `remote_guidance` block - not from this
folder. Each client edits its own half from its own **Settings** button
(the student client shows the student's camera/MIDI/profile, the teacher
client the teacher's - neither sees the other's, and the launcher has no
settings window for them). This relay's own settings - bind host, port,
registration, token lifetimes - are the **Settings** button on the
control panel here, which writes `server_config.json`. Or edit the client
block by hand:

Each client Settings dialog can also take a one-shot camera photograph
and display the selected keyboard profile's colored pixel mask over it in
a separate preview window. It uses the form's current, possibly unsaved
camera/profile values and saves neither the picture nor the form. A
resolution mismatch is reported rather than resized into a false-looking
alignment. It is the same check the platform's quiz windows offer as
**Preview keyboard profile** (`app/gui/profile_preview.py`), so "is this
camera still aligned with the calibration?" is answered the same way
wherever it is asked.

This Settings dialog is also the only MIDI port selector for its role.
The student and teacher Session pages read the saved port directly and
do not repeat a MIDI dropdown or Refresh button. The teacher keeps a
manual **Connect MIDI** action beside **Chord detection** on its single
live-control row. There is no student-guidance selector on that row:
`visual` / `haptic` / `both` is selected by the student client on its own
Session page.

Teacher and student usually run the same model of keyboard, which reports
the same port name twice. The picker lists those as `... #1` / `... #2`,
numbered in the order the machine enumerates them, and warns that the
numbering can change when a keyboard is replugged. Give the two roles
different ones - identical values silently point both clients at one
instrument. See [../REMOTE_GUIDANCE.md](../REMOTE_GUIDANCE.md) §9.8.

```json
"remote_guidance": {
  "network": {
    "server_url": "http://127.0.0.1:18765",
    "verify_tls": true
  },
  "student": {
    "camera": {"index": 0, "width": 1280, "height": 720, "fps": 30},
    "midi": {"port_name": "SE25 MIDI1 #1"},
    "keyboard_profile": "white-city-lab-20260717",
    "led": {"port": "/dev/tty.usbmodem1101"},
    "haptic": {"port": "/dev/tty.usbmodem2201"},
    "default_guidance_mode": "both"
  },
  "teacher": {
    "camera": {"index": 1},
    "midi": {"port_name": "SE25 MIDI1 #2"},
    "keyboard_profile": "teacher-desk-20260801"
  }
}
```

Student and teacher each have their **own** camera, MIDI port and
keyboard profile; neither touches the shared `camera`/`midi`/
`active_keyboard_profile` keys the ordinary (non-remote) tools use.

The student's LED strip and haptic rig are both serial devices. Blank
ports use auto-detection; `student.led.port` and `student.haptic.port`
remain config-only overrides for a machine where the two similar boards
are detected incorrectly. Setting both overrides to the same port is
rejected with a clear error.

**No credentials live in `config.json`.** Usernames, passwords, tokens
and TLS private keys are never written there.

---

## 4. HTTP/ws → HTTPS/wss

Development default is plain `http` + `ws` on localhost.

**Recommended for anything real:** leave the relay on plain HTTP bound to
localhost and put a reverse proxy in front that terminates TLS. Caddy
obtains and renews a real certificate on its own:

```
relay.example.ac.uk {
    reverse_proxy 127.0.0.1:18765
}
```

Nginx equivalent (the two `Upgrade` headers are what keeps the WebSocket
working through the proxy):

```nginx
server {
    listen 443 ssl;
    server_name relay.example.ac.uk;
    ssl_certificate     /etc/letsencrypt/live/relay.example.ac.uk/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/relay.example.ac.uk/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:18765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
```

Clients then use `https://relay.example.ac.uk` as their server URL; the
WebSocket URL is derived from it automatically (`wss://`).

**Direct TLS**, if you do not want a proxy: generate a development
certificate

```bash
python -m server.tools.generate_dev_certificate --host 127.0.0.1
```

then set in `server_config.json`:

```json
"tls": {"enabled": true, "certfile": "secrets/dev_cert.pem", "keyfile": "secrets/dev_key.pem"}
```

A self-signed certificate makes clients refuse the connection until it is
trusted, which is why it is never enabled by default.

### JWT keys are not the TLS certificate

Two different key pairs, two different jobs, and they are not
interchangeable:

| | JWT signing keys | TLS certificate |
|---|---|---|
| Files | `secrets/jwt_private.pem`, `secrets/jwt_public.pem` | e.g. `secrets/dev_cert.pem`, `secrets/dev_key.pem` |
| Created by | `python -m server.tools.generate_keys` (or `--init`) | `python -m server.tools.generate_dev_certificate`, or a CA / Caddy / certbot |
| Proves | that a token was issued by this server and has not been altered | that the *host* you connected to is the one you meant |
| Seen by | nobody outside the server; only the public half verifies | every client, during the handshake |
| Rotating it | logs everyone out (all tokens become unverifiable) | needs a client reconnect, tokens are unaffected |

A deployment behind Caddy/Nginx has JWT keys here and its TLS material in
the proxy. That is normal - the relay still signs tokens either way.

### Tokens and sessions

Access tokens last 30 days and refresh tokens 180 days by default
(configurable). That is deliberately long so a lesson series does not get
interrupted by a re-login, which makes revocation the important part:

- `POST /auth/logout` blacklists the presented access and refresh tokens
  by their `jti`;
- `POST /auth/refresh` rotates - the refresh token you present is spent,
  so a stolen copy stops working as soon as the real client refreshes;
- a revoked `jti` is rejected even while its `exp` is still in the future.

Server logs never contain a password or a whole token.

---

## 5. Deploying this folder on its own

`server/` has no dependency on the rest of the repository. To move it:

```bash
scp -r server/ user@host:/opt/remote-guidance/
```

```bash
ssh user@host
cd /opt/remote-guidance
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt
python -m server --init
python -m server --host 0.0.0.0 --port 18765
```

Run it from the directory that *contains* `server/`, so `python -m
server` resolves. Note that `secrets/` and `server_config.json` are
git-ignored, so a fresh copy generates its own - which also means tokens
issued by the old host will not verify on the new one.

As a systemd unit:

```ini
[Unit]
Description=Remote Guidance relay
After=network.target

[Service]
WorkingDirectory=/opt/remote-guidance
ExecStart=/opt/remote-guidance/.venv/bin/python -m server --host 127.0.0.1 --port 18765
Restart=on-failure
User=relay

[Install]
WantedBy=multi-user.target
```

Bind to `127.0.0.1` when a proxy terminates TLS in front; bind to
`0.0.0.0` only when the relay itself is the edge.

---

## 6. Backing up SQLite

The database is `data/remote_guidance.db` (plus `-wal`/`-shm` sidecars,
because it runs in WAL mode). **Do not copy those three files while the
server is running** - use SQLite's own online backup, which is consistent
without stopping anything:

```bash
sqlite3 data/remote_guidance.db ".backup 'backup/relay-$(date +%F).db'"
```

Or from Python:

```bash
python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
  data/remote_guidance.db backup/relay.db
```

Restoring is just putting the file back and starting the server; the
schema version is checked and migrated forward on start (`PRAGMA
user_version`), so an older backup upgrades in place.

No video is ever stored in the database. Recordings hold the note/finger
event list needed to drive cues; the media stays on the machine that
captured it, so a backup stays small.

---

## 7. REST API

All paths are prefixed `/api/v1`. Everything except `/health`,
`/auth/register`, `/auth/login` and `/auth/refresh` needs
`Authorization: Bearer <access token>`.

| Method | Path | Who | What |
|---|---|---|---|
| GET | `/health` | anyone | liveness, protocol/schema version, room and connection counts |
| POST | `/auth/register` | anyone (if enabled) | create an account, returns a token pair |
| POST | `/auth/login` | anyone | token pair |
| POST | `/auth/refresh` | refresh token | new pair; the presented refresh token is spent |
| POST | `/auth/logout` | any user | revoke the presented access/refresh tokens |
| GET | `/auth/me` | any user | the caller's account |
| POST | `/rooms` | teacher | create a room, returns its join code |
| POST | `/rooms/join` | any user | join by code |
| GET | `/rooms/{room_id}` | member | room details |
| POST | `/rooms/{room_id}/close` | owning teacher | close the room |
| GET | `/rooms/{room_id}/members` | member | members + who is online now |
| POST | `/rooms/{room_id}/recordings` | owning teacher | upload a fingering/MIDI sequence (metadata + events, no video) |
| GET | `/rooms/{room_id}/recordings` | member | list them |
| GET | `/recordings/{recording_id}` | member | full event list - the student downloads this **before** playback |
| POST | `/rooms/{room_id}/sessions` | owning teacher | open a guidance session; the teacher does not select the student's rendering mode |
| GET | `/sessions` | any user | list the caller's sessions, newest first, with event counts |
| GET | `/sessions/export` | any user | every guidance + performance event of many sessions in one reply |
| GET | `/sessions/{session_id}` | member | session state |
| GET | `/sessions/{session_id}/events` | member | stored guidance + performance events |
| GET | `/sessions/{session_id}/summary` | member | counts, plus the student's own reported summary |

`/sessions/{id}/summary` returns the student's result under
`student_reported_summary` with `summary_source: "student"`. The naming
is deliberate: the server did not compute it.

### Getting a study's timing data off a deployed server

Every other session route needs a session id you already have. During a
lesson the clients do have it, but after the fact the ids only exist in
whatever each client wrote locally - the Student Client records the
session id in its `data/quiz/<session name>/meta.json` as
`song_name: "remote:<session id>"` - and a folder that was never copied
off the student machine leaves the matching server rows unreachable
without a shell on the box. `/sessions` and `/sessions/export` exist for
that case and do nothing else.

    # what is there
    GET /api/v1/sessions?since=<unix seconds>&limit=200
    # pull it
    GET /api/v1/sessions/export?since=<unix seconds>&limit=100
    GET /api/v1/sessions/export?session_ids=<id>,<id>,<id>

`/sessions` returns each session plus `guidance_event_count` and
`performance_event_count`, so a session that recorded nothing can be
skipped without a second request, and `truncated: true` when more matched
than `limit` allowed through. Both accept `room_id`, `since` and `state`;
`limit` caps at 1000 for the listing and 500 for the export, and asking
for more explicit `session_ids` than `limit` is a 400 rather than a
silent trim.

Authorisation is the same membership rule as the single-session routes,
applied through the same helper: a session in a room the caller never
joined is never listed, naming that room outright is a 403, and an
explicit `session_ids` entry from another teacher's room is a 403 rather
than a silent omission. Neither endpoint can write anything.

The export returns stored payloads verbatim - in particular the per-cue
`timings` dictionary the Student Client sends with each performance row
(`teacher_send_wall_ns`, `server_receive_wall_ns`,
`student_receive_wall_ns`, then the student's own monotonic
`queue_enter` / `cue_dispatch_start` / `led_command_complete` /
`haptic_command_complete` / `visual_painted` / `cue_ready` marks). Read
`timing_kind: "software_dispatch_render"` before using any of it: those
are dispatch and render moments on the student's machine, not physical
LED or actuator onset, and the wall-clock marks come from three different
machines' clocks. Section 9's caveats apply unchanged - the monotonic
marks may be subtracted from each other, the wall-clock ones may not.

The current teacher sends `{"mode": "live"}` when opening a live
session. Its initial `guidance_mode` is the neutral value
`student_choice`, not a visual/haptic default. Once the student is ready,
its session-bound `recording.ready` frame reports `visual`, `haptic` or
`both`; the background writer stores that student-reported value on the
session. Older clients may still send a concrete value in the REST body,
but the student's later ready message remains authoritative.

The Teacher Client's Recorded guidance tab follows the same ownership
rule. After uploading the selected song's note/finger events, Trigger
creates a separate session with `mode: "recording"`, `recording_id` and
`playback_mode` (and no teacher guidance modality), then sends
`session.start` followed by `recording.start`. It no longer creates or
borrows a live session merely to enable recorded playback. A student
that has not yet entered Ready state retains the one-shot
`recording.start` envelope locally and consumes it immediately after it
becomes ready.

A recording upload never writes to either client's song library. It
inserts a new uuid-keyed recording and event rows in SQLite, so uploading
the same named source again creates another relay record instead of
overwriting the previous one. The student consumes the downloaded event
list in memory; its performed-session files are written separately by
the client under `data/quiz/<session name>/`.

---

## 8. WebSocket

One persistent connection per client:

```
/ws/v1/rooms/{room_id}
```

The **first frame must be an `auth` envelope** carrying the access token
in its payload. The token is not put in the URL query string, where it
would end up in proxy logs and browser history. Membership of the room in
the path is checked before the connection is registered, so an
unauthenticated socket never receives another room's traffic.

Every frame is one JSON envelope:

```json
{
  "v": 1,
  "type": "guidance.live",
  "message_id": "8f14e45f-ceea-467a-9f0a-8b1e4a1c53d1",
  "room_id": "…",
  "session_id": "…",
  "seq": 42,
  "sent_at_unix_ns": 1786000000000000000,
  "payload": {}
}
```

The relay adds its own block and never rewrites `sent_at_unix_ns`:

```json
"server": {
  "receive_wall_ns": 1786000000001234000,
  "send_wall_ns":    1786000000001250000,
  "sender_user_id": "…", "sender_username": "…", "sender_role": "teacher",
  "out_of_order": false
}
```

Message types:

| Type | Sender | Meaning |
|---|---|---|
| `auth` | client → server | first frame; token in payload |
| `presence` | server → room | who is connected now |
| `heartbeat` | client ↔ server | keep-alive, echoed with server stamps |
| `guidance.live` | teacher | one or more `actions` (note + finger + probabilities) with a `timeout_s` |
| `guidance.received` | student | arrived; sent *before* any cue is presented |
| `guidance.presented` | student | local cue is ready, with the student's own dispatch timings |
| `performance.response` | student | what was actually played; `stage` is `provisional` or `final` |
| `session.start` / `pause` / `resume` / `stop` | teacher | session control |
| `session.finished` | student | end of session + the student's own summary |
| `recording.ready` | student | ready for guidance; reports the student's selected modality and enabled output channels |
| `recording.start` / `pause` / `stop` | teacher | remote trigger for that sequence |
| `latency.probe` | teacher | benchmark probe |
| `latency.received` | student | immediate ack - this is what transport RTT ends on |
| `latency.presented` | student | after the local cue dispatch completed |
| `latency.ack` | server → sender | the relay accepted and forwarded the probe (isolates the teacher→server hop) |
| `error` | server → sender | refused frame; `code` says why, `fatal` says whether the socket survives |

Robustness rules the relay enforces:

- frames larger than 64 KiB are rejected before being parsed;
- a repeated `message_id` is **not** relayed twice - the sender gets an
  `error` with code `duplicate_message` instead, so a reconnecting client
  replaying its outbox cannot double-cue the student;
- a `seq` that goes backwards is flagged `out_of_order` in the server
  block but still delivered, because dropping a late cue silently would
  be worse than showing it late;
- `room_id` in a frame must match the socket's room;
- a client may reconnect and re-authenticate at any time; heartbeats
  detect a dead path in between.

---

## 9. Latency benchmark

Run from `main/`:

```bash
python remote_latency_benchmark.py --server http://127.0.0.1:18765 \
  --username teacher1 --student-username student1
```

This default run is self-contained: the benchmark authenticates both
existing accounts, creates a temporary room, joins and connects its own
simulated Student WebSocket, reads the room's membership back from the
relay so the run shows both accounts really were in one room, then closes
the room without deleting its database record. Only the relay and
benchmark processes need to be open.

No room ever has to be created or pasted in by hand. `--external-student`
creates the room here as well, prints its join code for a real Student
Client, and waits (`--wait-for-student`, default 180 s) until a `presence`
frame shows a student in the room before it starts probing. Use it only
when a real Student Client and its UI/LED/haptic dispatch are
intentionally part of the measurement. `--room-id <id>` is for the single
case where that Student Client has already joined a room of its own.

Defaults follow the report's benchmark: **1000 probes at 500 ms
intervals over one persistent WebSocket per endpoint**, with warm-up samples
recorded separately and excluded from the statistics.

`--interval-mode` chooses how the probes are spaced. The default `fixed`
period is easy to describe but samples the same phase of anything
periodic in the path on every probe; `uniform` jitters each wait by
`--interval-jitter` of the interval, and `poisson` draws it from an
exponential distribution (RFC 2330 § 11.1, truncated at 5x the mean).
`human` paces probes like a person being cued. It is fitted at run time
to every trial of whichever participants are in `data/quiz/` (at 14
participants: median 664 ms, log-sigma 0.441 over 11,335 trials, ~0.7 s),
and samples hierarchically - a participant, then that participant's own
lognormal - because pooling every trial inflates sigma. The fitted median
is also its default interval. Lognormal is checked against ex-Gaussian
and shifted lognormal rather than assumed, and a bootstrap interval is
reported with it; the CLI prints progress while it reads. Each run
records the population it used in `summary.json`. The first three modes
share one mean, so only the spacing pattern changes; `human` has its own
median and a mean above it. The pacing seed is recorded in
`summary.json` and can be replayed with `--interval-seed`. Results land in
`data/remote_guidance/latency/<run_id>/` as `samples.csv`,
`summary.json` and `latency.png`.

### What is measured

| Metric | Definition |
|---|---|
| **Transport RTT** | teacher's `perf_counter_ns` at probe send → same clock when `latency.received` arrives. **Primary metric.** |
| Teacher→server ack | probe send → the relay's `latency.ack`. Isolates which hop is slow. |
| Cue-presented RTT | external-Student mode only: probe send → `latency.presented`, including real Student local cue dispatch |
| Student dispatch duration | external-Student mode only: measured entirely on the real Student's monotonic clock |
| One-way estimate | see below |
| Loss | probes with no valid ack before the timeout, as a proportion of the run |

Reported for each: n, lost, loss rate, min, mean, standard deviation,
median, p95, p99, max.

### Why cross-machine timestamps are not one-way latency

`T_receive,B − T_send,A` is only a latency if A's and B's clocks agree.
Two independent computers do not: their wall clocks differ by an unknown
offset that is often larger than the delay being measured, so the
subtraction can even come out negative. The tool therefore:

- reports **RTT as the primary transport measure**, since both stamps
  come from one clock on one machine and no synchronisation is needed;
- reports a one-way figure **only** when both ends are NTP-synchronised
  and the estimated offset uncertainty is recorded alongside it;
- otherwise shows `RTT/2`, labelled explicitly as a *symmetry-based
  estimate* - which assumes the two directions are equally fast, and they
  often are not.

A lightweight four-timestamp offset estimator (many exchanges, lowest-RTT
samples preferred, uncertainty retained) is included so the assumption
can at least be quantified. It is **not** NTP and is not presented as a
substitute for it; `time.is` or `chronyc tracking` before a run remains
the sanity check.

Monotonic timestamps are never subtracted across machines. They are used
only on the machine that produced them, and travel as opaque values
otherwise.

### Software timing, not physical onset

Everything above is **software dispatch/render timing**: the moment a
command was written and flushed, or a frame was painted. It is not the
moment an LED emitted light or a motor started moving. Measuring physical
onset needs a photodiode on the key and an accelerometer on the actuator
sampled on one acquisition clock (the report's
`System Transmission Performance Benchmarking`); the schema keeps fields
free for that, and this tool does not claim it.

### Why student reaction time does not start at the teacher's send time

Student reaction time is always

```
reaction_time_ns = student_response_monotonic_ns - cue_ready_monotonic_ns
```

Both terms come from the student's own monotonic clock. `cue_ready` is
the moment the student's cue was actually ready - the maximum of the LED
command completing, the screen paint finishing, and the haptic serial
flush returning, over whichever channels that session enabled.

Starting the clock at `teacher_send`, `server_receive` or
`student_receive` would fold network delay, queueing and local dispatch
into a number describing a *person*, so a slow network would look like a
slow learner. Those transport figures are all recorded, separately, on
the same event - they are simply never part of the reaction time.
