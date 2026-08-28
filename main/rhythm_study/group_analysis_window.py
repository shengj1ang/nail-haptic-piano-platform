"""Rhythm Group Analysis (launcher section 11).

Qt wrapper around :mod:`rhythm_study.analysis` and
:mod:`rhythm_study.analysis_figures`, both of which hold the logic and
neither of which imports Qt. Pick the participants, set the onset
tolerance, Run to see the summary, Export to write every table, figure
and statistic under ``data/RhythmStudy/group_figures/``.

Deliberately one window, not the main study's three. That study splits
Participant / Group / Computational Model because it has 27 trials in a
3x3 factorial and several different questions. This study asks one
question - does the fingering and timing survive the haptic cue being
removed - and answers it with one table of three probes, so a second
window would only be somewhere else to look.

Nothing here can touch the main study: it reads ``data/RhythmStudy/``
and the ``rhythm-`` prefixed folders under ``data/quiz/``, and writes
only into ``data/RhythmStudy/group_figures/``.
"""

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import Config

from . import analysis_figures
from .analysis import (
    DEFAULT_ONSET_TOLERANCE_MS,
    AnalysisConfig,
    AnalysisResult,
    RhythmAnalysisError,
    analyse,
    available_participants,
    export,
    summary_text,
)
from .schedule import DATA_DIR

OUTPUT_DIR = DATA_DIR / "group_figures"


class RhythmGroupAnalysisWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        self.setWindowTitle("Rhythm Experiment - Group Analysis")
        self.cfg = cfg or Config.load()
        self.resize(1100, 820)
        self._result: Optional[AnalysisResult] = None

        # ---------------------------------------------------- participants
        self.participant_list = QListWidget()
        self.participant_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_participants)
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn = QPushButton("Select none")
        none_btn.clicked.connect(lambda: self._set_all(False))

        picker_buttons = QHBoxLayout()
        picker_buttons.addWidget(all_btn)
        picker_buttons.addWidget(none_btn)
        picker_buttons.addWidget(refresh_btn)
        picker_buttons.addStretch(1)

        participant_box = QGroupBox("Participants")
        participant_layout = QVBoxLayout(participant_box)
        participant_layout.addWidget(self.participant_list, 1)
        participant_layout.addLayout(picker_buttons)

        # ---------------------------------------------------- settings
        self.tolerance_spin = QDoubleSpinBox()
        self.tolerance_spin.setRange(10.0, 2000.0)
        self.tolerance_spin.setSingleStep(25.0)
        self.tolerance_spin.setValue(DEFAULT_ONSET_TOLERANCE_MS)
        self.tolerance_spin.setSuffix(" ms")
        self.tolerance_spin.setToolTip(
            "How close an onset must be to count towards the combined complete-performance "
            "score. Only that score depends on it - the accuracy and error measures do not."
        )

        self.run_btn = QPushButton("Run analysis")
        self.run_btn.clicked.connect(self._run)
        self.export_btn = QPushButton("Export CSVs, statistics && figures")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export)

        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("Onset tolerance for the combined score:"))
        settings_row.addWidget(self.tolerance_spin)
        settings_row.addStretch(1)
        settings_row.addWidget(self.run_btn)
        settings_row.addWidget(self.export_btn)

        # ---------------------------------------------------- output
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        # A fixed-width font: the summary is aligned columns, and a
        # proportional font turns them back into prose.
        self.output.setFont(QFont("Menlo", 11))
        self.output.setPlainText(
            "Pick participants and press Run.\n\n"
            "The main comparison is Probe 1 vs Probe 2 vs Probe 3 - the three haptic-off\n"
            "measurements taken after 5, 10 and 15 training repetitions. The participant is\n"
            "the unit of inference throughout; note events are never treated as independent."
        )

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(participant_box)
        layout.addLayout(settings_row)
        layout.addWidget(self.output, 1)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self._refresh_participants()

    # ------------------------------------------------------------------

    def _refresh_participants(self) -> None:
        checked = set(self._selected())
        self.participant_list.clear()
        names = available_participants()
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # First load has nothing remembered, so everyone starts ticked.
            state = Qt.CheckState.Checked if (not checked or name in checked) else Qt.CheckState.Unchecked
            item.setCheckState(state)
            self.participant_list.addItem(item)
        if not names:
            self.status_label.setText(
                "No participants under data/RhythmStudy/ - run some sessions first."
            )

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.participant_list.count()):
            self.participant_list.item(row).setCheckState(state)

    def _selected(self) -> List[str]:
        return [
            self.participant_list.item(row).text()
            for row in range(self.participant_list.count())
            if self.participant_list.item(row).checkState() == Qt.CheckState.Checked
        ]

    def _config(self) -> AnalysisConfig:
        return AnalysisConfig(onset_tolerance_ms=self.tolerance_spin.value())

    # ------------------------------------------------------------------

    def _run(self) -> None:
        participants = self._selected()
        if not participants:
            QMessageBox.information(self, "Nothing selected", "Tick at least one participant.")
            return
        self.status_label.setText("Running...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._result = analyse(participants, self._config())
        except RhythmAnalysisError as exc:
            # The commonest case by far is a trial that has not been
            # through Quiz Analysis yet, and the message says which.
            self._result = None
            QMessageBox.warning(self, "Cannot analyse", str(exc))
            self.status_label.setText("Analysis stopped - see the message above.")
            return
        except Exception as exc:
            self._result = None
            QMessageBox.warning(self, "Analysis failed", f"{type(exc).__name__}: {exc}")
            self.status_label.setText("Analysis failed.")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.output.setPlainText(summary_text(self._result))
        self.export_btn.setEnabled(True)
        coverage = self._result.coverage
        self.status_label.setText(
            f"Analysed {len(coverage['participants'])} participant(s), "
            f"{coverage['total_trials']} completed trials, {len(self._result.events)} events."
        )

    def _export(self) -> None:
        if self._result is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            written = export(self._result, OUTPUT_DIR)
            written += analysis_figures.render_all(self._result, OUTPUT_DIR)
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", f"{type(exc).__name__}: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.status_label.setText(f"Wrote {len(written)} file(s) to {OUTPUT_DIR}")
        QMessageBox.information(
            self,
            "Exported",
            f"{len(written)} file(s) written to:\n{OUTPUT_DIR}\n\n"
            + "\n".join(f"  {p.name}" for p in written),
        )
