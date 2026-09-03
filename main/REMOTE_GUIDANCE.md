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
| Platform README, Tele-training section | [`README.md`](README.md) § "Tele-training (launcher section 8)" | Orientation only: what the module is, the six launcher buttons, and where to read on. The user-facing detail it used to hold - setup wizard, guidance modes, what is reused, timing, the benchmark - is now § 12 of this file |
| Platform README, everything else | [`README.md`](README.md) | The non-remote platform: calibration profiles, quiz, sequence generator, analysis, haptic config |
| Final report | [`../shengjiang_final_report.pdf`](../shengjiang_final_report.pdf) | § "Tele-training Deployment" and Appendix § "Tele-training Measurement Protocol" - the requirements this module implements |
| Sequence generator | [`SEQUENCE_GENERATOR_ALGORITHM.md`](SEQUENCE_GENERATOR_ALGORITHM.md) | Only relevant because generated sequences can be uploaded as recordings |

`server/README.md` is the deeper reference for anything protocol- or
deployment-shaped. This file is the architectural overview and the
"things you would otherwise learn the hard way" list.

---

## 1. What it does

Two people, two machines, one relay in between.

- **Synchronous live cueing** - the teacher plays a key on their own
  keyboard; the platform's existing camera pipeline works out *which
  finger* they used; that key/finger pair - or the whole set of them,
  when the teacher plays a chord - is sent to the student, whose
  keyboard LED lights the key and whose screen and/or nail-mounted
  actuator cues the finger. Both machines keep a recording of the lesson,
  under one name - the student's in `data/quiz/`, the teacher's in
  `data/music/` (§4.13).
- **Asynchronous recorded sequences** - the teacher uploads a fingering
  plan recorded earlier (`data/music/` or `data/sequence/`) and triggers
  it remotely. The student downloads the whole thing first and schedules
  every cue locally, so no individual note waits on the network.

The teacher's **Recording library** workspace opens Tele-training's own
`TeacherRecordingWizard`, so a new `data/music/<song>/` performance can
be recorded without leaving the Teacher Client. It is a private copy of
section 3's three-stage algorithm and output format; it does **not**
import or subclass `app.gui.recording_wizard.RecordingWizard`.

"Upload" here does **not** copy or move that source folder. The teacher
reads it and sends only the note/finger/timing event list; the relay
stores each upload as a new recording row with a new uuid, and the
student downloads that JSON into memory. Even when both clients run from
the same checkout, neither client writes back to `data/music/` or
`data/sequence/`, so the source is not overwritten. The student's
performance is a separate artefact under `data/quiz/<session name>/`.
That name is made by the teacher, once per lesson, at **Start teaching**,
so two lessons cannot share a folder; the student has no session-name
field and invents nothing. `_start_recording()` also refuses outright to
open a video writer over a file that already exists - see trap 11.

Both modes come from the report's "Tele-training Guidance Modes".

---

## 2. The three processes

| Process | Entry point | Owns |
|---|---|---|
| Student Client | `student_remote_guidance.py` | camera 1, MIDI keyboard 1, LED strip, nail actuators, one shared-mode audio output stream while ready |
| Teacher Client | `teacher_remote_guidance.py` | camera 2, MIDI keyboard 2, one shared-mode audio output stream while MIDI is connected |
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
| `config.py` | 328 | The `remote_guidance` block of `config.json`; per-role `Config` views; serial-port validation |
| `protocol.py` | 210 | Versioned message envelope, message-type constants, `GuidanceAction`. **Client half of a matched pair with `server/schemas.py`** |
| `timing.py` | 393 | Wall vs monotonic clocks, `DispatchTimings`, `ClockOffsetEstimator`, `LatencyStats`, `one_way_estimate` |
| `network_client.py` | 483 | `RemoteApiClient` (stdlib urllib REST) + `RemoteWebSocketClient` (threaded, `websocket-client`), including quiet expected socket-close handling. **No Qt** |
| `qt_bridge.py` | 104 | The only place the network layer meets Qt - turns callbacks into signals |
| `cue_outputs.py` | 349 | `CompositeCueOutput`, `LedKeyCue`, `build_student_cue`; paints visual first so serial cannot hold it hostage, while cue-ready remains the last enabled channel (§4.1, §4.11) |
| `gui_common.py` | 571 | `SignInPanel`, `RoomPanel`, `ApiCallWorker`, `StageWindow` mixin |
| `client_styles.py` | 355 | Tele-training-only launcher-style QSS; warm Teacher and cool Student palettes shared by each role's main window and Settings, including explicit role-colored dropdown/spin arrows and checked-state ticks from `assets/` |
| `settings_window.py` | 559 | `RemoteSettingsDialog` - role-themed two-column Camera / Keyboard+MIDI workspace; non-editable detected-port dropdown with saved-offline preservation, temporary press-a-key MIDI identification/release, profile preview, and duplicate-name warning |
| `launcher_actions.py` | 116 | `ProcessSpec`s and health check for launcher section 8 |
| `setup_store.py` | 180 | **The isolation rules.** Profile folders the setup wizard may create and write; no Qt, no config |
| `setup_wizard.py` | 599 | `RemoteSetupWizard` - click-to-scan camera selection → calibration launcher → MIDI-mapping launcher. It constructs no camera when opened (§4.12) |
| `calibration_wizard.py` | 456 | Tele-training's copy of Initial Setup's five-page name → capture → boundary → cropped edges → key-fill flow; only its safe save destination differs (§4.12) |
| `midi_mapping_wizard.py` | 473 | Tele-training's copy of Initial Setup's two-page Middle C check → live camera/key-highlight mapping flow; it lists and writes only marked Tele-training profiles (§4.12) |
| `vision_worker.py` | 194 | Latest-only background camera + MediaPipe loop shared by the two clients; keeps native vision calls off both GUI/network fast paths, and optionally carries an unannotated copy of each frame for recording (§4.13) |
| `student/session.py` | 551 | **The core.** Event model, queueing, all timing rules. No Qt, no hardware |
| `student/window.py` | 1485 | Student GUI: compact cyan role bar plus separate Practice/Results workspaces; devices in, Qt signals out; owns the visual/haptic/both choice and Quiz-style local MIDI audio/timbre, announces readiness, and defers an early recorded trigger until ready |
| `teacher/live_detector.py` | 314 | Background camera/HandTracker snapshot + one latency-first non-blocking MIDI reader → finger matching plus note-on/off events for local audio; `set_raw_capture()` adds the unannotated frame a recording needs |
| `teacher/live_recorder.py` | 359 | Writes a live lesson to `data/music/remote-<epoch>/` in the Song Recording Wizard's layout, from the detector's own camera/MIDI output. No Qt (§4.13) |
| `teacher/recording_import.py` | 160 | `data/music`/`data/sequence` → uploadable recording |
| `teacher/recording_wizard.py` | 635 | Tele-training-private three-page Teacher recording flow; fixed Teacher Settings devices, amber UI, section 3-compatible capture/fingering/save pipeline, and complete close-time release |
| `teacher/window.py` | 1348 | Teacher GUI; compact amber role bar plus separate Live/Library/Results workspaces, Quiz-style local MIDI audio/timbre, private Teacher Recording Wizard, and session control without choosing the student's rendering mode |
| `tools/latency_benchmark.py` | 1489 | Self-contained two-login/two-WebSocket benchmark CLI with built-in Student responder; creates and closes its own room in **both** modes, reads the membership back, waits on `presence` for a real Student Client in external mode, and paces probes fixed/uniform/Poisson/human from a recorded seed, and analyses a finished run |
| `tools/latency_plots.py` | 353 | The run's report figures (PNG + vector PDF): overview with elevated-state episodes and an ECDF, hop decomposition, IPDV, and transport against the participants' measured reaction time. Describes, never grades |
| `tools/benchmark_window.py` | 802 | GUI wrapper around that CLI (runs it as a QProcess); defaults to Relay + Benchmark only, feeds both passwords over stdin, reports the room each run created instead of asking for one, shows the interval range its pacing choice produces, and carries a second page that reads back every past run's summary, analysis and plot |

### `server/` - the relay

See `server/README.md`. Structure: `config.py`, `database.py` (SQLite +
migrations), `auth.py` (Argon2id + RS256 JWT), `schemas.py`, `api.py`
(REST), `websocket.py` (relay), `app.py` (factory + background writer),
`gui.py` (Qt control panel), `__main__.py` (CLI), `tools/` (key and
certificate generation).

### Tests

| File | Lines | Covers |
|---|---:|---|
| `test-script/test_remote_guidance_config.py` | 4877 | Config compat, role isolation, serial clash, composite cue, session timing, latency stats and self-contained two-role benchmark controls/responder plus automatic benchmark-room lifecycle, membership read-back, student-presence wait fixed/uniform/Poisson/human probe pacing, the human fit tracking whichever participants exist, its per-participant sampling, progress reporting and cancellation, and the results page's listing/analysis/empty-and-broken handling, launcher actions, GUI staging plus main-window/Settings role themes, visible spin/tick controls and workspace separation, MIDI dropdown/empty-scan preservation, private Teacher recording-wizard isolation/device source/release, camera/profile preview, temporary MIDI test/release lifecycle, shared MIDI/audio routing, background vision/latency ordering, audio release, message handling, and the teacher's lesson recording: wizard-format save, epoch stamps, lead-in-corrected duration, unannotated frames, cue-before-archive tick ordering and the shared session name |
| `test-script/test_remote_guidance_server.py` | 878 | Passwords, tokens, authorization, WebSocket relay, persistence, and student-owned guidance mode |
| `test-script/test_remote_guidance_e2e.py` | 575 | Real server + fake teacher + fake student |
| `test-script/test_midi_ports.py` | 318 | `app.midi` port identity: two identical keyboards stay two keyboards (§9.8). Platform-wide, but this module is what needs it |
| `test-script/test_profile_preview.py` | 300 | `app.gui.profile_preview`: the mask is never resized to fit, and the shared button behaves in the quiz windows too. Also platform-wide |
| `test-script/test_chord_cue.py` | 454 | Cueing a chord on every channel (§4.11), that scoring stays single-note, and that the single-finger path the local quiz and the main study use is untouched |
| `test-script/test_remote_setup_wizard.py` | 603 | On-demand camera selection, five-page cropped calibration, both MIDI-mapping images/overlays, and what the setup flows cannot do (§4.12): write `config.json` or touch a profile they did not create |

**261 tests** in the three remote files, **23** in the MIDI port file,
**16** in the profile preview file, **32** in the chord cue file and **32**
in the setup wizard file, none of which need hardware or a real network.

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
| MIDI port identity and opening | `app.midi.list_input_ports` / `resolve_input_port` / `MidiInputReader` - never `mido.get_input_names()` or `mido.open_input()`, which cannot address two identical keyboards (§9.8) |
| Local key sound and timbres | `note_audio.NoteAudioPlayer`, `TIMBRES` and `DEFAULT_TIMBRE` - the same player and choices as Student Quiz; Tele-training only routes its existing MIDI events into it |
| Student cue channels | `app.gui.cue_window.ScreenCueOutput`, `app.haptic_cue.HapticCueOutput`, `profile_led_mapper` + `note_led_map` |
| Recording + LED sync mark | `app.music_recording` (`RawMidiRecorder`, `SyncInfo`) |
| Correctness / summary | `app.finger_matching.is_finger_correct`, `app.quiz.summarize` |
| Storage format | `app.quiz.QuizResult` / `save_quiz_results` / `QuizMeta` |
| Offline finger pass | `app.offline.analyze_recording` via `app.gui.analyze_worker.AnalyzeWorker` |
| Post-session review | `app.gui.quiz_analysis_window.QuizAnalysisWindow` |
| Camera-vs-profile preview | `app.gui.profile_preview` (`load_profile_template`, `overlay_template`, `KeyboardProfilePreviewDialog`) - shared with both quizzes and the main study's trial runner. Only the *snapshot* is local, because these windows hold no camera until a session starts |

A remote session therefore lands in `data/quiz/<name>/` in the **standard
layout**, and every existing analysis tool reads it unchanged.
`QuizMeta.guidance_type` is `remote-visual` / `remote-haptic` /
`remote-both`.

---

### 4.11 A chord widens the cue, never the scoring

The teacher's chord detection sends several `GuidanceAction`s in one
envelope, and the student cues **all** of them: every key lit, every
motor buzzing, every dot on. It is still **one cue event with one
`cue_ready`**, so there is exactly one origin to measure a reaction
against (§4.1).

Scoring is deliberately untouched. `RemoteEvent.target` is still
`actions[0]`, `on_note()` still finishes the event on the first press,
and `note_correct` / `finger_correct` still judge that press against the
primary note. Pressing a different note *of the same chord* scores as
wrong, and there is a test saying so.

That asymmetry is a decision, not an oversight. "Correct" has exactly one
definition and it lives in `app.finger_matching` / `app.quiz` (§4.4),
shared with the local quiz and every analysis tool downstream of it -
including a completed study's data. Widening the cue costs nothing there;
widening the verdict would mean a second definition of correct, a change
to `QuizResult`/`summarize`, and a change to what the report's outcome
measures mean. If chord scoring is ever wanted, that is the work, and it
starts in `app.quiz` rather than here.

### 4.12 Tele-training setup writes a profile and nothing else

Onboarding a new partner needs a calibration: a pixel mask of their
camera's view of their keyboard, plus that keyboard's key→note mapping.
The launcher's **Initial Setup** wizards produce exactly that - and also
write `config.json`'s top-level `camera`, `midi.port_name` and
`active_keyboard_profile`, and save over an existing
`data/keyboard-profile/<name>/`. Those side effects configure the machine
the *formal experiment* runs on, so using them here repoints its devices
and can destroy the calibration its recorded sessions were scored
against.

`setup_wizard.py`, `calibration_wizard.py` and
`midi_mapping_wizard.py` produce the profile and **nothing else**.
`setup_store.py` is the whole of the "may I write that?" decision, kept
GUI-free so the rules are testable without hardware. Two of them:

1. **`config.json` is never written - at all.** Not the top-level keys,
   and not even the `remote_guidance` block. Running the wizard therefore
   cannot change what any tool on this machine does; the new profile takes
   effect only when someone selects it in a client's own Settings dialog,
   which is where choosing devices already lives. A test greps all four
   setup modules for `cfg.save()`, `active_keyboard_profile =`,
   `RemoteGuidanceConfig` and `atomic_write_json` - the cheapest way to
   break this is one careless line.
2. **The wizard may only write into profiles it created.** Every profile
   it makes carries a `remote_setup.json` marker; writing into an
   existing directory is refused unless the marker is there, and creating
   one is refused if the directory exists at all. There is deliberately
   **no overwrite path**, not even for its own earlier profiles - a
   remote session may have been recorded against one.

Profiles live in the shared `data/keyboard-profile/`, on purpose. A
session records its profile by *name* in `QuizMeta`, and the offline
finger pass, the quiz analysis window, the video sync window and
`profile_led_mapper` all resolve that name against that one directory. A
separate remote directory would isolate the files and break every one of
those readers; the marker gives the same protection without moving
anything.

Three steps - **camera → calibration → MIDI mapping** - and the order is
load-bearing. The calibration is a pixel mask of *that* camera's frame,
so step 2 captures through step 1's settings instead of asking again, and
"the camera has to be the right one" is enforced by the flow rather than
by a warning nobody reads. Saving the calibration is what creates the
profile folder, so an abandoned attempt leaves nothing behind. Step 3 has
its own picker over the wizard's own profiles, which is how "redo only
the MIDI mapping" works.

**Opening the Tele-training wizard must not touch a camera.** Step 1
starts empty: only clicking **Scan for cameras** starts a background
probe, and even a completed scan opens nothing. The user must explicitly
select one of the detected indices before its live preview is
constructed; step 2 stays blocked until that selection. This differs
from the section 1 camera picker, which scans on construction, and is
intentional for Tele-training.

The calibration itself is deliberately a near-copy of
`app/gui/calibration_wizard.py`, kept at
`remote_guidance/calibration_wizard.py` so section 1 is not modified. Its
five pages and coordinate handling are the same: **profile name → live
capture → two-corner boundary → edge tuning on the boundary crop →
white/black key filling on that same crop**. On Finish, the crop-local
key map is pasted back into a full-frame-sized map before saving, exactly
as the platform readers require. Only the write actions differ: edge
slider values are not written to config, the active profile is not
changed, and the template is saved through
`setup_store.create_profile()` as a new, marked Tele-training profile.

MIDI mapping follows the same rule. The section 1 implementation in
`app/gui/midi_mapping_wizard.py` is copied into
`remote_guidance/midi_mapping_wizard.py`; do not collapse step 3 back to
a port picker and text counter. It has two image-bearing pages that are
part of the workflow:

1. **Connect / verify** shows `app/assets/image/MiddleC-Keyboard.png` and
   a live “last key pressed” readout so middle C can be verified as note
   60/C4 before mapping.
2. **Map keys** displays the selected camera continuously with the
   calibration key map overlaid. The next physical key is yellow,
   already mapped keys are green, and unreached keys are grey; labels,
   Undo, Skip and Reset match Initial Setup.

Only profiles carrying `remote_setup.json` appear in that page's picker,
and Save goes through `writable_profile_dir()` to write only that
profile's `midi_mapping.json`. It does not save the selected MIDI port to
config. The live frame and template must have the same resolution; on a
mismatch the raw camera image remains visible but the mask is never
resized into a false-looking alignment.

**There is no role anywhere in it, and a profile carries none.** A
calibration describes a rig - one camera, one keyboard - not a person.
Student and teacher normally need one each only because they normally sit
at two different rigs; sharing a rig means sharing the profile. An
earlier version asked for a role and stamped it into the marker: it
enforced nothing, since nothing ever read it back, while implying a
constraint that does not exist - and it carried the profile across a role
switch, which handed the teacher the student's calibration.

### 4.13 One live lesson, recorded on both machines under one name

The student has always kept the durable record of a session: cues,
responses, video and MIDI land in `data/quiz/<name>/` exactly like a local
quiz. The teacher kept nothing - the live table was in memory, and the
relay stores payloads and timestamps rather than a performance. But the
teacher's camera and MIDI keyboard are already open and already producing
exactly what the Song Recording Wizard saves, so a live session now writes
it (`teacher/live_recorder.py`):

```
data/music/remote-<epoch>/        teacher: the lesson as it was played
    raw/performance.mp4  raw/midi_raw.json  raw/notes.json
    raw/sync.json  raw/keyboard_profile/  score.mid  fingering.json  meta.json

data/quiz/remote-<epoch>/         student: the same lesson as it was received
    raw/performance.mp4  raw/midi_raw.json  raw/notes.json
    raw/sync.json  results.json  meta.json
```

Same layout as the wizard's, deliberately: the song picker, the uploader,
`load_playback_events` and the playback tools all read it unchanged, so a
lesson that was just taught live can be uploaded and replayed as a
pre-recorded one. `meta.json` is written last, after the offline finger
pass, because `list_songs()` treats its presence as "this song finished
saving" - a lesson whose pass fails keeps its raw capture and is simply
not listed.

**The name is generated once, by the teacher, at Start teaching**, and
travels in `session.start` together with the instant both ends start
recording. The student uses it verbatim; it has no session-name field and
invents nothing, so the two halves of a lesson are pairable without
consulting the relay and neither folder is ever renamed.

This replaced a design in which the student generated its own name at
**Ready for guidance** and the folder was renamed at the end. The name it
used was the last name the teacher had sent, and that field outlived the
session it belonged to - so a second lesson opened its video writer on the
first lesson's `performance.mp4`, truncated it, and then renamed the whole
folder. Running *n* lessons left one folder, named after the last, and the
`sync_align.json` of the previous lesson sitting in it. Three things stop
it now: the name is made per lesson and cleared by `_stop()`; recording
only begins on `session.start`, by which time the name is known;
`_start_recording()` refuses outright to open a writer over a file that
already exists (trap 11).

Two things the teacher's copy is not:

- **It is not a second measurement.** The one open MIDI port belongs to
  the live detector and is drained by the guidance timer, so a note's
  `abs_time` here is the moment that timer saw it - within one 33 ms tick
  of the key going down, and the same instant the cue was sent. Opening a
  second reader, or moving the port onto a stamping thread, would change
  the latency path §6 measures. Reaction times still come only from the
  student's clock (§4.2).
- **It is not the preview.** The live view has the whole calibrated key map
  painted over it, hands included; recording that would leave the offline
  finger pass with a video MediaPipe cannot track in. `keep_raw` on
  `LatestVisionWorker` copies each frame *before* `annotate` runs, on the
  vision thread, and only while a recording wants it. The video write
  itself happens at the end of the tick, after the cue has gone out.

---

## 5. Data flow of one live cue

```
TEACHER                          RELAY                    STUDENT
  background camera+MediaPipe ──► latest hand snapshot
  MIDI tick (never waits for vision)
  match_note_to_finger(latest completed hands)
  perf_counter_ns  ─┐
  guidance.live ────┼──────────►  adds server stamps ───►  StudentSession.accept()
                    │             forwards to room         stamps arrival
                    │                                      ├─ guidance.received ──►
  RTT ends here ◄───┴──────────────────────────────────────┘   (BEFORE any cue work)
                                                           tick() → present_next() (before vision snapshot/UI)
                                                           CompositeCueOutput.show_targets()
                                                             screen paint (repaint)─┐ shown first
                                                             LED write → flush      ├─ max = cue_ready
                                                             haptic write → flush  ─┘
                                  ◄─────────────────────── guidance.presented
                                                           MIDI note-on arrives
                                                           reaction = response - cue_ready
                                  ◄─────────────────────── performance.response (provisional)
  (recorder writes the unannotated frame + this tick's MIDI - after the send)
  ... session ends ...
  analyze_recording() over the                             analyze_recording() over video
  teacher's own video                                      ◄── performance.response (final)
  → data/music/remote-<epoch>/                             ◄── session.finished + summary
```

The two-stage result split (`provisional` → `final`) is deliberate: the
live finger verdict uses whatever hand landmarks were on screen at the
moment, the final one re-runs the same matcher over the recorded video
with the LED sync anchor.

Both ends run that same offline pass at the end, over their own video and
for different questions: the student's decides whether the *response* was
correct, the teacher's writes the fingering of the *lesson* (§4.13). Only
the student's is ever sent anywhere.

---

## 6. UI structure

Both clients are a **three-stage flow** (`QStackedWidget`), one screen at
a time. This was a deliberate simplification - everything used to be on
one page:

```
Step 1 of 3 - Sign in     server URL, username, password. Nothing else.
Step 2 of 3 - Room        one field: the join code. Nothing else.
Step 3 of 3 - Session     role-specific workspaces for setup/live view/results.
```

The chrome above that stack is deliberately short. Role, stage, relay
link and the one Settings button share a single command bar, followed by
one 30 px status strip. Long messages are kept in the label tooltip and
must not expand the minimum window width. The styles live only in
`client_styles.py`: Teacher uses amber/brown, Student uses cyan/navy, so
two clients running beside one another are recognisable immediately.
That sheet also owns the only custom checkbox indicator in section 8:
the checked state paints a role-specific SVG tick over the accent fill,
never a colour-only square. Setup, calibration, benchmark and relay
checkboxes keep Qt's native checked indicator. A regression test scans
both `remote_guidance/` and `server/` so a later custom indicator cannot
silently remove the tick again.

The Session page does not stack every large widget vertically:

- Teacher: **Live studio**, **Recording library**, **Student results**.
- Student: **Practice studio**, **Session results**.

Live/practice uses a horizontal, draggable control/preview split. The
preview is bounded at 560 × 360: it shows the whole scaled frame, but is
not intended to dominate the program. Result tables live on their own
pages and a finished session selects that page automatically.

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
student, or **Record a new song...** in the teacher's Recording library
workspace. Each opens *all* of that role's hardware - the teacher's MIDI
port included, and the student's LED strip included. The first two
release on stop/leave/close; the integrated recording wizard owns and
releases its camera/MIDI/LED/audio for its own window lifetime.

This used to happen in `enter_session()`, which meant a client held the
camera through the whole of setup and locked out every other tool. Three
things now depend on the later open, so do not move it back:

- The client Settings dialog is the **only** MIDI port selector. Neither
  Session page repeats a MIDI combo or Refresh button; startup reads
  `cfg.midi.port_name` from the saved per-role config.
- The teacher's `_open_detector()` is idempotent and returns a bool;
  `_connect_midi()` opens it lazily, so arming a session opens the camera
  and the keyboard in one step.
- A MIDI failure at start **warns and continues**: pre-recorded playback
  needs no teacher hardware, so it must not be blocked by a missing
  keyboard.

The student's LED strip is connected by **Ready for guidance** along with
everything else; there is no separate **Connect LED** button, and no
separate **Connect MIDI** on the teacher. Both did what arming a session
already does, and each was one more piece of state to get wrong.

**Chord detection** owns the first row of the Live controls card;
Instrument and the session actions remain in that same compact card. The removed MIDI
selector/Refresh row must not be reintroduced; changing the port belongs
in Settings.

### Local key audio and same-machine ownership

Both clients use the exact `note_audio.py` path used by Student Quiz.
Their Session pages list `TIMBRES` (`Piano`, `Electric Piano`, `Sine`,
`Mute`), and changing the choice affects new notes immediately. Teacher
sound follows the teacher's physical key presses; student sound follows
the student's physical response presses. Incoming guidance does not
play a second tone on the student.

This must not create a second owner for scarce input hardware. The
teacher detector's one `MidiInputReader` returns both the note-ons used
for finger guidance and the note-on/off events used for sound. The
student similarly routes the events already returned by its one
`RawMidiRecorder` to both scoring and sound. Do not add another MIDI
listener for audio.

When both clients run on one computer, each has at most one ordinary
`sounddevice.OutputStream`; the OS mixes those output streams rather
than either client requesting exclusive access. `Mute` creates no audio
stream at all. Teacher audio exists only while its MIDI connection is
open; student audio exists only while it is ready for a session. Stop,
Change room and window close call `stop_all()` and `close()` immediately.
If the host audio backend nevertheless refuses a stream, that client
warns and continues with guidance/recording instead of failing the
lesson or opening another device.

### Live latency fast path

The relay has no fixed cadence or database wait: it forwards an incoming
WebSocket frame immediately, then queues persistence in the background.
The observed same-machine delay can therefore be dominated by client
work when both camera/MediaPipe pipelines and serial outputs share an
already busy computer.

Keep these ordering/ownership rules together:

- `LatestVisionWorker` runs camera capture, MediaPipe and overlay drawing
  outside each client's Qt GUI thread. It stores only the latest completed
  frame/hands; old preview frames never queue and can never delay a cue.
  Its loop is capped at that role's configured camera FPS, so moving work
  off the GUI does not turn it into an unbounded CPU loop.
- Teacher `poll()` takes the latest completed hand snapshot, drains MIDI
  first and returns a waiting note immediately. It never waits for a new
  vision result just to send guidance.
- Student `_tick()` processes MIDI, `StudentSession.tick()` and the local
  recording scheduler before it consumes the latest vision snapshot.
- `CompositeCueOutput` repaints the visual channel before LED/haptic
  serial writes, so an unhealthy serial device cannot delay the visible
  instruction. `cue_ready` is still the maximum completion timestamp of
  all enabled channels, preserving the reaction-time rule in §4.1.

The regression tests inject blocked/slow vision and hardware channels:
a session tick must still run immediately, the visual call must be first,
and teacher MIDI must not call camera read. Do not move native vision back
onto either window timer or restore serial-first visual ordering.

### Teacher workspaces and recording wizard

The teacher Session stage contains a `QTabWidget` with exactly three
pages. **Live studio** owns Chord detection, Timbre, the two-stage session
controls (**Get ready**, then **Start teaching**) and bounded
camera preview. **Recording library** owns
the song picker, Refresh, Upload, playback mode, Trigger, its own
pause/resume/stop controls, and **Record a new song...**. **Student
results** owns the event table and student-computed summary, so neither
live nor recorded controls are vertically squeezed by a results panel.

**Record a new song...** constructs
`remote_guidance.teacher.recording_wizard.TeacherRecordingWizard` with
`self.cfg`, the isolated `teacher_config()` view. The copy must not call
`Config.load()` and its first page deliberately has no camera, MIDI or
profile picker: it shows all three Teacher Settings values read-only,
and Next remains disabled if the Teacher MIDI/profile is unavailable.
That prevents a one-off recording from quietly using section 1's
top-level devices.

The implementation copies section 3's capture algorithm and keeps using
the shared low-level data helpers (`RawMidiRecorder`, `SyncInfo`,
`AnalyzeWorker`, `build_score_midi`, `build_fingering_entries`, etc.), so
it still writes the exact `data/music/<song>/` format that playback,
upload and analysis expect. It does not import or subclass section 3's
UI class; future Tele-training UI/device changes therefore cannot alter
**3. Recording & Playback**. Its three pages use the Teacher amber theme:
read-only device audit, bounded camera plus capture controls, then review
and save.

The wizard opens the configured Teacher camera when its window opens;
MIDI, LED sync and local audio are claimed only by **Start recording**.
While it is open, the Teacher Client locks Settings, Change room and Live
hardware controls; any detector opened earlier by **Get ready**
is closed first. Closing the wizard stops polling and releases camera,
MIDI, LED, audio and video writer. Completing it refreshes the picker and
selects the new `music/<song>` entry.

Recorded sessions are independent of live sessions. Upload is available
as soon as a room and local song exist - do not make it depend on a live
`session_id`. **Trigger on student** creates its own REST session with
`mode: "recording"`, `recording_id` and `playback_mode`, sends
`session.start`, then `recording.start`. No teacher `guidance_mode` is
included. While either mode is active the other guidance workspace is
disabled, the Results workspace stays readable, and the active mode's
own transport buttons remain available. If
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

Without a live frame the current minimum-height hints are about 532 px
(Teacher) and 511 px (Student); a 560 × 360 preview can raise them to
about 607/586 px. Both must stay under ~700 so they fit a 768px screen -
there is a test.

### Settings: one button per program, each owning its own

Each of the three programs has exactly one **Settings** button and can
see only its own settings:

| Program | Button opens | Covers |
|---|---|---|
| Student Client | `RemoteSettingsDialog("student", ...)` | its camera, MIDI port, calibration profile |
| Teacher Client | `RemoteSettingsDialog("teacher", ...)` | its camera, MIDI port, calibration profile |
| Relay Server | `server.gui.ServerSettingsDialog` | bind host, port, registration, token TTLs |

The two client dialogs use the same visual identity as the window that
opened them: amber/brown for Teacher and cyan/navy for Student. A compact
role header sits above one horizontal device workspace (**Camera** on the
left, **Keyboard + MIDI** on the right), followed by a short ownership
note, one-line status strip and actions. Save and the camera/profile
preview are the role-accent primary actions; the live MIDI readout is an
accented monitor card. At 1040 × 650 the whole dialog is visible, and its
minimum-size hint remains about 956 × 496 rather than growing with long
status text (the full text is kept in a tooltip).

Camera resolution and FPS remain ordinary editable `QSpinBox` values:
the user may type an exact number, or use the two wide buttons on the
right. `client_styles.py` supplies separate, high-contrast amber/cyan SVG
up/down arrows and a visible divider/hover state; do not replace these
with the tiny low-contrast platform glyphs.

Both client settings dialogs include **Capture camera + profile
preview**. It uses the camera values and calibration profile currently
shown in the form, including unsaved edits; opens that camera only long
enough to take a snapshot; overlays the colored pixel mask and key-id
labels; releases the camera; and shows the result in a separate dialog so
Settings stays small. Neither the settings nor the photograph are saved.
A camera frame whose actual dimensions differ from the profile key map is
refused with both resolutions in the error - never resize the mask,
because that can make a wrong calibration look plausibly aligned.

Only the snapshot is local. The profile loading, the drawing and the
dialog are `app.gui.profile_preview`, shared with the same button on the
two quizzes and the main study's trial runner (README § "Checking the
camera against the profile"). The split matters in both directions:
`capture_profile_preview()` loads the template **before** opening the
camera, so a bad profile does not cost a camera claim to discover; and
the quiz windows pass in the frame they already have, because they hold
their camera for their whole lifetime and a second capture on the same
device fails on most backends. Keep new callers on that shared module
rather than growing a second overlay.

Both dialogs also put **Connect and test** beside their MIDI port picker.
It temporarily constructs `MidiListener` for the value currently shown;
pressing any key updates the dialog with `note N (name)` and the resolved
port label, matching the verification readout in the MIDI Mapping Wizard.
This is the normal way to determine which identically named keyboard is
`#1` or `#2` before saving. The test listener is never handed to a
session. Changing the selected port, refreshing the list, Save, Cancel
and window close all stop its timer and call
`MidiListener.close()` so Settings cannot retain the keyboard and make a
later **Ready for guidance** / **Get ready** fail.

The MIDI port control is a **non-editable `QComboBox`**, not a line edit.
Refresh replaces its choices with `list_input_ports()`. If the saved or
currently selected port is not detected (including the normal case where
no keyboard is plugged in), that exact saved label is inserted as the
first/only option and marked by a tooltip as offline; it is never erased.
With neither detected inputs nor a saved value the combo has zero items,
shows `No MIDI inputs detected`, and disables Connect and test. The
role-colored SVG arrow in `remote_guidance/assets/` must remain visible so
the control cannot again look like a text field.

**Two keyboards of the same model report the same MIDI name.** That is
the normal tele-training setup, and it used to mean the picker showed one
port where there were two. `app.midi` now labels duplicates `"SE25 MIDI1
#1"`, `"SE25 MIDI1 #2"` in OS enumeration order, and the client Settings
dialog says so in a warning under the port row whenever
`ambiguous_port_names()` is non-empty. A name the OS reports only once is
still its own label, unchanged.

The numbering is **enumeration order, not a device identity** - replug a
keyboard and #1 and #2 can swap. Nothing in MIDI identifies a physical
instrument portably, so this cannot be fixed in code; the warning tells
the user to re-check after replugging, or to rename the instruments
(macOS: Audio MIDI Setup → MIDI Studio) so the raw names differ and no
suffix is needed. `python test-script/MIDI.py` listens on every port at
once and prints which label a key press came from; it remains the
command-line alternative to the Settings test. See §9.8 for why this is
not left to mido.

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

Four `ProcessEntry` buttons - Student Client, Teacher Client, Relay
Server, Network Latency Benchmark - plus **Tele-training Setup Wizard**,
which is an ordinary sub-window rather than a process.

That difference is the point: the wizard claims the camera and the MIDI
keyboard to calibrate, and a client may be holding them. As a sub-window
it falls under the launcher's usual one-tool-at-a-time rule, which keeps
the two apart; as a `ProcessEntry` it could run beside a client and fight
it for the camera.

It is **setup, not settings**, and the two do not overlap: the wizard
*makes* a profile and writes no config key, while the clients' Settings
dialogs *choose* which profile and devices to use. Two things tests still
assert stay gone - a "Launch Local Stack" button (removed on request) and
any *settings* entry, because each endpoint owns its own settings and
shows only its own (§6 "Settings: one button per program"). See §4.12 for
what the wizard is and is not allowed to touch.

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
teacher      camera{...}, midi{port_name}, keyboard_profile,
             chord_detection, record_video
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

**`midi.port_name` stores a port *label*, not necessarily the driver's
name.** With one keyboard of a given name they are the same string, which
is why nothing had to be migrated. With two, the label carries the
`" #1"` / `" #2"` suffix from §6, and **both roles must be given
different ones** - a config where student and teacher hold the same name
sends both clients to the same physical keyboard, silently. A bare
driver name (what every config written before this existed holds) still
resolves, to the first port with that name, exactly as mido did.

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
**Get ready** → student **Ready for guidance** → teacher **Start
teaching**. The first two presses open each role's devices and local
key-audio stream (unless `Mute` is selected) and nothing more: no session
exists on the relay and nothing is recorded. **Start teaching** - enabled
only once the devices are up *and* the student has announced itself -
names the lesson, creates the relay session and sends `session.start`
carrying `session_name`, `start_at_unix_ns` and `lead_ms`. Both ends count
the same `teacher.lesson_lead_s` seconds (default 3) off that one wall
stamp and open their video writers together.

Each of those steps is readable from the buttons alone. The accent sits
on the one press the session is waiting for and nowhere else: **Get
ready** while nothing is open, then **Start teaching** once the devices
are up *and* the student has announced itself, and neither once a lesson
is running - **Start teaching** leaves the screen entirely for as long as
one is. The student's **Ready for guidance** goes dead on the press
itself rather than at the far end of opening the camera, and comes back
only if the start failed. While either end is writing a video, a red
**RECORDING** chip names the folder it is writing into; it follows the
writer, not the checkbox, so a lesson whose camera failed does not claim
to be recorded.

Pick each role's **Timbre** on its Session page; the choices are the same
as Student Quiz. For the recorded path, the teacher's explicit **Record a
new song...** action opens the private Teacher wizard and claims its
devices until the wizard closes.

Benchmark (default needs only the relay; it supplies both client roles):

```bash
python remote_latency_benchmark.py --server http://127.0.0.1:18765 \
  --username teacher1 --student-username student1
```

`latency_benchmark.main()` logs in both accounts, creates a temporary
room, joins the built-in Student to it with the room's own join code,
opens both real `RemoteWebSocketClient`s, reads the membership back from
the relay once both sockets are up (`room: members confirmed: teacher ...,
student ...` - a benchmark whose endpoints sat in different rooms would
just lose every probe), and closes (never deletes) the room during
cleanup. The built-in responder sends `latency.received` immediately and
owns no Qt/hardware.

**Picking a room is never the operator's job.** `--external-student`
creates the room here too, prints its join code for the real Student
Client, and blocks until the relay's `presence` frame shows a student in
the room (`--wait-for-student`, default 180 s) - probing an empty room
would score every early probe as transport loss. `--room-id` is the one
manual escape hatch, for a Student Client already sitting in a room of
its own, and is refused in built-in mode. The GUI mirrors this: no room
box in built-in mode, just a Room row that fills in with the id, join
code and confirmed membership as the child process reports them via its
`room:` lines. `--trigger-cue` is still refused in built-in mode so a
simulated reply cannot be mislabelled as hardware timing.

**Probe pacing** (`--interval-mode`, default `fixed`). A fixed 500 ms
period samples the same phase of anything periodic in the path - relay
heartbeats, Wi-Fi power-save windows, scheduler ticks - on every single
probe, and biases the result by however that phase happens to line up.
`uniform` jitters each wait by `--interval-jitter` (default ±50 %) of the
interval; `poisson` draws it from an exponential distribution, which is
what RFC 2330 § 11.1 recommends for unbiased network sampling, truncated
at 5× the mean so one draw cannot stall a run. All three modes have the
same **mean**, so the probe count, rate and expected duration do not
change with the choice - only the spacing pattern does. The seed is drawn
once and recorded in `summary.json` (`pacing.seed`), so a randomised run
can be repeated exactly with `--interval-seed`; that block also reports
the *realised* min/mean/max spacing, because the loop sleeps after each
send and so pays that probe's send cost on top of its wait. The window
shows the resulting range live ("Each wait: 300-700 ms intervals...")
from `describe_pacing()`, the same function the CLI prints.

`human` is the mode that paces probes like the platform is actually
driven - cue out, person reacts, next cue - and `measure_human_pacing()`
fits it to **every trial of every participant, at run time**.

**What it reads.** Every `data/quiz/P*-T*/results.json`, through
`app.quiz`'s own loader, validity rule and reaction-time field
(`timing_error_s`), so this cannot drift from what the study's analysis
counts: a trial is usable when it neither timed out nor was manually
invalidated as carry-over. Non-positive reaction times (the
press-before-cue artefact) have no logarithm and are dropped. `TEST-*`
and `remote-*` folders are not participants; the participant number is
never assumed to stop at 14.

**What it fits.** Three things, because "all the data" should mean the
analysis as well as the sample:

- **per participant, then pooled.** Pooling every trial into one
  distribution mixes within-person variability with between-person
  differences and *inflates sigma*: 0.441 pooled against 0.424 averaged
  over participants, whose medians run 503-767 ms. `human` mode
  therefore samples **hierarchically** - draw a participant, then draw
  from that participant's own lognormal, rescaled so the group median
  lands on the requested interval. One pooled lognormal would blur both
  spreads into a single wrong middle.
- **which family, not which family we assumed.** Lognormal is fitted
  against ex-Gaussian and shifted lognormal and wins on this data
  (KS 0.029 vs 0.062; AIC lower by 112 than ex-Gaussian). Shifted
  lognormal scores AIC 8 *better* still - and its fitted shift is
  10.9 ms, 1.6 % of the median, for a whole extra parameter, which is
  why the sampler stays two-parameter. The table is recorded and printed
  rather than summarised as a winner, because the pooled comparison is
  not what the probes are drawn from.
- **how certain.** A `BOOTSTRAP_RESAMPLES` bootstrap over the pooled
  log-RTs: at 14 participants the median's 95 % interval is 659-670 ms
  and sigma's is 0.436-0.447. Seeded, so the interval is a property of
  the data and does not move between two reads of the same corpus.

scipy is optional - without it the pooled and per-participant fits still
happen and only the family table and interval are skipped.

**Why it is not a constant.** The participant set grows. P01-P14's
numbers would go on describing 14 people long after P20 was recorded,
without ever looking wrong. `FALLBACK_HUMAN_*` apply only with no quiz
data at all (a fresh checkout, a copied-out deployment) or fewer than
`MIN_HUMAN_TRIALS`, where a fit describes the sample rather than human
reaction time. The fitted median is also human mode's default
`--interval`, so `--interval-mode human` alone reproduces the
participants' spacing.

**Cost and caching.** The whole analysis - 378 quiz folders, 11,335
reaction times, three family fits, 2000 bootstrap resamples - takes
about 0.7 s. It is cached against a *fingerprint* of the quiz folder
(file count, sizes, mtimes), so recording a participant invalidates it
by itself and nobody has to remember to; `clear_pacing_cache()` and the
results page's Refresh force it. `app.quiz` is imported inside the
function, not at module scope: it reaches the finger-matching stack and
so numpy, and this module is otherwise free of that weight.

**Reporting progress.** `measure_human_pacing(progress=...)` calls back
with `(done, total, message)`. The CLI prints those lines before dialling
anything, so a slow corpus is visibly the fit rather than a stalled
connection. The window runs the analysis on `_PacingAnalysisWorker`
behind a cancellable `QProgressDialog` the moment **Human** is selected -
not because 0.7 s needs a progress bar, but because the window must not
be the thing that decides how long the study's data takes to read.
Cancellation is cooperative (the progress callback raises; a QThread
cannot be safely killed) and returns the mode to Fixed rather than
leaving Human selected with nothing behind it. **Nothing else may
trigger the fit**: `analyse_summary()` takes its scale reference from the
run's own recorded population first and a cached fit second, precisely so
opening the results page cannot stall on a corpus scan.

Each run records the population it was paced against in `summary.json` →
`pacing.human` (median, sigma, per-participant table, trials, families,
intervals, source) - "measured from 14 participants" and "from 20" are
different claims. `config.human_log_sigma` stays `null` there, meaning
*fit it*; the fitted value is never written back, so a later read of that
config cannot mistake the fit for an operator's own choice. A lognormal's
mean is above its median (742 ms at the current fit), which is why
`expected_interval_s()` exists: a 1000-probe human-paced run takes
~12.6 min, not the ~11.2 min the median would suggest.

### Report figures

`latency_plots.py` writes each run's figures as **PNG and vector PDF** -
the same figure, so the printed one cannot drift from the one on screen.
They follow print rules (~9 pt type, one text width, no chart junk) and
**one measure per axis - never a second y-scale**. Colour is assigned by
job from three validated categorical slots, and every series carries a
direct label as well, so identity never rests on colour alone.

| Figure | The question it answers |
|---|---|
| `latency` | How did the round trip behave over the run, and what is its distribution? Time series with elevated-state episodes shaded, beside an **ECDF** - a histogram's bins bury exactly the tail a real-time path is judged on |
| `latency_hops` | Where is the time spent? Teacher→relay against the whole round trip, on one log axis; the gap between the curves is the student leg |
| `latency_jitter` | How much does it move? Consecutive-probe variation (RFC 3393 IPDV), which is closer to what a cue is felt as than absolute delay |
| `latency_human` | Does it matter? The transport distribution beside the participants' fitted reaction time, on one axis |

Two rules these follow. **They describe and do not grade**: no "too
slow" threshold is drawn, because that is a requirement rather than a
property of the measurement - where scale is needed the comparison drawn
is the study's own reaction time, and the reader concludes. And
`split_states()` **only claims two link states when there are two** -
one-dimensional two-means, accepted only if the centres differ by 2x and
neither holds under a tenth of the probes, with single-probe flickers
excluded from the shading by a minimum run length. On the first run
against the deployed relay this found a base state at 34 ms and an
elevated one at 142 ms, in episodes of a few seconds each, with both hops
moving together - i.e. on the shared access link rather than in the relay.

### The window's second page

`BenchmarkResultsPage` lists every folder under
`data/remote_guidance/latency/` that has a `summary.json` (newest first;
one without is an interrupted run, not a result) and shows that run's
`analyse_summary()` readings, its own `format_summary()` table and its
`latency.png`, scaled down to the pane but never up. It recomputes
nothing - the numbers are the ones the run wrote. A finished run switches
the window to this page, because after eight minutes the result should
not have to be gone looking for; a *failed* run does not, since the log
on the first page is what explains it. `analyse_summary()` describes the
distribution's shape and scale and deliberately passes no verdict on it -
the single outside number it brings in is the P01-P14 median reaction
time above, because a transport delay only means something next to the
human delay it is added to.

Tests:

```bash
python -m pytest test-script/test_remote_guidance_config.py test-script/test_remote_guidance_server.py test-script/test_remote_guidance_e2e.py test-script/test_midi_ports.py test-script/test_profile_preview.py -q
```

Which physical keyboard is which, when both report the same name:

```bash
python test-script/MIDI.py
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

8. **mido cannot see or open the second of two identical keyboards.**
   Which is precisely this module's setup. Two failures, both silent:
   `mido.backends.rtmidi.get_devices()` de-duplicates ports **by name
   string**, so with two Nektar SE25s macOS's four inputs (`SE25 MIDI1`,
   `SE25 MIDI2`, twice) came back as two; and `mido.open_input(name)`
   resolves the name with `list.index()`, which always returns the
   **first** port of that name - so even a complete list could not reach
   the second instrument. Nothing raises. The teacher just receives the
   student's notes.

   `app/midi.py` therefore enumerates and opens through **python-rtmidi
   directly, by index** (`MidiInputPort`, `resolve_input_port`,
   `MidiInputReader`); mido is still used for `MidiFile` writing, where
   it is fine. `MidiListener` and `RawMidiRecorder` sit on top and are
   unchanged from the outside apart from opening their port in
   `__init__` (so a failure raises where callers already catch
   `RuntimeError`, instead of being printed on a worker thread and
   leaving a listener that receives nothing forever). Do not "simplify"
   any of this back to `mido.get_input_names()` / `mido.open_input()` -
   there is a test file for it, `test-script/test_midi_ports.py`.

9. **A missing `_on_message` handler is invisible until that message
   type arrives.** `guidance.live` dispatched to `self._on_guidance()`,
   which was never written, so **every live cue** raised
   `AttributeError` inside the student's dispatcher - on the WebSocket
   thread, mid-lesson. What it looked like from the outside: the teacher
   detected notes correctly and reported them sent, the relay forwarded
   them, and the student sat on "Waiting for the teacher..." forever,
   with nothing on its own screen saying why. Only the *recorded* path
   worked, because that one goes `recording.start` → scheduler →
   `_release_scheduled_event` → `accept()` and never touches the live
   handler.

   Nothing caught it: the e2e tests drive a *fake* student, and no test
   put a real envelope through `StudentRemoteWindow._on_message`. There
   are now three - two behavioural, plus a structural one that parses
   both windows' `_on_message` and asserts every `self.<handler>()` it
   dispatches to actually exists. Add a message type, and that test
   covers the new branch for free.

   The handler itself must stay a one-liner onto `session.accept()`. It
   is what stamps arrival and acknowledges before any cue work, and the
   recorded path deliberately enters through the same door so both kinds
   of event produce identical timings (§4.1).

10. **An epoch stamp subtracted from an elapsed duration clamps to zero
    and says nothing.** Found while building the teacher's lesson
    recording (§4.13), which copied the line. Both Song Recording
    Wizards computed a song's length as
    `record.duration_s - first_note_on_time(events)` - correct while
    `abs_time` was recorder-relative, wrong from the July 2026
    epoch-timestamp migration onwards, because `first_note_on_time`
    became ~1.79e9 and `max(..., 0.0)` swallowed the result. **Every song
    recorded after that migration has `duration_s: 0.0` in its
    `meta.json`** (visible on disk: songs recorded before it still carry
    real lengths). Nothing crashed, nothing warned, and the only reader
    is the uploader's description text - which is exactly why it survived.

    The lead-in now has one implementation,
    `app.music_recording.lead_in_seconds(events, recording_start_time)`,
    which needs the recording's start moment precisely so the two
    quantities cannot be confused again, and all three producers call it.
    `test-script/test_music_recording.py` drives both wizards over a
    synthetic recording and asserts the saved length is the playing, not
    the capture. The general rule this is an instance of: **a stored time
    here is always an absolute `time.time()` value** - anything that
    subtracts one from something must be able to say what the other one
    is.

---

11. **A folder name that outlives its session gets written into twice.**
    The student named its own folder at **Ready for guidance** from
    `teacher_session_name` - the last name the teacher had sent - and that
    field was never cleared. Pressing Ready for lesson *n+1* before the
    teacher had opened it therefore reused lesson *n*'s name;
    `cv2.VideoWriter` truncates whatever is at the path it is handed, so
    lesson *n*'s video was destroyed at the moment lesson *n+1* started,
    and the end-of-session rename then moved the folder onto lesson
    *n+1*'s name. **Running *n* lessons back to back left exactly one
    folder, named after the last one.** Nothing failed, nothing warned:
    every lesson looked like it had saved.

    It also left the previous lesson's `sync_align.json` in the surviving
    folder, and a saved alignment outranks `sync.json` - so the offline
    finger pass mapped every event onto a frame index far past the end of
    the video it actually had, found no hands there, and returned
    `actual_finger: null` for every event with no error anywhere. A
    session with 100% key accuracy and 0% finger accuracy on a recording
    that plainly shows the hands is the fingerprint.

    Three independent fixes, because one was clearly not enough: the name
    is made per lesson at **Start teaching** and cleared by `_stop()`;
    recording starts on `session.start`, by which point the name is known,
    so no folder is ever renamed; and `_start_recording()` refuses to open
    a writer over an existing non-empty `performance.mp4` and says so in a
    blocking dialog. `data/quiz/_sessions.log` records every open and
    close with its absolute path, which is how this was finally caught.


## 10. Known gaps and what is not verified

### Real-hardware status

A user smoke-tested teacher key → student prompt on one computer in
August 2026. One run was about two seconds under heavy machine load and a
later retry was visibly faster, which exposed the client-side scheduling
bottlenecks addressed in §6's “Live latency fast path”. The new
background-vision/visual-first version has deterministic injected tests
but still needs a fresh user timing run with real devices. The following
remain unverified:

- LED key cue and the LED sync flash (student's, and the teacher's own
  during a recorded lesson - the teacher rig may have no strip at all,
  in which case `app.sync_led` falls back to the recorded start times)
- Haptic cue on an explicit serial port
- Instrumented teacher MIDI-press → student visual-onset timing on real hardware
- Student video recording and the offline finger pass
- Teacher-side lesson recording (§4.13) end to end on real devices: the
  layout, the naming and the tick wiring have tests, but no run has yet
  produced a `data/music/remote-<epoch>/` from a real camera and keyboard
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
- **Neither the keyboards nor the cameras have a stable identity, and
  adding one was investigated and rejected.** Both device APIs address
  hardware by position: `cv2.VideoCapture(index)` and rtmidi's port
  index. Two identical keyboards are told apart only by the `#1`/`#2`
  suffix `app.midi` assigns in enumeration order (§6), and two identical
  webcams only by `camera.index` - both of which can change when
  something is replugged.

  macOS *can* do better, and this was built and confirmed working before
  being taken out again: CoreMIDI gives every endpoint a `uniqueID` and
  persists it in `~/Library/Preferences/ByHost/com.apple.MIDI.*.plist`
  against the device's `USBLocationID`, and AVFoundation's camera
  `uniqueID` for a UVC device *is* the USB location
  (`0x1400005802e705` = location `0x00140000`, vendor `0x5802`, product
  `0xe705`). Both are readable from pure Python through ctypes, in the
  same order rtmidi and OpenCV enumerate, with no new dependency.

  **Windows cannot**, and Windows is a deployment target here
  (`launcher.bat`, `runtime/Python311-init.7z`). WinMM's `MIDIINCAPS`
  carries manufacturer id, product id, driver version and name - all four
  identical for two keyboards of the same model, with no serial and no
  port path. WinRT's `Windows.Devices.Midi` does expose a stable id, but
  python-rtmidi uses WinMM and there is no reliable way to correlate the
  two enumerations. The camera side has the same shape: MSMF/DirectShow
  symbolic links carry VID/PID and an instance path, but reaching them
  needs `comtypes`/`pywin32` *and* an assumption about OpenCV's index
  order that cannot be checked from the other platform.

  A device identity that works on the development Mac and silently falls
  back to enumeration order on the deployed Windows machine is worse than
  no identity at all, so there is none. What to do instead: keep each
  instrument in its own USB port, and on macOS rename them in Audio MIDI
  Setup → MIDI Studio so even the names stop being ambiguous. Revisit
  only if Windows support stops mattering, or if hardware with real USB
  serial numbers replaces this rig - neither of these keyboards nor
  either webcam reports one (the two webcams even share `SN00010`).
- **Reconnect replay is untested under real loss.** Duplicate suppression
  works (there is a test), but no test drops a connection mid-session.
- **No token refresh in the clients.** They log in and use the access
  token; a 30-day expiry mid-session is not handled.
- **`local_server.enabled` is read but nothing acts on it** since Launch
  Local Stack was removed.
- **Teacher pause/resume for live mode is only a message.** The student
  honours it; there is no visible teacher-side state machine. A pause does
  not pause the teacher's own lesson recording either - the video and MIDI
  run from Start to Stop.
- **The teacher's recorded note times are tick-quantised** (§4.13): the
  live detector's MIDI port is drained by the 33 ms guidance timer, so a
  note's stamp is when that timer saw it. Good enough to line the lesson up
  with its video and to replay it; it is not an independent onset
  measurement, and nothing treats it as one.
- **A lesson whose finger pass fails is not a song.** It keeps its video,
  MIDI and sync marks but has no `meta.json`, so `list_songs()` does not
  show it and there is no re-run button - the pass would have to be run by
  hand. Closing the Teacher Client mid-lesson lands in the same state.
- **Error surfaces are status lines.** A dropped relay mid-lesson shows a
  message but there is no recovery UI.

### Things a next session might reasonably do

- Try it on two machines and record real RTT/loss.
- Wire the photodiode/accelerometer rig into the reserved columns.
- Multi-student rooms (routing + teacher UI).
- Token refresh + a clearer reconnect UX.
- Trim `student/window.py` (1485 lines) - the session-page building and
  the recording/analysis plumbing could split out.
- Give an unanalysed `data/music/` folder a way back into the fingering
  pass, which would also cover a wizard recording abandoned before save.

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

---

## 12. Operating it - the user-facing reference

Moved here from the platform README, which had grown a 575-line
tele-training chapter of its own. Everything below is about *using* the
module rather than its architecture; the README now carries only an
orientation and a pointer to this file.

### Setting up a new remote student or teacher

**Tele-training Setup Wizard** (section 8) builds a **keyboard profile**
for this machine: the camera, the calibration of that camera's view of the
keyboard, and that keyboard's key→note mapping. Use it - not section 1's
Initial Setup - whenever someone new joins a tele-training session.

Three steps, and the last two can be re-entered from the step bar so one
part can be redone (remapping MIDI without recalibrating, most of all):

1. **Camera** - opening the Tele-training wizard does not scan or open a
   camera. Click **Scan for cameras**; the scan runs in the background;
   then explicitly choose one of the detected indices to start its live
   preview and set its resolution/flips.
2. **Calibration** - the same five-page flow as Initial Setup: name the
   new profile, capture a photo, click the keyboard's two corners, tune
   edge detection on that cropped keyboard, then mark white/black keys
   with the same paint-bucket controls. **Finish** creates the profile.
3. **MIDI mapping** - the same two-page visual flow as Initial Setup.
   First connect the port and use the displayed Middle C reference image
   plus live note readout to verify note 60/C4. Then pick the profile and
   use the live camera image: the next key is highlighted yellow, mapped
   keys are green and unreached keys grey. Press each highlighted
   physical key in turn, then save the mapping.

The camera comes first because the calibration is a pixel mask of *that
camera's* frame: step 2 captures through step 1's settings rather than
asking again, so the two cannot disagree.

**Its only output is the profile folder.** It writes no `config.json` key
at all - not the top-level `camera`, `midi.port_name` or
`active_keyboard_profile`, and not the `remote_guidance` block either - so
running it cannot change what any tool on this machine does. To use the
new profile, select it in the Student or Teacher Client's own **Settings**,
which is where choosing devices already lives.

There is also **no student/teacher choice** in it. A calibration describes
a camera looking at a keyboard, not a person; student and teacher normally
need one each only because they normally sit at different setups, and if
they share a setup they can share the profile.

Two more things it will not do:

- it saves only into profile folders **it created**, each marked with a
  `remote_setup.json` file, and refuses to write into any folder without
  that marker - so section 1's profiles, and any profile a recorded
  session was scored against, are unreachable from it;
- it never overwrites an existing folder at all, not even one of its own:
  a new calibration always means a new name.

The wizard is a normal launcher window, so the usual one-tool-at-a-time
rule applies - it will not run beside a client and fight it for the
camera. The Initial Setup calibration and MIDI mapping flows were copied
into `remote_guidance/calibration_wizard.py` and
`remote_guidance/midi_mapping_wizard.py` rather than changing section 1:
their images and interactions stay the same, while their save actions go
through the Tele-training-only store. Those write rules live in
`remote_guidance/setup_store.py`, kept separate from the UI so they can
be (and are) tested without hardware.

### Guidance modes

The student picks one before the session starts:

| Mode | Key LED | Finger cue |
|---|---|---|
| `visual` | yes | on-screen cue window (`app/gui/cue_window.py`) |
| `haptic` | yes | nail actuator (`app/haptic_cue.py`) |
| `both` | yes | both together |

**Chords are cued in full.** With **Chord detection** on, the teacher's
matcher returns every finger of a simultaneous press and the student cues
all of them: every key lit, every motor buzzing (the rig's command is a
bitmask, so it is one write), and every dot on the cue window. The
hand-photo style is the one channel that cannot show a set - there is one
photo per finger and no combined assets - so it cycles through the
chord's photos instead, ~5 per second, with the full chord named in the
text line. It stays **one cue event with one cue-ready moment**, so a
reaction time still has a single origin.

Scoring deliberately does not follow: a chord is judged on its primary
note, so pressing a different note of the same chord counts as wrong.
Widening the cue is free; widening the verdict would mean a second
definition of "correct" in a pipeline shared with the local quiz and an
already-run study. See [REMOTE_GUIDANCE.md](REMOTE_GUIDANCE.md) §4.11.

**None of this reaches the local quiz or the main user study.** They call
the single-finger cue API, which is unchanged - in particular a one-finger
cue never starts the photo-cycling timer, so condition B's stimulus is
exactly what it always was. There are tests for that.

The **key LED is present in all three** - the modes name only how the
*finger* is conveyed. All three are driven through one
`CompositeCueOutput` (`remote_guidance/cue_outputs.py`), which is an
ordinary `app.quiz.CueOutput`: the existing screen and haptic cue classes
are composed, not reimplemented, and `QuizWindow` is untouched.

### What is reused

Nothing about detection, scoring or storage is written twice:

| Concern | Module |
|---|---|
| Teacher's key→finger detection | `app.camera`, `app.hand_tracking`, `app.midi`, `app.finger_matching` (incl. `match_notes_to_fingers` for chords) |
| Student's cue channels | `app.gui.cue_window.ScreenCueOutput`, `app.haptic_cue.HapticCueOutput`, `profile_led_mapper` + `note_led_map` |
| Recording + sync mark | `app.music_recording` (`RawMidiRecorder`, `SyncInfo`) |
| Correctness and summary | `app.finger_matching.is_finger_correct`, `app.quiz.summarize` |
| Storage format | `app.quiz.QuizResult` / `save_quiz_results` / `QuizMeta` |
| Offline finger pass | `app.offline.analyze_recording` via `app.gui.analyze_worker.AnalyzeWorker` |
| Post-session review | `app.gui.quiz_analysis_window.QuizAnalysisWindow` |

A remote session therefore lands in `data/quiz/<session name>/` in the
standard layout (`raw/performance.mp4`, `midi_raw.json`, `notes.json`,
`sync.json`, plus `results.json` and `meta.json`), and every existing
analysis tool reads it unchanged. `QuizMeta.guidance_type` records it as
`remote-visual` / `remote-haptic` / `remote-both`.

The teacher's pre-recorded uploads come from the same libraries the local
tools use - `data/music/<song>/` and `data/sequence/<name>/`, read through
`app.music_recording.load_playback_events`. Only the note/finger event
list is uploaded; the video stays on the teacher's machine.

Running teacher and student on the same computer does not overwrite that
source folder. Every Upload creates a new relay database recording with
a new id, and the student downloads its event JSON into memory rather
than writing it back under `data/music/` or `data/sequence/`. Student
performance output is separate under `data/quiz/<session name>/`; choose
a new session name if an existing quiz result with that name must be
preserved.

### Provisional vs final results

The student scores every response twice:

1. **Provisional**, live, from the current hand landmarks, sent to the
   teacher as each event completes.
2. **Final**, after the session, when `analyze_recording` re-runs finger
   matching over the recorded video with the LED sync anchor - the same
   pass a local quiz does.

Both stages are labelled `stage: "provisional" | "final"` on the wire and
in the relay's database, and the teacher's table shows which it is
looking at. The server stores them and never recomputes either: the
session summary endpoint returns the student's own numbers under
`student_reported_summary` with `summary_source: "student"`.

### Timing: what is measured on which clock

Each event records:

```
teacher_send_wall_ns              server_receive_wall_ns
student_receive_wall_ns           queue_enter_monotonic_ns
cue_dispatch_start_monotonic_ns   led_command_complete_monotonic_ns
visual_painted_monotonic_ns       haptic_command_complete_monotonic_ns
cue_ready_monotonic_ns            cue_ready_wall_ns
student_response_monotonic_ns     student_response_wall_ns
```

`cue_ready` is the **last** enabled channel to become ready. In `both`
mode that is
`max(LED command complete, visual paint complete, haptic serial flush complete)`;
with one finger channel it still includes the LED, because the LED is in
every mode.

Student reaction time is always, and only:

```
reaction_time_ns = student_response_monotonic_ns - cue_ready_monotonic_ns
```

Both terms from the student's own monotonic clock. **Never** from
`teacher_send`, `server_receive` or `student_receive`: those would fold
network delay and queueing into a number describing a person, so a slow
link would read as a slow learner. They are all still recorded, in their
own fields, and the time an event spent waiting behind another is
recorded separately again as `queue_wait_ns`.

For `QuizResult` compatibility, wall-clock `cue_onset_time` /
`keypress_time` are saved as well - but the accurate figure is the
monotonic one, and that is what `timing_error_s` carries.

These are **software dispatch and render timings**: the moment a serial
write flushed or a frame finished painting. They are not the moment an
LED emitted light or a motor began to move. Measuring that needs a
photodiode and an accelerometer on one acquisition clock (the report's
"System Transmission Performance Benchmarking"); the CSV keeps columns
free for it and nothing here claims it.

A cue that arrives while another is still live goes into a bounded queue.
It is never silently dropped and never overwrites the live one; if the
queue is full the event is refused explicitly and the teacher is told the
student is behind.

### Latency benchmark

```bash
python remote_latency_benchmark.py --server http://127.0.0.1:18765 \
  --username teacher1 --student-username student1
```

or **8. Tele-training → Network Latency Benchmark** for the GUI wrapper.
The default is self-contained: it logs in as both roles, creates a
temporary benchmark room, connects a Teacher WebSocket and a built-in
simulated Student WebSocket, runs the probes, then closes the room without
deleting its database record. You therefore start only **Relay Server +
Network Latency Benchmark**; neither full client is needed. Both accounts
must already exist on the relay, and the GUI pre-fills the demo pair.

Built-in mode measures the real network/relay route and client WebSocket
code, but deliberately owns no Student Qt UI, LED or haptic device. Enable
**Use an external Student Client** and provide its room id only when the
real Student software/hardware dispatch path is the object of the test.
Defaults follow the report's benchmark: 1000 probes at 500 ms intervals
over one persistent WebSocket per endpoint, warm-up samples recorded
separately and excluded from the statistics, unique sequence number and
message id per probe. Output goes to
`data/remote_guidance/latency/<run id>/` as `samples.csv`, `summary.json`
and `latency.png`, with n, lost, loss rate, min, mean, sd, median, p95,
p99 and max for each metric.

- **Transport RTT is the primary metric** - both stamps come from one
  monotonic clock on the teacher's machine, so no clock synchronisation
  is needed.
- In built-in mode the two simulated endpoints share one host clock, so
  teacher-send → built-in-student-receive is a directly measured one-way
  duration through the relay.
- With an external Student, **one-way latency** is reported as a measurement only when
  `--clocks-synced` asserts both hosts are NTP-synchronised *and* the
  estimated offset uncertainty is recorded with it. Otherwise the output
  shows `RTT/2`, labelled a symmetry-based estimate. Two independent
  computers' wall clocks differ by an unknown offset that is often larger
  than the delay being measured, so subtracting one machine's timestamp
  from another's is not a latency - it can even come out negative. Check
  both hosts (`time.is`, `chronyc tracking`) before a run; the built-in
  four-timestamp offset estimator quantifies the assumption but is not
  NTP and is not offered as a substitute.
- Monotonic timestamps are never subtracted across machines. They travel
  as opaque values and are only used where they were produced.

The relay also answers each probe itself (`latency.ack`), so a slow run
can be attributed to the teacher→server hop or the server→student one.

### Tests

```bash
python -m pytest test-script/test_remote_guidance_config.py test-script/test_remote_guidance_server.py test-script/test_remote_guidance_e2e.py test-script/test_midi_ports.py
```

Covers old-config compatibility, remote saves not touching shared
settings, student/teacher config isolation, the LED/haptic port clash,
two identically named keyboards staying separately addressable,
the composite cue in all three modes, JWT issue/expiry/refresh/revoke,
room ownership and membership authorization, cross-room isolation,
duplicate-message idempotency, sequence ordering, survival of a relay
restart, reaction time originating at the local cue-ready moment, RTT and
one-way never being conflated, percentile/loss statistics, a full
server + fake teacher + fake student integration run, and the launcher's
process actions. No hardware or real network is involved.

---
