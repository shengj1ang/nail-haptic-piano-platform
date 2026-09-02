"""Sequence/Music Metrics Viewer.

Recomputes every scalar component of D = (C_m, C_s, C_c) - see
final_report_2026/appendix/implementation.tex §"Difficulty Components"
- for every already-saved song under data/music/ (real
recordings) and data/sequence/ (generated sequences), auto-detected via
app.song_library.list_song_entries(). The metrics come from
app.sequence_generator.evaluate_song(), the exact same compute_stats()
machinery the Experiment Sequence Generator uses to accept/reject a
candidate while generating - implementation.tex requires this so that
"metrics shown during generation and metrics reported later are directly
comparable".

Span-normalised components need note bounds: generated sequences carry
their generation START_NOTE/END_NOTE in meta.json; real recordings fall
back to the active profile's range (or their own min/max note when no
profile is calibrated).

Two checkboxes (Show music / Show sequence) and a name filter box narrow
the table down; both apply instantly against data already loaded by the
last Refresh, rather than re-scanning disk on every keystroke or checkbox
click - see _apply_filters().

"Validate Stimulus Set" runs app.stimulus_validation.validate_level_pools
(median/IQR/range per level, monotonic medians with <10% adjacent-pair
violations, separate cross-region and structural checks) over whichever
*generated* sequences are currently visible in the table (data/sequence/
only - a real recording's difficulty label isn't tied to the generator's
alpha/beta/gamma grammar, so pooling it into the levels wouldn't mean
anything). That means the Show/Filter controls above double as "which
sequences to compare" - type a batch name into "Name contains" to scope
the comparison to one generation run instead of pooling every generated
sequence ever saved, then click Validate; a confirmation dialog names the
batch(es) about to be compared before the report opens (see
app/gui/stimulus_validation_dialog.py), so picking the wrong filter is
caught before running the analysis rather than after.

"Export CSV" follows the same visible-rows convention: it writes the
table exactly as currently filtered - same columns, same rows - to a
CSV file picked in a save dialog (default data/sequence/metrics.csv).

Read-only otherwise: this doesn't generate or save anything, it only
reports what a saved fingering.json already contains. A real recording's
fingering can have unresolved/ambiguous notes (the camera pipeline
couldn't confidently match a finger) - those are excluded from the
metrics (see app.sequence_generator.sequence_from_fingering), so the
Resolved column shows how many of a song's notes actually went into the
numbers shown.
"""

import csv
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..music_recording import MUSIC_DATA_DIR, META_FILENAME, SongMeta, song_dir
from ..profiles import DATA_DIR as PROFILE_DATA_DIR
from ..sequence_generator import (
    COMPONENTS,
    LEVEL_DISPLAY,
    LEVEL_DIFFICULTY,
    SEQUENCE_DATA_DIR,
    Sequence,
    SequenceStats,
    evaluate_song,
    format_sequence_for_display,
    profile_note_range,
)
from ..song_library import SongEntry, list_song_entries
from ..stimulus_validation import validate_level_pools
from .stimulus_validation_dialog import StimulusValidationDialog

COLUMNS = ["Entry", "Difficulty", "Resolved", "Fingers", "Notes"] + [spec.label for spec in COMPONENTS]

METRICS_EXPLANATION = (
    "Difficulty is the multidimensional representation D = (C_m, C_s, C_c); no scalar score is computed.  "
    "C_m (motor cost): d̄_seq mean displacement between consecutive cue events, d̄_m / d_m95 mean and 95th-"
    "percentile same-hand key displacement, d̄_f mean same-hand finger-transition distance, R_L / R_R per-hand "
    "note ranges (all in semitones).  "
    "C_s (sequence complexity): H_norm normalised transition-class entropy, V_trans distinct-class fraction, "
    "P_pred first-order predictability (lower = less predictable).  "
    "C_c (bimanual coordination): A_h hand-alternation frequency, H_hand hand-transition entropy, B_h "
    "left/right balance, O_LR overlap of the hands' used note regions, X_f / X_e cross-region frequency and "
    "extent."
)

_DIFFICULTY_TO_LEVEL = {v: k for k, v in LEVEL_DIFFICULTY.items()}

# Matches the generator's default naming, "<batch>-<level symbol>-<id>" or
# just "<level symbol>-<id>" with no batch - so a batch name (if any) can be
# read back out of a saved sequence's own name for the confirmation dialog
# below. A hand-edited name that doesn't match this shape isn't an error,
# it just can't be attributed to a particular batch.
_SEQUENCE_NAME_PATTERN = re.compile(r"^(?:(?P<batch>.+)-)?(?P<symbol>[αβγ])-(?P<id>\d+)$")


def _parse_batch_name(name: str) -> str:
    match = _SEQUENCE_NAME_PATTERN.match(name)
    if not match:
        return "(unrecognized name format)"
    return match.group("batch") or "(no batch name)"


@dataclass
class _RowData:
    entry: SongEntry
    is_music: bool
    difficulty: Optional[int]
    actions: Optional[Sequence]  # None if evaluate_song() failed for this entry
    stats: Optional[SequenceStats]
    cells: List[str]  # COLUMNS[1:] - "Entry" itself is entry.label, shown separately


class SequenceMetricsWindow(QMainWindow):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__()
        self.setWindowTitle("Sequence/Music Metrics")
        self.cfg = cfg or Config.load()
        self.resize(1700, 900)

        self._rows_data: List[_RowData] = []

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Every song under data/music/ and data/sequence/, recomputed live:"))
        top_row.addStretch(1)
        top_row.addWidget(refresh_btn)

        self.show_music_check = QCheckBox("Show music")
        self.show_music_check.setChecked(True)
        self.show_music_check.toggled.connect(self._apply_filters)
        self.show_sequence_check = QCheckBox("Show sequence")
        self.show_sequence_check.setChecked(True)
        self.show_sequence_check.toggled.connect(self._apply_filters)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by name - blank shows everything")
        self.search_edit.textChanged.connect(self._apply_filters)

        filter_row = QHBoxLayout()
        filter_row.addWidget(self.show_music_check)
        filter_row.addWidget(self.show_sequence_check)
        filter_row.addWidget(QLabel("Name contains:"))
        filter_row.addWidget(self.search_edit, 1)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        # Excel-like columns: every border is user-draggable (Interactive).
        # The long Fingers/Notes columns get a wide starting width and the
        # table scrolls horizontally - Stretch mode would instead squeeze
        # them into whatever width the window leaves over, cutting them off
        # with no way to widen.
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(0, 200)  # Entry
        self.table.setColumnWidth(3, 480)  # Fingers
        self.table.setColumnWidth(4, 640)  # Notes
        self.table.verticalHeader().setVisible(False)

        self.validate_btn = QPushButton(
            "Validate Stimulus Set — α (alpha), β (beta), γ (gamma), visible rows")
        self.validate_btn.clicked.connect(self._validate_stimulus_set)

        self.export_csv_btn = QPushButton("Export CSV (currently visible rows)")
        self.export_csv_btn.setToolTip(
            "Save the table exactly as currently filtered - same columns, same rows - to a CSV file."
        )
        self.export_csv_btn.clicked.connect(self._export_csv)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.validate_btn, 1)
        buttons_row.addWidget(self.export_csv_btn)

        metrics_label = QLabel(METRICS_EXPLANATION)
        metrics_label.setWordWrap(True)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top_row)
        layout.addLayout(filter_row)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons_row)
        layout.addWidget(metrics_label)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self._refresh()

    # ------------------------------------------------------------------
    # Loading (disk I/O - only on Refresh)
    # ------------------------------------------------------------------

    def _fallback_bounds(self) -> Optional[Tuple[int, int]]:
        """The active profile's note range, for entries whose meta.json
        carries no generation bounds (real recordings, old sequences)."""
        try:
            return profile_note_range(self.cfg.active_keyboard_profile, PROFILE_DATA_DIR)
        except Exception:
            return None

    def _refresh(self) -> None:
        entries = list_song_entries()
        self._rows_data = []
        fallback = self._fallback_bounds()

        failed = []
        for entry in entries:
            is_music = entry.data_dir == MUSIC_DATA_DIR
            difficulty = self._read_difficulty(entry)

            try:
                actions, stats, total_notes = evaluate_song(entry.name, entry.data_dir, fallback_bounds=fallback)
            except Exception as exc:
                failed.append(f"{entry.label}: {exc}")
                self._rows_data.append(
                    _RowData(entry, is_music, difficulty, None, None, ["-"] * (len(COLUMNS) - 1))
                )
                continue

            level = _DIFFICULTY_TO_LEVEL.get(difficulty)
            fingers, notes = format_sequence_for_display(actions)
            cells = [
                LEVEL_DISPLAY[level] if level is not None else ("?" if difficulty is None else str(difficulty)),
                f"{len(actions)}/{total_notes}",
                fingers,
                notes,
            ] + [f"{stats.metric(spec.key):.3f}" for spec in COMPONENTS]
            self._rows_data.append(_RowData(entry, is_music, difficulty, actions, stats, cells))

        if not entries:
            self.status_label.setText("No songs found under data/music/ or data/sequence/.")
        elif failed:
            self.status_label.setText(
                f"Loaded {len(entries) - len(failed)}/{len(entries)} - couldn't read: {'; '.join(failed)}"
            )
        else:
            self.status_label.setText(f"Loaded {len(entries)} song(s).")

        self._apply_filters()

    # ------------------------------------------------------------------
    # Filtering (in-memory only, no disk I/O - runs on every keystroke/toggle)
    # ------------------------------------------------------------------

    def _apply_filters(self) -> None:
        show_music = self.show_music_check.isChecked()
        show_sequence = self.show_sequence_check.isChecked()
        keyword = self.search_edit.text().strip().lower()

        self.table.setRowCount(0)
        for row_data in self._rows_data:
            if row_data.is_music and not show_music:
                continue
            if not row_data.is_music and not show_sequence:
                continue
            if keyword and keyword not in row_data.entry.label.lower():
                continue

            row = self.table.rowCount()
            self.table.insertRow(row)
            self._set_cell(row, 0, row_data.entry.label)
            for col, text in enumerate(row_data.cells, start=1):
                self._set_cell(row, col, text)

        # Fit the short columns (Level/Resolved + components) to their
        # content; Entry/Fingers/Notes keep their user-adjustable widths.
        for col in range(1, len(COLUMNS)):
            if col not in (3, 4):
                self.table.resizeColumnToContents(col)

    def _set_cell(self, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, col, item)

    @staticmethod
    def _read_difficulty(entry: SongEntry) -> Optional[int]:
        try:
            meta = SongMeta.load(song_dir(entry.name, entry.data_dir) / META_FILENAME)
        except Exception:
            return None
        return meta.difficulty

    # ------------------------------------------------------------------
    # CSV export - only the currently visible (filtered) rows
    # ------------------------------------------------------------------

    def _export_csv(self) -> None:
        """Write the table exactly as currently shown - the Show/Filter
        controls double as "which rows to export", same convention as the
        Validate button. Written with utf-8-sig so the α/β/γ names
        survive a double-click into Excel."""
        if self.table.rowCount() == 0:
            QMessageBox.information(
                self,
                "Nothing to export",
                "No rows are currently visible. Adjust the Show/Filter controls above, then try again.",
            )
            return

        path_str, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            str(SEQUENCE_DATA_DIR / "metrics.csv"),
            "CSV files (*.csv)",
        )
        if not path_str:
            return  # cancelled

        try:
            with open(path_str, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(COLUMNS)
                for row in range(self.table.rowCount()):
                    writer.writerow(
                        [self.table.item(row, col).text() for col in range(len(COLUMNS))]
                    )
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't export CSV", str(exc))
            return

        self.status_label.setText(f"Exported {self.table.rowCount()} visible row(s) to {path_str}.")

    # ------------------------------------------------------------------
    # Stimulus set validation - only the currently visible (filtered) rows
    # ------------------------------------------------------------------

    def _validate_stimulus_set(self) -> None:
        visible_labels = {self.table.item(row, 0).text() for row in range(self.table.rowCount())}

        pools: Dict[str, List[Tuple[Sequence, SequenceStats]]] = {}
        batches = set()
        n_records = 0
        for row_data in self._rows_data:
            if row_data.entry.label not in visible_labels:
                continue
            if row_data.is_music or row_data.stats is None or row_data.actions is None:
                continue
            level = _DIFFICULTY_TO_LEVEL.get(row_data.difficulty)
            if level is None:
                continue
            pools.setdefault(level, []).append((row_data.actions, row_data.stats))
            batches.add(_parse_batch_name(row_data.entry.name))
            n_records += 1

        if not pools:
            QMessageBox.information(
                self,
                "Nothing to validate",
                "No generated sequences (data/sequence/) are currently visible. Adjust the Show/Filter controls "
                'above - e.g. type a batch name into "Name contains" - to pick which ones to compare, then try '
                "again.",
            )
            return

        batch_summary = ", ".join(sorted(batches))
        reply = QMessageBox.question(
            self,
            "Confirm sequences to compare",
            f"About to validate {n_records} generated sequence(s) from batch(es): {batch_summary}.\n\n"
            "If this isn't the set you want to compare, click No and narrow the Show/Filter controls above "
            'first (e.g. type a batch name into "Name contains").',
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        report = validate_level_pools(pools)
        # Batch name(s) of the validated sequences identify the export
        # folder (data/sequence_validation/<batch id>/); for generator-named
        # batches the batch name is the generation seed.
        batch_id = "+".join(sorted(batches)) if batches else None
        dialog = StimulusValidationDialog(report, self, batch_id=batch_id)
        dialog.exec()
