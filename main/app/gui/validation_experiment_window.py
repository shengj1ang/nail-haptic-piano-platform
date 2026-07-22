"""Launcher section "Validation Experiments": thin GUI front panels for
the standalone scripts under validation_experiments/.

Each window is a front panel - Start/Stop, motor-port and ACC-sensor
pickers, a progress bar, a live console log, and a plot preview -
wrapped around the script's unchanged run_experiment() function (run on
a QThread, analyze_worker.py-style, so the GUI never freezes during the
multi-minute serial sweep). On open, the preview shows the latest saved
response curve from data/validation_experiments/<experiment>/, and "Load Chart
from CSV" re-renders the plot from any saved sweep CSV (to a temp file
- saved outputs are never modified). The experiment logic and the
CSV/PNG/meta outputs under data/validation_experiments/ are exactly the
same as running the script from the command line.
"""

import glob
import os
import tempfile
from functools import partial
from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFontDatabase, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.gui.accelerometer_window import AccelerometerWindow
from validation_experiments.lra_resonance_intensity_calibration import (
    lra_amplitude_sweep,
    lra_frequency_sweep,
)
from validation_experiments.motor_acc_delay_experiment import motor_acc_delay
from validation_experiments.rig import SweepAborted, open_rig, send

# Test-buzz pulse fired at the selected motor ("P idx count amp on off"),
# so the operator can confirm which physical actuator the sweep will hit.
TEST_BUZZ_AMP = 64    # project-default cue intensity
TEST_BUZZ_MS = 500


class _SweepWorker(QThread):
    """Runs one validation-experiment run_experiment() off the GUI
    thread, relaying its log lines and per-step progress as signals."""

    progress = Signal(int, int)  # steps_done, steps_total
    log = Signal(str)
    succeeded = Signal(dict)  # run_experiment() summary
    aborted = Signal()
    failed = Signal(str)

    def __init__(self, run_fn: Callable):
        super().__init__()
        self._run_fn = run_fn
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        try:
            summary = self._run_fn(
                log=self.log.emit,
                progress=lambda done, total: self.progress.emit(done, total),
                should_stop=lambda: self._stop_requested,
                # No interactive fallback on a worker thread - if the rig
                # can't be auto-detected, fail with the port listing.
                interactive=False,
            )
        except SweepAborted:
            self.aborted.emit()
        except Exception as e:
            self.failed.emit(str(e))
        else:
            self.succeeded.emit(summary)


class _PlotView(QLabel):
    """Shows a response-curve PNG scaled to the available space (aspect
    kept). Ignored size policy so the scaled pixmap never feeds back
    into the layout's size negotiation."""

    PLACEHOLDER = "No saved output yet - run the experiment or load a CSV."

    def __init__(self):
        super().__init__(self.PLACEHOLDER)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumHeight(200)
        self._pixmap: QPixmap | None = None

    def show_png(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            raise ValueError(f"Could not read image: {path}")
        self._pixmap = pixmap
        self._rescale()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        self.setPixmap(self._pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class _SweepWindowBase(QMainWindow):
    """Shared Start/Stop + progress bar + log + plot-preview shell;
    subclasses supply the experiment module and how to phrase results."""

    TITLE = ""
    DESCRIPTION = ""
    MODULE = None       # validation_experiments module with run_experiment/render_csv
    PNG_GLOB = ""       # e.g. "frequency_response_*.png" - the module's plot files
    CSV_GLOB = ""       # e.g. "sweep_*.csv" - the module's data files
    # Physical-mounting instructions shown under the description; the
    # default is the sweeps' desk-mounted protocol - experiments with a
    # different protocol (e.g. the suspended delay test) override it.
    SETUP_HINT = (
        "<b>Physical setup:</b> the accelerometer must be glued/taped to "
        "the motor under test so the two move as one unit, and the pair "
        "must be fixed to a rigid desk surface. A loose sensor or a "
        "free-floating rig invalidates every measurement. Use \"Test "
        "Buzz\" to confirm the selected motor port drives the actuator "
        "the sensor is attached to."
    )

    def __init__(self, cfg=None):
        super().__init__()
        self.cfg = cfg  # unused, accepted for the launcher's window_cls(cfg) call
        self.setWindowTitle(self.TITLE)
        self._worker = None
        # Holds CSV re-renders so saved output files are never touched.
        self._tmpdir = tempfile.TemporaryDirectory(prefix="validation_sweep_")
        self._render_count = 0

        description = QLabel(self.DESCRIPTION)
        description.setWordWrap(True)

        setup_hint = QLabel(self.SETUP_HINT)
        setup_hint.setWordWrap(True)

        self.motor_spin = QSpinBox()
        self.motor_spin.setRange(0, 15)
        self.motor_spin.setValue(self.MODULE.MOTOR_INDEX)
        self.motor_spin.setToolTip("Motor port the actuator under test is wired to")
        self.acc_spin = QSpinBox()
        self.acc_spin.setRange(0, 7)
        # Default to the rig-wide sensor choice persisted by the
        # Accelerometer Live View window (config.json), falling back to
        # the script constant when launched without a config.
        self.acc_spin.setValue(self.MODULE.ACC_SENSOR_ID if cfg is None
                               else cfg.accelerometer.sensor_id)
        self.acc_spin.setToolTip("LIS3DH sensor id in the ACC stream")

        self.test_buzz_btn = QPushButton("Test Buzz")
        self.test_buzz_btn.setToolTip(
            f"Pulse the selected motor once ({TEST_BUZZ_MS} ms at amp "
            f"{TEST_BUZZ_AMP}) to confirm the wiring; the first click opens "
            "the serial port (~2 s)")
        self.test_buzz_btn.clicked.connect(self._test_buzz)
        self._test_ser = None  # lazy serial connection for test buzzes

        config_row = QHBoxLayout()
        config_row.addWidget(QLabel("Motor port:"))
        config_row.addWidget(self.motor_spin)
        config_row.addSpacing(12)
        config_row.addWidget(QLabel("ACC sensor id:"))
        config_row.addWidget(self.acc_spin)
        config_row.addSpacing(12)
        # Inputs the base class locks while a sweep runs; _build_extra_config
        # appends the subclass's experiment-specific parameter spins.
        self._config_inputs = [self.motor_spin, self.acc_spin]
        self._build_extra_config(config_row)
        config_row.addWidget(self.test_buzz_btn)
        defaults_note = QLabel("<i>Defaults recommended - only change them "
                               "when the rig setup demands it.</i>")
        config_row.addWidget(defaults_note)
        config_row.addStretch(1)

        self.status = QLabel("Idle - connect the rig and press Start.")
        self.status.setWordWrap(True)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m steps")

        self.plot_view = _PlotView()

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        self.load_csv_btn = QPushButton("Load Chart from CSV...")
        self.load_csv_btn.clicked.connect(self._load_csv)
        self.acc_view_btn = QPushButton("Open Accelerometer Live View")
        self.acc_view_btn.setToolTip(
            "Watch the ACC stream live - with the view connected, Test Buzz "
            "is sent over its connection, so the pulse shows up in the plot")
        self.acc_view_btn.clicked.connect(self._open_acc_view)
        self._acc_view = None  # child Accelerometer Live View window

        buttons = QHBoxLayout()
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addSpacing(12)
        buttons.addWidget(self.load_csv_btn)
        buttons.addWidget(self.acc_view_btn)
        buttons.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.plot_view)
        splitter.addWidget(self.log_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(description)
        layout.addWidget(setup_hint)
        layout.addLayout(config_row)
        layout.addLayout(buttons)
        layout.addWidget(self.progress_bar)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.resize(860, 760)

        self._show_latest_output()

    # -- subclass hooks -------------------------------------------------

    def _build_extra_config(self, config_row: QHBoxLayout) -> None:
        """Add experiment-specific parameter widgets to the config row;
        register any input in self._config_inputs to lock it during runs."""

    def _extra_run_kwargs(self) -> dict:
        """Experiment-specific kwargs forwarded to run_experiment()."""
        return {}

    def _pre_buzz_cmds(self, motor: int) -> list:
        """Commands sent right before a Test Buzz pulse (e.g. setting
        the PWM frequency the selected actuator needs)."""
        return []

    def _summary_text(self, summary: dict) -> str:
        raise NotImplementedError

    # -- saved-output preview -------------------------------------------

    def _latest(self, pattern: str) -> str | None:
        # Names carry Unix-epoch-seconds stamps (<prefix>_<epoch>), which
        # sort chronologically as strings while their digit count is equal.
        matches = sorted(glob.glob(os.path.join(self.MODULE.OUTPUT_DIR, pattern)))
        return matches[-1] if matches else None

    def _show_latest_output(self) -> None:
        latest_png = self._latest(self.PNG_GLOB)
        if latest_png is None:
            return
        try:
            self.plot_view.show_png(latest_png)
        except ValueError:
            return
        self.status.setText(
            f"Showing latest saved result: {os.path.basename(latest_png)} "
            "- press Start for a new run."
        )

    def _load_csv(self) -> None:
        latest_csv = self._latest(self.CSV_GLOB)
        start_dir = latest_csv if latest_csv else self.MODULE.OUTPUT_DIR
        csv_path, _ = QFileDialog.getOpenFileName(
            self, "Load sweep CSV", start_dir, "Sweep CSV (*.csv)")
        if not csv_path:
            return
        # Unique temp name per render: QPixmap must never see a stale file.
        self._render_count += 1
        out_png = os.path.join(self._tmpdir.name, f"render_{self._render_count}.png")
        try:
            summary = self.MODULE.render_csv(csv_path, out_png)
            self.plot_view.show_png(out_png)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't load CSV", str(e))
            return
        self.status.setText(
            f"Chart re-rendered from {os.path.basename(csv_path)} - "
            + self._summary_text(summary, saved=False)
        )

    # -- accelerometer live view (single shared serial port!) -----------

    def _open_acc_view(self) -> None:
        if self._acc_view is not None and self._acc_view.isVisible():
            self._acc_view.raise_()
            self._acc_view.activateWindow()
            return
        # One serial port on the rig: hand it over to the live view so
        # its Connect can't collide with a lingering test-buzz connection.
        self._close_test_serial()
        self._acc_view = AccelerometerWindow(self.cfg)
        self._acc_view.show()

    def _acc_stream_serial(self):
        """The live view's streaming connection, if it is open and
        streaming - Test Buzz writes ride on it instead of opening the
        port a second time."""
        if self._acc_view is None or not self._acc_view.isVisible():
            return None
        return self._acc_view.streaming_serial()

    # -- test buzz ------------------------------------------------------

    def _test_buzz(self) -> None:
        if self._worker is not None:
            return
        acc_ser = self._acc_stream_serial()
        if acc_ser is not None:
            motor = self.motor_spin.value()
            try:
                for cmd in self._pre_buzz_cmds(motor):
                    send(acc_ser, cmd, wait_s=0.0)
                send(acc_ser, f"P {motor} 1 {TEST_BUZZ_AMP} {TEST_BUZZ_MS} 0",
                     wait_s=0.0)
            except Exception as e:
                QMessageBox.warning(self, "Test buzz failed", str(e))
                return
            self.status.setText(
                f"Test buzz sent to motor port {motor} via the live view's "
                "connection - the pulse should be visible in its plot.")
            return
        if self._test_ser is None:
            self.status.setText("Connecting for test buzz...")
            self.status.repaint()
            try:
                self._test_ser = open_rig(log=self._on_log, interactive=False)
            except Exception as e:
                self.status.setText("Test buzz: connection failed.")
                QMessageBox.warning(self, "Couldn't connect", str(e))
                return
        motor = self.motor_spin.value()
        try:
            for cmd in self._pre_buzz_cmds(motor):
                send(self._test_ser, cmd, wait_s=0.0)
            # Async firmware pulse - fire and forget, no X needed.
            send(self._test_ser, f"P {motor} 1 {TEST_BUZZ_AMP} {TEST_BUZZ_MS} 0",
                 wait_s=0.0)
        except Exception as e:
            self._close_test_serial()
            self.status.setText("Test buzz: send failed.")
            QMessageBox.warning(self, "Test buzz failed", str(e))
            return
        self.status.setText(
            f"Test buzz sent to motor port {motor} - the actuator glued to "
            "the accelerometer should have vibrated.")

    def _close_test_serial(self) -> None:
        if self._test_ser is None:
            return
        try:
            send(self._test_ser, "X", wait_s=0.0)
        except Exception:
            pass
        try:
            self._test_ser.close()
        except Exception:
            pass
        self._test_ser = None

    # -- run control ----------------------------------------------------

    def _start(self) -> None:
        if self._worker is not None:
            return
        self.log_view.clear()
        # The sweep worker opens the port itself - release every other
        # connection first (test buzz, live view stream); a double-open
        # would fail on Windows and silently interleave reads on macOS.
        self._close_test_serial()
        if self._acc_view is not None and self._acc_view.isVisible():
            if self._acc_view.streaming_serial() is not None:
                self._acc_view.disconnect_stream()
                self.log_view.appendPlainText(
                    "Accelerometer Live View disconnected - the sweep needs "
                    "exclusive access to the serial port.")
            self._acc_view.set_connect_allowed(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.status.setText("Running... keep the rig still during the sweep.")
        self.start_btn.setEnabled(False)
        self.load_csv_btn.setEnabled(False)
        for widget in self._config_inputs:
            widget.setEnabled(False)
        self.test_buzz_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        run_fn = partial(
            self.MODULE.run_experiment,
            motor_index=self.motor_spin.value(),
            acc_sensor_id=self.acc_spin.value(),
            **self._extra_run_kwargs(),
        )
        self._worker = _SweepWorker(run_fn)
        self._worker.log.connect(self._on_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.aborted.connect(self._on_aborted)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _stop(self) -> None:
        if self._worker is None:
            return
        self.stop_btn.setEnabled(False)
        self.status.setText("Stopping after the current step...")
        self._worker.request_stop()

    # -- worker signals -------------------------------------------------

    def _on_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)

    def _on_succeeded(self, summary: dict) -> None:
        try:
            self.plot_view.show_png(summary["png_path"])
        except ValueError:
            pass
        self.status.setText("Done - " + self._summary_text(summary, saved=True))

    def _on_aborted(self) -> None:
        self.status.setText("Stopped - no output files written; the rig was "
                            "restored to its default state.")

    def _on_failed(self, message: str) -> None:
        self.log_view.appendPlainText(f"\nERROR: {message}")
        self.status.setText(f"Failed: {message}")

    def _on_finished(self) -> None:
        self._worker = None
        self.start_btn.setEnabled(True)
        self.load_csv_btn.setEnabled(True)
        for widget in self._config_inputs:
            widget.setEnabled(True)
        self.test_buzz_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if self._acc_view is not None and self._acc_view.isVisible():
            self._acc_view.set_connect_allowed(True)

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            # A step lasts ~1 s; the finally-block also restores the rig.
            self._worker.wait(15000)
        if self._acc_view is not None:
            self._acc_view.close()
            self._acc_view = None
        self._close_test_serial()
        self._tmpdir.cleanup()
        super().closeEvent(event)


class FrequencySweepWindow(_SweepWindowBase):
    TITLE = "LRA Frequency Sweep (Resonance)"
    MODULE = lra_frequency_sweep
    PNG_GLOB = "frequency_response_*.png"
    CSV_GLOB = "sweep_*.csv"
    DESCRIPTION = (
        "Finds the mounted LRA's resonant frequency: steps the PWM frequency "
        f"{lra_frequency_sweep.COARSE_START_HZ}-{lra_frequency_sweep.COARSE_STOP_HZ} Hz "
        f"(coarse {lra_frequency_sweep.COARSE_STEP_HZ} Hz pass, then fine "
        f"{lra_frequency_sweep.FINE_STEP_HZ} Hz pass around the peak) at amp="
        f"{lra_frequency_sweep.AMP} and measures RMS acceleration with the LIS3DH. "
        f"Takes ~{lra_frequency_sweep.estimated_duration_s():.0f} s; CSV + response "
        "curve are saved to data/validation_experiments/lra_resonance_intensity_calibration/. "
        "Adopted project value: "
        "f0 = 224 Hz (see the folder's README)."
    )

    def _build_extra_config(self, config_row: QHBoxLayout) -> None:
        self.amp_spin = QSpinBox()
        self.amp_spin.setRange(1, 128)
        self.amp_spin.setValue(lra_frequency_sweep.AMP)
        self.amp_spin.setToolTip(
            "PWM drive amplitude during the sweep (0-255 duty scale). The "
            f"default {lra_frequency_sweep.AMP} = 50% duty is the maximum AC "
            "fundamental an LRA can receive, giving the best signal-to-noise "
            "for finding the peak - lower it only if the rig must not be "
            "driven that hard. The resonant frequency itself does not depend "
            "on the drive level.")
        config_row.addWidget(QLabel("Drive amp:"))
        config_row.addWidget(self.amp_spin)
        config_row.addSpacing(12)
        self._config_inputs.append(self.amp_spin)

    def _extra_run_kwargs(self) -> dict:
        return {"amp": self.amp_spin.value()}

    def _summary_text(self, summary: dict, saved: bool) -> str:
        text = (f"resonant frequency: {summary['resonance_hz']} Hz "
                f"(rms_delta={summary['rms_delta']:.1f}).")
        if saved:
            text += (f" Saved {os.path.basename(summary['csv_path'])} and "
                     f"{os.path.basename(summary['png_path'])}.")
        return text


class AmplitudeSweepWindow(_SweepWindowBase):
    TITLE = "LRA Amplitude Sweep (Intensity)"
    MODULE = lra_amplitude_sweep
    PNG_GLOB = "amplitude_response_*.png"
    CSV_GLOB = "amp_sweep_*.csv"
    DESCRIPTION = (
        "Finds the drive amplitude for a clearly perceptible, comfortable cue: "
        f"with the PWM frequency fixed at the measured resonance "
        f"({lra_amplitude_sweep.FREQ_HZ} Hz), steps amp "
        f"{lra_amplitude_sweep.AMP_VALUES[0]}-{lra_amplitude_sweep.AMP_VALUES[-1]} "
        "and recommends the value whose RMS acceleration lands in the "
        f"{lra_amplitude_sweep.TARGET_BAND_MS2[0]}-{lra_amplitude_sweep.TARGET_BAND_MS2[1]} "
        f"m/s² target band. Takes ~{lra_amplitude_sweep.estimated_duration_s():.0f} s; "
        "CSV + response curve are saved to "
        "data/validation_experiments/lra_resonance_intensity_calibration/. Adopted "
        "project value: amp = 64 (see the folder's README)."
    )

    def _build_extra_config(self, config_row: QHBoxLayout) -> None:
        self.freq_spin = QSpinBox()
        self.freq_spin.setRange(50, 20000)  # firmware 'F' command's valid range
        self.freq_spin.setValue(lra_amplitude_sweep.FREQ_HZ)
        self.freq_spin.setSuffix(" Hz")
        self.freq_spin.setToolTip(
            "PWM frequency the amplitudes are measured at. The default "
            f"{lra_amplitude_sweep.FREQ_HZ} Hz is the resonance measured by "
            "the frequency sweep; change it only after re-measuring the "
            "resonance there - LRA amplitudes measured off-resonance are "
            "meaningless. Restored to the boot default when the sweep ends.")
        config_row.addWidget(QLabel("PWM freq:"))
        config_row.addWidget(self.freq_spin)
        config_row.addSpacing(12)
        self._config_inputs.append(self.freq_spin)

    def _extra_run_kwargs(self) -> dict:
        return {"freq_hz": self.freq_spin.value()}

    def _summary_text(self, summary: dict, saved: bool) -> str:
        text = (f"recommended amp: {summary['recommended_amp']} "
                f"({summary['recommended_rms_ms2']:.2f} m/s² RMS).")
        if saved:
            text += (f" Saved {os.path.basename(summary['csv_path'])} and "
                     f"{os.path.basename(summary['png_path'])}.")
        return text


class MotorAccDelayWindow(_SweepWindowBase):
    TITLE = "Motor → ACC Delay (Command Latency)"
    MODULE = motor_acc_delay
    PNG_GLOB = "delay_summary_*.png"
    CSV_GLOB = "delay_trials_*.csv"
    SETUP_HINT = (
        "<b>Physical setup (this test only):</b> glue the accelerometer to "
        "the motor under test, then <b>suspend the pair freely in the air</b> "
        "(e.g. hanging from its own wires) - do NOT fix it to the desk: desk "
        "mounting damps the vibration below reliable detection. Any starting "
        "orientation is fine; each trial automatically <b>waits until the rig "
        "hangs still</b> before measuring, so just let it settle after "
        "hanging it (and after each buzz). Use \"Test Buzz\" to confirm the "
        "selected motor port drives the actuator the sensor is attached to."
    )
    DESCRIPTION = (
        "Measures the command-to-vibration latency of an actuator with two "
        "standard onset metrics per trial: MOTION ONSET (CUSUM change-point "
        "detection - the first instant the signal departs from baseline "
        "noise, fair to both impulsive ERM starts and gradual LRA ring-ups) "
        "and DETECTION-LEVEL CROSSING "
        f"({motor_acc_delay.NOISE_MULT}× noise p95 - includes the LRA's "
        "resonant ring-up); both instants sub-sample refined by linear "
        f"interpolation. {motor_acc_delay.NUM_TRIALS} trials at amp="
        f"{motor_acc_delay.AMP}, ~{motor_acc_delay.estimated_duration_s():.0f} "
        f"s plus settling. Requires firmware ≥ v2.9.0 "
        f"({motor_acc_delay.ACC_INTERVAL_MS} ms stream at 1.344 kHz ODR). "
        "CSV + trace/summary figure + meta are saved to "
        "data/validation_experiments/motor_acc_delay_experiment/."
    )

    def _build_extra_config(self, config_row: QHBoxLayout) -> None:
        self.actuator_combo = QComboBox()
        self.actuator_combo.addItems(list(motor_acc_delay.ACTUATOR_MOTORS))
        self.actuator_combo.setToolTip(
            "Actuator under test - selecting one also sets its wiring-"
            "convention motor port (LRA → 11, ERM → 10); the port can "
            "still be overridden afterwards")
        self.actuator_combo.currentTextChanged.connect(self._on_actuator_changed)
        config_row.addWidget(QLabel("Actuator:"))
        config_row.addWidget(self.actuator_combo)
        config_row.addSpacing(12)
        self._config_inputs.append(self.actuator_combo)

        self.still_spin = QSpinBox()
        self.still_spin.setRange(10, 500)
        self.still_spin.setValue(int(motor_acc_delay.STILL_MAX_DEV))
        self.still_spin.setSuffix(" counts")
        self.still_spin.setToolTip(
            "Stillness gate: each trial waits until the 0.5 s-window p95 "
            "deviation drops below this. A genuinely still hanging rig "
            "reads ~30-45 counts, real swinging well over 100 - raise this "
            "if the log shows the p95 plateauing just above the limit while "
            "the rig looks still (that plateau is this rig's noise floor).")
        config_row.addWidget(QLabel("Stillness limit:"))
        config_row.addWidget(self.still_spin)
        config_row.addSpacing(12)
        self._config_inputs.append(self.still_spin)

    def _on_actuator_changed(self, actuator: str) -> None:
        self.motor_spin.setValue(motor_acc_delay.ACTUATOR_MOTORS[actuator])

    def _pre_buzz_cmds(self, motor: int) -> list:
        # An ERM never starts on the 224 Hz LRA boot default - give the
        # selected actuator its required PWM frequency before buzzing.
        freq = motor_acc_delay.ACTUATOR_PWM_HZ.get(
            self.actuator_combo.currentText(), motor_acc_delay.DEFAULT_PWM_FREQ)
        return [f"F {motor} {freq}"]

    def _extra_run_kwargs(self) -> dict:
        return {"actuator_type": self.actuator_combo.currentText(),
                "still_max_dev": float(self.still_spin.value())}

    def _summary_text(self, summary: dict, saved: bool) -> str:
        parts = []
        if summary.get("onset_mean_ms") is not None:
            sd = summary.get("onset_sd_ms")
            parts.append(f"motion onset {summary['onset_mean_ms']:.2f} ms"
                         + (f" (SD {sd:.2f})" if sd is not None else ""))
        if summary.get("mean_delay_ms") is not None:
            sd = summary.get("sd_delay_ms")
            parts.append(f"detection-level crossing "
                         f"{summary['mean_delay_ms']:.2f} ms"
                         + (f" (SD {sd:.2f})" if sd is not None else ""))
        if parts:
            text = (f"{summary['actuator_type']}: " + ", ".join(parts)
                    + f", n={summary['n_ok']}.")
        else:
            text = f"{summary.get('actuator_type', '?')}: no successful detections."
        if saved:
            text += (f" Saved {os.path.basename(summary['csv_path'])} and "
                     f"{os.path.basename(summary['png_path'])}.")
        return text
