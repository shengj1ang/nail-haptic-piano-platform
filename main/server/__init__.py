"""Relay server for the Remote Guidance / Tele-training module.

Deliberately self-contained. Nothing in this package imports the
surrounding piano platform - no camera, no MIDI, no MediaPipe, no LED and
no haptic modules - because every piece of hardware lives on a client
machine and the server only ever routes already-computed key/finger
events between the two endpoints of a room. Copying this one folder onto
a remote host and installing server/requirements.txt is enough to run it
(see doc/server.md).

That independence is also why the message envelope is described twice:
once here (server/schemas.py) and once on the client side
(remote_guidance/protocol.py). The two are a matched pair and must be
changed together - PROTOCOL_VERSION exists to make a mismatch loud
instead of silent. What is deliberately NOT duplicated is any analysis:
the server never re-runs finger matching and never computes its own
accuracy; it stores what the student's own (shared, already-tested)
scoring produced.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
