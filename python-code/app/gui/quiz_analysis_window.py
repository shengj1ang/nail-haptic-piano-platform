"""Reusable "run finger-matching over a saved quiz" window.

Deliberately independent of how a quiz's cues were delivered - it only
ever looks at a quiz's recorded video/MIDI (data/quiz/<name>/raw/) plus
the target-vs-actual note/timing results already saved by the quiz
runner, never at the cue mechanism itself. That means the exact same
window works for today's screen-guided quiz (student_quiz.py,
guidance_type "visual") and any future one (e.g. a vibration-motor
guidance_type) without changes here.

Used two ways:
  - Automatically, right after a quiz finishes (student_quiz.py opens
    this with initial_quiz_name set, which starts the analysis right
    away).
  - Standalone (quiz_analysis.py, also reachable from the launcher's
    "Data Analysis" section) to (re-)analyze any past quiz picked from a
    dropdown.
"""

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..quiz import (
    META_FILENAME,
    RAW_NOTES_FILENAME,
    RAW_VIDEO_FILENAME,
    RESULTS_FILENAME,
    QuizMeta,
    list_quizzes,
    load_quiz_results,
    quiz_dir,
    quiz_raw_dir,
    save_quiz_results,
    summarize,
)
from .analyze_worker import AnalyzeWorker


class QuizAnalysisWindow(QMainWindow):
    def __init__(self, cfg: Config, initial_quiz_name: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Quiz Analysis")
        self.cfg = cfg

        self.quiz_combo = QComboBox()
        self.quiz_combo.currentTextChanged.connect(self._load_quiz)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(lambda: self._refresh_quizzes())

        self.info_label = QLabel("Pick a quiz to analyze.")
        self.info_label.setWordWrap(True)
        self.analyze_btn = QPushButton("Analyze")
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.clicked.connect(self._run_analysis)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.results_label = QLabel("")
        self.results_label.setWordWrap(True)

        quiz_row = QHBoxLayout()
        quiz_row.addWidget(QLabel("Quiz:"))
        quiz_row.addWidget(self.quiz_combo, 1)
        quiz_row.addWidget(refresh_btn)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(quiz_row)
        layout.addWidget(self.info_label)
        layout.addWidget(self.analyze_btn)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.results_label)
        self.setCentralWidget(central)

        self.meta: Optional[QuizMeta] = None
        self.results: list = []
        self._worker: Optional[AnalyzeWorker] = None

        self._refresh_quizzes(select=initial_quiz_name)
        if initial_quiz_name and self.meta is not None:
            self._run_analysis()

    # ------------------------------------------------------------------

    def _refresh_quizzes(self, select: Optional[str] = None) -> None:
        quizzes = list_quizzes()
        self.quiz_combo.blockSignals(True)
        self.quiz_combo.clear()
        self.quiz_combo.addItems(quizzes)
        self.quiz_combo.blockSignals(False)

        if not quizzes:
            self.info_label.setText("No quizzes found under data/quiz/. Run one with student_quiz.py first.")
            return

        target = select if select in quizzes else quizzes[0]
        self.quiz_combo.setCurrentText(target)
        self._load_quiz(target)

    def _load_quiz(self, name: str) -> None:
        if not name:
            return
        try:
            self.meta = QuizMeta.load(quiz_dir(name) / META_FILENAME)
            self.results = load_quiz_results(quiz_dir(name) / RESULTS_FILENAME)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't load quiz", str(e))
            return

        status = "already analyzed" if self.meta.analyzed else "not yet analyzed"
        self.info_label.setText(
            f"{name}: song {self.meta.song_name}  |  guidance {self.meta.guidance_type}  |  "
            f"{self.meta.note_count} notes  |  {status}"
        )
        self.analyze_btn.setText("Re-analyze" if self.meta.analyzed else "Analyze")
        self.analyze_btn.setEnabled(True)

        if self.meta.analyzed:
            self._show_summary()
        else:
            self.results_label.setText("")

    # ------------------------------------------------------------------

    def _run_analysis(self) -> None:
        if self.meta is None:
            return
        name = self.meta.quiz_name
        video_path = quiz_raw_dir(name) / RAW_VIDEO_FILENAME
        notes_path = quiz_raw_dir(name) / RAW_NOTES_FILENAME

        pressed = [r for r in self.results if not r.timed_out]
        if not pressed:
            self._finalize([])
            return

        self.analyze_btn.setEnabled(False)
        self.quiz_combo.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.results_label.setText("Matching fingers against the video...")

        self._worker = AnalyzeWorker(video_path, notes_path, self.meta.keyboard_profile_name)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._finalize)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
            self.results_label.setText(f"Matching fingers against the video... {done}/{total} frames")
        else:
            self.results_label.setText(f"Matching fingers against the video... {done} frames")

    def _on_failed(self, message: str) -> None:
        self.progress_bar.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.quiz_combo.setEnabled(True)
        QMessageBox.warning(self, "Analysis failed", message)

    def _finalize(self, matches: list) -> None:
        pressed = [r for r in self.results if not r.timed_out]
        for result, match in zip(pressed, matches):
            result.actual_finger = match.finger if match else None
            result.finger_correct = (
                (result.actual_finger == result.target_finger) if (match and result.target_finger) else None
            )

        name = self.meta.quiz_name
        save_quiz_results(self.results, quiz_dir(name) / RESULTS_FILENAME)

        summary = summarize(self.results)
        self.meta.hits = summary["hits"]
        self.meta.misses = summary["misses"]
        self.meta.note_accuracy = summary["note_accuracy"]
        self.meta.mean_timing_error_s = summary["mean_timing_error_s"]
        self.meta.finger_accuracy = summary["finger_accuracy"]
        self.meta.analyzed = True
        self.meta.save(quiz_dir(name) / META_FILENAME)

        self.progress_bar.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("Re-analyze")
        self.quiz_combo.setEnabled(True)
        self._show_summary()

    def _show_summary(self) -> None:
        meta = self.meta
        timing_text = f"{meta.mean_timing_error_s * 1000:.0f} ms" if meta.mean_timing_error_s is not None else "n/a"
        finger_text = f"{meta.finger_accuracy * 100:.0f}%" if meta.finger_accuracy is not None else "n/a"
        self.results_label.setText(
            f"Note Accuracy: {meta.note_accuracy * 100:.0f}% ({meta.hits}/{meta.note_count}, {meta.misses} missed)\n"
            f"Mean Timing Error: {timing_text}\n"
            f"Finger Accuracy: {finger_text}"
        )
