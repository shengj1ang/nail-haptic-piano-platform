"""The Settings dialog each remote client opens - one role's devices,
nothing else.

Every client owns its own settings. The student's dialog shows the
student's camera, MIDI port and keyboard profile; the teacher's shows the
teacher's. Neither can see or change the other's, and neither is reachable
from the launcher any more.

This replaced a single "Remote Guidance Settings" window in the launcher
that carried Network, Student and Teacher tabs at once. Every setting on
it was shown to both people, three quarters of it was irrelevant to
whoever had opened it, and configuring a client meant leaving the client.

**No serial port is asked for.** The key LED strip and the vibration rig
are auto-detected and connected by the student client when a session
starts; there is nothing here to get wrong. `student.led.port` and
`student.haptic.port` still exist in config.json for a machine where
auto-detection picks the wrong board - see remote_guidance/config.py's
`serial_port_problems()`, which still refuses two identical ports - but
they are set by hand there, not through a form.

The Camera/Profile Preview button captures one frame with the values
currently visible in this form, overlays the selected profile's colored
pixel mask and key ids, and opens the result in a separate dialog. It
does not save the form or the photograph, and it refuses a resolution
mismatch rather than resizing a mask into a misleading fit.

The MIDI picker has a temporary Connect & test action. It opens only the
currently selected port and shows each note pressed, so two identical
keyboards can be identified before saving. The listener is never part of
the client session: changing the selection, saving, cancelling or closing
this dialog releases it immediately.

Only that role's slice of the `remote_guidance` block is written. The
top-level `camera`, `midi` and `active_keyboard_profile` that every
ordinary tool reads are shown here for reference and never touched -
saving a remote client's settings must not move the local quiz's camera.
`RemoteGuidanceConfig.save()` enforces that by merging into the existing
file.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.camera import Camera
from app.config import CameraConfig, Config
from app.gui.profile_preview import (
    KeyboardProfilePreviewDialog,
    load_profile_template,
    overlay_template,
)
from app.keyboard.midi_mapping import note_name
from app.keyboard.template import KeyboardTemplate
from app.midi import MidiListener, ambiguous_port_names, list_input_ports
from app.profiles import DATA_DIR as PROFILE_DATA_DIR
from app.profiles import list_profiles

from .config import ROLE_STUDENT, ROLE_TEACHER, RemoteGuidanceConfig
from .gui_common import STATUS_STYLES

ROLE_TITLES = {ROLE_STUDENT: "Student", ROLE_TEACHER: "Teacher"}
PREVIEW_CAPTURE_READS = 3


def capture_profile_preview(
    camera_config: CameraConfig,
    profile_name: str,
    profile_data_dir: Path = PROFILE_DATA_DIR,
    camera_factory: Callable[[CameraConfig], Camera] = Camera,
) -> tuple[np.ndarray, KeyboardTemplate]:
    """Capture one frame and paint the selected profile onto it.

    This is deliberately independent of the dialog so it can be checked
    without a real camera. The profile is never resized to fit a frame:
    pixel masks are the calibration, so a size mismatch is useful proof
    that the selected camera setup and profile do not belong together.
    """
    # Loaded before the camera is opened: an unusable profile should not
    # cost the user a camera claim to find out about.
    template = load_profile_template(profile_name, profile_data_dir)

    camera = camera_factory(camera_config)
    try:
        if not camera.is_opened:
            raise RuntimeError(f"Camera {camera_config.index!r} could not be opened.")
        frame = None
        # A newly opened camera often returns a dark or stale first frame;
        # keep the last of a few immediate reads as the actual snapshot.
        for _ in range(PREVIEW_CAPTURE_READS):
            candidate = camera.read()
            if candidate is not None:
                frame = candidate
    finally:
        camera.release()

    if frame is None:
        raise RuntimeError(f"Camera {camera_config.index!r} opened but did not return an image.")

    return overlay_template(frame, template, profile_name), template


class CameraGroup(QGroupBox):
    """The same six fields as app.config.CameraConfig - index, size, fps
    and the two flips - so a role's camera is described exactly the way
    every other tool in the platform describes one."""

    def __init__(self, title: str, camera: CameraConfig):
        super().__init__(title)
        self.index_edit = QLineEdit(str(camera.index))
        self.index_edit.setToolTip(
            "A camera index (0, 1, ...) or a path to a video file - cv2.VideoCapture accepts either."
        )
        self.width_spin = _spin(160, 7680, camera.width)
        self.height_spin = _spin(120, 4320, camera.height)
        self.fps_spin = _spin(1, 240, camera.fps)
        self.flip_v = QCheckBox("Flip vertical")
        self.flip_v.setChecked(camera.flip_vertical)
        self.flip_h = QCheckBox("Flip horizontal")
        self.flip_h.setChecked(camera.flip_horizontal)

        form = QFormLayout(self)
        form.addRow("Index / file:", self.index_edit)
        size_row = QHBoxLayout()
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("x"))
        size_row.addWidget(self.height_spin)
        size_row.addWidget(QLabel("@"))
        size_row.addWidget(self.fps_spin)
        size_row.addWidget(QLabel("fps"))
        form.addRow("Resolution:", size_row)
        flip_row = QHBoxLayout()
        flip_row.addWidget(self.flip_v)
        flip_row.addWidget(self.flip_h)
        form.addRow("", flip_row)

    def value(self) -> CameraConfig:
        raw = self.index_edit.text().strip()
        return CameraConfig(
            index=int(raw) if raw.lstrip("-").isdigit() else raw,
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=self.fps_spin.value(),
            flip_vertical=self.flip_v.isChecked(),
            flip_horizontal=self.flip_h.isChecked(),
        )

    def set_value(self, camera: CameraConfig) -> None:
        self.index_edit.setText(str(camera.index))
        self.width_spin.setValue(camera.width)
        self.height_spin.setValue(camera.height)
        self.fps_spin.setValue(camera.fps)
        self.flip_v.setChecked(camera.flip_vertical)
        self.flip_h.setChecked(camera.flip_horizontal)


class RemoteSettingsDialog(QDialog):
    """One role's camera, MIDI port and keyboard profile.

    Modal, and opened from the client's own Settings button rather than
    living on any of its three stages - the client's pages are already
    one-thing-at-a-time, and device setup is not something anyone does
    mid-lesson. Saving writes only this role's keys.
    """

    def __init__(self, role: str, cfg: Config, remote: Optional[RemoteGuidanceConfig] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.role = role
        self.title = ROLE_TITLES.get(role, role.title())
        self.setWindowTitle(f"{self.title} Settings")
        self.cfg = cfg
        self.remote = remote or RemoteGuidanceConfig.load()
        self.saved = False
        self.midi_test_listener: Optional[MidiListener] = None
        self._midi_test_timer = QTimer(self)
        self._midi_test_timer.timeout.connect(self._poll_midi_test)

        role_config = getattr(self.remote, role)

        self.camera_group = CameraGroup("Camera", role_config.camera)

        self.port_combo = QComboBox()
        # Editable: the port may belong to a keyboard that is not plugged
        # in yet, or to the machine this config will be copied to.
        self.port_combo.setEditable(True)
        # Two keyboards of the same model report the same name, so the list
        # can contain "... #1"/"... #2" - see the hint below and app.midi.
        self.port_hint = QLabel("")
        self.port_hint.setWordWrap(True)
        self.port_hint.setStyleSheet(STATUS_STYLES["warn"])
        port_refresh = QPushButton("Refresh")
        port_refresh.clicked.connect(self._refresh_ports)
        self.midi_test_btn = QPushButton("Connect & test")
        self.midi_test_btn.setToolTip(
            "Temporarily open the selected MIDI input. Press any key and check the note readout below; "
            "the port is released when Settings closes."
        )
        self.midi_test_btn.clicked.connect(self._toggle_midi_test)
        self.midi_test_output = QLabel("MIDI test: not connected.")
        self.midi_test_output.setWordWrap(True)
        test_font = self.midi_test_output.font()
        test_font.setPointSize(test_font.pointSize() + 2)
        self.midi_test_output.setFont(test_font)
        self.port_combo.currentTextChanged.connect(self._on_midi_port_changed)

        self.profile_combo = QComboBox()
        self.profile_combo.setEditable(True)
        self.profile_combo.addItems(list_profiles())
        self.profile_combo.setCurrentText(role_config.keyboard_profile)

        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(port_refresh)
        port_row.addWidget(self.midi_test_btn)

        keyboard_box = QGroupBox("Keyboard")
        keyboard_form = QFormLayout(keyboard_box)
        keyboard_form.addRow("MIDI port:", port_row)
        keyboard_form.addRow("", self.port_hint)
        keyboard_form.addRow("", self.midi_test_output)
        keyboard_form.addRow("Calibration profile:", self.profile_combo)
        self.preview_btn = QPushButton("Capture camera + profile preview")
        self.preview_btn.setToolTip(
            "Use the camera values currently shown above, take one picture, and overlay this profile's "
            "keyboard mask in a separate preview window. Nothing is saved."
        )
        self.preview_btn.clicked.connect(self._capture_profile_preview)
        keyboard_form.addRow("", self.preview_btn)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        note = QLabel(self._note_text())
        note.setWordWrap(True)
        note.setStyleSheet(STATUS_STYLES["idle"])

        shared = QLabel(
            "This changes only the "
            f"{self.title.lower()}'s own settings. The camera, MIDI port and profile the launcher's other "
            f"tools use (index {cfg.camera.index}, {cfg.midi.port_name!r}, {cfg.active_keyboard_profile!r}) "
            "are left exactly as they are."
        )
        shared.setWordWrap(True)
        shared.setStyleSheet(STATUS_STYLES["idle"])

        local_btn = QPushButton("Use this machine's setup")
        local_btn.setToolTip(
            "Copy the camera, MIDI port and profile the ordinary tools use into this role. Nothing is saved "
            "until you press Save."
        )
        local_btn.clicked.connect(self._fill_from_local)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        buttons.addButton(local_btn, QDialogButtonBox.ButtonRole.ResetRole)

        layout = QVBoxLayout(self)
        layout.addWidget(self.camera_group)
        layout.addWidget(keyboard_box)
        layout.addWidget(note)
        layout.addWidget(shared)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        # After the layout, so the hint has a parent before it is ever made
        # visible - a parentless widget shown here becomes its own window.
        self._populate_ports(role_config.midi.port_name or "")

    # ------------------------------------------------------------------

    def _note_text(self) -> str:
        if self.role == ROLE_STUDENT:
            return (
                "The key LED strip and the nail actuators have nothing to set: they are found and connected "
                "automatically when a session starts. Guidance mode, response timeout and video recording are "
                "chosen per session, on the session page."
            )
        return (
            "The teacher client has no LED strip and no actuators - it only measures which key was played and "
            "with which finger, and sends that. There is nothing else to configure here."
        )

    def _populate_ports(self, preferred: str) -> None:
        """Fill the port list and say so when two instruments share a name.

        The "#1"/"#2" suffixes come from the order the OS lists devices, so
        with two identical keyboards the numbers are the only thing telling
        them apart - and they can swap over when something is replugged.
        Saying that here is cheaper than debugging a lesson where the
        teacher's notes arrive from the student's keyboard."""
        self.port_combo.clear()
        self.port_combo.addItems(list_input_ports())
        self.port_combo.setCurrentText(preferred)

        duplicates = ambiguous_port_names()
        if duplicates:
            names = ", ".join(repr(name) for name in duplicates)
            self.port_hint.setText(
                f"More than one instrument reports {names}. The #1/#2 numbers follow the order this "
                "machine lists them and can change when a keyboard is replugged - check which is which "
                "after replugging, or rename the instruments (macOS: Audio MIDI Setup > MIDI Studio) so "
                "the numbers are not needed."
            )
        else:
            self.port_hint.setText("")
        self.port_hint.setVisible(bool(duplicates))

    def _refresh_ports(self) -> None:
        self._populate_ports(self.port_combo.currentText())

    def _on_midi_port_changed(self, _text: str) -> None:
        # A running listener belongs to the old selection. Keeping it open
        # would make the readout claim to test a different keyboard and hold
        # that old device after the user had moved on.
        if self.midi_test_listener is not None:
            self._release_midi_test("MIDI selection changed; test connection released.")

    def _toggle_midi_test(self) -> None:
        if self.midi_test_listener is not None:
            self._release_midi_test("MIDI test disconnected.")
            return

        port_name = self.port_combo.currentText().strip() or None
        try:
            listener = MidiListener(port_name)
        except RuntimeError as exc:
            message = str(exc)
            self.midi_test_output.setText(f"MIDI test failed: {message}")
            self._set_status(f"MIDI test failed: {message}", "error")
            QMessageBox.warning(self, "MIDI connection failed", message)
            return

        self.midi_test_listener = listener
        # A legacy bare driver name may resolve to a numbered unique label.
        # Reflect the port actually opened without triggering the
        # currentTextChanged release path above.
        self.port_combo.blockSignals(True)
        self.port_combo.setCurrentText(listener.port_name)
        self.port_combo.blockSignals(False)
        self.midi_test_btn.setText("Disconnect test")
        self.midi_test_output.setText(
            f"Connected to {listener.port_name!r}. Press any key on that keyboard."
        )
        self._midi_test_timer.start(33)
        self._set_status(
            f"Testing MIDI port {listener.port_name!r}. This temporary connection closes with Settings.",
            "ok",
        )

    def _poll_midi_test(self) -> None:
        if self.midi_test_listener is None:
            return
        for event in self.midi_test_listener.pop_events():
            self.midi_test_output.setText(
                f"Last key pressed: note {event.note} ({note_name(event.note)}) "
                f"from {self.midi_test_listener.port_name!r}."
            )

    def _release_midi_test(self, message: str = "MIDI test connection released.") -> None:
        self._midi_test_timer.stop()
        listener = self.midi_test_listener
        self.midi_test_listener = None
        if listener is not None:
            listener.close()
        self.midi_test_btn.setText("Connect & test")
        self.midi_test_output.setText(message)

    def _fill_from_local(self) -> None:
        self.camera_group.set_value(replace(self.cfg.camera))
        self.port_combo.setCurrentText(self.cfg.midi.port_name or "")
        self.profile_combo.setCurrentText(self.cfg.active_keyboard_profile)
        self._set_status(
            "Filled in from this machine's current setup. If the other role runs on this same machine, give "
            "them a different camera and MIDI port. Nothing is saved until you press Save.",
            "warn",
        )

    def _capture_profile_preview(self) -> None:
        """Preview the unsaved camera/profile values currently on screen."""
        camera_config = self.camera_group.value()
        profile_name = self.profile_combo.currentText().strip()
        self.preview_btn.setEnabled(False)
        self._set_status("Capturing one frame and rendering the keyboard mask...", "idle")
        try:
            frame, template = capture_profile_preview(camera_config, profile_name)
        except Exception as exc:  # noqa: BLE001 - camera/profile failures are user-facing
            message = str(exc) or type(exc).__name__
            self._set_status(f"Preview failed: {message}", "error")
            QMessageBox.warning(self, "Camera/profile preview failed", message)
            return
        finally:
            self.preview_btn.setEnabled(True)

        dialog = KeyboardProfilePreviewDialog(frame, profile_name, len(template.keys), parent=self)
        dialog.exec()
        self._set_status(
            f"Previewed camera {camera_config.index!r} with profile {profile_name!r}. Nothing was saved.",
            "ok",
        )

    def collect(self) -> RemoteGuidanceConfig:
        """Fold the form back into the config object. Only this role's
        keys are touched - the other role's, and the network block, are
        carried through untouched."""
        role_config = getattr(self.remote, self.role)
        role_config.camera = self.camera_group.value()
        role_config.midi.port_name = self.port_combo.currentText().strip() or None
        role_config.keyboard_profile = self.profile_combo.currentText().strip() or "default"
        return self.remote

    def _save(self) -> None:
        remote = self.collect()
        problems = remote.validate()
        if problems:
            self._set_status(problems[0], "error")
            QMessageBox.warning(self, "Cannot save", "\n\n".join(problems))
            return
        remote.save()
        self.saved = True
        self.accept()

    def done(self, result: int) -> None:
        self._release_midi_test()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._release_midi_test()
        super().closeEvent(event)

    def _set_status(self, message: str, level: str = "idle") -> None:
        self.status.setText(message)
        self.status.setStyleSheet(STATUS_STYLES.get(level, ""))


def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(int(value))
    return spin
