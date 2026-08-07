"""Paint a keyboard calibration profile onto a camera frame, and show it.

"Is this camera still looking at the keyboard the way it was calibrated?"
is a question worth being able to answer in one click, from whichever
window is about to rely on the answer - a quiz that is about to record a
participant, or a remote client's Settings.

Two pieces, kept apart on purpose:

  - overlay_profile_mask() is pure. Frame in, annotated frame out. No
    camera, no Qt, so it can be tested without either.
  - KeyboardProfilePreviewDialog just displays a frame someone else
    produced, in its own window, so the window that opened it stays the
    size it was.

Where the frame comes from is the caller's business, and the two callers
differ in a way that matters:

  - student_quiz.py's QuizWindow button (inherited by the haptic quiz and
    the main study's trial runner) uses the frame that window already has
    on screen. It holds the camera open for its whole lifetime, and
    opening a second capture on the same device fails on most backends -
    so it must not open its own.
  - remote_guidance/settings_window.py has no camera open (its Session
    page deliberately claims no hardware until a session starts), so it
    opens one, takes a snapshot and releases it again.

The profile is never resized to fit the frame. A pixel mask *is* the
calibration, so a size mismatch is a real answer - stretching it would
make a wrong calibration look plausibly aligned, which is the one
outcome this preview exists to rule out.
"""

from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from ..keyboard.template import KeyboardTemplate
from ..keyboard.visualize import build_color_luts, draw_labels, overlay_keys
from ..profiles import DATA_DIR as PROFILE_DATA_DIR
from .image_view import ImageView

MASK_ALPHA = 0.45


def load_profile_template(
    profile_name: str,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> KeyboardTemplate:
    """The calibrated template behind a profile name, checked far enough
    to be drawable.

    Separate from the drawing so a caller holding no frame yet can fail on
    a bad profile *before* claiming a camera to find out. Raises with a
    message meant to be shown to a user: ValueError for a bad choice,
    FileNotFoundError for a profile that was never calibrated, RuntimeError
    for one whose saved mask is unusable.
    """
    name = (profile_name or "").strip()
    if not name:
        raise ValueError("Choose a keyboard calibration profile first.")

    template_path = Path(profile_data_dir) / name / "keyboard_template.json"
    if not template_path.exists():
        raise FileNotFoundError(f"Profile {name!r} has no keyboard_template.json at {template_path}.")

    try:
        return KeyboardTemplate.load(template_path)
    except FileNotFoundError as exc:
        # KeyboardTemplate.load raises with the bare path of the mask image
        # it could not read. That lands in a message box, so say whose
        # profile it is and what is wrong with it.
        raise RuntimeError(
            f"Profile {name!r} has no readable keyboard key map - {exc} is missing. Recalibrate the profile."
        ) from exc


def overlay_template(
    frame: np.ndarray,
    template: KeyboardTemplate,
    profile_name: str,
) -> np.ndarray:
    """Draw an already-loaded template's key masks and ids over one frame,
    returning an annotated copy. The input frame is not touched."""
    if template.key_map is None:
        raise RuntimeError(f"Profile {profile_name!r} has no readable keyboard key map.")
    if frame.shape[:2] != template.key_map.shape[:2]:
        frame_h, frame_w = frame.shape[:2]
        map_h, map_w = template.key_map.shape[:2]
        raise ValueError(
            f"Resolution mismatch: camera returned {frame_w} × {frame_h}, but profile {profile_name!r} "
            f"was calibrated at {map_w} × {map_h}. Choose the matching camera settings/profile or recalibrate."
        )

    preview = frame.copy()
    overlay_keys(preview, template.key_map, build_color_luts(len(template.keys)), alpha=MASK_ALPHA)
    draw_labels(preview, template.key_map, len(template.keys))
    return preview


def overlay_profile_mask(
    frame: np.ndarray,
    profile_name: str,
    profile_data_dir: Path = PROFILE_DATA_DIR,
) -> Tuple[np.ndarray, KeyboardTemplate]:
    """Load a profile and draw it over one frame, for a caller that
    already has the frame in hand. Returns the annotated copy and the
    template, so the caller can report the key count."""
    template = load_profile_template(profile_name, profile_data_dir)
    return overlay_template(frame, template, profile_name), template


class KeyboardProfilePreviewDialog(QDialog):
    """One annotated frame in its own window, keeping the opener compact."""

    def __init__(
        self,
        frame: np.ndarray,
        profile_name: str,
        key_count: int,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Camera + Keyboard Profile Preview — {profile_name}")

        frame_h, frame_w = frame.shape[:2]
        explanation = QLabel(
            f"{frame_w} × {frame_h} frame with profile {profile_name!r} ({key_count} keys). "
            "The colored regions are the saved pixel masks and the numbers are key ids. "
            "This preview is not saved."
        )
        explanation.setWordWrap(True)

        self.view = ImageView()
        self.view.set_frame(frame)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(explanation)
        layout.addWidget(self.view, 0)
        layout.addWidget(buttons)
