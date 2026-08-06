"""Teacher client: watch the teacher's own hands and keyboard, turn what
they play into guidance, and follow what the student does with it.

live_detector.py is the Camera -> HandTracker -> MIDI -> finger_matching
chain the platform already has, wrapped so a window can drive it from a
timer; recording_import.py turns an existing data/music or data/sequence
folder into an uploadable recording. Neither re-implements any of it.
"""
