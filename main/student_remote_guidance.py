"""Student Client - Remote Guidance / Tele-training.

Receives key/finger guidance from a remote teacher over the relay (see
server/) and presents it on this machine: the keyboard backlight lights
the target key, and the target finger is cued on screen, by the
nail-mounted actuator, or both. The whole session's video and MIDI are
recorded exactly as a local quiz is, and the results land in
data/quiz/<session name>/ in the standard format, so every existing
analysis tool reads them unchanged.

Run it standalone:

    python student_remote_guidance.py

or from the launcher's "8. Tele-training" section, which starts it as its
own process (this client holds a camera and a MIDI port, so it cannot
share one with the teacher client).

Configure the student's camera, MIDI port, keyboard profile and the two
serial ports (LED strip and haptic rig) in the launcher's Remote Guidance
Settings, or in config.json's remote_guidance.student block.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remote_guidance.student.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
