"""Tele-training's private Teacher music-recording wizard.

This is intentionally a copy of the section 3 Recording & Playback flow,
not an import or subclass of ``app.gui.recording_wizard.RecordingWizard``.
Tele-training can therefore evolve its device ownership and UI without
changing the experiment-wide recording tool.

The important role-specific difference is that devices are read-only here:
camera, MIDI input and keyboard profile all come from the ``Config`` view
created by ``teacher_config()`` from Teacher Settings.  The wizard cannot
silently switch to the launcher's ordinary setup or choose a different MIDI
port just for one recording.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import cv2
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGroupBox,
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

from app.camera import Camera
from app.config import Config
from app.gui.analyze_worker import AnalyzeWorker
from app.gui.image_view import ImageView
from app.midi import save_midi_log
from app.music_recording import (
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
from app.profiles import DATA_DIR as PROFILE_DATA_DIR
from app.profiles import list_profiles, snapshot_profile
from app.sequence_generator import LEVEL_DIFFICULTY, LEVEL_DISPLAY, LEVELS
from common.led_controller import LEDArrayController
from note_audio import DEFAULT_TIMBRE, TIMBRES, NoteAudioPlayer
from note_led_map import WHITE_LEDS

from ..client_styles import TEACHER_STATUS_STYLES, TEACHER_STYLE_SHEET

log = logging.getLogger("remote_guidance.teacher.recording_wizard")

PAGE_INFO, PAGE_RECORD, PAGE_REVIEW = range(3)

LED_FLASH_DELAY_MS = 300
LED_FLASH_DURATION_MS = 800
SYNC_LED_PIXELS = [key[0] for key in WHITE_LEDS[:5]]


class TeacherRecordingInfoPage(QWizardPage):
    """Song metadata plus a read-only audit of Teacher Settings."""

    def __init__(self, wizard: "TeacherRecordingWizard"):
        super().__init__()
        self.setTitle("1  /  Recording details")
        self.setSubTitle("Name the performance and confirm the Teacher devices that will be used.")
        self._wizard = wizard

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Song title")
        self.title_edit.textChanged.connect(self.completeChanged)

        self.difficulty_combo = QComboBox()
        for level in LEVELS:
            self.difficulty_combo.addItem(LEVEL_DISPLAY[level], LEVEL_DIFFICULTY[level])

        details_box = QGroupBox("Song")
        details_layout = QVBoxLayout(details_box)
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("Song title:"))
        title_row.addWidget(self.title_edit, 1)
        details_layout.addLayout(title_row)
        difficulty_row = QHBoxLayout()
        difficulty_row.addWidget(QLabel("Difficulty (label only):"))
        difficulty_row.addWidget(self.difficulty_combo)
        difficulty_row.addStretch(1)
        details_layout.addLayout(difficulty_row)
        details_layout.addStretch(1)

        self.camera_value = QLabel()
        self.midi_value = QLabel()
        self.profile_value = QLabel()
        for label in (self.camera_value, self.midi_value, self.profile_value):
            label.setWordWrap(True)

        devices_box = QGroupBox("Teacher Settings · read only")
        devices_layout = QVBoxLayout(devices_box)
        devices_layout.addWidget(QLabel("Camera"))
        devices_layout.addWidget(self.camera_value)
        devices_layout.addWidget(QLabel("MIDI keyboard"))
        devices_layout.addWidget(self.midi_value)
        devices_layout.addWidget(QLabel("Keyboard profile"))
        devices_layout.addWidget(self.profile_value)
        device_note = QLabel(
            "Change these only from Teacher Settings. This recording wizard never reads the launcher's "
            "ordinary camera, MIDI or active profile."
        )
        device_note.setObjectName("mutedText")
        device_note.setWordWrap(True)
        devices_layout.addWidget(device_note)
        devices_layout.addStretch(1)

        columns = QHBoxLayout(self)
        columns.setContentsMargins(8, 8, 8, 8)
        columns.setSpacing(10)
        columns.addWidget(details_box, 1)
        columns.addWidget(devices_box, 1)

    def initializePage(self) -> None:
        camera = self._wizard.cfg.camera
        self.camera_value.setText(
            f"Camera {camera.index!r} · {camera.width} × {camera.height} @ {camera.fps} fps"
        )
        port = self._wizard.cfg.midi.port_name
        self.midi_value.setText(port or "No Teacher MIDI keyboard is selected.")
        profile = self._wizard.cfg.active_keyboard_profile
        if profile in list_profiles(PROFILE_DATA_DIR):
            self.profile_value.setText(profile)
            self.profile_value.setStyleSheet(TEACHER_STATUS_STYLES["ok"])
        else:
            self.profile_value.setText(f"{profile!r} is not a calibrated profile.")
            self.profile_value.setStyleSheet(TEACHER_STATUS_STYLES["error"])
        self.completeChanged.emit()

    def song_name(self) -> str:
        return self.title_edit.text().strip()

    def difficulty(self) -> int:
        return int(self.difficulty_combo.currentData())

    def difficulty_display(self) -> str:
        return self.difficulty_combo.currentText()

    def keyboard_profile_name(self) -> str:
        return self._wizard.cfg.active_keyboard_profile

    def port_name(self) -> str:
        return self._wizard.cfg.midi.port_name or ""

    def isComplete(self) -> bool:
        return bool(
            self.song_name()
            and self.port_name()
            and self.keyboard_profile_name() in list_profiles(PROFILE_DATA_DIR)
        )


class TeacherRecordingRecordPage(QWizardPage):
    """Camera preview, capture controls and section 3-compatible raw data."""

    def __init__(self, wizard: "TeacherRecordingWizard"):
        super().__init__()
        self.setTitle("2  /  Record the performance")
        self.setSubTitle(
            "Start flashes the first white keys for video/MIDI synchronisation. Wait for the flash, play, then Stop."
        )
        self._wizard = wizard

        self.view = ImageView(max_size=(560, 360))
        camera_box = QGroupBox("Teacher camera preview")
        camera_box.setObjectName("cameraCard")
        camera_layout = QVBoxLayout(camera_box)
        camera_layout.addWidget(self.view, 1, Qt.AlignmentFlag.AlignCenter)

        self.status_label = QLabel("Not recording.")
        self.status_label.setObjectName("midiMonitor")
        self.status_label.setWordWrap(True)
        self.stats_label = QLabel("0 notes captured")
        self.stats_label.setObjectName("mutedText")

        self.timbre_combo = QComboBox()
        for key, timbre in TIMBRES.items():
            self.timbre_combo.addItem(timbre.name, key)
        self.timbre_combo.setCurrentIndex(max(self.timbre_combo.findData(DEFAULT_TIMBRE), 0))
        self.timbre_combo.currentIndexChanged.connect(self._on_timbre_changed)

        self.start_btn = QPushButton("Start recording")
        self.start_btn.setProperty("role", "primary")
        self.stop_btn = QPushButton("Stop recording")
        self.stop_btn.setProperty("role", "danger")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

        controls_box = QGroupBox("Capture controls")
        controls = QVBoxLayout(controls_box)
        controls.addWidget(self.status_label)
        controls.addWidget(self.stats_label)
        timbre_row = QHBoxLayout()
        timbre_row.addWidget(QLabel("Instrument:"))
        timbre_row.addWidget(self.timbre_combo, 1)
        controls.addLayout(timbre_row)
        controls.addStretch(1)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        layout.addWidget(camera_box, 3)
        layout.addWidget(controls_box, 2)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.led: LEDArrayController | None = None
        self.midi_recorder: RawMidiRecorder | None = None
        self.audio: NoteAudioPlayer | None = None
        self.video_writer: cv2.VideoWriter | None = None
        self.video_path: Path | None = None
        self.raw_events: list = []
        self.video_start_time: float | None = None
        self._video_fps = 0.0
        self._frames_written = 0
        self.midi_start_time: float | None = None
        self.led_on_time: float | None = None
        self.led_off_time: float | None = None
        self.duration_s = 0.0
        self._recording = False
        self._finished = False

    def set_polling(self, active: bool) -> None:
        if active:
            self._timer.start(33)
        else:
            self._timer.stop()

    def _on_timbre_changed(self, index: int) -> None:
        if self.audio is not None:
            self.audio.set_timbre(self.timbre_combo.itemData(index))

    def _start(self) -> None:
        info = self._wizard.info_page
        song = sanitize_song_name(info.song_name())
        self.video_path = raw_dir(song) / RAW_VIDEO_FILENAME
        self.video_path.parent.mkdir(parents=True, exist_ok=True)

        self.led = LEDArrayController()
        try:
            self.led.connect()
        except Exception as exc:  # noqa: BLE001 - recording remains usable without a flash
            self.led = None
            QMessageBox.warning(
                self,
                "LED controller not connected",
                f"Could not connect to the LED controller ({exc}).\n\n"
                "Continuing without the sync flash; video/MIDI timestamps are still recorded.",
            )

        try:
            self.midi_recorder = RawMidiRecorder(info.port_name())
        except RuntimeError as exc:
            QMessageBox.warning(self, "MIDI connection failed", str(exc))
            self._close_led()
            return

        try:
            self.audio = NoteAudioPlayer(timbre=self.timbre_combo.currentData())
        except Exception as exc:  # noqa: BLE001 - recording does not depend on audio feedback
            self.audio = None
            QMessageBox.warning(
                self,
                "Audio output not available",
                f"Could not start audio playback ({exc}). Continuing without sound feedback.",
            )

        frame = self._wizard.camera.read()
        if frame is None:
            QMessageBox.warning(self, "Camera not available", "Could not read a frame from the Teacher camera.")
            self.release_capture_devices()
            return

        height, width = frame.shape[:2]
        fps = self._wizard.cfg.camera.fps or 30
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.video_writer = cv2.VideoWriter(str(self.video_path), fourcc, fps, (width, height))
        self._video_fps = fps
        self._frames_written = 0
        self.raw_events = []
        self.led_on_time = None
        self.led_off_time = None
        self.midi_start_time = self.midi_recorder.start_time
        self.video_start_time = time.time()
        self._recording = True
        self._finished = False
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Recording · preparing sync flash...")

        if self.led is not None:
            QTimer.singleShot(LED_FLASH_DELAY_MS, self._flash_leds_on)
        else:
            QTimer.singleShot(LED_FLASH_DELAY_MS, self._show_play_message)

    def _show_play_message(self) -> None:
        if self._recording:
            self.status_label.setText("Recording · play the piece now.")

    def _flash_leds_on(self) -> None:
        if not self._recording or self.led is None:
            return
        self.led_on_time = time.time()
        for strip, index in SYNC_LED_PIXELS:
            self.led.set_pixel(strip, index, 255, 255, 255, 255)
        self.status_label.setText("Recording · sync flash on...")
        QTimer.singleShot(LED_FLASH_DURATION_MS, self._flash_leds_off)

    def _flash_leds_off(self) -> None:
        if not self._recording or self.led is None:
            return
        first_strip, first_index = SYNC_LED_PIXELS[0]
        self.led.set_pixel(first_strip, first_index, 0, 0, 0, 0)
        self.led_off_time = time.time()
        for strip, index in SYNC_LED_PIXELS[1:]:
            self.led.set_pixel(strip, index, 0, 0, 0, 0)
        self.status_label.setText("Recording · play the piece now.")

    def _write_video_frame(self, frame) -> None:
        elapsed = time.time() - (self.video_start_time or time.time())
        target_frames = min(
            int(elapsed * self._video_fps) + 1,
            self._frames_written + int(self._video_fps) + 1,
        )
        while self._frames_written < target_frames:
            self.video_writer.write(frame)
            self._frames_written += 1

    def _tick(self) -> None:
        frame = self._wizard.camera.read()
        if frame is None:
            return
        self.view.set_frame(frame)
        if not self._recording:
            return
        if self.video_writer is not None:
            self._write_video_frame(frame)
        if self.midi_recorder is not None:
            events = self.midi_recorder.pop_events()
            self.raw_events.extend(events)
            if self.audio is not None:
                for event in events:
                    if event.type == "note_on":
                        self.audio.play_key(event.note)
                    else:
                        self.audio.stop_key(event.note)
        note_count = sum(1 for event in self.raw_events if event.type == "note_on")
        elapsed = time.time() - (self.video_start_time or time.time())
        self.stats_label.setText(f"{elapsed:.1f}s elapsed · {note_count} notes captured")

    def _stop(self) -> None:
        self._recording = False
        self.duration_s = time.time() - (self.video_start_time or time.time())
        if self.midi_recorder is not None:
            self.raw_events.extend(self.midi_recorder.pop_events())
        self.release_capture_devices()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._finished = True
        note_count = sum(1 for event in self.raw_events if event.type == "note_on")
        self.status_label.setText(f"Stopped · {note_count} notes · {self.duration_s:.1f}s recorded")
        self.completeChanged.emit()

    def _close_led(self) -> None:
        if self.led is not None:
            led, self.led = self.led, None
            try:
                led.close()
            except Exception:  # noqa: BLE001 - continue releasing the other devices
                log.exception("closing the recording LED controller failed")

    def release_capture_devices(self) -> None:
        """Release only capture resources; the preview camera belongs to the wizard."""
        if self.midi_recorder is not None:
            recorder, self.midi_recorder = self.midi_recorder, None
            try:
                recorder.close()
            except Exception:  # noqa: BLE001
                log.exception("closing the recording MIDI input failed")
        if self.audio is not None:
            audio, self.audio = self.audio, None
            try:
                audio.stop_all()
            except Exception:  # noqa: BLE001
                log.exception("stopping recording audio voices failed")
            try:
                audio.close()
            except Exception:  # noqa: BLE001
                log.exception("closing recording audio output failed")
        if self.video_writer is not None:
            writer, self.video_writer = self.video_writer, None
            try:
                writer.release()
            except Exception:  # noqa: BLE001
                log.exception("closing the recording video writer failed")
        self._close_led()

    def close_devices(self) -> None:
        self._timer.stop()
        self._recording = False
        self.release_capture_devices()

    def isComplete(self) -> bool:
        return self._finished and any(event.type == "note_on" for event in self.raw_events)


class TeacherRecordingReviewPage(QWizardPage):
    """Compute fingers and write the same data/music layout as section 3."""

    def __init__(self, wizard: "TeacherRecordingWizard"):
        super().__init__()
        self.setTitle("3  /  Review and save")
        self.setSubTitle("Compute per-note fingering from the recording and save it to the shared music library.")
        self._wizard = wizard

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("midiMonitor")
        self.save_btn = QPushButton("Compute fingering and save")
        self.save_btn.setProperty("role", "primary")
        self.save_btn.clicked.connect(self._save)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)

        box = QGroupBox("Recording summary")
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(self.summary_label)
        box_layout.addWidget(self.save_btn)
        box_layout.addWidget(self.progress_bar)
        box_layout.addWidget(self.result_label)
        box_layout.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(box)

        self._saved = False
        self._worker: AnalyzeWorker | None = None
        self._notes = []
        self._trimmed_notes = []
        self._duration_s = 0.0
        self._save_dir: Path | None = None

    def initializePage(self) -> None:
        info = self._wizard.info_page
        record = self._wizard.record_page
        note_count = sum(1 for event in record.raw_events if event.type == "note_on")
        self.summary_label.setText(
            f"Song: {info.song_name()!r}\n"
            f"Difficulty: {info.difficulty_display()} · Profile: {info.keyboard_profile_name()}\n"
            f"Duration: {record.duration_s:.1f}s · Notes captured: {note_count}"
        )
        self._saved = False
        self.save_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.result_label.clear()
        self.completeChanged.emit()

    def _set_back_enabled(self, enabled: bool) -> None:
        button = self._wizard.button(QWizard.WizardButton.BackButton)
        if button is not None:
            button.setEnabled(enabled)

    def _save(self) -> None:
        info = self._wizard.info_page
        record = self._wizard.record_page
        song = sanitize_song_name(info.song_name())
        self._save_dir = song_dir(song)
        raw_path = raw_dir(song)
        raw_path.mkdir(parents=True, exist_ok=True)
        self.save_btn.setEnabled(False)
        self._set_back_enabled(False)
        self.result_label.setText("Saving raw log...")
        QApplication.processEvents()

        try:
            snapshot_profile(info.keyboard_profile_name(), raw_path / "keyboard_profile")
            save_raw_midi_log(record.raw_events, raw_path / RAW_MIDI_FILENAME)
            self._notes = notes_only(record.raw_events)
            notes_path = raw_path / RAW_NOTES_FILENAME
            save_midi_log(self._notes, notes_path)
            sync = SyncInfo(
                video_start_time=record.video_start_time,
                midi_start_time=record.midi_start_time or record.video_start_time,
                led_on_time=record.led_on_time or 0.0,
                led_off_time=record.led_off_time or 0.0,
            )
            sync_path = raw_path / RAW_SYNC_FILENAME
            sync.save(sync_path)
            lead_in = first_note_on_time(record.raw_events)
            trimmed_events = trim_to_first_note(record.raw_events)
            self._trimmed_notes = notes_only(trimmed_events)
            self._duration_s = max(record.duration_s - lead_in, 0.0)
            build_score_midi(trimmed_events, self._save_dir / SCORE_MIDI_FILENAME)
        except Exception as exc:  # noqa: BLE001 - all failures are shown in the wizard
            self.save_btn.setEnabled(True)
            self._set_back_enabled(True)
            self.result_label.clear()
            QMessageBox.critical(self, "Save failed", str(exc))
            return

        if not self._notes:
            self._finish_save([])
            return

        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.result_label.setText("Matching fingers against the video...")
        self._worker = AnalyzeWorker(
            record.video_path,
            notes_path,
            info.keyboard_profile_name(),
            sync_path=sync_path,
        )
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
        self.result_label.clear()
        QMessageBox.critical(self, "Save failed", message)

    def _finish_save(self, matches: list) -> None:
        info = self._wizard.info_page
        try:
            entries = build_fingering_entries(self._trimmed_notes, matches)
            save_fingering(entries, self._save_dir / FINGERING_FILENAME)
            SongMeta(
                title=info.song_name(),
                difficulty=info.difficulty(),
                created_at=time.time(),
                duration_s=self._duration_s,
                note_count=len(self._notes),
            ).save(self._save_dir / META_FILENAME)
        except Exception as exc:  # noqa: BLE001
            self.progress_bar.setVisible(False)
            self.save_btn.setEnabled(True)
            self._set_back_enabled(True)
            QMessageBox.critical(self, "Save failed", str(exc))
            return

        self.progress_bar.setVisible(False)
        self._set_back_enabled(True)
        self._saved = True
        self.result_label.setText(f"Saved to {self._save_dir}")
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._saved


class TeacherRecordingWizard(QWizard):
    """Three-page recording flow owned only by Tele-training Teacher."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.setObjectName("teacherRecordingWizard")
        self.setWindowTitle("Tele-training · Teacher Music Recording")
        # ClassicStyle keeps the page title inside the themed surface.
        # ModernStyle paints its own white banner, which clashes with the
        # Teacher client's dark palette and makes the pale title unreadable.
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setOptions(QWizard.WizardOption.NoBackButtonOnLastPage)
        self.setStyleSheet(TEACHER_STYLE_SHEET)
        self.resize(1080, 720)
        self.setMinimumSize(900, 620)

        # cfg is the isolated teacher_config() view constructed by the
        # Teacher Client. Never load Config or RemoteGuidanceConfig here.
        self.cfg = cfg
        self.camera = Camera(cfg.camera)

        self.info_page = TeacherRecordingInfoPage(self)
        self.record_page = TeacherRecordingRecordPage(self)
        self.review_page = TeacherRecordingReviewPage(self)
        self.setPage(PAGE_INFO, self.info_page)
        self.setPage(PAGE_RECORD, self.record_page)
        self.setPage(PAGE_REVIEW, self.review_page)
        self.setStartId(PAGE_INFO)
        self.currentIdChanged.connect(self._on_page_changed)

    def _on_page_changed(self, page_id: int) -> None:
        self.record_page.set_polling(page_id == PAGE_RECORD)

    def closeEvent(self, event) -> None:
        self.record_page.close_devices()
        try:
            self.camera.release()
        except Exception:  # noqa: BLE001
            log.exception("closing the Teacher recording camera failed")
        super().closeEvent(event)
