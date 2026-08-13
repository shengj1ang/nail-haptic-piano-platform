"""Read-only complexity and reference-constraint view for one saved song."""

from typing import Dict, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..profiles import DATA_DIR as PROFILE_DATA_DIR
from ..sequence_generator import LEVEL_DISPLAY, profile_note_range
from ..single_song_metrics import (
    CONSTRAINT_LABELS,
    METRIC_DESCRIPTIONS,
    SingleSongEvaluation,
    SingleSongEvaluationError,
    constraint_bound_text,
    evaluate_single_song,
    metric_groups,
    saved_difficulty_text,
)
from ..song_library import SongEntry, list_song_entries


REFERENCE_NOTICE = (
    "Descriptive metrics and reference constraint comparison only. The alpha/beta/gamma rows below show the "
    "underlying pass/violation evidence for the coefficient-free profile. They do not formally prove a real "
    "song's difficulty level. No weighted scalar difficulty score is computed."
)


class SingleSongMetricsWindow(QMainWindow):
    """Analyse only the currently selected music/<name> or sequence/<name>."""

    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        self.setWindowTitle("Single Song Complexity Evaluation")
        self.cfg = cfg or Config.load()
        self.resize(1250, 920)

        self._entries: Dict[str, SongEntry] = {}
        self._metric_tables: Dict[str, QTableWidget] = {}

        self.song_combo = QComboBox()
        self.song_combo.setMinimumContentsLength(28)
        self.song_combo.currentIndexChanged.connect(self._analyse_current)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh)

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Saved song:"))
        picker_row.addWidget(self.song_combo, 1)
        picker_row.addWidget(self.refresh_btn)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        scroll_content = QWidget()
        content_layout = QVBoxLayout(scroll_content)
        content_layout.setSpacing(10)

        overview_box = QGroupBox("Selected song")
        overview_layout = QVBoxLayout(overview_box)
        self.name_label = self._detail_label()
        self.source_label = self._detail_label()
        self.difficulty_label = self._detail_label()
        self.count_label = self._detail_label()
        self.bounds_label = self._detail_label()
        for label in (
            self.name_label,
            self.source_label,
            self.difficulty_label,
            self.count_label,
            self.bounds_label,
        ):
            overview_layout.addWidget(label)
        content_layout.addWidget(overview_box)

        profile_box = QGroupBox("Coefficient-free alpha / beta / gamma reference profile")
        profile_layout = QVBoxLayout(profile_box)
        self.constraint_status_label = self._detail_label()
        self.constraint_status_label.setStyleSheet("font-weight: bold;")
        self.overall_profile_label = self._detail_label()
        self.motor_profile_label = self._detail_label()
        self.sequence_profile_label = self._detail_label()
        self.coordination_profile_label = self._detail_label()
        for label in (
            self.constraint_status_label,
            self.overall_profile_label,
            self.motor_profile_label,
            self.sequence_profile_label,
            self.coordination_profile_label,
        ):
            profile_layout.addWidget(label)
        profile_method = QLabel(
            "Method: violation magnitudes are compared constraint by constraint using Pareto dominance. "
            "Different metrics are never added, averaged, ranked by pass count, or given coefficients. "
            "When levels trade advantages across metrics, the result remains Mixed / No unique closest. "
            "Metrics without existing level thresholds remain descriptive and do not affect the profile."
        )
        profile_method.setWordWrap(True)
        profile_method.setStyleSheet("font-style: italic;")
        profile_layout.addWidget(profile_method)
        content_layout.addWidget(profile_box)

        sequence_box = QGroupBox("Parsed sequence")
        sequence_layout = QVBoxLayout(sequence_box)
        sequence_layout.addWidget(QLabel("Finger sequence:"))
        self.fingers_edit = self._read_only_text(82)
        sequence_layout.addWidget(self.fingers_edit)
        sequence_layout.addWidget(QLabel("Note sequence:"))
        self.notes_edit = self._read_only_text(105)
        sequence_layout.addWidget(self.notes_edit)
        content_layout.addWidget(sequence_box)

        metrics_heading = QLabel(
            "D = (C_m, C_s, C_c) descriptive metrics — values are recomputed with the same implementation "
            "used by the Experiment Sequence Generator."
        )
        metrics_heading.setWordWrap(True)
        content_layout.addWidget(metrics_heading)
        for group, specs in metric_groups().items():
            box = QGroupBox(self._group_title(group))
            box_layout = QVBoxLayout(box)
            table = QTableWidget(len(specs), 3)
            table.setHorizontalHeaderLabels(["Metric", "Value", "Meaning"])
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            table.setWordWrap(True)
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            for row, spec in enumerate(specs):
                self._set_cell(table, row, 0, f"{spec.label} ({spec.key})")
                self._set_cell(table, row, 1, "—")
                self._set_cell(table, row, 2, METRIC_DESCRIPTIONS[spec.key])
            self._fit_table_height(table)
            self._metric_tables[group] = table
            box_layout.addWidget(table)
            content_layout.addWidget(box)

        notice = QLabel(REFERENCE_NOTICE)
        notice.setWordWrap(True)
        notice.setStyleSheet("font-weight: bold;")
        content_layout.addWidget(notice)

        reference_box = QGroupBox("Alpha / beta / gamma reference constraint comparison")
        reference_layout = QVBoxLayout(reference_box)
        self.reference_table = QTableWidget(0, 3)
        self.reference_table.setHorizontalHeaderLabels(
            ["Reference level", "Constraints satisfied", "Constraints violated"]
        )
        self.reference_table.verticalHeader().setVisible(False)
        self.reference_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.reference_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.reference_table.setWordWrap(True)
        reference_header = self.reference_table.horizontalHeader()
        reference_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        reference_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        reference_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        reference_layout.addWidget(self.reference_table)
        content_layout.addWidget(reference_box)

        structure_box = QGroupBox("Formal-sequence structural checks")
        structure_layout = QVBoxLayout(structure_box)
        structure_hint = QLabel(
            "These are the generator's length, two-hand, repeated-fragment, hand-share, consecutive-finger, "
            "and repeated-note fingering rules. Problems do not prevent descriptive metrics from being shown."
        )
        structure_hint.setWordWrap(True)
        structure_layout.addWidget(structure_hint)
        self.structure_edit = self._read_only_text(135)
        structure_layout.addWidget(self.structure_edit)
        content_layout.addWidget(structure_box)

        warnings_box = QGroupBox("Loading notes")
        warnings_layout = QVBoxLayout(warnings_box)
        self.warnings_label = self._detail_label()
        warnings_layout.addWidget(self.warnings_label)
        content_layout.addWidget(warnings_box)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(scroll_content)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(picker_row)
        layout.addWidget(self.status_label)
        layout.addWidget(scroll, 1)
        self.setCentralWidget(central)

        self._clear_results()
        self._refresh()

    @staticmethod
    def _detail_label() -> QLabel:
        label = QLabel("—")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _read_only_text(height: int) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setMaximumBlockCount(1000)
        edit.setMinimumHeight(height)
        edit.setMaximumHeight(height)
        return edit

    @staticmethod
    def _set_cell(table: QTableWidget, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, column, item)

    @staticmethod
    def _fit_table_height(table: QTableWidget, maximum: Optional[int] = None) -> None:
        table.resizeRowsToContents()
        height = table.horizontalHeader().height() + table.verticalHeader().length() + 6
        table.setFixedHeight(min(height, maximum) if maximum is not None else height)

    @staticmethod
    def _group_title(group: str) -> str:
        return {
            "C_m": "C_m — motor movement cost",
            "C_s": "C_s — sequence complexity",
            "C_c": "C_c — bimanual coordination",
        }[group]

    def _fallback_bounds(self) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
        try:
            return profile_note_range(self.cfg.active_keyboard_profile, PROFILE_DATA_DIR), None
        except Exception as exc:
            return None, str(exc)

    def _refresh(self) -> None:
        """Rescan pickers only; analysis remains limited to one selection."""
        previous = self.song_combo.currentText()
        try:
            entries = list_song_entries()
        except Exception as exc:
            self._entries = {}
            self.song_combo.clear()
            self.song_combo.setEnabled(False)
            self._clear_results()
            self.status_label.setText(f"Could not scan data/music/ and data/sequence/: {exc}")
            return

        self._entries = {entry.label: entry for entry in entries}
        self.song_combo.blockSignals(True)
        self.song_combo.clear()
        for entry in entries:
            self.song_combo.addItem(entry.label)

        target = previous if previous in self._entries else "music/123"
        index = self.song_combo.findText(target)
        if index < 0 and entries:
            index = 0
        if index >= 0:
            self.song_combo.setCurrentIndex(index)
        self.song_combo.blockSignals(False)
        self.song_combo.setEnabled(bool(entries))

        if not entries:
            self._clear_results()
            self.status_label.setText(
                "No saved songs were found under data/music/ or data/sequence/. "
                "Save a recording or generated sequence, then press Refresh."
            )
            return
        self._analyse_current()

    def _analyse_current(self, _index: int = -1) -> None:
        entry = self._entries.get(self.song_combo.currentText())
        if entry is None:
            self._clear_results()
            return

        fallback, fallback_error = self._fallback_bounds()
        try:
            result = evaluate_single_song(
                entry,
                fallback_bounds=fallback,
                fallback_bounds_error=fallback_error,
            )
        except SingleSongEvaluationError as exc:
            self._clear_results()
            self.name_label.setText(f"Song: {entry.name}")
            self.source_label.setText(f"Source: {entry.label.split('/', 1)[0]} ({entry.label})")
            self.status_label.setText(f"Could not analyse {entry.label}: {exc}")
            return
        except Exception as exc:
            # A corrupt individual entry must never escape into launcher.py.
            self._clear_results()
            self.name_label.setText(f"Song: {entry.name}")
            self.source_label.setText(f"Source: {entry.label.split('/', 1)[0]} ({entry.label})")
            self.status_label.setText(f"Unexpected error while analysing {entry.label}: {exc}")
            return

        self._show_result(result)

    def _clear_results(self) -> None:
        for label in (
            getattr(self, "name_label", None),
            getattr(self, "source_label", None),
            getattr(self, "difficulty_label", None),
            getattr(self, "count_label", None),
            getattr(self, "bounds_label", None),
            getattr(self, "constraint_status_label", None),
            getattr(self, "overall_profile_label", None),
            getattr(self, "motor_profile_label", None),
            getattr(self, "sequence_profile_label", None),
            getattr(self, "coordination_profile_label", None),
            getattr(self, "warnings_label", None),
        ):
            if label is not None:
                label.setText("—")
        if hasattr(self, "fingers_edit"):
            self.fingers_edit.clear()
            self.notes_edit.clear()
            self.structure_edit.clear()
        for table in self._metric_tables.values():
            for row in range(table.rowCount()):
                self._set_cell(table, row, 1, "—")
        if hasattr(self, "reference_table"):
            self.reference_table.setRowCount(0)
            self.reference_table.setFixedHeight(70)

    def _show_result(self, result: SingleSongEvaluation) -> None:
        source = result.entry.label.split("/", 1)[0]
        self.status_label.setText(f"Analysed {result.entry.label} successfully (read-only).")
        self.name_label.setText(f"Song title/name: {result.title}")
        self.source_label.setText(f"Source: {source} — {result.entry.label}")
        self.difficulty_label.setText(
            "Saved difficulty label: "
            f"{saved_difficulty_text(result.saved_difficulty)}. This is the label stored in meta.json when "
            "the song was saved; it is not a difficulty automatically calculated by this analysis."
        )
        self.count_label.setText(
            f"Notes: {result.total_notes} total in fingering.json; "
            f"{len(result.actions)} with successfully parsed fingers used for metrics."
        )
        self.bounds_label.setText(
            f"Metric note bounds: {result.stats.k_min} to {result.stats.k_max} "
            f"({result.note_bounds_source}; span S={result.stats.span:g})."
        )
        self._show_reference_profile(result, source)
        self.fingers_edit.setPlainText(result.fingers_text)
        self.notes_edit.setPlainText(result.notes_text)

        for group, specs in metric_groups().items():
            table = self._metric_tables[group]
            for row, spec in enumerate(specs):
                value = result.stats.metric(spec.key)
                self._set_cell(table, row, 1, f"{0.0 if abs(value) < 0.0005 else value:.3f}")
            self._fit_table_height(table)

        self.reference_table.setRowCount(len(result.reference_comparisons))
        for row, comparison in enumerate(result.reference_comparisons):
            self._set_cell(self.reference_table, row, 0, LEVEL_DISPLAY[comparison.level])
            satisfied = [self._satisfied_constraint_text(result, comparison.level, key)
                         for key in comparison.satisfied]
            violated = [self._violated_constraint_text(message) for message in comparison.violations]
            self._set_cell(
                self.reference_table,
                row,
                1,
                "\n".join(f"✓ {line}" for line in satisfied) if satisfied else "None",
            )
            self._set_cell(
                self.reference_table,
                row,
                2,
                "\n".join(f"✗ {line}" for line in violated) if violated else "None in numeric comparison",
            )
        self._fit_table_height(self.reference_table, maximum=620)

        if result.structural_problems:
            self.structure_edit.setPlainText(
                "Not compliant with the formal generated-sequence structure:\n"
                + "\n".join(f"• {problem}" for problem in result.structural_problems)
                + "\n\nMetrics above remain descriptive and were still computed."
            )
        else:
            self.structure_edit.setPlainText(
                "No formal structural violations were found. This still does not classify the song as an "
                "alpha, beta, or gamma difficulty level."
            )

        notes = []
        if result.metadata_warning:
            notes.append(result.metadata_warning)
        notes.extend(result.warnings)
        self.warnings_label.setText("\n".join(f"• {note}" for note in notes) if notes else "No loading warnings.")

    def _show_reference_profile(self, result: SingleSongEvaluation, source: str) -> None:
        if result.exact_constraint_matches:
            matches = self._level_list(result.exact_constraint_matches)
            if source == "music":
                qualification = (
                    "All displayed numeric and structural checks pass, but for a real recording this remains "
                    "reference compatibility—not an algorithmically proven musical-difficulty classification."
                )
            else:
                qualification = (
                    "All displayed numeric and structural checks pass; this reports compatibility with the "
                    "saved generator rules, not a scalar difficulty score."
                )
            self.constraint_status_label.setText(
                f"Displayed constraint status: exact reference match for {matches}. {qualification}"
            )
        else:
            self.constraint_status_label.setText(
                "Displayed constraint status: Unclassified — no alpha/beta/gamma reference level passes every "
                "displayed numeric constraint and formal structural check."
            )

        profiles = {profile.group: profile for profile in result.group_reference_profiles}
        incomparable = [profile for profile in profiles.values() if not profile.comparable]
        overall = result.overall_non_dominated_levels
        if len(overall) == 1:
            mechanical_text = (
                f"{self._level_list(overall)} is the uniquely non-dominated numeric reference. "
                "This is a closest reference, not a formal classification."
            )
        else:
            mechanical_text = (
                f"No unique closest level: {self._level_list(overall)} are non-dominated because each is "
                "better on at least one constraint and worse on another."
            )
        if incomparable:
            limitations = "; ".join(profile.limitation or profile.group for profile in incomparable)
            overall_text = (
                f"Not comparable as a complete three-group profile ({limitations}). In the mechanical numeric "
                f"comparison only: {mechanical_text}"
            )
        else:
            overall_text = mechanical_text
        if result.structural_problems:
            overall_text += " Structural failures remain separate hard warnings and are not converted to a score."
        self.overall_profile_label.setText(f"Overall numeric reference: {overall_text}")

        self.motor_profile_label.setText(self._group_profile_text("C_m motor profile", profiles["C_m"]))
        self.sequence_profile_label.setText(self._group_profile_text("C_s sequence profile", profiles["C_s"]))
        self.coordination_profile_label.setText(
            self._group_profile_text("C_c coordination profile", profiles["C_c"])
        )

    @classmethod
    def _group_profile_text(cls, heading, profile) -> str:
        if not profile.comparable:
            return f"{heading}: Not comparable — {profile.limitation}."
        levels = profile.non_dominated_levels
        if len(levels) == 1:
            return f"{heading}: {cls._level_list(levels)}-like (uniquely non-dominated)."
        return f"{heading}: Mixed / no unique closest ({cls._level_list(levels)} are non-dominated)."

    @staticmethod
    def _level_list(levels) -> str:
        return ", ".join(LEVEL_DISPLAY[level] for level in levels) if levels else "none"

    @staticmethod
    def _satisfied_constraint_text(result: SingleSongEvaluation, level: str, key: str) -> str:
        value = result.stats.metric(key)
        label = CONSTRAINT_LABELS.get(key, key)
        return f"{label}={value:.3f} within {constraint_bound_text(level, key)}"

    @staticmethod
    def _violated_constraint_text(message: str) -> str:
        key = message.split("=", 1)[0]
        return f"{CONSTRAINT_LABELS.get(key, key)}: {message}"
