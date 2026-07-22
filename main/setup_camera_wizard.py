"""Camera Selection Wizard.

Scans which camera indices currently work, lets you preview each live, and
saves the chosen one (plus flip settings) into config.json - see
app/gui/camera_selection_window.py.
"""

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config
from app.gui.camera_selection_window import CameraSelectionWindow


def main() -> None:
    cfg = Config.load()

    app = QApplication(sys.argv)
    window = CameraSelectionWindow(cfg)
    window.resize(900, 700)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
