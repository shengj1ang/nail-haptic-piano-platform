"""Wiring Guide - launcher section 1 (initial setup).

A single reference for wiring the whole rig to the Teensy 4.1: the power
rails (3.3 V / 5 V / 10 V / GND), the pin assignments for the finger
motors, the two WS2812 LED strips and the LIS3DH accelerometer bus, all
next to a labelled board pinout so the pin numbers map straight onto the
physical header. The content mirrors the *Current Wiring* table in
teensy_driver/README.md - that file stays the source of truth.

The one interactive part is the accelerometer wiring scan. The four
accel bus wires (SCK / MOSI / MISO / CS) are easy to swap by accident,
and a wrong order just reads back 0xFF with no hint which wire is where.
With the sensor's four wires plugged onto pins 33/34/35/36 in ANY order,
"A SCAN" (firmware >= v2.10.0) tries all 24 role permutations and reports
the one that answers WHO_AM_I=0x33. It only works once 3.3 V and GND are
wired correctly - so the scan doubles as a check that the sensor is
actually powered.

The scan is read-only diagnostics: the firmware restores the normal bus
state when it finishes and never changes the running pin mapping, so the
standard wiring (33=SCK, 34=MOSI, 35=MISO, 36=CS) is still what the rest
of the platform expects. Serial I/O runs on a QThread (reusing
validation_experiments/rig.py) so the ~1 s scan never freezes the GUI.
"""

import sys
import time
from pathlib import Path

from common import haptic_config as hc

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFontDatabase, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from validation_experiments.rig import open_rig, send

# The mapping every other tool in the platform assumes. The scan is
# considered "passed" only when the detected mapping is exactly this.
STANDARD_MAPPING = {"sck": 33, "mosi": 34, "miso": 35, "cs": 36}
ROLE_ORDER = ["sck", "mosi", "miso", "cs"]
ROLE_LABEL = {"sck": "SCK", "mosi": "MOSI", "miso": "MISO", "cs": "CS"}

PINOUT_IMAGE = (
    Path(__file__).resolve().parent.parent
    / "assets" / "image" / "teensy41_card11a_rev3.png"
)

SCAN_TIMEOUT_S = 20.0  # the 24-permutation scan takes ~1 s; this is slack

# Dark theme, so this stand-alone sub-window matches the launcher instead
# of falling back to the default light palette (black text on the dark
# result panels was unreadable before).
STYLE_SHEET = """
QWidget#wiringRoot { background: #1e1f24; }
QLabel { color: #e6e7ec; }
QLabel#title { color: #f2f2f5; font-size: 18px; font-weight: 700; }
QLabel#result {
    background: #1e1f24;
    border: 1px solid #35363e;
    border-radius: 6px;
    padding: 10px;
}
QTabWidget::pane {
    border: 1px solid #35363e;
    border-radius: 8px;
    top: -1px;
    background: #26272e;
}
QTabBar::tab {
    background: #2a2b33;
    color: #c8c9d2;
    border: 1px solid #35363e;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 16px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #26272e; color: #7fb2ff; font-weight: 700; }
QTabBar::tab:hover { color: #eceef2; }
QTabWidget > QWidget { background: #26272e; }
QPushButton {
    padding: 9px 14px;
    border-radius: 6px;
    border: 1px solid #3a3b44;
    background: #2f303a;
    color: #eceef2;
    font-weight: 600;
}
QPushButton:hover { background: #3a3c48; border-color: #7fb2ff; }
QPushButton:disabled { color: #7d7e88; background: #24252c; }
QPlainTextEdit {
    background: #17181c;
    color: #d6d7dc;
    border: 1px solid #35363e;
    border-radius: 6px;
}
"""

# ---- static wiring content (mirrors teensy_driver/README.md) ----------

POWER_HTML = """
<table cellpadding='5' width='100%'>
  <tr style='color:#9fd0ff'>
    <th align='left'>Rail</th><th align='left'>Powers</th>
    <th align='left'>Source</th></tr>
  <tr><td><b>3.3 V</b></td>
      <td>Motor-driver board logic + LIS3DH accelerometer(s)</td>
      <td>Teensy <b>3.3V</b> pin</td></tr>
  <tr><td><b>5 V</b></td>
      <td>WS2812 LED strips (cut to the length you need)</td>
      <td>Teensy <b>Vin</b> pin (5 V)</td></tr>
  <tr><td><b>10 V</b></td>
      <td>Motor drive rail (chopped by PWM)</td>
      <td>External supply via boost module</td></tr>
  <tr><td><b>GND</b></td>
      <td>Everything — common return</td>
      <td>Tie <b>all</b> grounds together</td></tr>
</table>
<div style='color:#c9b072; margin-top:6px'>
&#9888; Teensy 4.1 pins are <b>not 5 V tolerant</b> — the LIS3DH and all
signal lines run at 3.3 V. LED strips: add a 1000&nbsp;&micro;F cap across
the strip power and a 330&nbsp;&Omega; resistor in each data line. Without a
shared ground nothing works reliably.
</div>
"""

PINS_HTML = """
<table cellpadding='5' width='100%'>
  <tr style='color:#9fd0ff'>
    <th align='left'>Pin(s)</th><th align='left'>Function</th></tr>
  <tr><td><b>0 – 9</b></td><td>Finger vibration motors (PWM) —
      L5&rarr;0 &hellip; R5&rarr;9</td></tr>
  <tr><td><b>{erm_port}</b></td><td>ERM test channel / spare motor</td></tr>
  <tr><td><b>{lra_port}</b></td><td>LRA test channel / spare motor</td></tr>
  <tr><td><b>24</b></td><td>WS2812 LED strip 0 data (cut to length)</td></tr>
  <tr><td><b>29</b></td><td>WS2812 LED strip 1 data (cut to length)</td></tr>
  <tr><td><b>33</b></td><td>Accel SPI <b>SCK</b> (shared bus)</td></tr>
  <tr><td><b>34</b></td><td>Accel SPI <b>MOSI</b> (shared bus)</td></tr>
  <tr><td><b>35</b></td><td>Accel SPI <b>MISO</b> (shared bus)</td></tr>
  <tr><td><b>36 / 37 / 38</b></td><td>Accel <b>CS</b> — one per sensor
      (id 0 / 1 / 2)</td></tr>
  <tr><td><b>USB</b></td><td>Serial link to host</td></tr>
</table>
<div style='color:#9a9ba5; margin-top:6px'>
Each accelerometer needs 6 wires: 3.3V, GND, SCK, MOSI, MISO, CS. Extra
sensors only add their own CS wire — the other five tap the shared bus.
</div>
<div style='color:#9fd0ff; margin-top:8px'>
{haptic_line}
</div>
"""


def pins_html() -> str:
    """The pin table, with the actuator test channels and the actuator
    currently in use filled in from common.haptic_config - one mapping
    for the whole project rather than a second copy in this page."""
    config = hc.get_haptic_config()
    defaults = config.active
    in_use = hc.actuator_label(config.using)
    return PINS_HTML.format(
        erm_port=hc.get_actuator_motor_port(hc.ERM),
        lra_port=hc.get_actuator_motor_port(hc.LRA),
        haptic_line=(
            f"Currently in use: <b>{in_use}</b> on port "
            f"<b>{hc.get_actuator_motor_port(config.using)}</b>, default "
            f"drive {defaults.default_frequency}&nbsp;Hz, amp "
            f"{defaults.default_amp} — set in Initial Setup &rarr; Haptic "
            "Actuator Defaults (config.json). The finger motors on pins "
            "0–9 are driven with the same default."),
    )


def parse_scan_match(line: str) -> dict | None:
    """Parse a firmware scan line into {role: pin}, or None if it is not a
    match line. Expected form:
        ACC SCAN CS=36 SCK=33 MOSI=34 MISO=35 WHOAMI=0x33 MATCH
    """
    if not line.startswith("ACC SCAN CS=") or "MATCH" not in line:
        return None
    mapping: dict[str, int] = {}
    for token in line.split():
        if "=" not in token:
            continue
        key, _, val = token.partition("=")
        key = key.lower()
        if key in ("sck", "mosi", "miso", "cs"):
            try:
                mapping[key] = int(val)
            except ValueError:
                return None
    if all(role in mapping for role in ROLE_ORDER):
        return mapping
    return None


class _ScanWorker(QThread):
    """Opens the rig, fires one `A SCAN`, and relays every scan line plus a
    parsed result. Read-only apart from the scan command itself."""

    line = Signal(str)      # raw firmware line, for the log view
    result = Signal(list)   # list[dict{role: pin}] of MATCH mappings
    failed = Signal(str)

    def run(self) -> None:
        try:
            ser = open_rig(log=self.line.emit, interactive=False)
        except Exception as e:
            self.failed.emit(str(e))
            return
        try:
            send(ser, "A STOP", wait_s=0.2)   # never mix scan output with a stream
            ser.reset_input_buffer()
            send(ser, "A SCAN", wait_s=0.0)

            matches: list[dict] = []
            deadline = time.monotonic() + SCAN_TIMEOUT_S
            while time.monotonic() < deadline:
                raw = ser.readline().decode("utf-8", errors="ignore").strip()
                if not raw:
                    continue
                self.line.emit(raw)
                match = parse_scan_match(raw)
                if match is not None:
                    matches.append(match)
                if raw.startswith("ACC SCAN DONE"):
                    break
            else:
                self.failed.emit("Timed out waiting for the scan to finish.")
                return
            self.result.emit(matches)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            try:
                ser.close()
            except Exception:
                pass


class WiringGuideWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.setWindowTitle("Wiring Guide")
        self.cfg = cfg if cfg is not None else Config.load()
        self._worker = None

        root = QWidget()
        root.setObjectName("wiringRoot")
        root.setStyleSheet(STYLE_SHEET)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)

        # ---- left: labelled Teensy 4.1 pinout -------------------------
        image_label = QLabel()
        image_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        pix = QPixmap(str(PINOUT_IMAGE))
        if not pix.isNull():
            image_label.setPixmap(
                pix.scaledToHeight(620, Qt.TransformationMode.SmoothTransformation))
        else:
            image_label.setText(f"(pinout image not found:\n{PINOUT_IMAGE})")
        outer.addWidget(image_label)

        # ---- right: title + tabbed guide (no scrolling) ---------------
        right = QWidget()
        rcol = QVBoxLayout(right)
        rcol.setContentsMargins(0, 0, 0, 0)
        rcol.setSpacing(10)

        title = QLabel("Wiring Guide — Teensy 4.1 rig")
        title.setObjectName("title")
        rcol.addWidget(title)

        tabs = QTabWidget()
        tabs.addTab(self._html_tab(POWER_HTML), "Power rails")
        tabs.addTab(self._html_tab(pins_html()), "Pin assignments")
        tabs.addTab(self._build_scan_tab(), "Accelerometer scan")
        rcol.addWidget(tabs, 1)

        outer.addWidget(right, 1)

        self.setCentralWidget(root)
        self.resize(1080, 720)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _html_tab(self, html: str) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 14, 14, 14)
        label = QLabel(html)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(label)
        lay.addStretch(1)
        return page

    def _build_scan_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 14, 14, 14)

        intro = QLabel(
            "The four accelerometer bus wires (SCK / MOSI / MISO / CS) are "
            "easy to swap. Plug them onto pins <b>33, 34, 35, 36</b> in "
            "<i>any</i> order, then run the scan — the firmware tries every "
            "wiring and tells you which wire belongs on which pin.<br><br>"
            "Correct order the platform expects: "
            "<b>33 = SCK, 34 = MOSI, 35 = MISO, 36 = CS</b>.<br>"
            "<span style='color:#c9b072'>Precondition: 3.3 V and GND must "
            "already be wired — the scan can only find a powered sensor. "
            "Keep CS on pin 36 during the scan (37/38 are outside it).</span>"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.scan_btn = QPushButton("Run Brute-Force Scan")
        self.scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_btn.clicked.connect(self._start_scan)
        lay.addWidget(self.scan_btn)

        self.status = QLabel("Ready. Plug the four wires onto 33/34/35/36, then scan.")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.result_label = QLabel("<i>No scan run yet.</i>")
        self.result_label.setObjectName("result")
        self.result_label.setWordWrap(True)
        self.result_label.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.result_label)

        lay.addWidget(QLabel("Scan log:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(140)
        self.log_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        lay.addWidget(self.log_view)
        return page

    # ------------------------------------------------------------------
    # Scan lifecycle
    # ------------------------------------------------------------------

    def _start_scan(self) -> None:
        if self._worker is not None:
            return
        self.log_view.clear()
        self.scan_btn.setEnabled(False)
        self.status.setText("Scanning… trying all 24 wiring permutations.")
        self.result_label.setText("<i>Scan in progress…</i>")

        self._worker = _ScanWorker()
        self._worker.line.connect(self.log_view.appendPlainText)
        self._worker.result.connect(self._on_result)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_result(self, matches: list) -> None:
        if not matches:
            self.status.setText("Scan complete: no accelerometer found.")
            self.result_label.setText(
                "<b style='color:#ff8f8f'>No LIS3DH detected on pins "
                "33/34/35/36.</b><br><br>Check that:<br>"
                "&bull; all four bus wires are on 33, 34, 35, 36<br>"
                "&bull; <b>3.3 V and GND are connected</b> (grounds common)<br>"
                "&bull; the sensor's CS is on pin 36 (not 37/38) for the scan<br>"
                "&bull; the wire contacts are seated<br><br>"
                "Then run the scan again."
            )
            return

        if len(matches) > 1:
            # More than one stable 0x33 is unexpected with a single sensor
            # (usually means noise); surface them all rather than guess.
            rows = "<br>".join(self._mapping_sentence(m) for m in matches)
            self.status.setText("Scan complete: multiple matches (unexpected).")
            self.result_label.setText(
                "<b style='color:#ffcf7f'>Multiple wiring matches found.</b><br>"
                "This is unusual for a single sensor — re-seat the wires "
                "and scan again. Detected:<br><br>" + rows
            )
            return

        mapping = matches[0]
        if mapping == STANDARD_MAPPING:
            self.status.setText("Scan complete: wiring is correct.")
            self.result_label.setText(
                "<b style='color:#8fdf8f'>&#10003; Wiring is correct.</b><br><br>"
                "33 = SCK, 34 = MOSI, 35 = MISO, 36 = CS.<br>"
                "The accelerometer is ready to use — no re-plugging needed."
            )
        else:
            self.status.setText("Scan complete: wires are in the wrong order.")
            self.result_label.setText(
                "<b style='color:#ffcf7f'>Wires found, but in the wrong "
                "order.</b><br><br>" + self._fix_table(mapping) +
                "<br>Move each wire to its <b>should be</b> pin, then scan "
                "again to confirm."
            )

    def _mapping_sentence(self, mapping: dict) -> str:
        parts = [f"{ROLE_LABEL[r]}=pin {mapping[r]}" for r in ROLE_ORDER]
        return ", ".join(parts)

    def _fix_table(self, mapping: dict) -> str:
        """A per-role 'now on / should be' table so the user can see at a
        glance which physical wire to move where."""
        rows = [
            "<tr style='color:#9fd0ff'><th align='left'>Wire</th>"
            "<th align='left'>Now on pin</th>"
            "<th align='left'>Should be on</th></tr>"
        ]
        for role in ROLE_ORDER:
            now = mapping[role]
            want = STANDARD_MAPPING[role]
            colour = "" if now == want else " style='color:#ffcf7f'"
            rows.append(
                f"<tr{colour}><td>{ROLE_LABEL[role]}</td>"
                f"<td>{now}</td><td>{want}</td></tr>"
            )
        return "<table cellpadding='4'>" + "".join(rows) + "</table>"

    def _on_failed(self, message: str) -> None:
        self.status.setText("Scan failed.")
        self.result_label.setText(
            f"<b style='color:#ff8f8f'>Could not run the scan.</b><br><br>"
            f"{message}"
        )

    def _on_finished(self) -> None:
        self._worker = None
        self.scan_btn.setEnabled(True)

    def closeEvent(self, event) -> None:
        worker = self._worker
        if worker is not None:
            worker.wait(int(SCAN_TIMEOUT_S * 1000) + 2000)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = WiringGuideWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
