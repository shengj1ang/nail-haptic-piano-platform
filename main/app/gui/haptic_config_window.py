"""Initial Setup -> "Haptic Actuator Defaults".

The one place the platform's haptic defaults are set: which actuator is
in use (LRA or ERM) and, for each of them, the default drive frequency
and amp. Everything else in the project - the quiz's haptic cue, the
Haptic Motor Bench, every validation experiment's Test Buzz and initial
control values - reads those numbers from config.json through
common.haptic_config instead of hard-coding them.

Every change is written to config.json the moment it is made (no Save
button, no restart): a window opened afterwards reads the new values,
and windows already open that subscribed to common.haptic_config are
refreshed through its listener callback.

WHAT THIS DOES NOT CHANGE: it sets DEFAULTS. A sweep still sweeps its own
frequency/amp points, and anything typed into an experiment window wins
over the default for that run. LRA frequency means a mechanical
drive/resonance frequency; ERM frequency is a PWM carrier and is not the
rotor's vibration frequency - the two boxes below are labelled
accordingly.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from common import haptic_config as hc

from ..config import Config

#: Kept narrow on purpose - this window is a short form, and a wrapped
#: prose label with a fixed measure is what stops a paragraph from
#: setting a window minimum width that runs off a laptop screen.
WINDOW_WIDTH = 620
TEXT_WIDTH = 560

#: How long after the last keystroke/arrow-click the value is written.
#: Short enough to feel immediate, long enough that typing "1000" writes
#: once rather than four times.
SAVE_DEBOUNCE_MS = 300

ACTUATOR_BLURB = {
    hc.LRA: ("Linear resonant actuator — vibrates properly only near its "
             "mechanical resonance, so its frequency is a real drive "
             "frequency."),
    hc.ERM: ("Eccentric rotating mass — its frequency is the PWM carrier "
             "that chops the drive rail, <i>not</i> the rotor's vibration "
             "frequency. Too low a carrier and the rotor never starts."),
}


def _wrapped(text: str) -> QLabel:
    """Prose that wraps to the window instead of widening it."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setMaximumWidth(TEXT_WIDTH)
    return label


class _ActuatorBox(QGroupBox):
    """The default frequency + amp of one actuator, with its own valid
    ranges taken from common.haptic_config (the same ranges the config
    validator enforces, so the spin box can never offer a value the
    config would reject)."""

    def __init__(self, actuator_type: str, on_changed):
        label = hc.actuator_label(actuator_type)
        super().__init__(f"{label} defaults")
        self.actuator_type = actuator_type
        self._on_changed = on_changed
        self._loading = True

        freq_min, freq_max = hc.frequency_range(actuator_type)
        amp_min, amp_max = hc.amp_range()

        self.freq_spin = QSpinBox()
        self.freq_spin.setRange(freq_min, freq_max)
        self.freq_spin.setSuffix(" Hz")
        self.freq_spin.setToolTip(
            f"Default drive frequency for the {label} "
            f"({freq_min}-{freq_max} Hz). "
            + ("The LRA's mechanical resonance — measure it with the LRA "
               "Frequency Sweep experiment."
               if actuator_type == hc.LRA else
               "The PWM carrier frequency, not the rotor's vibration "
               "frequency."))
        self.freq_spin.valueChanged.connect(self._changed)

        self.amp_spin = QSpinBox()
        self.amp_spin.setRange(amp_min, amp_max)
        self.amp_spin.setToolTip(
            f"Default drive amp for the {label} ({amp_min}-{amp_max}, the "
            "firmware's PWM duty byte).")
        self.amp_spin.valueChanged.connect(self._changed)

        self.note = _wrapped("")
        self.note.setStyleSheet("color: #c9b072;")  # advisory amber
        self.note.setVisible(False)

        grid = QGridLayout()
        grid.addWidget(QLabel(f"{label} default frequency:"), 0, 0)
        grid.addWidget(self.freq_spin, 0, 1)
        grid.addWidget(QLabel(f"Allowed {freq_min}–{freq_max} Hz"), 0, 2)
        grid.addWidget(QLabel(f"{label} default amp:"), 1, 0)
        grid.addWidget(self.amp_spin, 1, 1)
        grid.addWidget(QLabel(f"Allowed {amp_min}–{amp_max}"), 1, 2)
        grid.setColumnStretch(3, 1)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(_wrapped(ACTUATOR_BLURB[actuator_type]))
        layout.addLayout(grid)
        layout.addWidget(self.note)

    def set_values(self, defaults: hc.ActuatorDefaults) -> None:
        """Show stored values without triggering a save."""
        self._loading = True
        self.freq_spin.setValue(defaults.default_frequency)
        self.amp_spin.setValue(defaults.default_amp)
        self._loading = False
        self._refresh_note()

    def values(self) -> tuple:
        return self.freq_spin.value(), self.amp_spin.value()

    def _refresh_note(self) -> None:
        """A legal-but-unusual frequency is allowed (this is a research
        rig) - it just says so rather than silently changing it."""
        note = hc.frequency_note(self.actuator_type, self.freq_spin.value())
        self.note.setText(note)
        self.note.setVisible(bool(note))

    def _changed(self) -> None:
        self._refresh_note()
        if not self._loading:
            self._on_changed()


class HapticConfigWindow(QMainWindow):
    """Pick the actuator in use and each actuator's default drive; saved
    to config.json immediately."""

    def __init__(self, cfg: Config = None):
        super().__init__()
        self.setWindowTitle("Haptic Actuator Defaults")
        self.cfg = cfg
        self._loading = True
        # True only while this window is writing, so the change
        # notification our own save fires doesn't reload us mid-edit.
        self._saving = False

        heading = QLabel("Which haptic actuator does this rig use, and how "
                         "should it be driven by default?")
        heading_font = heading.font()
        heading_font.setPointSize(heading_font.pointSize() + 2)
        heading_font.setBold(True)
        heading.setFont(heading_font)
        heading.setWordWrap(True)
        heading.setMaximumWidth(TEXT_WIDTH)

        intro = _wrapped(
            "Saved to <code>config.json</code> as you type — no Save button "
            "and no restart. These are <b>defaults</b>: a sweep still sweeps "
            "its own frequency/amp points, and a value you type in an "
            "experiment window wins over the default for that run.")

        # -- actuator in use ---------------------------------------------
        self._radios = {}
        self._radio_group = QButtonGroup(self)
        radio_row = QHBoxLayout()
        radio_row.addWidget(QLabel("Actuator in use:"))
        for actuator_type in hc.ACTUATOR_TYPES:
            radio = QRadioButton(hc.actuator_label(actuator_type))
            radio.setToolTip(
                f"Drive the {hc.actuator_label(actuator_type)} by default "
                f"(wired to motor port "
                f"{hc.get_actuator_motor_port(actuator_type)} by convention).")
            radio.toggled.connect(
                lambda checked, t=actuator_type: checked and self._using_changed(t))
            self._radio_group.addButton(radio)
            self._radios[actuator_type] = radio
            radio_row.addWidget(radio)
        radio_row.addStretch(1)

        using_box = QGroupBox("Actuator in use")
        using_layout = QVBoxLayout(using_box)
        using_layout.addLayout(radio_row)
        using_layout.addWidget(_wrapped(
            "Selecting an actuator switches the whole default drive — the "
            "haptic quiz cue, Test Buzz and every window's initial "
            "frequency/amp follow the block below for the selected type."))

        # -- per-actuator defaults ---------------------------------------
        self._boxes = {t: _ActuatorBox(t, self._defaults_changed)
                       for t in hc.ACTUATOR_TYPES}

        # -- live summary -------------------------------------------------
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setMaximumWidth(TEXT_WIDTH)
        summary_font = self.summary.font()
        summary_font.setBold(True)
        self.summary.setFont(summary_font)

        self.status = _wrapped("")
        self.status.setStyleSheet("color: #9a9ba5;")

        # Corrections the loader had to make to an out-of-range or
        # malformed stored value are surfaced, never applied silently.
        self.warnings = _wrapped("")
        self.warnings.setStyleSheet("color: #c9b072;")
        self.warnings.setVisible(False)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(intro)
        layout.addWidget(using_box)
        for actuator_type in hc.ACTUATOR_TYPES:
            layout.addWidget(self._boxes[actuator_type])
        layout.addWidget(self.summary)
        layout.addWidget(self.warnings)
        layout.addWidget(self.status)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.setCentralWidget(scroll)
        self.resize(WINDOW_WIDTH, 660)

        # Debounced writer: typing a multi-digit frequency saves once.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._save)

        self._reload_from_config()
        self._loading = False

        # Refresh if the config is changed elsewhere while this window is
        # open (and stop listening when it closes).
        hc.subscribe(self._on_external_change)

    # -- loading ---------------------------------------------------------

    def _reload_from_config(self) -> None:
        """Fill every control from config.json without saving back."""
        was_loading = self._loading
        self._loading = True
        config = hc.get_haptic_config(reload=True)
        self._radios[config.using].setChecked(True)
        for actuator_type in hc.ACTUATOR_TYPES:
            self._boxes[actuator_type].set_values(config.for_type(actuator_type))
        warnings = hc.last_warnings()
        self.warnings.setVisible(bool(warnings))
        if warnings:
            self.warnings.setText(
                "config.json had values that could not be used as stored, so "
                "they were corrected on load (save below to keep the "
                "correction):<br>• " + "<br>• ".join(warnings))
        self._loading = was_loading
        self._refresh_summary()

    def _on_external_change(self, config: hc.HapticConfig) -> None:
        """Another window saved - show the new values (our own saves are
        filtered out by _saving, so a save can't loop back into a load)."""
        if self._saving:
            return
        self._reload_from_config()

    # -- summary ---------------------------------------------------------

    def _refresh_summary(self) -> None:
        config = hc.get_haptic_config()
        defaults = config.active
        port = hc.get_actuator_motor_port(config.using)
        self.summary.setText(
            f"Current haptic actuator: {hc.actuator_label(config.using)}<br>"
            f"Default drive: {defaults.default_frequency} Hz, "
            f"amp {defaults.default_amp} "
            f"<span style='font-weight:normal'>(motor port {port} by wiring "
            "convention; each experiment window still lets you pick a "
            "port)</span>")

    # -- saving ----------------------------------------------------------

    def _using_changed(self, actuator_type: str) -> None:
        if self._loading:
            return
        self._save_timer.stop()
        self._save()

    def _defaults_changed(self) -> None:
        if self._loading:
            return
        self._save_timer.start()

    def _selected_type(self) -> str:
        for actuator_type, radio in self._radios.items():
            if radio.isChecked():
                return actuator_type
        return hc.DEFAULT_USING

    def _save(self) -> None:
        lra_freq, lra_amp = self._boxes[hc.LRA].values()
        erm_freq, erm_amp = self._boxes[hc.ERM].values()
        self._saving = True
        try:
            hc.update_haptic_config(
                using=self._selected_type(),
                lra_frequency=lra_freq, lra_amp=lra_amp,
                erm_frequency=erm_freq, erm_amp=erm_amp)
        except ValueError as e:
            # The spin-box ranges mirror the validator's, so this is a
            # belt-and-braces path: report it, never write a bad value.
            self.status.setText(f"Not saved — {e}")
            return
        finally:
            self._saving = False
        # Keep a Config the launcher already handed out in step, so a
        # window holding it sees the same numbers.
        if self.cfg is not None:
            self.cfg.haptic = hc.get_haptic_config()
        self._refresh_summary()
        self.status.setText(f"Saved to config.json — {hc.summary_line()}")

    # -- lifecycle -------------------------------------------------------

    def closeEvent(self, event) -> None:
        # Write a pending debounced edit before going away, so closing
        # right after typing never loses the last change.
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save()
        hc.unsubscribe(self._on_external_change)
        super().closeEvent(event)
