"""Role-specific Qt styles for the two Tele-training clients.

The launcher established the project's dark, card-based visual language.
These sheets keep that language while making the two programs impossible to
confuse at a glance: teacher uses warm amber, student uses cool cyan.

Only the Tele-training windows import this module.  No platform-wide widget or
palette is changed here.
"""

from __future__ import annotations

from pathlib import Path


ASSET_DIR = Path(__file__).with_name("assets")


def _client_style(
    root_name: str,
    *,
    background: str,
    surface: str,
    surface_raised: str,
    border: str,
    accent: str,
    accent_hover: str,
    accent_text: str,
    dropdown_arrow: Path,
    checkbox_tick: Path,
    spin_up_arrow: Path,
    spin_down_arrow: Path,
) -> str:
    settings_name = root_name.replace("Central", "Settings")
    return f"""
QMainWindow, QWidget#{root_name}, QDialog#{settings_name}, QWizard, QWizardPage {{
    background: {background};
    color: #edf0f4;
}}
QWidget#{root_name}, QDialog#{settings_name}, QWizard, QWizardPage {{
    font-size: 13px;
}}
QLabel {{
    color: #d7dce2;
}}
QFrame#topBar {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 10px;
}}
QLabel#roleTitle {{
    color: #ffffff;
    font-size: 17px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel#dialogSubtitle {{
    color: #9da5af;
    padding-left: 1px;
}}
QLabel#stageChip, QLabel#roomChip {{
    color: {accent};
    background: {surface_raised};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 4px 9px;
    font-weight: 600;
}}
QLabel#linkStatus {{
    color: #aeb4bd;
    padding: 3px 5px;
}}
QLabel#globalStatus {{
    color: #c7ccd3;
    background: {surface};
    border-left: 3px solid {accent};
    border-radius: 4px;
    padding: 4px 9px;
}}
QLabel#sectionTitle {{
    color: #ffffff;
    font-size: 15px;
    font-weight: 650;
}}
QLabel#mutedText {{
    color: #9da5af;
}}
QLabel#midiMonitor {{
    color: {accent};
    background: {background};
    border: 1px solid {border};
    border-radius: 7px;
    padding: 10px;
    font-weight: 600;
}}
QGroupBox {{
    color: #e1e5ea;
    font-weight: 600;
    border: 1px solid {border};
    border-radius: 10px;
    margin-top: 13px;
    padding: 13px 10px 10px 10px;
    background: {surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 11px;
    padding: 0 6px;
    color: {accent};
}}
QGroupBox#cameraCard {{
    background: #0c0e11;
}}
QPushButton {{
    min-height: 20px;
    padding: 7px 12px;
    border-radius: 6px;
    border: 1px solid {border};
    background: {surface_raised};
    color: #edf0f4;
}}
QPushButton:hover {{
    border-color: {accent};
    background: {accent_hover};
}}
QPushButton:pressed {{
    background: {surface};
}}
QPushButton:disabled {{
    color: #676e77;
    background: {surface};
    border-color: {border};
}}
QPushButton[role="primary"] {{
    color: {accent_text};
    background: {accent};
    border-color: {accent};
    font-weight: 700;
}}
QPushButton[role="primary"]:hover {{
    background: {accent_hover};
    color: #ffffff;
}}
QPushButton[role="danger"] {{
    color: #ffb7b7;
    background: #3b2024;
    border-color: #704049;
}}
QPushButton[role="danger"]:hover {{
    background: #51282e;
    border-color: #df737d;
}}
/* Last, and deliberately: a role attribute and a pseudo-class carry the
   same weight in Qt's stylesheets, so the rules above would otherwise
   win over QPushButton:disabled and leave a dead highlighted button
   looking exactly like a live one. */
QPushButton[role="primary"]:disabled, QPushButton[role="danger"]:disabled {{
    color: #676e77;
    background: {surface};
    border-color: {border};
    font-weight: 600;
}}
QLabel#recordingChip {{
    color: #ffdadd;
    background: #4a1d22;
    border: 1px solid #b3434f;
    border-radius: 8px;
    padding: 5px 9px;
    font-weight: 700;
}}
QTabWidget::pane {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 9px;
    top: -1px;
}}
QTabBar::tab {{
    min-width: 132px;
    padding: 9px 16px;
    margin-right: 4px;
    color: #aab0b9;
    background: {background};
    border: 1px solid {border};
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}}
QTabBar::tab:hover {{
    color: #ffffff;
    border-color: {accent};
}}
QTabBar::tab:selected {{
    color: {accent};
    background: {surface};
    border-top: 2px solid {accent};
    font-weight: 700;
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {{
    min-height: 22px;
    padding: 5px 8px;
    color: #edf0f4;
    background: {background};
    border: 1px solid {border};
    border-radius: 5px;
    selection-background-color: {accent};
    selection-color: {accent_text};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {{
    border-color: {accent};
}}
QComboBox::drop-down {{
    border: none;
    border-left: 1px solid {border};
    width: 28px;
}}
QComboBox::down-arrow {{
    image: url("{dropdown_arrow.as_posix()}");
    width: 12px;
    height: 8px;
}}
QComboBox QAbstractItemView {{
    color: #edf0f4;
    background: {surface_raised};
    border: 1px solid {accent};
    selection-background-color: {accent};
    selection-color: {accent_text};
}}
QCheckBox {{
    color: #d7dbe1;
    spacing: 7px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {border};
    border-radius: 4px;
    background: {background};
}}
QCheckBox::indicator:hover {{
    border-color: {accent};
}}
QCheckBox::indicator:checked {{
    background: {accent};
    border-color: {accent};
    image: url("{checkbox_tick.as_posix()}");
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 28px;
    background: {surface_raised};
    border: none;
    border-left: 1px solid {border};
    border-bottom: 1px solid {border};
    border-top-right-radius: 5px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 28px;
    background: {surface_raised};
    border: none;
    border-left: 1px solid {border};
    border-bottom-right-radius: 5px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {accent_hover};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{spin_up_arrow.as_posix()}");
    width: 12px;
    height: 7px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{spin_down_arrow.as_posix()}");
    width: 12px;
    height: 7px;
}}
QTableWidget {{
    color: #e9edf2;
    background: {background};
    alternate-background-color: {surface_raised};
    gridline-color: {border};
    border: 1px solid {border};
    border-radius: 7px;
    selection-background-color: {accent};
    selection-color: {accent_text};
}}
QHeaderView::section {{
    color: {accent};
    background: {surface_raised};
    border: none;
    border-right: 1px solid {border};
    border-bottom: 1px solid {border};
    padding: 7px 5px;
    font-weight: 650;
}}
QTableCornerButton::section {{
    background: {surface_raised};
    border: none;
    border-right: 1px solid {border};
    border-bottom: 1px solid {border};
}}
QProgressBar {{
    height: 9px;
    color: transparent;
    background: {background};
    border: 1px solid {border};
    border-radius: 5px;
}}
QProgressBar::chunk {{
    background: {accent};
    border-radius: 4px;
}}
QSplitter::handle {{
    background: {border};
    margin: 3px;
}}
QToolTip {{
    color: #ffffff;
    background: {surface_raised};
    border: 1px solid {accent};
    padding: 4px;
}}
"""


TEACHER_STYLE_SHEET = _client_style(
    "teacherCentral",
    background="#181513",
    surface="#24201d",
    surface_raised="#332a24",
    border="#493a31",
    accent="#f2a65a",
    accent_hover="#60432d",
    accent_text="#20150b",
    dropdown_arrow=ASSET_DIR / "dropdown_arrow_teacher.svg",
    checkbox_tick=ASSET_DIR / "checkbox_tick_teacher.svg",
    spin_up_arrow=ASSET_DIR / "spin_up_teacher.svg",
    spin_down_arrow=ASSET_DIR / "spin_down_teacher.svg",
)

TEACHER_STATUS_STYLES = {
    "ok": "color: #8bd49c;",
    "warn": "color: #ffc66d;",
    "error": "color: #ff8a94;",
    "idle": "color: #b9b1aa;",
}


STUDENT_STYLE_SHEET = _client_style(
    "studentCentral",
    background="#111820",
    surface="#18242f",
    surface_raised="#223442",
    border="#30495c",
    accent="#57c7ff",
    accent_hover="#24516a",
    accent_text="#071923",
    dropdown_arrow=ASSET_DIR / "dropdown_arrow_student.svg",
    checkbox_tick=ASSET_DIR / "checkbox_tick_student.svg",
    spin_up_arrow=ASSET_DIR / "spin_up_student.svg",
    spin_down_arrow=ASSET_DIR / "spin_down_student.svg",
)

STUDENT_STATUS_STYLES = {
    "ok": "color: #79ddb0;",
    "warn": "color: #ffd166;",
    "error": "color: #ff8993;",
    "idle": "color: #aebbc7;",
}
