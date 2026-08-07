# Remote Guidance / Tele-training - design and handover notes

**Read this first if you are picking up work on the remote guidance
module.** It describes what exists today, why it is shaped the way it
is, and what is still missing. It is written for a future session (human
or AI) starting cold.

**Status: working, incomplete, and expected to change a lot.** The
end-to-end path runs and is tested, but nothing has been verified against
real hardware or a real two-machine network. See
[§10 Known gaps](#10-known-gaps-and-what-is-not-verified) before trusting
anything here.

---

## 0. Where the other documentation is

| Document | Path | What it covers |
|---|---|---|
| **Relay server operator guide** | [`server/README.md`](server/README.md) | Running the server, accounts/rooms, HTTP↔HTTPS and ws↔wss, JWT keys vs TLS certificates, deploying `server/` on its own, SQLite backup, the full REST + WebSocket reference, and the latency-metric definitions |
| Platform README, Tele-training section | [`README.md`](README.md) § "Tele-training (launcher section 8)" | User-facing quick start, config block, guidance modes, what is reused, timing summary |
| Platform README, everything else | [`README.md`](README.md) | The non-remote platform: calibration profiles, quiz, sequence generator, analysis, haptic config |
| Report methodology | `../../final_report_2026/method/method.tex` | § "Network-Based Guidance Latency", § "Tele-training Guidance Modes", § "System Transmission Performance Benchmarking" - the requirements this module implements |
| Sequence generator | [`SEQUENCE_GENERATOR_ALGORITHM.md`](SEQUENCE_GENERATOR_ALGORITHM.md) | Only relevant because generated sequences can be uploaded as recordings |

`server/README.md` is the deeper reference for anything protocol- or
deployment-shaped. This file is the architectural overview and the
"things you would otherwise learn the hard way" list.

---

## 1. What it does

Two people, two machines, one relay in between.

- **Synchronous live cueing** - the teacher plays a key on their own
  keyboard; the platform's existing camera pipeline works out *which
  finger* they used; that key/finger pair is sent to the student, whose
  keyboard LED lights the key and whose screen and/or nail-mounted
  actuator cues the finger.
- **Asynchronous recorded sequences** - the teacher uploads a fingering
  plan recorded earlier (`data/music/` or `data/sequence/`) and triggers
  it remotely. The student downloads the whole thing first and schedules
  every cue locally, so no individual note waits on the network.

The teacher's **Recorded guidance** tab can open the platform's existing
`RecordingWizard` directly, so a new `data/music/<song>/` performance can
be recorded without leaving the Teacher Client. This is an integration
of `app.gui.recording_wizard`, not a second recording implementation.

"Upload" here does **not** copy or move that source folder. The teacher
reads it and sends only the note/finger/timing event list; the relay
stores each upload as a new recording row with a new uuid, and the
student downloads that JSON into memory. Even when both clients run from
the same checkout, neither client writes back to `data/music/` or
`data/sequence/`, so the source is not overwritten. The student's
performance is a separate artefact under `data/quiz/<session name>/`;
reusing an existing session name can overwrite the files in that quiz
folder, so use a unique session name when the earlier result must be
kept.

Both modes come from the report's "Tele-training Guidance Modes".

---

## 2. The three processes

| Process | Entry point | Owns |
|---|---|---|
| Student Client | `student_remote_guidance.py` | camera 1, MIDI keyboard 1, LED strip, nail actuators |
| Teacher Client | `teacher_remote_guidance.py` | camera 2, MIDI keyboard 2 |
| Relay Server | `python -m server` | nothing physical |

They are separate OS processes, not windows in one app. The launcher's
usual "one tool at a time" rule cannot express this - all three run
simultaneously and the two clients hold different cameras - so section 8
starts them with `QProcess.startDetached` (see
`remote_guidance/launcher_actions.py`).

---

## 3. File map

### `remote_guidance/` - the client half

| File | Lines | Responsibility |
|---|---:|---|
| `config.py` | 320 | The `remote_guidance` block of `config.json`; per-role `Config` views; serial-port validation |
| `protocol.py` | 210 | Versioned message envelope, message-type constants, `GuidanceAction`. **Client half of a matched pair with `server/schemas.py`** |
| `timing.py` | 393 | Wall vs monotonic clocks, `DispatchTimings`, `ClockOffsetEstimator`, `LatencyStats`, `one_way_estimate` |
| `network_client.py` | 479 | `RemoteApiClient` (stdlib urllib REST) + `RemoteWebSocketClient` (threaded, `websocket-client`). **No Qt** |
| `qt_bridge.py` | 104 | The only place the network layer meets Qt - turns callbacks into signals |
| `cue_outputs.py` | 310 | `CompositeCueOutput`, `LedKeyCue`, `build_student_cue` |
| `gui_common.py` | 571 | `SignInPanel`, `RoomPanel`, `ApiCallWorker`, `StageWindow` mixin |
| `settings_window.py` | 389 | `RemoteSettingsDialog` - **one role's** camera/MIDI/profile, plus a one-shot camera/profile mask preview opened in its own dialog |
| `launcher_actions.py` | 116 | `ProcessSpec`s and health check for launcher section 8 |
| `student/session.py` | 551 | **The core.** Event model, queueing, all timing rules. No Qt, no hardware |
| `student/window.py` | 1170 | Student GUI: devices in, Qt signals out; owns the visual/haptic/both choice, announces it when ready, and defers an early recorded trigger until ready |
| `teacher/live_detector.py` | 218 | Camera → HandTracker → MIDI → finger matching, assembled for the teacher |
| `teacher/recording_import.py` | 160 | `data/music`/`data/sequence` → uploadable recording |
| `teacher/window.py` | 980 | Teacher GUI; separate live/recorded tabs, integrated Song Recording Wizard, and session control without choosing the student's rendering mode |
| `tools/latency_benchmark.py` | 572 | The benchmark CLI |
| `tools/benchmark_window.py` | 251 | GUI wrapper around that CLI (runs it as a QProcess) |

### `server/` - the relay

See `server/README.md`. Structure: `config.py`, `database.py` (SQLite +
migrations), `auth.py` (Argon2id + RS256 JWT), `schemas.py`, `api.py`
(REST), `websocket.py` (relay), `app.py` (factory + background writer),
`gui.py` (Qt control panel), `__main__.py` (CLI), `tools/` (key and
certificate generation).

### Tests

| File | Lines | Covers |
|---|---:|---|
| `test-script/test_remote_guidance_config.py` | 2163 | Config compat, role isolation, serial clash, composite cue, session timing, latency stats, launcher actions, GUI staging, tabs/wizard integration and camera/profile preview |
| `test-script/test_remote_guidance_server.py` | 878 | Passwords, tokens, authorization, WebSocket relay, persistence, and student-owned guidance mode |
| `test-script/test_remote_guidance_e2e.py` | 575 | Real server + fake teacher + fake student |

**223 tests**, none of which need hardware or a real network.

---

## 4. Rules that must not be broken

These are the invariants the whole design rests on. Breaking one is
usually silent, which is why each has a test.

### 4.1 Reaction time starts at the student's own cue-ready moment

```
reaction_time_ns = student_response_monotonic_ns - cue_ready_monotonic_ns
```

Both terms from the **student's** `perf_counter_ns()`. Never from
`teacher_send`, `server_receive` or `student_receive` - those would fold
network delay and queueing into a number describing a *person*, so a slow
link would read as a slow learner. All those transport stamps are still
recorded, in their own fields, on the same event.

`cue_ready` is the **last** enabled channel to become ready:
`max(LED command complete, visual paint complete, haptic serial flush
complete)`. The LED is in every guidance mode, so it is always in the
maximum.

### 4.2 Monotonic clocks never cross machines

A `perf_counter_ns()` value is meaningful only on the machine that
produced it. Cross-machine values travel as **opaque** data and are
echoed back untouched. Transport RTT is measured entirely on the sender's
own clock (send stamp → ack stamp), so it needs no clock synchronisation
- which is why it is the primary metric.

One-way latency is reported as a *measurement* only when both hosts are
confirmed NTP-synchronised **and** the offset uncertainty travels with
it; otherwise the output is `RTT/2`, explicitly labelled a
symmetry-based estimate. See `timing.one_way_estimate`.

### 4.3 Software dispatch is not physical onset

Every cue timestamp marks when a serial write flushed or a frame
finished painting - not when an LED emitted light or a motor moved.
Field names, docstrings, the `timing_kind:
"software_dispatch_render"` marker on the wire, and the benchmark's
caveat strings all say so. Measuring real onset needs a photodiode and an
accelerometer on one acquisition clock; `samples.csv` keeps two columns
free for it and nothing fills them in.

### 4.4 The server computes nothing

No finger matching, no accuracy. The relay stores what the student's own
scoring produced. `GET /sessions/{id}/summary` returns it under
`student_reported_summary` with `summary_source: "student"`. There must
only ever be one definition of "correct" in the system, and it lives in
`app.finger_matching` / `app.quiz.summarize`.

### 4.5 The server imports nothing from the platform

`server/` must stay copyable to a remote host on its own. No camera,
MIDI, MediaPipe, LED or haptic import. Verify with:

```bash
grep -rE "^\s*(from|import)\s+(app|common|remote_guidance|cv2|mediapipe|mido|serial)" server/
```

This is why the message envelope is defined **twice** -
`remote_guidance/protocol.py` and `server/schemas.py`. They are a matched
pair; change both together. `PROTOCOL_VERSION` makes a mismatch loud.

### 4.6 The remote config block is strictly additive

`config.json`'s top-level `camera`, `midi` and `active_keyboard_profile`
keep their meaning for every non-remote tool. `RemoteGuidanceConfig.save()`
writes exactly one key and re-writes the rest untouched.
`role_config()` hands a tool a **deep copy** with that role's devices
substituted - the shared `cfg` is never mutated.

A `config.json` with no `remote_guidance` block loads with defaults.

### 4.7 No credentials in `config.json`

Passwords, tokens and TLS private keys are never written there. Only the
server URL, username and last room id.

The pre-filled demo password (`DEMO_ACCOUNTS` in `gui_common.py`, see
§6) is not an exception: it is a source constant on the *client*, and
`RemoteGuidanceConfig.save()` has no key it could be written to. If demo
credentials ever start round-tripping through `config.json`, that is the
rule being broken.

### 4.8 Qt never runs a network loop

The WebSocket receive loop, heartbeat and reconnect backoff all live on
worker threads in `network_client.py` (which imports no Qt). Windows
attach through `qt_bridge.py`, whose signals are delivered as queued
connections on the GUI thread.

### 4.9 Cues queue, never overwrite

A cue arriving while another is live goes into a bounded queue. If the
queue is full the event is **refused explicitly** and the teacher is
told - never dropped silently, never allowed to replace a live cue.
Queue wait is recorded as its own field, part of neither network delay
nor reaction time.

### 4.10 Nothing is reimplemented

| Concern | Module it comes from |
|---|---|
| Teacher key→finger detection | `app.camera`, `app.hand_tracking`, `app.midi`, `app.finger_matching` (incl. `match_notes_to_fingers` for chords) |
| Student cue channels | `app.gui.cue_window.ScreenCueOutput`, `app.haptic_cue.HapticCueOutput`, `profile_led_mapper` + `note_led_map` |
| Recording + LED sync mark | `app.music_recording` (`RawMidiRecorder`, `SyncInfo`) |
| Correctness / summary | `app.finger_matching.is_finger_correct`, `app.quiz.summarize` |
| Storage format | `app.quiz.QuizResult` / `save_quiz_results` / `QuizMeta` |
| Offline finger pass | `app.offline.analyze_recording` via `app.gui.analyze_worker.AnalyzeWorker` |
| Post-session review | `app.gui.quiz_analysis_window.QuizAnalysisWindow` |

A remote session therefore lands in `data/quiz/<name>/` in the **standard
layout**, and every existing analysis tool reads it unchanged.
`QuizMeta.guidance_type` is `remote-visual` / `remote-haptic` /
`remote-both`.

---

## 5. Data flow of one live cue

```
TEACHER                          RELAY                    STUDENT
  camera+MIDI tick
  match_note_to_finger
  perf_counter_ns  ─┐
  guidance.live ────┼──────────►  adds server stamps ───►  StudentSession.accept()
                    │             forwards to room         stamps arrival
                    │                                      ├─ guidance.received ──►
  RTT ends here ◄───┴──────────────────────────────────────┘   (BEFORE any cue work)
                                                           tick() → present_next()
                                                           CompositeCueOutput.show_target()
                                                             LED write → flush   ─┐
                                                             haptic write → flush ─┼─ max = cue_ready
                                                             screen paint (repaint)┘
                                  ◄─────────────────────── guidance.presented
                                                           MIDI note-on arrives
                                                           reaction = response - cue_ready
                                  ◄─────────────────────── performance.response (provisional)
  ... session ends ...
                                                           analyze_recording() over video
                                  ◄─────────────────────── performance.response (final)
                                  ◄─────────────────────── session.finished + summary
```

The two-stage result split (`provisional` → `final`) is deliberate: the
live finger verdict uses whatever hand landmarks were on screen at the
moment, the final one re-runs the same matcher over the recorded video
with the LED sync anchor.

---

## 6. UI structure

Both clients are a **three-stage flow** (`QStackedWidget`), one screen at
a time. This was a deliberate simplification - everything used to be on
one page:

```
Step 1 of 3 - Sign in     server URL, username, password. Nothing else.
Step 2 of 3 - Room        one field: the join code. Nothing else.
Step 3 of 3 - Session     devices, controls, camera, event table.
```

**The join code is the only room identifier a user ever sees.** The room
uuid is still stored - every REST call is keyed by it, and
`config.json` keeps the last one - but it is never shown and never
typed. Step 2 is a single line edit with no mode selector, the same for
both roles:

| Typed | Student | Teacher |
|---|---|---|
| a join code | joins | reopens that room |
| anything else | refused ("that is not a join code") | creates a room with that name |

`protocol.looks_like_join_code()` is what decides, and it is strict:
exactly `JOIN_CODE_LENGTH` characters, all from `JOIN_CODE_ALPHABET`
(which omits I, L, O, 0 and 1, so an ordinary six-letter word fails).
Both constants mirror `server/database.py`; a test asserts they agree.

One endpoint covers both roles because `POST /rooms/join` already
returns the room to its **own owner** without adding a membership row.

Two things this replaced, worth not reintroducing:

- a two-entry mode combo per role, whose "by id" options asked a person
  to type a uuid;
- a teacher default of *Create* over a field pre-filled with the last
  room **id** - one click made a room named after a uuid.

`RoomPanel.entered_code` exists because the relay returns `join_code`
only to a room's owner: a student's response carries none, so without
keeping what they typed there would be nothing to pre-fill next launch.

`StageWindow` (in `gui_common.py`) is the mixin; subclasses implement
`enter_session(room)` and `leave_session()`.

**Entering the Session page opens no hardware.** `enter_session()` opens
only the WebSocket. The camera, MediaPipe and the MIDI port saved in that
role's Settings are claimed by an explicit hardware action:
`_start_live_session()` on the teacher, `_start_session()` on the
student, or **Record a new song...** on the teacher's Recorded guidance
tab. The first two release on stop/leave/close; the integrated recording
wizard owns and releases its camera/MIDI/LED for its own window lifetime.

This used to happen in `enter_session()`, which meant a client held the
camera through the whole of setup and locked out every other tool. Three
things now depend on the later open, so do not move it back:

- The client Settings dialog is the **only** MIDI port selector. Neither
  Session page repeats a MIDI combo or Refresh button; startup reads
  `cfg.midi.port_name` from the saved per-role config.
- The teacher's `_open_detector()` is idempotent and returns a bool;
  `_connect_midi()` opens it lazily, so the manual **Connect MIDI**
  button still works before a session.
- A MIDI failure at start **warns and continues**: pre-recorded playback
  needs no teacher hardware, so it must not be blocked by a missing
  keyboard.

The student's LED strip stays on its own **Connect LED** button - the
sync flash wants checking before a session, not during one.

The teacher's **Connect MIDI** button and **Chord detection** checkbox
share the single Live guidance control row with the session actions. The
removed MIDI selector/Refresh row must not be reintroduced; changing the
port belongs in Settings.

### Teacher Live / Recorded tabs and recording wizard

The teacher Session stage contains a `QTabWidget` with exactly two
pages. **Live guidance** owns Connect MIDI, Chord detection, the live
session controls and camera view. **Recorded guidance** owns the song
picker, Refresh, Upload, playback mode, Trigger, its own pause/resume/stop
controls, and **Record a new song...**. The student's event table and
summary remain below the tabs because results belong to either mode.

**Record a new song...** constructs the existing
`app.gui.recording_wizard.RecordingWizard` with `self.cfg` - the
teacher-role `Config` view - so it uses the camera, MIDI port and keyboard
profile from Teacher Settings rather than the platform's unrelated
top-level defaults. It opens as a separate wizard window because its
three-page capture/review UI is intentionally large. While it is open,
the Teacher Client locks Settings, Change room and the Live hardware
controls to prevent two owners claiming the same devices; any detector
opened earlier by manual **Connect MIDI** is closed before the wizard or
a recorded session starts. Closing the wizard restores the controls;
completing it refreshes the picker and selects the new `music/<song>`
entry.

Recorded guidance is independent of Live guidance. Upload is available
as soon as a room and local song exist - do not make it depend on a live
`session_id`. **Trigger on student** creates its own REST session with
`mode: "recording"`, `recording_id` and `playback_mode`, sends
`session.start`, then `recording.start`. No teacher `guidance_mode` is
included. While either mode is active the other tab is disabled, and
the active tab's own transport buttons remain available. If
`recording.start` reaches the student before **Ready for guidance**, the
student stores that envelope in `_pending_recording_start` and consumes
it immediately after becoming ready; it must not silently discard the
one-shot trigger.

### Guidance modality belongs to the student

Only the student Session page has the **Guidance** combo
(`visual` / `haptic` / `both`). The teacher page has no corresponding
label or selector: the teacher decides what note/finger cue to send, not
how the student's devices render it. Consequently:

- the teacher creates a REST session with only `{"mode": "live"}`;
- the teacher's `session.start` frame carries session control data but no
  `guidance_mode`;
- a new relay session starts with the neutral database value
  `student_choice`;
- `recording.ready` reports the student's actual selection and enabled
  channels. If the student became ready before the teacher created the
  session, `_on_remote_session_start()` repeats this announcement after
  assigning `session_id`;
- the relay persists that session-bound ready event and replaces
  `student_choice` with the reported `visual`, `haptic` or `both`. It
  accepts the update only from a student in the same room.

The `recording.ready` name predates this ownership rule and is now the
general “student is ready for guidance” message, for live as well as
pre-recorded sessions. Do not put the combo back in the teacher UI or
quietly restore a teacher/default modality in either creation path.

### Pre-filled sign-in

`DEMO_ACCOUNTS` in `gui_common.py` maps role → (username, password) and
pre-fills the form: `demoteacher` and `demostudent` on the development
relay. Rule 4.7 still holds - it is a source constant, and
`RemoteGuidanceConfig.save()` has no key to write it to.

Two details that are load-bearing:

- `config.json` keeps **one** `network.username` shared by both roles,
  so whichever client ran last leaves its name there. A stored value
  belonging to the *other* role is ignored in favour of this role's demo
  user; anything else the user typed is kept.
- The password box is only pre-filled when the username still *is* the
  demo user, so a demo secret is never offered to a real account.
  `sign_out()` calls `restore_demo_password()`, because `_on_signed_in()`
  clears the box (there is a test for both).

Window minimum heights: teacher 623 px, student 541 px (both must stay
under ~700 so they fit a 768px screen - there is a test).

### Settings: one button per program, each owning its own

Each of the three programs has exactly one **Settings** button and can
see only its own settings:

| Program | Button opens | Covers |
|---|---|---|
| Student Client | `RemoteSettingsDialog("student", ...)` | its camera, MIDI port, calibration profile |
| Teacher Client | `RemoteSettingsDialog("teacher", ...)` | its camera, MIDI port, calibration profile |
| Relay Server | `server.gui.ServerSettingsDialog` | bind host, port, registration, token TTLs |

Both client settings dialogs include **Capture camera + profile
preview**. It uses the camera values and calibration profile currently
shown in the form, including unsaved edits; opens that camera only long
enough to take a snapshot; overlays the existing
`app.keyboard.visualize` colored pixel mask and key-id labels; releases
the camera; and shows the result in a separate dialog so Settings stays
small. Neither the settings nor the photograph are saved. A camera frame
whose actual dimensions differ from the profile key map is refused with
both resolutions in the error - never resize the mask, because that can
make a wrong calibration look plausibly aligned.

The relay's main control panel opens at **440 × 360 px**, which is also
its minimum size, so it can sit beside the two clients. Its complete
dashboard (server details, controls, rooms/clients and log) is inside one
resizable `QScrollArea`; the rooms tree and log also retain their own
scroll bars. Do not recover space by removing fields: shrinking the
window is meant to reveal the outer scroll bar instead.

The clients' button lives in the window header (`build_settings_button()`
in `gui_common.StageWindow`), so it is reachable from all three stages,
and goes insensitive for the length of a session - the devices are
already open by then. Saving calls `apply_settings()`, which rebuilds
that role's `Config` view; nothing has to be reopened.

**No serial port appears in any of them.** The LED strip and the nail
actuators are auto-detected and connected by the student client when a
session starts (`_ensure_led`), and a strip that is not found is a
*warning* - the finger cue and the scoring still work without the key
backlight. `student.led.port` / `student.haptic.port` survive in
config.json for a machine where auto-detection picks the wrong board;
see the caveat in §7.

This replaced a single **Remote Guidance Settings** window in the
launcher carrying Network / Student / Teacher tabs at once. Three
problems with that, all of which the split fixes: whoever opened it was
looking mostly at settings that were not theirs; configuring a client
meant leaving the client; and the launcher held settings for a program
that runs as its own process on (in the real deployment) another
machine.

### Launcher section 8

Four `ProcessEntry` buttons and **nothing else**: Student Client,
Teacher Client, Relay Server, Network Latency Benchmark. Two things
tests assert stay gone - a "Launch Local Stack" button (removed on
request) and any settings entry.

---

## 7. Configuration

`config.json` → `remote_guidance`:

```
schema_version
network      server_url, username, room_id, join_code, verify_tls,
             heartbeat_interval_s, reconnect_*, guidance_queue_size
student      camera{...}, midi{port_name}, keyboard_profile,
             led{port}, haptic{port}, default_guidance_mode,
             record_video, default_timeout_s
teacher      camera{...}, midi{port_name}, keyboard_profile, chord_detection
local_server enabled, host, port, use_gui
```

**The two student serial ports are config-only, and blank means
auto-detect.** No UI asks for them any more: the strip and the actuators
connect themselves when a session starts.

The caveat that made them explicit in the first place has not gone away.
The LED strip and the vibration rig are separate boards, and
`common.serial_utils.auto_detect_port` scores them almost identically -
with both plugged in it can pick the wrong one, and "wrong" means motor
commands going to the LED controller. **If a rig ever cues the wrong
device, pin `student.led.port` and `student.haptic.port` by hand in
config.json.** Identical ports are still rejected
(`serial_port_problems()`), and the student client still refuses to
start a haptic session while they clash.

Guidance modes: `visual` / `haptic` / `both` name only how the **finger**
is conveyed. The key LED is present in all three.

**The relay port is 18765, written down in three places.**
`NetworkConfig.server_url`, `LocalServerConfig.port` and
`server.config.ServerConfig.port` are separate defaults in two packages
that must not import each other (rule 4.5), so `RelayPortDefaultTests`
is the only thing keeping them in step. A mismatch is silent: the relay
starts and every client fails to reach a port with nothing on it. Change
all three, plus `server/server_config.example.json`, together.

`network.username` is **one field shared by both roles** - whichever
client last signed in leaves its name there. That is why `SignInPanel`
ignores a stored username belonging to the other role (see §6).

---

## 8. Running it

```bash
python -m server --init
```

```bash
python -m server --gui
```

```bash
python teacher_remote_guidance.py
```

```bash
python student_remote_guidance.py
```

Both sign-in forms arrive filled in with `http://127.0.0.1:18765` and
this role's demo account, so on one machine it is: Sign in → room → Sign
in → join. The demo accounts have to **exist on the relay** - register
them once (either client's *Create account* button does it, or see
`server/README.md` § "The demo accounts").

Teacher creates a room → reads the join code → student joins → teacher
**Start live session** → student **Ready for guidance**. Those last two
presses open the live-session devices. For the recorded path, the
teacher's explicit **Record a new song...** action opens the integrated
wizard and claims its devices until the wizard closes.

Benchmark (needs a student connected and answering):

```bash
python remote_latency_benchmark.py --server http://127.0.0.1:18765 --username teacher1 --room-id <room id>
```

Tests:

```bash
python -m pytest test-script/test_remote_guidance_config.py test-script/test_remote_guidance_server.py test-script/test_remote_guidance_e2e.py -q
```

Whole platform (`test_led_array.py` prompts on stdin when a serial board
is attached, so it is excluded):

```bash
python -m pytest test-script/ -q --ignore=test-script/test_led_array.py
```

---

## 9. Traps discovered the hard way

Each of these cost real debugging time. They all have tests now.

1. **`RuntimeError: Internal C++ object (ApiCallWorker) already deleted`**
   A finished `QThread` handed to `deleteLater()` leaves a Python wrapper
   whose C++ object is gone; calling `isRunning()` on it raises. This
   made the *second* click of every button fail. Fix: drop the reference
   in a `finished` handler **and** treat a dead wrapper as "not running".
   Pattern lives in `_ApiPanel.busy()` - reuse it, don't re-invent it.

2. **`starlette` `TestClient` teardown order.** `unittest` runs
   `tearDown` *before* `addCleanup` callbacks, so closing the client
   while a WebSocket session is still open deadlocks. Register the client
   cleanup with `addCleanup` first (it then runs last). Also: never call
   `__enter__` twice on a `WebSocketTestSession`.

3. **`test_haptic_config.py` can write to the real `config.json`.** When
   a test fails mid-run its temp-config redirect can be bypassed, leaving
   `haptic.erm.default_amp` set to the test's value. **After any failing
   test run, check `git diff config.json` before doing anything else.**

4. **`requirements.txt` tracks the `fingercam` conda env**
   (Python 3.11, `/opt/anaconda3/envs/fingercam/bin/python`), *not* the
   base Anaconda interpreter. Pin versions from that env or you will
   silently downgrade working packages.

5. **`websocket-client` is the one new dependency.** Chosen because it is
   *synchronous* - the Qt clients run it on a worker thread rather than
   hosting a second asyncio event loop beside Qt's.

6. **`uvicorn` uses `wsproto`**, not the `websockets` package - that is
   what this environment ships. See `build_uvicorn_kwargs`.

7. **`ScreenCueOutput.show_target()` only schedules a repaint.** The
   remote module added an optional `flush()` that calls `repaint()`, so
   the visual timestamp is taken *after* the frame is drawn. Without it
   the "visual painted" time is a lie.

---

## 10. Known gaps and what is not verified

### Never tested on real hardware

Nothing below has run against a physical device. The code paths execute
and the objects are constructed, but no LED has lit and no motor has
moved under this module:

- LED key cue and the LED sync flash
- Haptic cue on an explicit serial port
- Teacher live finger detection with a real camera + MIDI keyboard
- Student video recording and the offline finger pass
- The two-machine benchmark (**all latency figures so far are loopback
  on one machine** - ~1.3 ms RTT, which says nothing about a campus
  network)

### Design gaps

- **One student per room.** The `room_members` schema is keyed
  `(room_id, user_id)` with a per-row role, so more students is a routing
  change rather than a migration - but the relay currently broadcasts to
  every peer and the teacher UI assumes one.
- **No video between clients.** Deliberate for now ("teacher sees student
  performance" means events and metrics). If live video is ever wanted it
  must not go through this WebSocket.
- **Physical onset measurement is a stub.** `physical_led_onset_ns` /
  `physical_haptic_onset_ns` columns exist and are always empty.
- **Reconnect replay is untested under real loss.** Duplicate suppression
  works (there is a test), but no test drops a connection mid-session.
- **No token refresh in the clients.** They log in and use the access
  token; a 30-day expiry mid-session is not handled.
- **`local_server.enabled` is read but nothing acts on it** since Launch
  Local Stack was removed.
- **Teacher pause/resume for live mode is only a message.** The student
  honours it; there is no visible teacher-side state machine.
- **Error surfaces are status lines.** A dropped relay mid-lesson shows a
  message but there is no recovery UI.

### Things a next session might reasonably do

- Try it on two machines and record real RTT/loss.
- Wire the photodiode/accelerometer rig into the reserved columns.
- Multi-student rooms (routing + teacher UI).
- Token refresh + a clearer reconnect UX.
- Trim `student/window.py` (1157 lines) - the session-page building and
  the recording/analysis plumbing could split out.

---

## 11. Quick orientation for an AI assistant

If you have room to read only a few files, read them in this order:

1. `remote_guidance/student/session.py` - the timing rules, no Qt noise
2. `remote_guidance/protocol.py` - the message vocabulary
3. `server/websocket.py` - what the relay does and refuses to do
4. `remote_guidance/timing.py` - why RTT and one-way are kept apart
5. `server/README.md` - the operator/protocol reference

Then §4 of this file, which is the list of things that look like details
and are actually load-bearing.
