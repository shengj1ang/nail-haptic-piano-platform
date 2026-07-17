"""Student Quiz - practice a recorded song's fingering, one note at a
time.

Pick a previously recorded song (see music_recording_wizard.py), give this
attempt a name, and click Start: the target key's LED lights up (if a
strip is connected) and a cue tells you which finger to use - either the
on-screen cue window (see app/gui/cue_window.py, drag it to a second
monitor and fullscreen it if you like) for guidance_type "visual", or a
vibration motor on that finger (see app/haptic_cue.py) for "haptic" -
picked by which QuizWindow(cfg, guidance_type=...) constructs; see
student_quiz_haptic.py for the haptic entry point. Everything else about
the quiz is identical between the two. Press a key on the real MIDI
keyboard (or let it time out) and the quiz moves to the next note.

Exactly like a teacher's recording, the whole session's video + MIDI are
captured throughout (data/quiz/<quiz name>/raw/), and afterward
app.offline.analyze_recording is run once to work out which finger the
student actually used for each keypress. The result - Note Accuracy
(target key vs actual key), mean Timing Error (keypress time - cue onset
time), and Finger Accuracy (target finger vs detected finger) - is saved
under data/quiz/<quiz name>/ alongside a per-note breakdown.
"""

import sys
import time
from pathlib import Path
from typing import Optional

import cv2
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import profile_led_mapper
from app.camera import Camera
from app.config import Config
from app.gui.cue_window import CUE_STYLES, DEFAULT_CUE_STYLE, ScreenCueOutput
from app.haptic_cue import HapticCueOutput
from app.gui.image_view import ImageView
from app.gui.quiz_analysis_window import QuizAnalysisWindow
from app.keyboard.midi_mapping import MidiMapping
from app.midi import MidiEvent, list_input_ports, save_midi_log
from app.music_recording import META_FILENAME as SONG_META_FILENAME
from app.music_recording import RawMidiRecorder, SongMeta, SyncInfo, save_raw_midi_log, song_dir
from app.profiles import DATA_DIR as PROFILE_DATA_DIR
from app.quiz import (
    META_FILENAME as QUIZ_META_FILENAME,
)
from app.quiz import (
    RAW_MIDI_FILENAME,
    RAW_NOTES_FILENAME,
    RAW_SYNC_FILENAME,
    RAW_VIDEO_FILENAME,
    RESULTS_FILENAME,
    QuizMeta,
    QuizResult,
    QuizTarget,
    load_quiz_targets,
    quiz_dir,
    quiz_raw_dir,
    sanitize_quiz_name,
    save_quiz_results,
    summarize,
)
from app.song_library import SongEntry, find_song_entry, list_song_entries
from common.led_controller import LEDArrayController
from note_audio import DEFAULT_TIMBRE, TIMBRES, NoteAudioPlayer
from note_led_map import WHITE_LEDS

LED_FLASH_DELAY_MS = 300
LED_FLASH_DURATION_MS = 800

# The sync mark only needs one LED to flash, not the whole rig - the first
# white key is as good a landmark as any for finding it in the footage.
SYNC_LED_STRIP, SYNC_LED_IDX = WHITE_LEDS[0][0]
COUNTDOWN_S = 5.0
GAP_S = 0.4  # brief pause between one note's result and the next cue


class QuizWindow(QMainWindow):
    def __init__(self, cfg: Config, guidance_type: str = "visual"):
        super().__init__()
        self.guidance_type = guidance_type
        label = "Visual" if guidance_type == "visual" else "Haptic"
        self.setWindowTitle(f"Student Quiz - {label} Guidance")
        self.cfg = cfg

        # The cue style is chosen ahead of time in the launcher's "Visual
        # Guidance Cue Selection" window and persisted in config.json -
        # read it from there instead of asking again on every quiz launch.
        # An unknown stored value (hand-edited config, older config file)
        # falls back to the default rather than erroring.
        cue_style = None
        if guidance_type == "visual":
            cue_style = cfg.visual_cue_style if cfg.visual_cue_style in CUE_STYLES else DEFAULT_CUE_STYLE

        self.camera = Camera(cfg.camera)

        # The cue is shown through this interface only - this is the one
        # place that knows whether "which finger" is conveyed on screen or
        # by a vibration motor; everything below just calls show_target()/
        # show_message()/clear(). Subclasses override _make_cue() to supply
        # a different CueOutput (the pilot study's experiment runner routes
        # all three feedback conditions through one shared cue window).
        self.cue = self._make_cue(guidance_type, cue_style)
        self.cue.show_message("Pick a song and press Start.")

        self.led = LEDArrayController()
        self.led_connected = False

        self.song_meta: Optional[SongMeta] = None
        self.current_song_name: str = ""  # bare name (no "music/"/"sequence/" label prefix), for QuizMeta.song_name
        self.targets: list[QuizTarget] = []
        self.mapping: Optional[MidiMapping] = None
        self.led_mapper = None

        self.midi_recorder: Optional[RawMidiRecorder] = None
        self.audio: Optional[NoteAudioPlayer] = None
        self.video_writer: Optional[cv2.VideoWriter] = None
        self.video_path: Optional[Path] = None
        self.raw_events: list = []
        self.video_start_time: Optional[float] = None
        self._video_fps: float = 0.0
        self._frames_written: int = 0
        self.led_on_time: Optional[float] = None
        self.led_off_time: Optional[float] = None

        self.quiz_name = ""
        self.timeout_s = 5.0
        self.results: list[QuizResult] = []
        self.current_index = 0
        self.cue_onset_time = 0.0
        self.lit_note: Optional[int] = None
        self.phase = "idle"  # idle -> flashing -> countdown -> presenting -> gap -> idle
        self.phase_start_wall = 0.0
        self._analysis_window: Optional[QuizAnalysisWindow] = None

        # ------------------------------------------------------------ UI
        self.song_combo = QComboBox()
        self.song_combo.currentTextChanged.connect(self._load_song)
        song_refresh_btn = QPushButton("Refresh")
        song_refresh_btn.clicked.connect(self._refresh_songs)

        self.quiz_name_edit = QLineEdit()
        self.quiz_name_edit.setPlaceholderText("Quiz name")

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(1.0, 30.0)
        self.timeout_spin.setSingleStep(0.5)
        self.timeout_spin.setValue(5.0)
        self.timeout_spin.setSuffix(" s")

        self.port_combo = QComboBox()
        port_refresh_btn = QPushButton("Refresh")
        port_refresh_btn.clicked.connect(self._refresh_ports)

        self.led_connect_btn = QPushButton("Connect LED")
        self.led_connect_btn.clicked.connect(self._toggle_led)
        self.led_status = QLabel("LED: not connected (required to start)")

        self.timbre_combo = QComboBox()
        for key, timbre in TIMBRES.items():
            self.timbre_combo.addItem(timbre.name, key)
        self.timbre_combo.setCurrentIndex(max(self.timbre_combo.findData(DEFAULT_TIMBRE), 0))
        self.timbre_combo.currentIndexChanged.connect(self._on_timbre_changed)

        self.start_btn = QPushButton("Start Quiz")
        self.start_btn.setEnabled(False)  # needs the LED connected first - see _toggle_led()
        self.start_btn.clicked.connect(self._start_quiz)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_quiz)

        self.view = ImageView()
        self.status_label = QLabel("Pick a song to begin.")
        self.results_label = QLabel("")
        self.results_label.setWordWrap(True)

        song_row = QHBoxLayout()
        song_row.addWidget(QLabel("Song:"))
        song_row.addWidget(self.song_combo, 1)
        song_row.addWidget(song_refresh_btn)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Quiz name:"))
        name_row.addWidget(self.quiz_name_edit, 1)
        name_row.addWidget(QLabel("Timeout:"))
        name_row.addWidget(self.timeout_spin)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("MIDI port:"))
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(port_refresh_btn)
        port_row.addWidget(self.led_status)
        port_row.addWidget(self.led_connect_btn)
        port_row.addWidget(QLabel("Timbre:"))
        port_row.addWidget(self.timbre_combo)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.cancel_btn)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(song_row)
        layout.addLayout(name_row)
        layout.addLayout(port_row)
        layout.addWidget(self.view)
        layout.addWidget(self.status_label)
        layout.addLayout(btn_row)
        layout.addWidget(self.results_label)
        self.setCentralWidget(central)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        self._refresh_ports()
        self._refresh_songs()

    def _make_cue(self, guidance_type: str, cue_style: Optional[str]):
        if guidance_type == "haptic":
            return HapticCueOutput()
        return ScreenCueOutput(cue_style)

    # ------------------------------------------------------------------
    # Song / port / LED setup
    # ------------------------------------------------------------------

    def _refresh_songs(self) -> None:
        entries = list_song_entries()
        self.song_combo.blockSignals(True)
        self.song_combo.clear()
        self.song_combo.addItems([entry.label for entry in entries])
        self.song_combo.blockSignals(False)
        if entries:
            self._load_song(entries[0].label)
        else:
            self.status_label.setText(
                "No songs found under data/music/ or data/sequence/. Record one first, or generate one with "
                "experiment_sequence_wizard.py."
            )

    def _load_song(self, label: str) -> None:
        if not label:
            return
        entry: Optional[SongEntry] = find_song_entry(label)
        if entry is None:
            return

        # Keyboard profile always comes from config.json's active_keyboard_profile
        # (set by the Keyboard Calibration Wizard), never from the song itself -
        # a song outlives any one profile, so a value frozen into its meta.json
        # at record time would go stale the moment the keyboard is recalibrated.
        keyboard_profile_name = self.cfg.active_keyboard_profile
        try:
            self.song_meta = SongMeta.load(song_dir(entry.name, entry.data_dir) / SONG_META_FILENAME)
            self.targets = load_quiz_targets(entry.name, data_dir=entry.data_dir)
            self.mapping = MidiMapping.load(PROFILE_DATA_DIR / keyboard_profile_name / "midi_mapping.json")
        except Exception as e:
            QMessageBox.warning(self, "Couldn't load song", str(e))
            return

        self.current_song_name = entry.name

        try:
            self.led_mapper = profile_led_mapper.build_mapper(self.led, keyboard_profile_name)
        except Exception:
            self.led_mapper = None  # LED cueing just won't be available for this profile

        self.status_label.setText(
            f"{label}: {len(self.targets)} notes  |  profile {keyboard_profile_name}  |  "
            f"difficulty {self.song_meta.difficulty}"
        )

        ports = [self.port_combo.itemText(i) for i in range(self.port_combo.count())]
        if self.cfg.midi.port_name in ports:
            self.port_combo.setCurrentText(self.cfg.midi.port_name)

    def _refresh_ports(self) -> None:
        ports = list_input_ports()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        self.port_combo.blockSignals(False)

    def _toggle_led(self) -> None:
        if self.led_connected:
            if self.led_mapper is not None:
                self.led_mapper.clear_all()
            self.led.close()
            self.led_connected = False
            self.led_status.setText("LED: not connected (required to start)")
            self.led_connect_btn.setText("Connect LED")
            if self.phase == "idle":
                self.start_btn.setEnabled(False)
            return

        try:
            self.led.connect()
            self.led.off()
        except Exception as exc:
            QMessageBox.warning(self, "LED connection failed", str(exc))
            return

        self.led_connected = True
        self.led_status.setText(f"LED: connected on {self.led.port}")
        self.led_connect_btn.setText("Disconnect LED")
        if self.phase == "idle":
            self.start_btn.setEnabled(True)

    def _on_timbre_changed(self, index: int) -> None:
        # Takes effect immediately if a quiz is already recording; otherwise
        # it's just remembered for the next Start Quiz.
        if self.audio is not None:
            self.audio.set_timbre(self.timbre_combo.itemData(index))

    # ------------------------------------------------------------------
    # Starting a quiz
    # ------------------------------------------------------------------

    def _start_quiz(self) -> None:
        if not self.led_connected:
            QMessageBox.warning(self, "LED not connected", "Connect the LED strip before starting a quiz.")
            return

        if not self.targets:
            QMessageBox.warning(self, "No song loaded", "Pick a recorded song first.")
            return

        quiz_name = sanitize_quiz_name(self.quiz_name_edit.text())
        if not self.quiz_name_edit.text().strip():
            QMessageBox.warning(self, "Name needed", "Give this quiz attempt a name first.")
            return

        port_name = self.port_combo.currentText()
        if not port_name:
            QMessageBox.warning(self, "No MIDI port selected", "Pick a MIDI port first.")
            return

        try:
            self.midi_recorder = RawMidiRecorder(port_name)
        except RuntimeError as e:
            QMessageBox.warning(self, "MIDI connection failed", str(e))
            return

        try:
            self.audio = NoteAudioPlayer(timbre=self.timbre_combo.currentData())
        except Exception as e:
            self.audio = None
            QMessageBox.warning(
                self,
                "Audio output not available",
                f"Could not start audio playback ({e}). Continuing without sound feedback.",
            )

        r_dir = quiz_raw_dir(quiz_name)
        r_dir.mkdir(parents=True, exist_ok=True)
        self.video_path = r_dir / RAW_VIDEO_FILENAME

        frame = self.camera.read()
        if frame is None:
            QMessageBox.warning(self, "Camera not available", "Could not read a frame from the camera.")
            self.midi_recorder.close()
            self.midi_recorder = None
            return

        h, w = frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        fps = self.cfg.camera.fps or 30
        self.video_writer = cv2.VideoWriter(str(self.video_path), fourcc, fps, (w, h))
        self._video_fps = fps
        self._frames_written = 0

        self.quiz_name = quiz_name
        self.timeout_s = self.timeout_spin.value()
        self.raw_events = []
        self.results = []
        self.current_index = 0
        self.led_on_time = None
        self.led_off_time = None
        self.video_start_time = time.time()

        self.results_label.setText("")
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        for w_ in (self.song_combo, self.quiz_name_edit, self.port_combo, self.timeout_spin):
            w_.setEnabled(False)

        if self.led_connected:
            self.phase = "flashing"
            self.phase_start_wall = time.time()
            self.status_label.setText("Recording started - flashing the first key's LED for sync...")
            self.cue.show_message("Get ready...")
            QTimer.singleShot(LED_FLASH_DELAY_MS, self._flash_leds_on)
        else:
            self._begin_countdown()

    def _flash_leds_on(self) -> None:
        if self.phase != "flashing":
            return
        self.led_on_time = time.time()
        self.led.set_pixel(SYNC_LED_STRIP, SYNC_LED_IDX, 255, 255, 255, 255)
        QTimer.singleShot(LED_FLASH_DURATION_MS, self._flash_leds_off)

    def _flash_leds_off(self) -> None:
        if self.phase != "flashing":
            return
        self.led.set_pixel(SYNC_LED_STRIP, SYNC_LED_IDX, 0, 0, 0, 0)
        self.led_off_time = time.time()
        self._begin_countdown()

    def _begin_countdown(self) -> None:
        self.phase = "countdown"
        self.phase_start_wall = time.time()
        self.status_label.setText("Get ready...")
        self.cue.show_message(f"Get ready: {COUNTDOWN_S:.0f}")

    # ------------------------------------------------------------------
    # The per-note cue/response loop
    # ------------------------------------------------------------------

    def _show_current_target(self) -> None:
        target = self.targets[self.current_index]
        if self.led_connected and self.led_mapper is not None:
            self.led_mapper.light_key(target.note)
            self.lit_note = target.note
        self.cue.show_target(target.note, target.finger)
        self.cue_onset_time = time.time()
        self.phase = "presenting"
        self.status_label.setText(
            f"Note {self.current_index + 1}/{len(self.targets)}: {target.note_name} - finger {target.finger or '?'}"
        )

    def _clear_current_led(self) -> None:
        if self.led_connected and self.led_mapper is not None and self.lit_note is not None:
            self.led_mapper.clear_key(self.lit_note)
        self.lit_note = None

    def _record_result(
        self,
        timed_out: bool,
        actual_note: Optional[int] = None,
        keypress_time: Optional[float] = None,
    ) -> None:
        target = self.targets[self.current_index]
        actual_key_id = self.mapping.key_for_note(actual_note) if (actual_note is not None and self.mapping) else None
        timing_error = keypress_time - self.cue_onset_time if keypress_time is not None else None

        self.results.append(
            QuizResult(
                index=target.index,
                target_note=target.note,
                target_note_name=target.note_name,
                target_key_id=target.key_id,
                target_finger=target.finger,
                cue_onset_time=self.cue_onset_time,
                timed_out=timed_out,
                actual_note=actual_note,
                actual_key_id=actual_key_id,
                keypress_time=keypress_time,
                timing_error_s=timing_error,
                note_correct=(actual_note == target.note) if not timed_out else False,
            )
        )

        self._clear_current_led()
        self.cue.clear()
        self.current_index += 1
        # Same short breathing room after every note, including the last -
        # otherwise the final note's audio gets cut off mid-tone because
        # _finish_quiz() tears down the audio stream practically the same
        # instant the note starts sounding.
        self.phase = "gap"
        self.phase_start_wall = time.time()

    def _write_video_frame(self, frame) -> None:
        # The camera's real delivery rate can lag the fps the writer was
        # opened with (autoexposure, USB bandwidth, ...), and this tick only
        # runs when a frame actually arrives - so writing exactly one frame
        # per tick would under-fill the video relative to its declared fps,
        # making the encoded clip play back faster than the session actually
        # took. app.offline also maps MIDI event times to frames via
        # frame_idx = event_time * fps, so that drift silently breaks
        # video/MIDI sync too. Duplicate the latest frame as needed so the
        # written frame count always matches real elapsed time * fps
        # (capped to one second of catch-up per tick, so a one-off stall -
        # e.g. the process being paused - can't spend seconds re-encoding
        # the same frame in a tight loop).
        elapsed = time.time() - self.video_start_time
        target_frames = int(elapsed * self._video_fps) + 1
        target_frames = min(target_frames, self._frames_written + int(self._video_fps) + 1)
        while self._frames_written < target_frames:
            self.video_writer.write(frame)
            self._frames_written += 1

    def _tick(self) -> None:
        frame = self.camera.read()
        if frame is not None:
            self.view.set_frame(frame)
            if self.video_writer is not None:
                self._write_video_frame(frame)

        if self.midi_recorder is not None:
            new_events = self.midi_recorder.pop_events()
            self.raw_events.extend(new_events)
            if self.audio is not None:
                for e in new_events:
                    if e.type == "note_on":
                        self.audio.play_key(e.note)
                    else:
                        self.audio.stop_key(e.note)

        if self.phase == "countdown":
            remaining = COUNTDOWN_S - (time.time() - self.phase_start_wall)
            if remaining <= 0:
                self.current_index = 0
                self._show_current_target()
            else:
                self.cue.show_message(f"Get ready: {remaining:.0f}")

        elif self.phase == "presenting":
            # The phase only ever advances forward, so any note_on at or
            # after this cue's onset is a fresh response - a plain scan
            # over the (small) event list is plenty at this event rate.
            note_on = next(
                (e for e in self.raw_events if e.type == "note_on" and e.abs_time >= self.cue_onset_time),
                None,
            )
            if note_on is not None:
                self._record_result(timed_out=False, actual_note=note_on.note, keypress_time=note_on.abs_time)
            elif time.time() - self.cue_onset_time >= self.timeout_s:
                self._record_result(timed_out=True)

        elif self.phase == "gap":
            if time.time() - self.phase_start_wall >= GAP_S:
                if self.current_index >= len(self.targets):
                    self._finish_quiz()
                else:
                    self._show_current_target()

    # ------------------------------------------------------------------
    # Finishing / cancelling
    # ------------------------------------------------------------------

    def _release_session(self) -> None:
        if self.midi_recorder is not None:
            self.raw_events.extend(self.midi_recorder.pop_events())
            self.midi_recorder.close()
            self.midi_recorder = None
        if self.audio is not None:
            self.audio.stop_all()
            # Let the current timbre's release fade finish before tearing
            # down the stream, not just a fixed guess - release_ms varies
            # per timbre (8ms for sine, up to 200ms for electric piano).
            release_s = TIMBRES[self.audio.timbre_name].release_ms / 1000.0
            time.sleep(release_s + 0.05)
            self.audio.close()
            self.audio = None
        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
        self._clear_current_led()

    def _unlock_inputs(self) -> None:
        self.start_btn.setEnabled(self.led_connected)
        self.cancel_btn.setEnabled(False)
        for w_ in (self.song_combo, self.quiz_name_edit, self.port_combo, self.timeout_spin):
            w_.setEnabled(True)

    def _cancel_quiz(self) -> None:
        self._release_session()
        self.phase = "idle"
        self.cue.show_message("Cancelled - pick a song and press Start.")
        self.status_label.setText("Quiz cancelled - nothing was saved.")
        self._unlock_inputs()

    def _finish_quiz(self) -> None:
        # Grabbed before _release_session() drops the recorder - the MIDI
        # clock starts when the recorder is constructed, a moment before
        # video_start_time (audio/camera/writer setup sits in between), and
        # offline analysis needs that gap from sync.json to line MIDI events
        # up with the right video frames.
        midi_start_time = self.midi_recorder.start_time if self.midi_recorder else self.video_start_time

        self._release_session()
        self.phase = "idle"
        self.cue.show_message("Recording complete - see the analysis window for finger accuracy.")
        self.status_label.setText("Saving recording...")

        r_dir = quiz_raw_dir(self.quiz_name)
        save_raw_midi_log(self.raw_events, r_dir / RAW_MIDI_FILENAME)

        sync = SyncInfo(
            video_start_time=self.video_start_time,
            midi_start_time=midi_start_time,
            led_on_time=self.led_on_time or 0.0,
            led_off_time=self.led_off_time or 0.0,
        )
        sync.save(r_dir / RAW_SYNC_FILENAME)

        pressed = [r for r in self.results if not r.timed_out]
        notes_for_analysis = [MidiEvent(time=r.keypress_time, note=r.actual_note) for r in pressed]
        save_midi_log(notes_for_analysis, r_dir / RAW_NOTES_FILENAME)

        # Note Accuracy and Timing Error don't need the video at all, so
        # they're already final here - only Finger Accuracy is pending,
        # filled in by the separate (reusable) analysis window below.
        s_dir = quiz_dir(self.quiz_name)
        save_quiz_results(self.results, s_dir / RESULTS_FILENAME)

        summary = summarize(self.results)
        meta = QuizMeta(
            quiz_name=self.quiz_name,
            song_name=self.current_song_name,
            keyboard_profile_name=self.cfg.active_keyboard_profile,
            port_name=self.port_combo.currentText(),
            created_at=time.time(),
            timeout_s=self.timeout_s,
            note_count=len(self.results),
            hits=summary["hits"],
            misses=summary["misses"],
            note_accuracy=summary["note_accuracy"],
            mean_timing_error_s=summary["mean_timing_error_s"],
            finger_accuracy=None,
            guidance_type=self.guidance_type,
            analyzed=False,
        )
        meta.save(s_dir / QUIZ_META_FILENAME)

        self.results_label.setText(
            f"Note Accuracy: {summary['note_accuracy'] * 100:.0f}% ({summary['hits']}/{len(self.results)}, "
            f"{summary['misses']} missed)\n"
            f"Saved to {s_dir} - see the analysis window for Finger Accuracy."
        )
        self.status_label.setText("Quiz finished.")
        self._unlock_inputs()

        # Finger-matching is identical regardless of how the cue was shown,
        # so it lives in its own reusable window instead of here - see
        # app/gui/quiz_analysis_window.py. Overridable: the pilot study's
        # experiment runner suppresses the popup mid-session and batches
        # analysis afterwards instead.
        self._open_analysis_window()

    def _open_analysis_window(self) -> None:
        self._analysis_window = QuizAnalysisWindow(self.cfg, initial_quiz_name=self.quiz_name)
        self._analysis_window.show()

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self.midi_recorder is not None:
            self.midi_recorder.close()
        if self.audio is not None:
            self.audio.close()
        if self.video_writer is not None:
            self.video_writer.release()
        if self.led_connected:
            self.led.close()
        self.cue.close()
        self.camera.release()
        super().closeEvent(event)


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = QuizWindow(cfg)
    window.resize(1000, 780)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
