"""PySide6 wizard for step 3: teacher records a song.

Three pages:
  0. Song info - title, difficulty (1/2/3, a label only), keyboard
     profile, MIDI port.
  1. Record - starting it opens the LED controller + MIDI port and begins
     writing video, then (once both are rolling) flashes the first white
     key's backlight on and back off once for video/MIDI sync - see
     app.music_recording.SyncInfo - before the teacher performs the piece.
     Stop ends capture.
  2. Review & save - runs app.offline.analyze_recording over the just
     recorded video + MIDI log to work out which finger played each note,
     then writes everything under data/music/<song>/ (see
     app/music_recording.py for the exact file layout).
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from common.led_controller import LEDArrayController
from note_audio import DEFAULT_TIMBRE, TIMBRES, NoteAudioPlayer
from note_led_map import WHITE_LEDS

from ..camera import Camera
from ..config import Config
from ..midi import list_input_ports, save_midi_log
from ..profiles import DATA_DIR as PROFILE_DATA_DIR
from ..profiles import list_profiles
from .analyze_worker import AnalyzeWorker
from ..music_recording import (
    FINGERING_FILENAME,
    META_FILENAME,
    RAW_MIDI_FILENAME,
    RAW_NOTES_FILENAME,
    RAW_SYNC_FILENAME,
    RAW_VIDEO_FILENAME,
    SCORE_MIDI_FILENAME,
    RawMidiRecorder,
    SongMeta,
    SyncInfo,
    build_fingering_entries,
    build_score_midi,
    first_note_on_time,
    notes_only,
    raw_dir,
    sanitize_song_name,
    save_fingering,
    save_raw_midi_log,
    song_dir,
    trim_to_first_note,
)
from .image_view import ImageView

PAGE_INFO, PAGE_RECORD, PAGE_REVIEW = range(3)

LED_FLASH_DELAY_MS = 300  # let a couple of frames record before the flash, so it isn't cut off
LED_FLASH_DURATION_MS = 800

# The sync mark only needs one LED to flash, not the whole rig - the first
# white key is as good a landmark as any for finding it in the footage.
SYNC_LED_STRIP, SYNC_LED_IDX = WHITE_LEDS[0][0]


class InfoPage(QWizardPage):
    def __init__(self, wizard: "RecordingWizard"):
        super().__init__()
        self.setTitle("Step 3a - Song info")
        self.setSubTitle("Name the song, pick a difficulty label, the keyboard profile, and the MIDI port.")
        self._wizard = wizard

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Song title")
        self.title_edit.textChanged.connect(self.completeChanged)

        self.difficulty_combo = QComboBox()
        self.difficulty_combo.addItems(["1", "2", "3"])

        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self.completeChanged)
        refresh_profiles_btn = QPushButton("Refresh")
        refresh_profiles_btn.clicked.connect(self._refresh_profiles)

        self.port_combo = QComboBox()
        self.port_combo.currentTextChanged.connect(self.completeChanged)
        refresh_ports_btn = QPushButton("Refresh")
        refresh_ports_btn.clicked.connect(self._refresh_ports)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("Song title:"))
        title_row.addWidget(self.title_edit, 1)

        difficulty_row = QHBoxLayout()
        difficulty_row.addWidget(QLabel("Difficulty (1-3, label only):"))
        difficulty_row.addWidget(self.difficulty_combo)
        difficulty_row.addStretch(1)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Keyboard profile:"))
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(refresh_profiles_btn)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("MIDI port:"))
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(refresh_ports_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(title_row)
        layout.addLayout(difficulty_row)
        layout.addLayout(profile_row)
        layout.addLayout(port_row)

    def initializePage(self) -> None:
        self._refresh_profiles()
        self._refresh_ports()

    def _refresh_profiles(self) -> None:
        profiles = list_profiles(PROFILE_DATA_DIR)
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(profiles)
        if self._wizard.cfg.active_profile in profiles:
            self.profile_combo.setCurrentText(self._wizard.cfg.active_profile)
        self.profile_combo.blockSignals(False)
        self.completeChanged.emit()

    def _refresh_ports(self) -> None:
        ports = list_input_ports()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if self._wizard.cfg.midi.port_name in ports:
            self.port_combo.setCurrentText(self._wizard.cfg.midi.port_name)
        self.port_combo.blockSignals(False)
        self.completeChanged.emit()

    def song_name(self) -> str:
        return self.title_edit.text().strip()

    def difficulty(self) -> int:
        return int(self.difficulty_combo.currentText())

    def profile_name(self) -> str:
        return self.profile_combo.currentText()

    def port_name(self) -> str:
        return self.port_combo.currentText()

    def isComplete(self) -> bool:
        return bool(self.song_name() and self.profile_name() and self.port_name())


class RecordPage(QWizardPage):
    def __init__(self, wizard: "RecordingWizard"):
        super().__init__()
        self.setTitle("Step 3b - Record the performance")
        self.setSubTitle(
            "Starting the recording flashes the first white key's backlight on and off once, for "
            "video/MIDI sync - wait for it to finish, then play the piece. Stop when done."
        )
        self._wizard = wizard

        self.view = ImageView()
        self.status_label = QLabel("Not recording.")
        self.stats_label = QLabel("")

        self.start_btn = QPushButton("Start recording")
        self.stop_btn = QPushButton("Stop recording")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

        self.timbre_combo = QComboBox()
        for key, timbre in TIMBRES.items():
            self.timbre_combo.addItem(timbre.name, key)
        self.timbre_combo.setCurrentIndex(max(self.timbre_combo.findData(DEFAULT_TIMBRE), 0))
        self.timbre_combo.currentIndexChanged.connect(self._on_timbre_changed)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(QLabel("Timbre:"))
        btn_row.addWidget(self.timbre_combo)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addWidget(self.status_label)
        layout.addWidget(self.stats_label)
        layout.addLayout(btn_row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self.led: LEDArrayController | None = None
        self.midi_recorder: RawMidiRecorder | None = None
        self.audio: NoteAudioPlayer | None = None
        self.video_writer: cv2.VideoWriter | None = None
        self.video_path: Path | None = None

        self.raw_events: list = []
        self.video_start_time: float | None = None
        self.midi_start_time: float | None = None
        self.led_on_time: float | None = None
        self.led_off_time: float | None = None
        self.duration_s: float = 0.0
        self._recording = False
        self._finished = False

    def set_polling(self, active: bool) -> None:
        if active:
            self._timer.start(33)
        else:
            self._timer.stop()

    def _on_timbre_changed(self, index: int) -> None:
        # Takes effect immediately if already recording; otherwise it's
        # just remembered for the next Start recording.
        if self.audio is not None:
            self.audio.set_timbre(self.timbre_combo.itemData(index))

    # ------------------------------------------------------------------

    def _start(self) -> None:
        info = self._wizard.info_page
        song = sanitize_song_name(info.song_name())
        self.video_path = raw_dir(song) / RAW_VIDEO_FILENAME
        self.video_path.parent.mkdir(parents=True, exist_ok=True)

        self.led = LEDArrayController()
        try:
            self.led.connect()
        except Exception as e:
            self.led = None
            QMessageBox.warning(
                self,
                "LED controller not connected",
                f"Could not connect to the LED controller ({e}).\n\n"
                "Continuing without the sync flash - video/MIDI timestamps will still be captured, "
                "but there won't be a visual sync mark in the footage.",
            )

        try:
            self.midi_recorder = RawMidiRecorder(info.port_name())
        except RuntimeError as e:
            QMessageBox.warning(self, "MIDI connection failed", str(e))
            if self.led is not None:
                self.led.close()
                self.led = None
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

        frame = self._wizard.camera.read()
        if frame is None:
            QMessageBox.warning(self, "Camera not available", "Could not read a frame from the camera.")
            self.midi_recorder.close()
            self.midi_recorder = None
            if self.audio is not None:
                self.audio.close()
                self.audio = None
            if self.led is not None:
                self.led.close()
                self.led = None
            return

        h, w = frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        fps = self._wizard.cfg.camera.fps or 30
        self.video_writer = cv2.VideoWriter(str(self.video_path), fourcc, fps, (w, h))

        self.raw_events = []
        self.led_on_time = None
        self.led_off_time = None
        self.midi_start_time = self.midi_recorder.start_time
        self.video_start_time = time.time()
        self._recording = True
        self._finished = False

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Recording - preparing sync flash...")

        if self.led is not None:
            QTimer.singleShot(LED_FLASH_DELAY_MS, self._flash_leds_on)
        else:
            QTimer.singleShot(
                LED_FLASH_DELAY_MS, lambda: self.status_label.setText("Recording - play the piece now.")
            )

    def _flash_leds_on(self) -> None:
        if not self._recording or self.led is None:
            return
        self.led_on_time = time.time()
        self.led.set_pixel(SYNC_LED_STRIP, SYNC_LED_IDX, 255, 255, 255, 255)
        self.status_label.setText("Recording - sync flash on...")
        QTimer.singleShot(LED_FLASH_DURATION_MS, self._flash_leds_off)

    def _flash_leds_off(self) -> None:
        if not self._recording or self.led is None:
            return
        self.led.set_pixel(SYNC_LED_STRIP, SYNC_LED_IDX, 0, 0, 0, 0)
        self.led_off_time = time.time()
        self.status_label.setText("Recording - play the piece now.")

    def _tick(self) -> None:
        frame = self._wizard.camera.read()
        if frame is None:
            return
        self.view.set_frame(frame)

        if self._recording:
            if self.video_writer is not None:
                self.video_writer.write(frame)
            if self.midi_recorder is not None:
                new_events = self.midi_recorder.pop_events()
                self.raw_events.extend(new_events)
                if self.audio is not None:
                    for e in new_events:
                        if e.type == "note_on":
                            self.audio.play_key(e.note)
                        else:
                            self.audio.stop_key(e.note)
            note_count = sum(1 for e in self.raw_events if e.type == "note_on")
            elapsed = time.time() - self.video_start_time
            self.stats_label.setText(f"{elapsed:.1f}s elapsed - {note_count} notes captured")

    def _stop(self) -> None:
        self._recording = False
        self.duration_s = time.time() - (self.video_start_time or time.time())

        if self.midi_recorder is not None:
            self.raw_events.extend(self.midi_recorder.pop_events())
            self.midi_recorder.close()
            self.midi_recorder = None
        if self.audio is not None:
            self.audio.stop_all()
            time.sleep(0.05)  # let the release fade finish before tearing down the stream
            self.audio.close()
            self.audio = None
        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
        if self.led is not None:
            self.led.close()
            self.led = None

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._finished = True

        note_count = sum(1 for e in self.raw_events if e.type == "note_on")
        self.status_label.setText(f"Stopped - {note_count} notes, {self.duration_s:.1f}s recorded.")
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._finished and any(e.type == "note_on" for e in self.raw_events)


class ReviewPage(QWizardPage):
    def __init__(self, wizard: "RecordingWizard"):
        super().__init__()
        self.setTitle("Step 3c - Review & save")
        self.setSubTitle("Compute per-note fingering from the recording and save the song.")
        self._wizard = wizard

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.save_btn = QPushButton("Compute fingering && save")
        self.save_btn.clicked.connect(self._save)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.save_btn)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.result_label)

        self._saved = False
        self._worker: AnalyzeWorker | None = None
        self._notes = []
        self._trimmed_notes = []
        self._duration_s = 0.0
        self._save_dir: Path | None = None

    def initializePage(self) -> None:
        info = self._wizard.info_page
        record = self._wizard.record_page
        note_count = sum(1 for e in record.raw_events if e.type == "note_on")
        self.summary_label.setText(
            f"Song: {info.song_name()!r}   Difficulty: {info.difficulty()}   Profile: {info.profile_name()}\n"
            f"Duration: {record.duration_s:.1f}s   Notes captured: {note_count}"
        )
        self._saved = False
        self.save_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.result_label.setText("")
        self.completeChanged.emit()

    def _set_back_enabled(self, enabled: bool) -> None:
        back_btn = self._wizard.button(QWizard.WizardButton.BackButton)
        if back_btn is not None:
            back_btn.setEnabled(enabled)

    def _save(self) -> None:
        info = self._wizard.info_page
        record = self._wizard.record_page

        song = sanitize_song_name(info.song_name())
        self._save_dir = song_dir(song)
        r_dir = raw_dir(song)
        r_dir.mkdir(parents=True, exist_ok=True)

        self.save_btn.setEnabled(False)
        self._set_back_enabled(False)
        self.result_label.setText("Saving raw log...")
        QApplication.processEvents()

        try:
            save_raw_midi_log(record.raw_events, r_dir / RAW_MIDI_FILENAME)

            # raw/ stays on the recording's original clock (frame 0 = video/MIDI
            # capture start) since analyze_recording() below needs that to line
            # frames up with MIDI events - it's only the *saved* score/fingering
            # that gets the leading dead air (LED sync flash, then however long
            # the performer took to actually start) trimmed off, below.
            self._notes = notes_only(record.raw_events)
            notes_path = r_dir / RAW_NOTES_FILENAME
            save_midi_log(self._notes, notes_path)

            sync = SyncInfo(
                video_start_time=record.video_start_time,
                midi_start_time=record.midi_start_time or record.video_start_time,
                led_on_time=record.led_on_time or 0.0,
                led_off_time=record.led_off_time or 0.0,
            )
            sync.save(r_dir / RAW_SYNC_FILENAME)

            lead_in_s = first_note_on_time(record.raw_events)
            trimmed_events = trim_to_first_note(record.raw_events)
            self._trimmed_notes = notes_only(trimmed_events)
            self._duration_s = max(record.duration_s - lead_in_s, 0.0)

            build_score_midi(trimmed_events, self._save_dir / SCORE_MIDI_FILENAME)
        except Exception as e:
            self.save_btn.setEnabled(True)
            self._set_back_enabled(True)
            self.result_label.setText("")
            QMessageBox.critical(self, "Save failed", str(e))
            return

        if not self._notes:
            self._finish_save([])
            return

        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # indeterminate until the first progress update
        self.result_label.setText("Matching fingers against the video...")

        self._worker = AnalyzeWorker(record.video_path, notes_path, info.profile_name())
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._finish_save)
        self._worker.failed.connect(self._on_analyze_failed)
        self._worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
            self.result_label.setText(f"Matching fingers against the video... {done}/{total} frames")
        else:
            self.result_label.setText(f"Matching fingers against the video... {done} frames")

    def _on_analyze_failed(self, message: str) -> None:
        self.progress_bar.setVisible(False)
        self.save_btn.setEnabled(True)
        self._set_back_enabled(True)
        self.result_label.setText("")
        QMessageBox.critical(self, "Save failed", message)

    def _finish_save(self, matches: list) -> None:
        info = self._wizard.info_page
        record = self._wizard.record_page

        try:
            # self._trimmed_notes is the same length/order as self._notes (only
            # times are shifted), so it lines up with matches (computed against
            # the untrimmed video/notes) note-for-note.
            entries = build_fingering_entries(self._trimmed_notes, matches)
            save_fingering(entries, self._save_dir / FINGERING_FILENAME)

            meta = SongMeta(
                title=info.song_name(),
                difficulty=info.difficulty(),
                profile_name=info.profile_name(),
                port_name=info.port_name(),
                created_at=datetime.now(timezone.utc).isoformat(),
                duration_s=self._duration_s,
                note_count=len(self._notes),
            )
            meta.save(self._save_dir / META_FILENAME)
        except Exception as e:
            self.progress_bar.setVisible(False)
            self.save_btn.setEnabled(True)
            self._set_back_enabled(True)
            QMessageBox.critical(self, "Save failed", str(e))
            return

        self.progress_bar.setVisible(False)
        self._set_back_enabled(True)
        self._saved = True
        self.result_label.setText(f"Saved to {self._save_dir}")
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._saved


class RecordingWizard(QWizard):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("Song Recording Wizard")
        self.setOptions(QWizard.WizardOption.NoBackButtonOnLastPage)

        self.cfg = cfg
        self.camera = Camera(cfg.camera)

        self.info_page = InfoPage(self)
        self.record_page = RecordPage(self)
        self.review_page = ReviewPage(self)
        self.setPage(PAGE_INFO, self.info_page)
        self.setPage(PAGE_RECORD, self.record_page)
        self.setPage(PAGE_REVIEW, self.review_page)
        self.setStartId(PAGE_INFO)

        self.currentIdChanged.connect(self._on_page_changed)

    def _on_page_changed(self, page_id: int) -> None:
        self.record_page.set_polling(page_id == PAGE_RECORD)

    def closeEvent(self, event) -> None:
        self.record_page.set_polling(False)
        if self.record_page.midi_recorder is not None:
            self.record_page.midi_recorder.close()
        if self.record_page.audio is not None:
            self.record_page.audio.close()
        if self.record_page.video_writer is not None:
            self.record_page.video_writer.release()
        if self.record_page.led is not None:
            self.record_page.led.close()
        self.camera.release()
        super().closeEvent(event)
