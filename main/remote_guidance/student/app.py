"""Run the Student Client as its own process.

The three remote roles cannot share a process: the student and teacher
each hold a camera and a MIDI port, and the launcher's ordinary
one-tool-at-a-time rule exists precisely because those are exclusive. The
launcher therefore starts this through QProcess (see launcher.py), and
this module is also the standalone entry point.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config

from ..config import RemoteGuidanceConfig
from .window import StudentRemoteWindow


def main(argv: list[str] | None = None) -> int:
    cfg = Config.load()
    remote = RemoteGuidanceConfig.load()

    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    window = StudentRemoteWindow(cfg, remote)
    # Sign in, room and session are separate stages, so only one is on
    # screen at a time and the window never has to be tall enough for
    # all of them; the session splitter rebalances from there.
    window.resize(1060, 720)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
