"""Latency analysis of the real, recorded tele-training sessions.

The Network Latency Benchmark button fires synthetic probes to
characterise the path. This one reads the delays that *real* guidance
cues actually met: it opens the relay's own store (server/data), lists
the sessions that recorded per-cue timings, and for the session the
operator picks shows the transport and cue-delivery breakdown.

The analysis and every caveat live in remote_guidance.session_latency;
this window only lays the result out. The one rule that shapes the layout
is that module's: same-clock differences are latencies and are shown as
such (relay processing, the student's cue pipeline, the visual-haptic
skew), while the cross-machine leg has no synchronised clock behind it, so
only its offset-removed variation - the delivery jitter above the
session's own floor - is reported, never an absolute one-way number.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from remote_guidance.session_latency import (
    DEFAULT_DB,
    SessionLatency,
    analyse_session,
    list_sessions,
)

# Matplotlib is a platform dependency, but a missing Qt backend must not
# take the whole window down - the numbers are the result, the figure is a
# reading of them.
try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure

    _HAVE_MPL = True
except ImportError:  # pragma: no cover - environment without the Qt backend
    _HAVE_MPL = False


def _ms(d: Optional[Dict[str, float]], field: str = "median") -> str:
    return f"{d[field]:.2f} ms" if d else "n/a"


def _stat_table(title: str, d: Optional[Dict[str, float]], note: str = "") -> str:
    if not d:
        return f"<tr><td>{title}</td><td colspan='4' style='color:#888'>no data</td></tr>"
    row = (
        f"<tr><td>{title}</td>"
        f"<td align='right'>{d['median']:.2f}</td>"
        f"<td align='right'>{d['mean']:.2f}</td>"
        f"<td align='right'>{d['p95']:.2f}</td>"
        f"<td align='right'>{d['max']:.2f}</td>"
        f"<td align='right' style='color:#888'>{d['n']}</td></tr>"
    )
    if note:
        row += f"<tr><td colspan='6' style='color:#888'><i>{note}</i></td></tr>"
    return row


def _summary_html(r: SessionLatency) -> str:
    info = r.info
    parts: List[str] = []

    parts.append(f"<h3>Session {info.session_id[:8]} — {info.guidance_mode or '?'}</h3>")
    parts.append(
        f"<p>{info.timed_rows} cues with timing"
        + (f", note accuracy {r.note_accuracy * 100:.0f}%" if r.note_accuracy is not None else "")
        + (f", finger accuracy {r.finger_accuracy * 100:.0f}%" if r.finger_accuracy is not None else "")
        + (f", {r.timeouts} timeouts" if r.timeouts else "")
        + ".</p>"
    )

    def block(title: str, rows: List[str], caveat: str = "") -> None:
        parts.append(f"<h3>{title}</h3>")
        parts.append(
            "<table cellpadding='4' width='100%'>"
            "<tr><td></td><td align='right'><b>median</b></td><td align='right'><b>mean</b></td>"
            "<td align='right'><b>p95</b></td><td align='right'><b>max</b></td>"
            "<td align='right' style='color:#888'>n</td></tr>"
            + "".join(rows)
            + "</table>"
        )
        if caveat:
            parts.append(f"<p style='color:#888'><i>{caveat}</i></p>")

    block(
        "Transport &amp; rendering — same clock, real latencies (ms)",
        [
            _stat_table("Relay server processing", r.relay_processing_ms),
            _stat_table("Student cue build (local dispatch)", r.local_dispatch_ms),
            _stat_table("|Visual − haptic onset skew|", r.abs_visual_haptic_skew_ms,
                        "software render/dispatch moments, not physical LED/actuator onset"),
        ],
        "Both timestamps of each row above come from one machine's clock, so these are "
        "measured durations that need no synchronisation.",
    )

    block(
        "Network delivery — cross-machine, offset removed (ms)",
        [
            _stat_table("Delivery jitter above session floor", r.delivery_excess_ms),
        ],
        "Teacher, server and student clocks are not synchronised, so an absolute one-way "
        f"delay cannot be read. The session floor (min teacher→student span) here is "
        f"{r.session_floor_ms:.0f} ms and is offset + minimum path, NOT a latency; subtracting "
        "it cancels the constant offset and leaves the per-cue delivery jitter shown above."
        if r.session_floor_ms is not None else "No cross-machine spans in this session.",
    )

    block(
        "Cue queue &amp; human response (ms)",
        [
            _stat_table("Queue wait (paced by design)", r.queue_wait_ms,
                        "the cue waits for the previous trial to end; this is intended pacing, not delay"),
            _stat_table("Reaction time (student, verbatim)", r.reaction_time_ms),
        ],
    )

    return "".join(parts)


class RemoteLatencyAnalysisWindow(QMainWindow):
    """Pick one recorded tele-training session; read its real latency back."""

    def __init__(self, cfg=None):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("Remote Latency Analysis")
        self.resize(1120, 780)
        self._current: Optional[str] = None

        # -- left: the session picker --
        self.session_list = QListWidget()
        self.session_list.currentItemChanged.connect(self._on_pick)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.reload_sessions)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 10, 6, 10)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet("color:#888;")
        left_layout.addWidget(QLabel("Recorded tele-training sessions:"))
        left_layout.addWidget(self.session_list, 1)
        left_layout.addWidget(self.source_label)
        left_layout.addWidget(refresh_btn)
        left.setMaximumWidth(360)

        # -- right: summary + figure --
        self.header_label = QLabel("Select a session on the left.")
        self.header_label.setWordWrap(True)
        font = self.header_label.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.header_label.setFont(font)

        self.summary_view = QTextEdit()
        self.summary_view.setReadOnly(True)

        self.figure_host = QWidget()
        self.figure_layout = QVBoxLayout(self.figure_host)
        self.figure_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        figure_scroll = QScrollArea()
        figure_scroll.setWidgetResizable(True)
        figure_scroll.setWidget(self.figure_host)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 10, 10, 10)
        right_layout.addWidget(self.header_label)
        right_layout.addWidget(self.summary_view, 3)
        right_layout.addWidget(QLabel("Per-cue delivery jitter:"))
        right_layout.addWidget(figure_scroll, 2)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(left)
        layout.addWidget(right, 1)
        self.setCentralWidget(central)

        self.reload_sessions()

    # ------------------------------------------------------------------
    def reload_sessions(self) -> None:
        keep = self._current
        self.session_list.blockSignals(True)
        self.session_list.clear()

        if not Path(DEFAULT_DB).exists():
            self.session_list.blockSignals(False)
            self.source_label.setText(f"No database at {DEFAULT_DB}.")
            self.header_label.setText("No recorded sessions found.")
            self.summary_view.setHtml(
                "<p>The relay store <code>server/data/remote_guidance.db</code> is not present. "
                "Pull it from the server, then reopen this window.</p>"
            )
            self._clear_figures()
            return

        sessions = [s for s in list_sessions() if s.timed_rows > 0]
        for s in sessions:
            item = QListWidgetItem(s.label() + ("" if s.room_id else "  ·  (room row missing)"))
            item.setData(Qt.ItemDataRole.UserRole, s.session_id)
            self.session_list.addItem(item)
        self.session_list.blockSignals(False)
        self.source_label.setText(f"{len(sessions)} sessions with timing · {DEFAULT_DB}")

        if not sessions:
            self.header_label.setText("No sessions with timing data.")
            self.summary_view.setHtml(
                "<p>The database has no performance rows carrying a per-cue timings block yet.</p>"
            )
            self._clear_figures()
            return

        target = 0
        if keep:
            for i in range(self.session_list.count()):
                if self.session_list.item(i).data(Qt.ItemDataRole.UserRole) == keep:
                    target = i
                    break
        self.session_list.setCurrentRow(target)

    def _on_pick(self, current: Optional[QListWidgetItem], _previous=None) -> None:
        if current is None:
            return
        session_id = current.data(Qt.ItemDataRole.UserRole)
        self._current = session_id
        try:
            result = analyse_session(session_id)
        except Exception as exc:  # noqa: BLE001 - report, never crash the window
            self.header_label.setText(f"Session {session_id[:8]}")
            self.summary_view.setHtml(f"<p style='color:#b55'>Could not analyse: {exc}</p>")
            self._clear_figures()
            return
        self.header_label.setText(f"Session {session_id[:8]} — {result.info.guidance_mode or '?'}")
        self.summary_view.setHtml(_summary_html(result))
        self._show_figure(result)

    # ------------------------------------------------------------------
    def _clear_figures(self) -> None:
        while self.figure_layout.count():
            item = self.figure_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_figure(self, r: SessionLatency) -> None:
        self._clear_figures()
        excess = r.raw.get("delivery_excess_ms") or []
        if not _HAVE_MPL:
            hint = QLabel("matplotlib is unavailable, so the figure is not drawn; the numbers above stand.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#888;")
            self.figure_layout.addWidget(hint)
            return
        if not excess:
            hint = QLabel("This session has no cross-machine spans to plot.")
            hint.setStyleSheet("color:#888;")
            self.figure_layout.addWidget(hint)
            return

        fig = Figure(figsize=(7.4, 3.6), dpi=100)
        ax1, ax2 = fig.subplots(1, 2)
        ax1.plot(range(len(excess)), excess, linewidth=0.9, color="#3a76c4")
        ax1.set_title("Delivery jitter per cue")
        ax1.set_xlabel("cue index")
        ax1.set_ylabel("ms above session floor")
        ax2.hist(excess, bins=30, color="#3a76c4", alpha=0.85)
        ax2.set_title("Distribution")
        ax2.set_xlabel("ms above session floor")
        ax2.set_ylabel("cues")
        for ax in (ax1, ax2):
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
        fig.tight_layout()
        self.figure_layout.addWidget(FigureCanvas(fig))
