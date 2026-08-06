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

Only that role's slice of the `remote_guidance` block is written. The
top-level `camera`, `midi` and `active_keyboard_profile` that every
ordinary tool reads are shown here for reference and never touched -
saving a remote client's settings must not move the local quiz's camera.
`RemoteGuidanceConfig.save()` enforces that by merging into the existing
file.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

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

from app.config import CameraConfig, Config
from app.midi import list_input_ports
from app.profiles import list_profiles

from .config import ROLE_STUDENT, ROLE_TEACHER, RemoteGuidanceConfig
from .gui_common import STATUS_STYLES

ROLE_TITLES = {ROLE_STUDENT: "Student", ROLE_TEACHER: "Teacher"}


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

        role_config = getattr(self.remote, role)

        self.camera_group = CameraGroup("Camera", role_config.camera)

        self.port_combo = QComboBox()
        # Editable: the port may belong to a keyboard that is not plugged
        # in yet, or to the machine this config will be copied to.
        self.port_combo.setEditable(True)
        self.port_combo.addItems(list_input_ports())
        self.port_combo.setCurrentText(role_config.midi.port_name or "")
        port_refresh = QPushButton("Refresh")
        port_refresh.clicked.connect(self._refresh_ports)

        self.profile_combo = QComboBox()
        self.profile_combo.setEditable(True)
        self.profile_combo.addItems(list_profiles())
        self.profile_combo.setCurrentText(role_config.keyboard_profile)

        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(port_refresh)

        keyboard_box = QGroupBox("Keyboard")
        keyboard_form = QFormLayout(keyboard_box)
        keyboard_form.addRow("MIDI port:", port_row)
        keyboard_form.addRow("Calibration profile:", self.profile_combo)

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

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(list_input_ports())
        self.port_combo.setCurrentText(current)

    def _fill_from_local(self) -> None:
        self.camera_group.set_value(replace(self.cfg.camera))
        self.port_combo.setCurrentText(self.cfg.midi.port_name or "")
        self.profile_combo.setCurrentText(self.cfg.active_keyboard_profile)
        self._set_status(
            "Filled in from this machine's current setup. If the other role runs on this same machine, give "
            "them a different camera and MIDI port. Nothing is saved until you press Save.",
            "warn",
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

    def _set_status(self, message: str, level: str = "idle") -> None:
        self.status.setText(message)
        self.status.setStyleSheet(STATUS_STYLES.get(level, ""))


def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(int(value))
    return spin
