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

The three vibration-intensity experiments also carry a "Plot metric"
selector (validation_experiments/acceleration_metrics.py): demeaned
three-axis vector RMS (the recommended default) or the legacy magnitude
RMS. Both are always measured and saved, so switching the selector
re-plots the displayed run - peak/recommendation, annotations and
colour-bar label all follow the choice - with no new hardware run. A run
saved before raw three-axis samples were kept offers the legacy metric
only; the vector option is then disabled rather than faked, because a
stored scalar magnitude RMS cannot be turned back into a vector RMS.
The Motor -> ACC Delay window has no selector: its figure is a latency
plot and its onset detector is deliberately left alone (see that
experiment's module docstring). It does carry a "Vibration duration"
input (0.5-10 s, default 2 s): the motor runs that long in every trial,
the whole window is recorded, and the value feeds the run-time estimate
shown next to the controls.

FULL-SIZE CHARTS: the in-window preview is a thumbnail - a four-panel
latency summary or a spectrogram carries far too much detail to read at
that size. Clicking the preview (or "Enlarge Chart") opens the PNG at
its own resolution in PlotPreviewWindow: fit / 100% / zoom buttons,
Ctrl+scroll and Ctrl+±, scrollbars for panning once it is bigger than
the window. ONE such window is SHARED by every validation experiment
(shared_plot_preview()) - clicking a chart in another window swaps the
image instead of piling up windows - and it follows its source panel, so
a finished run or a Plot-metric switch refreshes what it shows. It
closes with the last validation window.

LAYOUT RULE FOR THESE WINDOWS: controls go on several short rows
(_extra_config_rows), and any sentence-length text is built with
_wrapped_label so it wraps instead of setting the window's minimum
width. A single long QLabel in a QHBoxLayout cannot wrap - it made
every one of these windows demand ~920 px and run off a laptop screen.
The opening size is clamped to the screen by _size_to_screen().
"""

import glob
import os
import tempfile
from functools import partial
from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import (
    QFontDatabase,
    QGuiApplication,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.gui.accelerometer_window import AccelerometerWindow
from validation_experiments.acceleration_metrics import (
    DEFAULT_METRIC,
    METRIC_NAMES,
    metric_spec,
)
from validation_experiments.actuator_spectrogram import actuator_spectrogram
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


class PlotPreviewWindow(QMainWindow):
    """Full-size viewer for a saved chart, SHARED by every validation
    window (see shared_plot_preview()).

    These figures are dense - a four-panel latency summary or a
    spectrogram is unreadable at in-panel size - so clicking any preview
    opens the PNG here at its own resolution, scrollable and zoomable.
    One instance is reused: clicking a chart in another experiment window
    swaps this window's image instead of piling up windows."""

    #: Fit never enlarges past 1:1 - upscaling a 150-dpi PNG only blurs it.
    MAX_FIT_ZOOM = 1.0
    ZOOM_STEP = 1.25
    MIN_ZOOM, MAX_ZOOM = 0.1, 8.0

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Chart preview")
        self._pixmap: QPixmap | None = None
        self._zoom = 1.0
        self._fit = True
        #: The _PlotView this window is currently showing, so a re-render
        #: in that panel (new run, metric switch) refreshes the preview.
        self.owner = None

        self._image = QLabel()
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Ignored)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._image)
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.fit_btn = QPushButton("Fit to window")
        self.fit_btn.setToolTip("Scale the chart to the window (Ctrl+0)")
        self.fit_btn.clicked.connect(self.fit_to_window)
        self.actual_btn = QPushButton("100%")
        self.actual_btn.setToolTip("Show the chart at its saved resolution "
                                   "(Ctrl+1)")
        self.actual_btn.clicked.connect(lambda: self.set_zoom(1.0))
        self.zoom_out_btn = QPushButton("−")
        self.zoom_out_btn.setToolTip("Zoom out (Ctrl+−, or Ctrl+scroll)")
        self.zoom_out_btn.clicked.connect(
            lambda: self.set_zoom(self._zoom / self.ZOOM_STEP))
        self.zoom_in_btn = QPushButton("+")
        self.zoom_in_btn.setToolTip("Zoom in (Ctrl++, or Ctrl+scroll)")
        self.zoom_in_btn.clicked.connect(
            lambda: self.set_zoom(self._zoom * self.ZOOM_STEP))
        for button in (self.zoom_out_btn, self.zoom_in_btn):
            button.setFixedWidth(36)

        self.info = QLabel()
        self.info.setStyleSheet("color: #9a9ba5;")  # muted

        bar = QHBoxLayout()
        bar.addWidget(self.fit_btn)
        bar.addWidget(self.actual_btn)
        bar.addWidget(self.zoom_out_btn)
        bar.addWidget(self.zoom_in_btn)
        bar.addSpacing(12)
        bar.addWidget(self.info)
        bar.addStretch(1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(bar)
        layout.addWidget(self._scroll, 1)
        self.setCentralWidget(central)

        for keys, slot in (
                (QKeySequence.StandardKey.ZoomIn,
                 lambda: self.set_zoom(self._zoom * self.ZOOM_STEP)),
                (QKeySequence("Ctrl+="),
                 lambda: self.set_zoom(self._zoom * self.ZOOM_STEP)),
                (QKeySequence.StandardKey.ZoomOut,
                 lambda: self.set_zoom(self._zoom / self.ZOOM_STEP)),
                (QKeySequence("Ctrl+0"), self.fit_to_window),
                (QKeySequence("Ctrl+1"), lambda: self.set_zoom(1.0)),
                (QKeySequence("Esc"), self.close)):
            QShortcut(keys, self, activated=slot)

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(int(available.width() * 0.85),
                        int(available.height() * 0.85))
        else:
            self.resize(1100, 800)

    # -- content --------------------------------------------------------

    def show_image(self, path: str, title: str = "", caption: str = "",
                   owner=None) -> None:
        """Display `path` (raising on an unreadable file), re-fitting for
        a NEW image but keeping the user's zoom when the same panel just
        re-rendered the same chart underneath them."""
        pixmap = QPixmap(path)
        if pixmap.isNull():
            raise ValueError(f"Could not read image: {path}")
        same_owner = owner is not None and owner is self.owner
        self._pixmap = pixmap
        self.owner = owner
        caption = caption or os.path.basename(path)
        self.setWindowTitle(f"{title} - {caption}" if title else caption)
        if not same_owner:
            self._fit = True
        self._apply_zoom()

    def fit_to_window(self) -> None:
        self._fit = True
        self._apply_zoom()

    def set_zoom(self, zoom: float) -> None:
        self._fit = False
        self._zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, float(zoom)))
        self._apply_zoom()

    def _fit_zoom(self) -> float:
        viewport = self._scroll.viewport().size()
        if self._pixmap is None or self._pixmap.width() == 0:
            return 1.0
        return min(viewport.width() / self._pixmap.width(),
                   viewport.height() / self._pixmap.height(),
                   self.MAX_FIT_ZOOM)

    def _apply_zoom(self) -> None:
        if self._pixmap is None:
            return
        if self._fit:
            self._zoom = self._fit_zoom()
        size = self._pixmap.size() * self._zoom
        scaled = self._pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        # setWidgetResizable(True) makes the scroll area stretch the label
        # to the VIEWPORT, which clips anything larger instead of
        # scrolling it - so it is only right while the image fits (where
        # it also keeps the image centred). Once the image is bigger, the
        # label must be sized to the image so scrollbars appear and the
        # user can pan.
        viewport = self._scroll.viewport().size()
        fits = (scaled.width() <= viewport.width()
                and scaled.height() <= viewport.height())
        self._scroll.setWidgetResizable(fits)
        self._image.setPixmap(scaled)
        if not fits:
            self._image.resize(scaled.size())
        self.info.setText(
            f"{self._pixmap.width()} × {self._pixmap.height()} px   ·   "
            f"{self._zoom * 100:.0f}%" + ("  (fit)" if self._fit else ""))

    # -- interaction ----------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit:
            self._apply_zoom()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.set_zoom(self._zoom * (self.ZOOM_STEP if delta > 0
                                            else 1 / self.ZOOM_STEP))
                event.accept()
                return
        super().wheelEvent(event)


#: The one preview window every validation experiment shares.
_shared_preview: PlotPreviewWindow | None = None


def shared_plot_preview() -> PlotPreviewWindow:
    """The shared full-size chart viewer, created on first use."""
    global _shared_preview
    if _shared_preview is None:
        _shared_preview = PlotPreviewWindow()
    return _shared_preview


def close_shared_plot_preview() -> None:
    """Drop the shared viewer - called when the last validation window
    closes, so it never lingers on its own."""
    global _shared_preview
    if _shared_preview is not None:
        _shared_preview.close()
        _shared_preview = None


class _PlotView(QLabel):
    """Shows a response-curve PNG scaled to the available space (aspect
    kept). Ignored size policy so the scaled pixmap never feeds back
    into the layout's size negotiation.

    CLICKING IT opens the chart full size in the shared
    PlotPreviewWindow - these figures carry far too much detail to read
    in a panel this size."""

    PLACEHOLDER = "No saved output yet - run the experiment or load a CSV."
    HINT = "Click the chart to open it full size in a separate window."

    clicked = Signal()

    def __init__(self):
        super().__init__(self.PLACEHOLDER)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        # Deliberately small: this is the floor the whole window can be
        # squeezed to on a short laptop screen, not the working size (the
        # splitter gives the preview most of the space by default and the
        # user can drag it).
        self.setMinimumHeight(130)
        self._pixmap: QPixmap | None = None
        self._path: str | None = None
        self._caption = ""
        self._title = ""

    def set_title(self, title: str) -> None:
        """Names this panel's chart in the shared preview's title bar."""
        self._title = title

    @property
    def path(self) -> str | None:
        return self._path

    def show_png(self, path: str, caption: str | None = None) -> None:
        """Display `path`. `caption` names the chart in the full-size
        preview's title bar - re-renders go to throwaway temp files whose
        names ("render_2.png") would tell the user nothing, so those
        callers pass the source run and metric instead."""
        pixmap = QPixmap(path)
        if pixmap.isNull():
            raise ValueError(f"Could not read image: {path}")
        self._pixmap = pixmap
        self._path = path
        self._caption = caption or os.path.basename(path)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(self.HINT)
        self._rescale()
        # A finished run or a metric switch replaces the image under an
        # open preview - keep that preview on the current chart.
        preview = _shared_preview
        if preview is not None and preview.isVisible() and preview.owner is self:
            preview.show_image(path, self._title, self._caption, owner=self)

    def open_full_size(self) -> bool:
        """Show this panel's chart in the shared preview window."""
        if self._path is None:
            return False
        preview = shared_plot_preview()
        preview.show_image(self._path, self._title, self._caption, owner=self)
        preview.show()
        preview.raise_()
        preview.activateWindow()
        return True

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

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
    # Whether this experiment plots a vibration-intensity metric the user
    # may choose. False for experiments whose figure is not an intensity
    # plot (the delay test's latency figure).
    METRIC_SELECTOR = True
    # Opening size, clamped to the screen by _size_to_screen(). Controls
    # belong on several wrapped rows (_extra_config_rows) rather than one
    # long one, so no window needs to be wider than this.
    PREFERRED_SIZE = (900, 760)

    #: Every open validation window, across all subclasses - the shared
    #: full-size chart preview is closed when the last one goes.
    _live_windows = set()
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
        _SweepWindowBase._live_windows.add(self)
        self._worker = None
        # Holds CSV re-renders so saved output files are never touched.
        self._tmpdir = tempfile.TemporaryDirectory(prefix="validation_sweep_")
        self._render_count = 0
        # The run whose chart is on screen (its CSV path): set by the
        # latest-output preview, by Load CSV and by a finished run. The
        # Plot metric selector re-plots THAT run.
        self._current_csv = None

        # Long prose wraps to whatever width the window has instead of
        # dictating one: without this a paragraph-length DESCRIPTION sets
        # the window's minimum width and pushes it off a laptop screen.
        description = self._wrapped_label(self.DESCRIPTION)
        setup_hint = self._wrapped_label(self.SETUP_HINT)

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
        config_row.addStretch(1)
        # Its own wrapped line rather than a trailing item on the config
        # row: as a row item this note alone added ~380 px of unwrappable
        # width to every one of these windows.
        defaults_note = self._wrapped_label(
            "<i>Defaults recommended - only change them when the rig setup "
            "demands it.</i>")

        self.status = QLabel("Idle - connect the rig and press Start.")
        self.status.setWordWrap(True)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m steps")

        self.plot_view = _PlotView()
        self.plot_view.setToolTip(_PlotView.HINT)
        self.plot_view.set_title(self.TITLE)
        self.plot_view.clicked.connect(self._open_plot_preview)
        self.enlarge_btn = QPushButton("Enlarge Chart")
        self.enlarge_btn.setToolTip(
            "Open the chart full size in a separate window - these figures "
            "are too detailed to read in the panel. You can also just click "
            "the chart. The window is shared by every validation "
            "experiment, so it swaps to whichever chart you click last.")
        self.enlarge_btn.clicked.connect(self._open_plot_preview)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(70)
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
        buttons.addWidget(self.enlarge_btn)
        buttons.addWidget(self.acc_view_btn)
        buttons.addStretch(1)

        metric_row = self._build_metric_row()

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
        # Windows with more controls than fit on one line add extra config
        # rows here, so the config area wraps instead of running off-screen.
        for extra_row in self._extra_config_rows():
            layout.addLayout(extra_row)
        layout.addWidget(defaults_note)
        if metric_row is not None:
            layout.addLayout(metric_row)
        layout.addLayout(buttons)
        layout.addWidget(self.progress_bar)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self._size_to_screen()

        self._show_latest_output()

    # -- layout helpers -------------------------------------------------

    @staticmethod
    def _wrapped_label(text: str) -> QLabel:
        """A prose label that wraps instead of widening the window. The
        minimum width keeps a sensible measure on a narrow window; the
        Minimum vertical policy lets it grow taller as it wraps."""
        label = QLabel(text)
        label.setWordWrap(True)
        # Qt derives a window's minimum HEIGHT from heightForWidth at this
        # width, so too small a value makes long prose wrap into a very
        # tall minimum; 520 keeps both dimensions inside a laptop screen.
        label.setMinimumWidth(520)
        label.setSizePolicy(QSizePolicy.Policy.Preferred,
                            QSizePolicy.Policy.Minimum)
        return label

    def _size_to_screen(self) -> None:
        """Open at the preferred size but never larger than the screen -
        no fixed over-wide geometry that runs off a laptop display."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(*self.PREFERRED_SIZE)
            return
        available = screen.availableGeometry()
        width = min(self.PREFERRED_SIZE[0], int(available.width() * 0.92))
        height = min(self.PREFERRED_SIZE[1], int(available.height() * 0.92))
        self.resize(width, height)

    # -- subclass hooks -------------------------------------------------

    def _build_extra_config(self, config_row: QHBoxLayout) -> None:
        """Add experiment-specific parameter widgets to the config row;
        register any input in self._config_inputs to lock it during runs."""

    def _extra_config_rows(self) -> list:
        """Extra config rows (QHBoxLayouts) added below the main config row,
        for windows with more controls than fit on one line. Default: none.
        Widgets built here should still be appended to self._config_inputs."""
        return []

    def _on_csv_loaded(self, csv_path: str) -> None:
        """Called after "Load Chart from CSV" successfully rendered
        csv_path - subclasses may track it (e.g. for live re-styling)."""

    def _extra_run_kwargs(self) -> dict:
        """Experiment-specific kwargs forwarded to run_experiment()."""
        return {}

    def _pre_buzz_cmds(self, motor: int) -> list:
        """Commands sent right before a Test Buzz pulse (e.g. setting
        the PWM frequency the selected actuator needs)."""
        return []

    def _summary_text(self, summary: dict) -> str:
        raise NotImplementedError

    # -- plot metric ----------------------------------------------------

    def _build_metric_row(self) -> QHBoxLayout | None:
        """The "Plot metric" selector row (None when this experiment does
        not plot a vibration-intensity metric)."""
        if not self.METRIC_SELECTOR:
            self.metric_combo = None
            return None
        self.metric_combo = QComboBox()
        for name in METRIC_NAMES:
            self.metric_combo.addItem(metric_spec(name).gui_label, name)
        self.metric_combo.setCurrentIndex(
            self.metric_combo.findData(DEFAULT_METRIC))
        self.metric_combo.setToolTip(
            "Which vibration-intensity metric the chart, its peak and its "
            "colour-bar/axis label are based on.\n\n"
            "• Demeaned 3-axis vector RMS (recommended): each axis is "
            "demeaned over the measurement window, so gravity, the sensor's "
            "static bias and the mounting orientation drop out.\n"
            "• Legacy magnitude RMS: the original baseline-subtracted |a| "
            "formula, kept so new runs can be compared with historical ones.\n\n"
            "Both metrics are always measured and saved, so switching this "
            "only re-plots the displayed run - no new hardware run needed. "
            "Runs saved before raw three-axis samples were kept can only "
            "use the legacy metric.")
        # The user's own choice, separate from the combo's current value:
        # displaying a legacy-only run forces the combo to legacy, and a
        # NEW run must still default to the recommended metric.
        self._user_metric = DEFAULT_METRIC
        self.metric_combo.currentIndexChanged.connect(self._on_metric_changed)
        self._config_inputs.append(self.metric_combo)

        row = QHBoxLayout()
        row.addWidget(QLabel("Plot metric:"))
        row.addWidget(self.metric_combo)
        row.addSpacing(12)
        # Short by design - the full explanation is the combo's tooltip;
        # as a row item a long sentence sets the window's minimum width.
        row.addWidget(QLabel("<i>Both are measured every run.</i>"))
        row.addStretch(1)
        return row

    def _selected_metric(self) -> str | None:
        if not self.METRIC_SELECTOR or self.metric_combo is None:
            return None
        return self.metric_combo.currentData()

    def _set_metric_item_enabled(self, name: str, enabled: bool) -> None:
        index = self.metric_combo.findData(name)
        if index < 0:
            return
        item = self.metric_combo.model().item(index)
        item.setEnabled(enabled)
        if not enabled:
            item.setToolTip("Not available for this run - it has no raw "
                            "three-axis samples.")

    def _sync_metric_combo(self, csv_path: str | None) -> str:
        """Enable only the metrics the displayed run actually supports and
        select one of them, without triggering a re-render. Returns a
        note to append to the status line when the run is restricted."""
        if not self.METRIC_SELECTOR or self.metric_combo is None:
            return ""
        supported = list(METRIC_NAMES)
        if csv_path is not None:
            try:
                supported = self.MODULE.available_metrics_for(csv_path)
            except Exception:
                supported = list(METRIC_NAMES)
        self.metric_combo.blockSignals(True)
        for name in METRIC_NAMES:
            self._set_metric_item_enabled(name, name in supported)
        if supported and self.metric_combo.currentData() not in supported:
            self.metric_combo.setCurrentIndex(
                self.metric_combo.findData(supported[0]))
        self.metric_combo.blockSignals(False)
        if DEFAULT_METRIC in supported:
            return ""
        return (" This run has no raw three-axis samples, so only the "
                f"{metric_spec(supported[0]).short_label} is available - "
                "the vector RMS cannot be derived from a stored scalar RMS.")

    def _set_current_run(self, csv_path) -> str:
        """Remember which saved run the preview is showing, point the
        metric selector at what that run supports, and return the note
        (if any) the caller should append to its status line."""
        self._current_csv = csv_path
        return self._sync_metric_combo(csv_path)

    def _rerender_current(self, metric: str):
        """Re-plot the displayed run with `metric`; returns
        (png_path, summary_or_None). The summary is what lets the status
        line restate the metric-dependent RESULT (e.g. the amplitude
        sweep's target band and cue amp), not just the curve. Renders to
        a temp file so saved outputs stay untouched; subclasses that keep
        their own PNG in sync override this."""
        self._render_count += 1
        out_png = os.path.join(self._tmpdir.name,
                               f"render_{self._render_count}.png")
        summary = self.MODULE.render_csv(self._current_csv, out_png,
                                         metric=metric)
        return out_png, summary

    def _restore_user_metric(self) -> None:
        """Re-arm the selector for a NEW run: every metric is available
        again (a new run always saves raw samples), and the user's own
        choice - not the value a legacy-only preview forced - applies."""
        if not self.METRIC_SELECTOR or self.metric_combo is None:
            return
        self.metric_combo.blockSignals(True)
        for name in METRIC_NAMES:
            self._set_metric_item_enabled(name, True)
        self.metric_combo.setCurrentIndex(
            self.metric_combo.findData(self._user_metric))
        self.metric_combo.blockSignals(False)

    def _on_metric_changed(self) -> None:
        metric = self._selected_metric()
        self._user_metric = metric          # an explicit, user-made choice
        if self._worker is not None or self._current_csv is None:
            return
        try:
            png_path, summary = self._rerender_current(metric)
            self.plot_view.show_png(
                png_path,
                caption=f"{os.path.basename(self._current_csv)} · "
                        f"{metric_spec(metric).short_label}")
        except Exception as e:
            self.status.setText(f"Couldn't re-plot with that metric: {e}")
            return
        text = (f"Chart re-plotted from {os.path.basename(self._current_csv)} "
                f"using {metric_spec(metric).short_label}.")
        # Everything the metric governs - target band, recommendation,
        # in-band values - is restated here, so the status line can never
        # keep describing the metric that was showing a moment ago.
        if summary is not None:
            text += " " + self._summary_text(summary, saved=False)
        self.status.setText(text)

    # -- full-size chart preview ----------------------------------------

    def _open_plot_preview(self) -> None:
        if not self.plot_view.open_full_size():
            self.status.setText("No chart to enlarge yet - run the "
                                "experiment or load a CSV first.")

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
        # The PNG and its CSV share the run's <ts> stem, so the newest of
        # each belong to the same run.
        note = self._set_current_run(self._latest(self.CSV_GLOB))
        self.status.setText(
            f"Showing latest saved result: {os.path.basename(latest_png)} "
            "- press Start for a new run." + note
        )

    def _load_csv(self) -> None:
        latest_csv = self._latest(self.CSV_GLOB)
        start_dir = latest_csv if latest_csv else self.MODULE.OUTPUT_DIR
        csv_path, _ = QFileDialog.getOpenFileName(
            self, "Load sweep CSV", start_dir, "Sweep CSV (*.csv)")
        if not csv_path:
            return
        # Point the metric selector at what THIS run supports before
        # rendering, so an old legacy-only CSV is asked for the legacy
        # metric rather than one it cannot provide.
        note = self._set_current_run(csv_path)
        # Unique temp name per render: QPixmap must never see a stale file.
        self._render_count += 1
        out_png = os.path.join(self._tmpdir.name, f"render_{self._render_count}.png")
        try:
            summary = self.MODULE.render_csv(csv_path, out_png,
                                             **self._render_kwargs())
            self.plot_view.show_png(out_png,
                                    caption=os.path.basename(csv_path))
        except Exception as e:
            QMessageBox.warning(self, "Couldn't load CSV", str(e))
            return
        self._on_csv_loaded(csv_path)
        self.status.setText(
            f"Chart re-rendered from {os.path.basename(csv_path)} - "
            + self._summary_text(summary, saved=False) + note
        )

    def _render_kwargs(self) -> dict:
        """Extra kwargs for the module's render_csv() - the chosen plot
        metric for the intensity experiments, nothing for the others."""
        metric = self._selected_metric()
        return {} if metric is None else {"metric": metric}

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
        self._restore_user_metric()
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

        metric = self._selected_metric()
        run_fn = partial(
            self.MODULE.run_experiment,
            motor_index=self.motor_spin.value(),
            acc_sensor_id=self.acc_spin.value(),
            **({} if metric is None else {"plot_metric": metric}),
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
        note = self._set_current_run(summary.get("csv_path"))
        self.status.setText("Done - " + self._summary_text(summary, saved=True)
                            + note)

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
        # The full-size preview is shared, so it only goes away with the
        # LAST validation window - closing one of several must not pull
        # the chart out from under the others.
        _SweepWindowBase._live_windows.discard(self)
        if not _SweepWindowBase._live_windows:
            close_shared_plot_preview()
        if self._worker is not None:
            self._worker.request_stop()
            # Long enough for the slowest step to notice the request (the
            # delay test's drive window can be 10 s); the finally-block
            # also restores the rig.
            self._worker.wait(20000)
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
                f"({metric_spec(summary['metric']).short_label} = "
                f"{summary['resonance_counts']:.1f} counts).")
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
        "Finds the drive amplitude for a clearly perceptible, comfortable "
        f"cue: with the PWM frequency fixed at the measured resonance "
        f"({lra_amplitude_sweep.FREQ_HZ} Hz), steps amp "
        f"{lra_amplitude_sweep.AMP_VALUES[0]}-"
        f"{lra_amplitude_sweep.AMP_VALUES[-1]} and reports the "
        "<b>recommended cue amp</b> — the amp whose measured intensity is "
        "closest to the target cue intensity, <i>not</i> the amp that "
        "vibrates hardest. <b>Each metric has its own target band</b> (they "
        "are different rulers): "
        + " · ".join(
            f"{metric_spec(name).short_label} "
            f"{lra_amplitude_sweep.cue_target(name).band_label} "
            f"({lra_amplitude_sweep.cue_target(name).status_label})"
            for name in METRIC_NAMES)
        + f". Takes ~{lra_amplitude_sweep.estimated_duration_s():.0f} s; "
        "CSV + response curve are saved to "
        "data/validation_experiments/lra_resonance_intensity_calibration/. "
        "Adopted project value: amp = 64 (see the folder's README)."
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
        # Every number here is metric-specific: each metric has its own
        # target band (they are different rulers), so the band is always
        # quoted next to the amp it produced.
        label = metric_spec(summary["metric"]).short_label
        band = summary.get("target_band_ms2")
        amp = summary.get("recommended_cue_amp",
                          summary.get("recommended_amp"))
        if amp is None or not band:
            text = (f"{lra_amplitude_sweep.UNCALIBRATED_LABEL} for the "
                    f"{label} - curve plotted, but no cue amp is "
                    "recommended (another metric's band is never "
                    "substituted).")
        else:
            value = summary.get("recommended_cue_amp_ms2",
                                summary.get("recommended_ms2"))
            status = summary.get("target_calibration_status", "")
            text = (f"recommended cue amp: {amp} ({value:.2f} m/s², {label}) "
                    f"- closest to the {band[0]:g}-{band[1]:g} m/s² target "
                    f"band{f' [{status}]' if status else ''}. This is the "
                    "amp nearest the target INTENSITY, not the strongest "
                    "vibration.")
            in_band = summary.get("in_band_amps") or []
            if in_band:
                text += f" In band: {', '.join(str(a) for a in in_band)}."
        if saved:
            text += (f" Saved {os.path.basename(summary['csv_path'])} and "
                     f"{os.path.basename(summary['png_path'])}.")
        return text


class SpectrogramWindow(_SweepWindowBase):
    # 2-D drive-frequency x amp intensity map for either actuator type,
    # with a motor-port / actuator-type / precision / vibrate-time picker.
    TITLE = "Actuator Spectrogram (ERM/LRA)"
    MODULE = actuator_spectrogram
    PNG_GLOB = "spectrogram_*.png"
    CSV_GLOB = "spectrogram_*.csv"
    DESCRIPTION = (
        "Drives the motor at every (drive-frequency, amp) combination and "
        "fills a grid box with the measured accelerometer RMS intensity — "
        "x = amp, y = drive frequency, darker = stronger. Both intensity "
        "metrics are measured per cell and the full three-axis samples are "
        "saved, so \"Plot metric\" re-colours the map (and moves the peak) "
        "without a new run. Selecting the Type "
        "seeds the amp/frequency ranges (then adjustable). Test Buzz uses the "
        "LRA config (224 Hz, amp 64) for both types. Outputs are saved to "
        "data/validation_experiments/actuator_spectrogram/."
    )

    def _build_extra_config(self, config_row: QHBoxLayout) -> None:
        # This experiment covers the real motor ports only (LRA=11, ERM=10).
        # Its many parameters live on two wrapped rows below (see
        # _extra_config_rows), so the config area never runs off-screen.
        self.motor_spin.setRange(0, 11)

    def _extra_config_rows(self) -> list:
        # Row 1: what to sweep (type + the two ranges it seeds).
        self.type_combo = QComboBox()
        for t in actuator_spectrogram.MOTOR_TYPES:
            self.type_combo.addItem(t, t)
        self.type_combo.setToolTip(
            "Actuator type. Selecting it resets the amp and frequency ranges "
            "to that type's defaults (ERM freq 0-1000 Hz, LRA freq 0-350 Hz; "
            "both amp 0-255).")

        self.amp_min_spin = QSpinBox()
        self.amp_min_spin.setRange(actuator_spectrogram.AMP_MIN,
                                   actuator_spectrogram.AMP_MAX)
        self.amp_max_spin = QSpinBox()
        self.amp_max_spin.setRange(actuator_spectrogram.AMP_MIN,
                                   actuator_spectrogram.AMP_MAX)
        self.amp_min_spin.setToolTip("Lowest amp (PWM duty) to sweep.")
        self.amp_max_spin.setToolTip("Highest amp (PWM duty) to sweep.")

        self.freq_min_spin = QSpinBox()
        self.freq_min_spin.setRange(0, actuator_spectrogram.FREQ_MAX_LIMIT)
        self.freq_min_spin.setSuffix(" Hz")
        self.freq_max_spin = QSpinBox()
        self.freq_max_spin.setRange(actuator_spectrogram.FREQ_DRIVE_MIN,
                                    actuator_spectrogram.FREQ_MAX_LIMIT)
        self.freq_max_spin.setSuffix(" Hz")
        self.freq_min_spin.setToolTip(
            "Low end of the drive-frequency axis (the sweep can't drive below "
            f"{actuator_spectrogram.FREQ_DRIVE_MIN} Hz, the firmware minimum).")
        self.freq_max_spin.setToolTip("High end of the drive-frequency axis.")

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Type:"))
        row1.addWidget(self.type_combo)
        row1.addSpacing(16)
        row1.addWidget(QLabel("Amp:"))
        row1.addWidget(self.amp_min_spin)
        row1.addWidget(QLabel("–"))
        row1.addWidget(self.amp_max_spin)
        row1.addSpacing(16)
        row1.addWidget(QLabel("Freq:"))
        row1.addWidget(self.freq_min_spin)
        row1.addWidget(QLabel("–"))
        row1.addWidget(self.freq_max_spin)
        row1.addStretch(1)

        # Row 2: how to sweep + how to display.
        self.precision_combo = QComboBox()
        for name in actuator_spectrogram.PRECISION_STEPS:
            self.precision_combo.addItem(name, name)
        self.precision_combo.setCurrentText(actuator_spectrogram.DEFAULT_PRECISION)
        self.precision_combo.setToolTip(
            "Scan precision - steps BOTH the drive-frequency and the amp axes. "
            "Finer = more cells = a much longer 2-D sweep (Coarse/Medium/Fine = "
            "freq step 100/50/25 Hz, amp step 32/16/8).")

        self.vibrate_spin = QDoubleSpinBox()
        self.vibrate_spin.setRange(actuator_spectrogram.MEASURE_S_MIN,
                                   actuator_spectrogram.MEASURE_S_MAX)
        self.vibrate_spin.setDecimals(1)
        self.vibrate_spin.setSingleStep(0.5)
        self.vibrate_spin.setValue(actuator_spectrogram.MEASURE_S)
        self.vibrate_spin.setSuffix(" s")
        self.vibrate_spin.setToolTip(
            "How long each (frequency, amp) cell is driven continuously before "
            "its intensity is measured - the whole window is used. Longer = "
            "steadier reading, but a longer run.")

        self.annotate_combo = QComboBox()
        self.annotate_combo.addItem("No values", "off")
        self.annotate_combo.addItem("RMS value", "rms")
        self.annotate_combo.addItem("Normalized", "normalized")
        self.annotate_combo.setToolTip(
            "Print each cell's value inside its box: the selected metric's "
            "raw m/s² number, or a 0-1 normalised value (its position between "
            "the map's min and max). Text colour auto-contrasts per cell. "
            "Changing this re-renders the displayed run immediately and saves "
            "it straight into the run's PNG file. Best with Coarse precision "
            "- a dense grid gets crowded.")
        # Changing the Annotate mode (or the Plot metric) re-renders the
        # displayed run's own PNG in place; _current_csv, set by the base
        # class, says which run that is.
        self.annotate_combo.currentIndexChanged.connect(self._on_annotate_changed)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Precision:"))
        row2.addWidget(self.precision_combo)
        row2.addSpacing(16)
        row2.addWidget(QLabel("Vibrate:"))
        row2.addWidget(self.vibrate_spin)
        row2.addSpacing(16)
        row2.addWidget(QLabel("Annotate:"))
        row2.addWidget(self.annotate_combo)
        row2.addStretch(1)

        # Row 3: live run-time estimate for each precision, at the current
        # type / ranges / Vibrate time (so it reflects what you actually set).
        self.estimate_label = self._wrapped_label("")
        self.estimate_label.setStyleSheet("color: #9a9ba5;")  # muted
        row3 = QHBoxLayout()
        row3.addWidget(self.estimate_label)

        # Seed the ranges from the default type, then wire the change handlers
        # (so seeding itself doesn't re-trigger them).
        self._apply_type_defaults()
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        for w in (self.amp_min_spin, self.amp_max_spin,
                  self.freq_min_spin, self.freq_max_spin, self.vibrate_spin):
            w.valueChanged.connect(self._update_estimate)
        self._update_estimate()

        self._config_inputs.extend(
            [self.type_combo, self.amp_min_spin, self.amp_max_spin,
             self.freq_min_spin, self.freq_max_spin, self.precision_combo,
             self.vibrate_spin, self.annotate_combo])
        return [row1, row2, row3]

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        if seconds < 90:
            return f"{seconds:.0f}s"
        minutes = seconds / 60
        return f"{minutes:.0f}min" if minutes < 60 else f"{minutes / 60:.1f}h"

    def _estimate_for(self, precision: str) -> float:
        m = actuator_spectrogram
        freq_step, amp_step = m.steps_for(precision)
        freqs = m.freq_values(freq_step, self.freq_min_spin.value(),
                              self.freq_max_spin.value())
        amps = m.amp_values(amp_step, self.amp_min_spin.value(),
                            self.amp_max_spin.value())
        per_cell = m.SETTLE_S + self.vibrate_spin.value() + m.REST_S
        return len(freqs) * (m.BASELINE_S + len(amps) * per_cell)

    def _update_estimate(self) -> None:
        parts = [f"{p} ~{self._fmt_duration(self._estimate_for(p))}"
                 for p in actuator_spectrogram.PRECISION_STEPS]
        self.estimate_label.setText(
            "Est. run time (current ranges) —   " + "     ".join(parts))

    def _apply_type_defaults(self) -> None:
        """Reset the amp/frequency ranges to the selected type's defaults."""
        d = actuator_spectrogram.type_defaults(self.type_combo.currentData())
        self.amp_min_spin.setValue(d["amp_min"])
        self.amp_max_spin.setValue(d["amp_max"])
        self.freq_min_spin.setValue(d["freq_min"])
        self.freq_max_spin.setValue(d["freq_max"])

    def _on_type_changed(self) -> None:
        self._apply_type_defaults()
        # Point the port at the type's usual wiring (ERM=10, LRA=11); the user
        # can still override it.
        self.motor_spin.setValue(11 if self.type_combo.currentData() == "LRA" else 10)
        self._update_estimate()

    # -- live annotate / metric switching (writes the run's own PNG) -----

    def _sync_annotate_combo(self, csv_path: str) -> None:
        """Point the Annotate combo at the mode the displayed run was last
        rendered with (from its meta), without triggering a re-render."""
        meta = actuator_spectrogram.load_meta(csv_path)
        mode = (meta or {}).get("parameters", {}).get(
            "annotate_mode", actuator_spectrogram.DEFAULT_ANNOTATE)
        idx = self.annotate_combo.findData(mode)
        if idx >= 0:
            self.annotate_combo.blockSignals(True)
            self.annotate_combo.setCurrentIndex(idx)
            self.annotate_combo.blockSignals(False)

    def _set_current_run(self, csv_path) -> str:
        note = super()._set_current_run(csv_path)
        if csv_path is not None:
            self._sync_annotate_combo(csv_path)
        return note

    def _on_csv_loaded(self, csv_path: str) -> None:
        self._set_current_run(csv_path)

    def _rerender_current(self, metric: str):
        # This experiment keeps its saved PNG in step with the displayed
        # options (as the Annotate control already did), so the file on
        # disk always matches what the map claims in its colour-bar.
        # set_display_options returns only the path, so no summary.
        return actuator_spectrogram.set_display_options(
            self._current_csv, metric=metric), None

    def _on_annotate_changed(self) -> None:
        """Re-render the displayed run with the new annotate mode, saving
        straight into that run's own PNG file, and refresh the preview."""
        if self._worker is not None or self._current_csv is None:
            return
        mode = self.annotate_combo.currentData()
        try:
            png_path = actuator_spectrogram.set_display_options(
                self._current_csv, annotate_mode=mode,
                metric=self._selected_metric())
            self.plot_view.show_png(png_path)
        except Exception as e:
            self.status.setText(f"Couldn't re-render annotation: {e}")
            return
        self.status.setText(
            f"Annotation set to '{self.annotate_combo.currentText()}' - "
            f"saved into {os.path.basename(png_path)}.")

    def _pre_buzz_cmds(self, motor: int) -> list:
        # Test Buzz uses the LRA's best config (its 224 Hz resonance =
        # DEFAULT_PWM_FREQ; the base fires it at TEST_BUZZ_AMP = 64, the LRA's
        # adopted amp) for BOTH actuator types - a consistent known-good wiring
        # check, not the swept drive.
        return [f"F {motor} {actuator_spectrogram.DEFAULT_PWM_FREQ}"]

    def _extra_run_kwargs(self) -> dict:
        return {"motor_type": self.type_combo.currentData(),
                "precision": self.precision_combo.currentData(),
                "measure_s": self.vibrate_spin.value(),
                "annotate_mode": self.annotate_combo.currentData(),
                "amp_min": self.amp_min_spin.value(),
                "amp_max": self.amp_max_spin.value(),
                "freq_min": self.freq_min_spin.value(),
                "freq_max": self.freq_max_spin.value()}

    def _summary_text(self, summary: dict, saved: bool) -> str:
        text = (f"{summary.get('motor_type', '')} map done - strongest "
                f"vibration at {summary['peak_freq_hz']} Hz, amp "
                f"{summary['peak_amp']} ({summary['peak_ms2']:.2f} m/s², "
                f"{metric_spec(summary['metric']).short_label}).")
        if saved:
            text += (f" Saved {os.path.basename(summary['csv_path'])} and "
                     f"{os.path.basename(summary['png_path'])}.")
        return text


class MotorAccDelayWindow(_SweepWindowBase):
    TITLE = "Motor → ACC Delay (Command Latency)"
    MODULE = motor_acc_delay
    PNG_GLOB = "delay_summary_*.png"
    CSV_GLOB = "delay_trials_*.csv"
    # No plot-metric selector: this window's figure is a LATENCY plot, and
    # the onset detector runs on a per-sample statistic that a windowed RMS
    # cannot replace (see motor_acc_delay's module docstring). The run
    # still saves raw three-axis samples and both offline intensity
    # metrics per trial, shared with the sweeps.
    METRIC_SELECTOR = False
    # Kept short on purpose: the detail lives in the control tooltips and
    # in the experiment's README, so this window's minimum height stays
    # inside a laptop screen.
    SETUP_HINT = (
        "<b>Physical setup (this test only):</b> glue the accelerometer to "
        "the motor, then <b>suspend the pair freely in the air</b> — do NOT "
        "fix it to the desk (desk mounting damps the vibration below "
        "reliable detection). Any orientation is fine; each trial waits "
        "until the rig hangs still. Use \"Test Buzz\" to check the port."
    )
    DESCRIPTION = (
        "Measures two <b>separate</b> per-trial times: <b>vibration onset "
        "latency</b> (command → the first sustained departure from rest "
        "noise, by CUSUM change-point detection with a spike guard) and "
        "<b>settling time</b> (onset → the vibration envelope entering and "
        "holding the steady band). Both settling forms are saved — from the "
        "onset and from the command — plus the historical detection-level "
        f"crossing. {motor_acc_delay.NUM_TRIALS} trials at amp="
        f"{motor_acc_delay.AMP}; the motor runs for the whole Vibration "
        "duration and every raw X/Y/Z sample of it is saved. Firmware "
        "≥ v2.9.0. Outputs → "
        "data/validation_experiments/motor_acc_delay_experiment/."
    )

    def _build_extra_config(self, config_row: QHBoxLayout) -> None:
        # Only the actuator picker shares the first row with the motor
        # port and ACC sensor id; everything else lives on the wrapped
        # rows below, so the window fits a laptop screen.
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

    def _extra_config_rows(self) -> list:
        self.amp_spin = QSpinBox()
        self.amp_spin.setRange(1, 255)
        self.amp_spin.setValue(motor_acc_delay.AMP)
        self.amp_spin.setToolTip(
            "PWM drive amplitude (0-255 duty scale). The default "
            f"{motor_acc_delay.AMP} is the calibrated ~0.5 m/s² cue level - "
            "the latency at THIS amp is the one that bounds the study's "
            "timestamp error. Higher amps ring the LRA up faster and will "
            "read lower, but measure a different cue than the study "
            "delivers; use them for exploration only.")

        self.freq_spin = QSpinBox()
        self.freq_spin.setRange(50, 20000)  # firmware 'F' command's valid range
        self.freq_spin.setSuffix(" Hz")
        self.freq_spin.setValue(motor_acc_delay.ACTUATOR_PWM_HZ[
            self.actuator_combo.currentText()])
        self.freq_spin.setToolTip(
            "PWM drive frequency; follows the actuator selection "
            "automatically (LRA → 224 Hz resonance, ERM → 5 kHz so the "
            "chopped drive acts as smooth DC - an ERM never starts at "
            "224 Hz). Override only with a reason: an off-resonance LRA "
            "or a sub-kHz ERM invalidates the measurement. Restored to "
            "the boot default when the run ends.")

        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(motor_acc_delay.VIB_DURATION_MIN_S,
                                    motor_acc_delay.VIB_DURATION_MAX_S)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSingleStep(motor_acc_delay.VIB_DURATION_STEP_S)
        self.duration_spin.setValue(motor_acc_delay.VIB_DURATION_S)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setToolTip(
            "How long the motor is driven in each trial - and how much "
            "data the settling detector gets.\n\n"
            "The motor vibrates continuously for this whole time and every "
            "raw X/Y/Z accelerometer sample of the window is saved, so the "
            "trial can be re-analysed offline.\n\n"
            f"Default {motor_acc_delay.VIB_DURATION_S:g} s, range "
            f"{motor_acc_delay.VIB_DURATION_MIN_S:g}-"
            f"{motor_acc_delay.VIB_DURATION_MAX_S:g} s. The steady state is "
            "estimated from the last "
            f"{motor_acc_delay.STEADY_REF_FRACTION:.0%} of the window and "
            "the envelope must hold inside the steady band for "
            f"{motor_acc_delay.STEADY_HOLD_FRACTION:.0%} of the duration "
            f"(min {motor_acc_delay.STEADY_HOLD_MIN_S * 1000:.0f} ms, max "
            f"{motor_acc_delay.STEADY_HOLD_MAX_S * 1000:.0f} ms), so a "
            "longer window demands more evidence but takes longer to run. "
            "Shorten it only for a fast-settling actuator: a slow ERM "
            "spin-up needs the room.")
        self.duration_spin.valueChanged.connect(self._update_estimate)

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

        # Row 1: how the actuator is driven.
        drive_row = QHBoxLayout()
        drive_row.addWidget(QLabel("Drive amp:"))
        drive_row.addWidget(self.amp_spin)
        drive_row.addSpacing(16)
        drive_row.addWidget(QLabel("PWM freq:"))
        drive_row.addWidget(self.freq_spin)
        drive_row.addSpacing(16)
        drive_row.addWidget(QLabel("Vibration duration:"))
        drive_row.addWidget(self.duration_spin)
        drive_row.addStretch(1)

        # Row 2: how the measurement is gated, plus the live run-time
        # estimate (which the Vibration duration moves).
        measure_row = QHBoxLayout()
        measure_row.addWidget(QLabel("Stillness limit:"))
        measure_row.addWidget(self.still_spin)
        measure_row.addStretch(1)

        # Row 3: the live run-time estimate, on its own wrapped line (as
        # a row item this sentence alone forced a ~700 px window).
        self.estimate_label = self._wrapped_label("")
        self.estimate_label.setStyleSheet("color: #9a9ba5;")  # muted
        estimate_row = QHBoxLayout()
        estimate_row.addWidget(self.estimate_label)

        self._config_inputs.extend([self.amp_spin, self.freq_spin,
                                    self.duration_spin, self.still_spin])
        self._update_estimate()
        return [drive_row, measure_row, estimate_row]

    def _update_estimate(self) -> None:
        duration = self.duration_spin.value()
        total = motor_acc_delay.estimated_duration_s(duration)
        hold_ms = motor_acc_delay.steady_hold_s(duration) * 1000.0
        self.estimate_label.setText(
            f"<i>Est. run time ~{total:.0f} s plus settling — "
            f"{motor_acc_delay.NUM_TRIALS} × "
            f"({motor_acc_delay.BASELINE_DURATION_S:g} s baseline + "
            f"{duration:g} s vibration + "
            f"{motor_acc_delay.INTER_TRIAL_REST_S:g} s rest); "
            f"steady-state hold {hold_ms:.0f} ms.</i>")

    def _on_actuator_changed(self, actuator: str) -> None:
        # Selecting an actuator resets every actuator-dependent parameter
        # to that actuator's defaults (each can still be overridden after).
        self.motor_spin.setValue(motor_acc_delay.ACTUATOR_MOTORS[actuator])
        self.freq_spin.setValue(motor_acc_delay.ACTUATOR_PWM_HZ[actuator])
        self.amp_spin.setValue(motor_acc_delay.AMP)

    def _pre_buzz_cmds(self, motor: int) -> list:
        # An ERM never starts on the 224 Hz LRA boot default - give the
        # buzz the same PWM frequency the run would use.
        return [f"F {motor} {self.freq_spin.value()}"]

    def _extra_run_kwargs(self) -> dict:
        return {"actuator_type": self.actuator_combo.currentText(),
                "still_max_dev": float(self.still_spin.value()),
                "amp": self.amp_spin.value(),
                "pwm_freq_hz": self.freq_spin.value(),
                "vib_duration_s": self.duration_spin.value()}

    @staticmethod
    def _stat_phrase(summary: dict, prefix: str, label: str) -> str | None:
        """"<label> mean ± SD (median M, range lo-hi, n=N)" for one of the
        latency series, or None when nothing was detected."""
        mean = summary.get(f"{prefix}_mean_ms")
        if mean is None:
            return None
        sd = summary.get(f"{prefix}_sd_ms")
        text = f"{label} {mean:.2f} ms"
        if sd is not None:
            text += f" ± {sd:.2f}"
        median = summary.get(f"{prefix}_median_ms")
        low = summary.get(f"{prefix}_min_ms")
        high = summary.get(f"{prefix}_max_ms")
        details = []
        if median is not None:
            details.append(f"median {median:.2f}")
        if low is not None and high is not None:
            details.append(f"range {low:.2f}-{high:.2f}")
        details.append(f"n={summary.get(f'{prefix}_n', 0)}")
        return text + " (" + ", ".join(details) + ")"

    def _summary_text(self, summary: dict, saved: bool) -> str:
        # Onset latency and settling time are reported as separate
        # numbers - never combined into one "delay".
        parts = [p for p in (
            self._stat_phrase(summary, "onset", "onset latency"),
            self._stat_phrase(summary, "settling", "settling time"),
        ) if p]
        label = summary.get("actuator_type", "?")
        duration = summary.get("vib_duration_s")
        if duration:
            label += f", {duration:g} s drive"
        # Flag non-default drive so exploratory runs are never mistaken
        # for the calibrated-cue measurement.
        amp = summary.get("amp")
        if amp is not None and amp != motor_acc_delay.AMP:
            label += f" (amp {amp}, NOT the calibrated cue)"
        if parts:
            text = f"{label}: " + "; ".join(parts) + "."
        else:
            text = f"{label}: no vibration onset detected."
        failures = [f"{summary.get(key, 0)} {name}"
                    for key, name in (("n_no_onset", "no_onset"),
                                      ("n_not_settled", "not settled"),
                                      ("n_insufficient_data",
                                       "insufficient data"))
                    if summary.get(key)]
        text += f" {summary.get('n_ok', 0)}/{summary.get('n_trials', 0)} ok"
        text += (" (" + ", ".join(failures) + ")." if failures else ".")
        if saved:
            text += (f" Saved {os.path.basename(summary['csv_path'])} and "
                     f"{os.path.basename(summary['png_path'])}.")
        return text
