"""Student client: receives guidance, presents it locally, records what
happened.

session.py holds the whole event/timing model with no Qt in it, so the
rules that matter - reaction time measured from the local cue-ready
moment, queue wait recorded separately from network delay, no silent
overwriting of a pending cue - are unit-testable without a display, a
camera or a serial port.
"""
