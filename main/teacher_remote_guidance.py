"""Teacher Client - Remote Guidance / Tele-training.

Play a key on the teacher's own keyboard and the student's corresponding
key LED and finger cue fire on the far end. Which finger is used comes
from the platform's existing camera pipeline (Camera -> HandTracker ->
finger matching against the teacher's calibration profile), so the
teacher demonstrates a fingering instead of describing it.

Also uploads an already-recorded fingering sequence (data/music/ or
data/sequence/) and triggers it remotely, for the asynchronous mode where
the student's machine schedules every cue locally.

Run it standalone:

    python teacher_remote_guidance.py

or from the launcher's "8. Tele-training" section, which starts it as its
own process.

Configure the teacher's camera, MIDI port and keyboard profile in the
launcher's Remote Guidance Settings, or in config.json's
remote_guidance.teacher block - they are kept separate from the student's
and from the ordinary tools' shared settings.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remote_guidance.teacher.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
