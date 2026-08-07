"""Which key the teacher pressed, and with which finger.

This is the platform's existing live-detection chain, assembled for the
teacher role and nothing more:

    app.camera.Camera            -> the teacher's own camera
    app.hand_tracking.HandTracker-> hand landmarks
    app.midi.MidiListener        -> note-on events, on its own thread
    app.finger_matching          -> match_note_to_finger for one note,
                                    match_notes_to_fingers for a chord
    app.keyboard.template/
    app.keyboard.midi_mapping    -> the teacher's calibration profile

No matching algorithm is written here. `poll()` returns whatever the
shared matcher decided, already packed into protocol GuidanceActions.

The window drives this from a QTimer, the same way
app/gui/finger_detector_window.py drives its own loop - MIDI reading is
already off the GUI thread inside MidiListener, and one MediaPipe pass
per tick is what every other live tool in this codebase does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from app.camera import Camera
from app.config import Config
from app.finger_matching import match_note_to_finger, match_notes_to_fingers
from app.hand_tracking import Hand, HandTracker, draw_hands
from app.keyboard.midi_mapping import MidiMapping, note_name
from app.keyboard.template import KeyboardTemplate
from app.keyboard.visualize import build_color_luts, draw_labels, overlay_keys
from app.midi import MidiListener
from app.profiles import DATA_DIR as PROFILE_DATA_DIR

from ..protocol import GuidanceAction

log = logging.getLogger("remote_guidance.teacher.detector")


@dataclass
class DetectionResult:
    """One tick's worth of output: the frame to show, plus any guidance
    the teacher's playing just produced."""

    frame: Optional[np.ndarray] = None
    actions: List[GuidanceAction] = None
    hands: Dict[str, Hand] = None

    def __post_init__(self) -> None:
        if self.actions is None:
            self.actions = []
        if self.hands is None:
            self.hands = {}


class TeacherLiveDetector:
    """Owns the teacher's camera, tracker, profile and MIDI port.

    Constructed from a *role-specific* Config (see
    remote_guidance.config.teacher_config), so it opens the teacher's
    camera and MIDI port and loads the teacher's keyboard profile - never
    the shared ones the ordinary tools use."""

    def __init__(
        self,
        cfg: Config,
        chord_detection: bool = True,
        profile_data_dir: Path = PROFILE_DATA_DIR,
        open_camera: bool = True,
    ):
        self.cfg = cfg
        self.chord_detection = chord_detection
        self.profile_data_dir = profile_data_dir

        self.camera: Optional[Camera] = Camera(cfg.camera) if open_camera else None
        self.tracker: Optional[HandTracker] = HandTracker() if open_camera else None
        self.midi: Optional[MidiListener] = None

        self.template: Optional[KeyboardTemplate] = None
        self.mapping: Optional[MidiMapping] = None
        self._luts = None
        self.last_hands: Dict[str, Hand] = {}
        self.profile_error: Optional[str] = None

        self.load_profile(cfg.active_keyboard_profile)

    # -- setup ---------------------------------------------------------

    def load_profile(self, profile_name: str) -> bool:
        """Loads the key regions and the note<->key mapping. Missing or
        half-finished profiles are reported, not raised: the teacher can
        still be seen on camera and can still send note-only guidance."""
        self.profile_error = None
        profile_dir = self.profile_data_dir / profile_name
        template_path = profile_dir / "keyboard_template.json"
        mapping_path = profile_dir / "midi_mapping.json"

        if not template_path.exists():
            self.template = self.mapping = self._luts = None
            self.profile_error = f"profile {profile_name!r} has no keyboard_template.json"
            return False

        self.template = KeyboardTemplate.load(template_path)
        self._luts = build_color_luts(len(self.template.keys))
        if mapping_path.exists():
            self.mapping = MidiMapping.load(mapping_path)
        else:
            self.mapping = None
            self.profile_error = f"profile {profile_name!r} has no midi_mapping.json - run the MIDI Mapping Wizard"
        return self.mapping is not None

    @property
    def profile_ready(self) -> bool:
        return self.template is not None and self.mapping is not None

    def connect_midi(self, port_name: Optional[str] = None) -> str:
        """Opens the teacher's MIDI port. Raises RuntimeError with the
        available ports listed if it cannot - same behaviour as every
        other tool here."""
        self.disconnect_midi()
        self.midi = MidiListener(port_name or self.cfg.midi.port_name)
        return self.midi.port_name

    def disconnect_midi(self) -> None:
        if self.midi is not None:
            self.midi.close()
            self.midi = None

    @property
    def midi_connected(self) -> bool:
        return self.midi is not None

    # -- per-tick ------------------------------------------------------

    def poll(self, annotate: bool = True) -> DetectionResult:
        """One camera frame plus any note-ons since the last call.

        Simultaneous note-ons are matched together through
        match_notes_to_fingers so no fingertip is credited with two notes
        at once - the chord support the report describes, available from
        the first version of the protocol because guidance.live already
        carries a list of actions."""
        result = DetectionResult()

        frame = self.camera.read() if self.camera is not None else None
        if frame is not None and self.tracker is not None:
            self.last_hands = self.tracker.process(frame)
            if annotate:
                self._annotate(frame)
            result.frame = frame
        result.hands = self.last_hands

        if self.midi is None:
            return result

        notes = [event.note for event in self.midi.pop_events()]
        if notes:
            result.actions = self.actions_for(notes)
        return result

    def actions_for(self, notes: List[int]) -> List[GuidanceAction]:
        """Turn note numbers into guidance actions using the shared
        matcher and the current hand landmarks."""
        if not self.profile_ready:
            # No calibration: still send the notes, with no finger. The
            # student's LED cue works from the note alone.
            return [GuidanceAction(note=n, note_name=note_name(n)) for n in notes]

        if self.chord_detection and len(notes) > 1:
            matches = match_notes_to_fingers(notes, self.template, self.mapping, self.last_hands)
        else:
            matches = [match_note_to_finger(n, self.template, self.mapping, self.last_hands) for n in notes]

        actions = []
        for note, match in zip(notes, matches):
            actions.append(
                GuidanceAction(
                    note=note,
                    note_name=note_name(note),
                    finger=match.finger if match else None,
                    key_id=match.key_id if match else self.mapping.key_for_note(note),
                    finger_probability=match.probability if match else None,
                    finger_probabilities=dict(match.probabilities) if match else {},
                )
            )
        return actions

    def _annotate(self, frame: np.ndarray) -> None:
        if self.template is not None and frame.shape[:2] == self.template.key_map.shape[:2]:
            overlay_keys(frame, self.template.key_map, self._luts, alpha=0.35)
            draw_labels(frame, self.template.key_map, len(self.template.keys))
        draw_hands(frame, self.last_hands)

    # -- teardown ------------------------------------------------------

    def close(self) -> None:
        """Releases every device it opened, each independently, so one
        failure cannot leave the camera or the MIDI port held."""
        self.disconnect_midi()
        if self.tracker is not None:
            try:
                self.tracker.close()
            except Exception:  # noqa: BLE001
                log.exception("closing the hand tracker failed")
            self.tracker = None
        if self.camera is not None:
            try:
                self.camera.release()
            except Exception:  # noqa: BLE001
                log.exception("releasing the camera failed")
            self.camera = None
