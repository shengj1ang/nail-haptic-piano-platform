"""Run the Teacher Client as its own process - see student/app.py for
why the roles do not share one."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.config import Config

from ..config import RemoteGuidanceConfig
from .window import TeacherRemoteWindow


def main(argv: list[str] | None = None) -> int:
    cfg = Config.load()
    remote = RemoteGuidanceConfig.load()

    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    window = TeacherRemoteWindow(cfg, remote)
    # Sign in, room and session are separate stages, so only one is on
    # screen at a time and the window never has to be tall enough for
    # all of them; the session splitter rebalances from there.
    window.resize(1150, 740)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
