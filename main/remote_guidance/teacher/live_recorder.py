"""Record a live tele-training lesson on the teacher's machine.

The student has always kept the durable record of a session: its cues, its
responses and its video land in ``data/quiz/<name>/`` exactly like a local
quiz. The teacher kept nothing - the live table was in memory and the
relay only stored timestamps and payloads. A lesson therefore had no
teacher-side performance to look at afterwards, even though the teacher's
camera and MIDI keyboard were both already open and producing exactly the
material the Song Recording Wizard saves.

This module writes that material, in the wizard's own layout, so nothing
downstream has to learn a new format:

    data/music/remote-<epoch>/
        raw/performance.mp4      the teacher's camera, unannotated
        raw/midi_raw.json        every note_on/note_off, epoch-stamped
        raw/notes.json           the note-on subset (app.midi.MidiEvent)
        raw/sync.json            SyncInfo - video/MIDI start + LED flash
        raw/keyboard_profile/    the calibration these times were matched with
        score.mid                trimmed to the first note, as the wizard does
        fingering.json           app.offline.analyze_recording's verdicts
        meta.json               SongMeta - written last, exactly as the wizard

The result is a normal ``data/music`` entry: it shows up in the Recording
library, it can be uploaded to a room, and the student can be asked to play
back the lesson that was just taught live.

Two deliberate differences from ``recording_wizard.py``:

* **The MIDI is stamped at the GUI tick, not by RawMidiRecorder.** The live
  session's one open MIDI port belongs to ``TeacherLiveDetector``, and it is
  drained by the guidance timer - taking it away, or opening a second
  reader, would change the latency path this project measures. So a note's
  ``abs_time`` here is the moment the guidance timer saw it (≤ one 33 ms
  tick after the key went down), which is also the moment the cue was sent
  to the student. It is the teacher's send stamp, not an independent one.
* **Only unannotated frames are written.** The live preview draws the whole
  key map over the frame; recording that would leave the offline finger
  pass with a video it cannot track hands in.

No Qt here on purpose: the window owns the timers, this owns the files and
the devices it opens itself (the video writer and, if one answers, the LED
strip for the sync flash).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable, List, Optional

import cv2

from app.midi import MidiEvent, MidiMessage, save_midi_log
from app.music_recording import (
    FINGERING_FILENAME,
    META_FILENAME,
    MUSIC_DATA_DIR,
    RAW_MIDI_FILENAME,
    RAW_NOTES_FILENAME,
    RAW_SYNC_FILENAME,
    RAW_VIDEO_FILENAME,
    SCORE_MIDI_FILENAME,
    RawMidiEvent,
    SongMeta,
    SyncInfo,
    build_fingering_entries,
    build_score_midi,
    lead_in_seconds,
    notes_only,
    raw_dir,
    sanitize_song_name,
    save_fingering,
    save_raw_midi_log,
    song_dir,
    trim_to_first_note,
)
from app.profiles import snapshot_profile
from common.led_controller import LEDArrayController
from note_led_map import WHITE_LEDS

log = logging.getLogger("remote_guidance.teacher.live_recorder")

SYNC_LED_PIXELS = [key[0] for key in WHITE_LEDS[:5]]

# A live lesson is not a graded stimulus, so it carries the lowest of the
# generator's three labels rather than pretending to a difficulty nobody
# chose. SongMeta.difficulty is a label only - see its docstring.
LIVE_DIFFICULTY = 1


def live_session_name(now: Optional[float] = None) -> str:
    """The name both ends of a live session use.

    ``remote-<epoch seconds>`` - the same shape the student already
    generates when its session-name box is left empty, so a teacher
    recording and the student's quiz folder can be paired by name alone.
    Epoch seconds, never a formatted date: every stored time in this
    project is a ``time.time()`` value."""
    return f"remote-{int(now if now is not None else time.time())}"


class TeacherLiveRecorder:
    """The Song Recording Wizard's capture, driven by the live tick.

    Owns the video writer and (optionally) the LED strip used for the sync
    flash. The camera and the MIDI port stay with ``TeacherLiveDetector``:
    this receives their output rather than opening anything that guidance
    depends on.
    """

    def __init__(
        self,
        song_name: str,
        *,
        fps: float,
        profile_name: str,
        data_dir: Path = MUSIC_DATA_DIR,
        sync_flash: bool = True,
        led_port: Optional[str] = None,
    ):
        self.song_name = sanitize_song_name(song_name)
        self.fps = float(fps) if fps and fps > 0 else 30.0
        self.profile_name = profile_name
        self.data_dir = Path(data_dir)
        self.sync_flash = bool(sync_flash)
        self.led_port = led_port

        self.raw_events: List[RawMidiEvent] = []
        self.video_start_time: Optional[float] = None
        self.midi_start_time: Optional[float] = None
        self.led_on_time: Optional[float] = None
        self.led_off_time: Optional[float] = None
        self.duration_s = 0.0

        self.video_writer: Optional[cv2.VideoWriter] = None
        self.led: Optional[LEDArrayController] = None
        self._last_frame = None
        self._frames_written = 0
        self._recording = False
        self._notes: List[MidiEvent] = []
        self._trimmed_notes: List[MidiEvent] = []

    # -- paths ----------------------------------------------------------

    @property
    def directory(self) -> Path:
        return song_dir(self.song_name, self.data_dir)

    @property
    def raw_path(self) -> Path:
        return raw_dir(self.song_name, self.data_dir)

    @property
    def video_path(self) -> Path:
        return self.raw_path / RAW_VIDEO_FILENAME

    @property
    def notes_path(self) -> Path:
        return self.raw_path / RAW_NOTES_FILENAME

    @property
    def sync_path(self) -> Path:
        return self.raw_path / RAW_SYNC_FILENAME

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def note_count(self) -> int:
        return sum(1 for event in self.raw_events if event.type == "note_on")

    # -- capture --------------------------------------------------------

    def start(self, frame, *, midi_start_time: Optional[float] = None) -> None:
        """Open the video writer sized to ``frame`` and start the clocks.

        ``frame`` must be an unannotated camera frame. Raises whatever
        OpenCV or the filesystem raises - the caller decides whether a
        lesson without a recording should still go ahead."""
        if frame is None:
            raise ValueError("a live recording needs one camera frame to size the video")
        self.raw_path.mkdir(parents=True, exist_ok=True)
        height, width = frame.shape[:2]
        writer = cv2.VideoWriter(
            str(self.video_path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (width, height)
        )
        if not writer.isOpened():
            raise RuntimeError(f"could not open a video writer at {self.video_path}")

        try:
            snapshot_profile(self.profile_name, self.raw_path / "keyboard_profile")
        except Exception:  # noqa: BLE001 - a missing snapshot must not cost the lesson
            log.exception("snapshotting profile %r for %s failed", self.profile_name, self.song_name)

        self.video_writer = writer
        self._frames_written = 0
        self._last_frame = frame
        self.raw_events = []
        self.led_on_time = self.led_off_time = None
        now = time.time()
        self.video_start_time = now
        self.midi_start_time = midi_start_time if midi_start_time is not None else now
        self._recording = True

    def connect_led(self) -> bool:
        """Open the LED strip for the sync flash, if there is one.

        The teacher rig may have no strip at all, and on a one-machine demo
        the student client owns it. Both are ordinary outcomes: the flash is
        the preferred sync anchor, not the only one - app.sync_led falls
        back to the recorded start times."""
        if not self.sync_flash or self.led is not None:
            return False
        led = LEDArrayController(port=self.led_port)
        try:
            led.connect()
        except Exception as exc:  # noqa: BLE001
            log.info("no LED sync flash for %s: %s", self.song_name, exc)
            return False
        self.led = led
        return True

    def flash_on(self) -> None:
        if not self._recording or self.led is None:
            return
        try:
            self.led_on_time = time.time()
            for strip, index in SYNC_LED_PIXELS:
                self.led.set_pixel(strip, index, 255, 255, 255, 255)
        except Exception:  # noqa: BLE001
            log.exception("the sync flash failed to switch on")

    def flash_off(self) -> None:
        if self.led is None:
            return
        try:
            first_strip, first_index = SYNC_LED_PIXELS[0]
            self.led.set_pixel(first_strip, first_index, 0, 0, 0, 0)
            self.led_off_time = time.time()
            for strip, index in SYNC_LED_PIXELS[1:]:
                self.led.set_pixel(strip, index, 0, 0, 0, 0)
        except Exception:  # noqa: BLE001
            log.exception("the sync flash failed to switch off")
        finally:
            # The strip is wanted for 1.1 s in total. Handing the port back
            # straight away keeps a teacher recording from holding a device
            # a student client on the same machine cues with.
            self._close_led()

    def add_midi(self, messages: Iterable[MidiMessage], stamp: Optional[float] = None) -> None:
        """Store one tick's MIDI, with mido's velocity-0 convention applied
        exactly as RawMidiRecorder applies it."""
        if not self._recording:
            return
        abs_time = time.time() if stamp is None else float(stamp)
        for message in messages:
            velocity = int(message.velocity)
            self.raw_events.append(
                RawMidiEvent(
                    abs_time=abs_time,
                    type="note_on" if (message.type == "note_on" and velocity > 0) else "note_off",
                    note=int(message.note),
                    velocity=velocity,
                )
            )

    def write_frame(self, frame=None) -> None:
        """Keep the file at elapsed*fps frames, repeating the last frame.

        Same catch-up rule as the wizard and the student: the guidance timer
        skips a preview frame whenever a note arrives, and an encoded clip
        short of frames plays back fast and drags app.offline's frame
        mapping with it. Capped at a second of catch-up per call."""
        if not self._recording or self.video_writer is None:
            return
        if frame is not None:
            self._last_frame = frame
        if self._last_frame is None:
            return
        elapsed = time.time() - (self.video_start_time or time.time())
        target_frames = min(
            int(elapsed * self.fps) + 1,
            self._frames_written + int(self.fps) + 1,
        )
        while self._frames_written < target_frames:
            self.video_writer.write(self._last_frame)
            self._frames_written += 1

    def stop(self) -> None:
        """Close the devices this owns. Idempotent - the window calls it
        from Stop, from the student's session.finished, and from close."""
        if self._recording:
            self.duration_s = time.time() - (self.video_start_time or time.time())
        self._recording = False
        if self.video_writer is not None:
            writer, self.video_writer = self.video_writer, None
            try:
                writer.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing the live recording video writer failed")
        self._close_led()

    def _close_led(self) -> None:
        if self.led is None:
            return
        led, self.led = self.led, None
        try:
            led.close()
        except Exception:  # noqa: BLE001
            log.exception("closing the live recording LED controller failed")

    # -- saving ---------------------------------------------------------

    def save_raw(self) -> None:
        """Everything that does not need the offline pass: the raw log, the
        note-on log, sync.json and score.mid. Called once, after stop()."""
        self.raw_path.mkdir(parents=True, exist_ok=True)
        save_raw_midi_log(self.raw_events, self.raw_path / RAW_MIDI_FILENAME)
        self._notes = notes_only(self.raw_events)
        save_midi_log(self._notes, self.notes_path)
        SyncInfo(
            video_start_time=self.video_start_time or 0.0,
            midi_start_time=self.midi_start_time or self.video_start_time or 0.0,
            led_on_time=self.led_on_time or 0.0,
            led_off_time=self.led_off_time or 0.0,
        ).save(self.sync_path)

        # However long the teacher talked before playing - measured from
        # the start of the recording, never from the note's epoch stamp
        # (see app.music_recording.lead_in_seconds).
        lead_in = lead_in_seconds(self.raw_events, self.video_start_time)
        trimmed = trim_to_first_note(self.raw_events)
        self._trimmed_notes = notes_only(trimmed)
        self.duration_s = max(self.duration_s - lead_in, 0.0)
        build_score_midi(trimmed, self.directory / SCORE_MIDI_FILENAME)

    def save_fingering_and_meta(self, matches: List) -> None:
        """fingering.json, then meta.json - in that order and last of all,
        because app.music_recording.list_songs treats a meta.json as "this
        song finished saving"."""
        entries = build_fingering_entries(self._trimmed_notes, matches)
        save_fingering(entries, self.directory / FINGERING_FILENAME)
        SongMeta(
            title=self.song_name,
            difficulty=LIVE_DIFFICULTY,
            created_at=self.video_start_time or time.time(),
            duration_s=self.duration_s,
            note_count=len(self._notes),
        ).save(self.directory / META_FILENAME)

    @property
    def has_notes(self) -> bool:
        return bool(self._notes)
