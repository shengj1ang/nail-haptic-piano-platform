"""Remote Guidance / Tele-training - the client half.

Two independent GUI processes (student and teacher) plus a benchmark
tool, all talking to the relay in server/ over one persistent WebSocket.
See the root README's Tele-training section for the whole picture and
doc/server.md for the protocol.

Layout:

    config.py         the remote_guidance block of config.json, and the
                      per-role Config copies that keep the student's and
                      teacher's camera/MIDI/profile apart
    protocol.py       the versioned message envelope (client half of a
                      matched pair with server/schemas.py)
    timing.py         wall vs monotonic clocks, the clock-offset
                      estimator, and latency statistics
    network_client.py the threaded WebSocket client - no Qt
    qt_bridge.py      turns that client's callbacks into Qt signals so a
                      window never runs a network loop itself
    cue_outputs.py    CompositeCueOutput and the LED key cue, composed
                      from the existing quiz cue implementations
    student/          session core (no Qt) + window + entry point
    teacher/          live detection, recording import, window, entry
    tools/            latency benchmark

Rules this module is built around:

  - Every device call happens on the client that owns the device. The
    server imports none of it.
  - Nothing here re-implements finger matching, quiz scoring, LED
    mapping, haptic drive or recording formats; it composes the existing
    modules.
  - A monotonic timestamp is only ever used on the machine that produced
    it. Student reaction time is measured from the student's own
    cue-ready moment, never from a timestamp taken on the teacher's
    machine.
"""

__all__ = ["__version__", "PROTOCOL_VERSION"]

__version__ = "1.0.0"

from .protocol import PROTOCOL_VERSION  # noqa: E402,F401
