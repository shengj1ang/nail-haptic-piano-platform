"""How the detected finger is written wherever a reader sees it - the
event table in app/gui/quiz_detail_window.py and the evidence line in
app/gui/event_review_window.py, so the same event never reads two
different ways in two windows.

actual_finger is the detector's softmax argmax while finger_correct is
the θ rule on the *cued* finger's mass (app.finger_matching), so the two
legitimately disagree in both directions. Rather than print a raw argmax
beside a ✓ - which reads as a contradiction it isn't - the two
disagreeing cases are named in the text itself: "R3 (≈R2)" for an event
scored as the cued R3 that a neighbour narrowly won on probability, and
"R4 (p<θ)" for the mirror case where the argmax is the cued finger but
never cleared θ. Display only: results.json keeps the argmax, and every
statistic reads finger_correct rather than re-deriving it.
"""

from PySide6.QtGui import QColor

from ..finger_matching import FINGER_PROBABILITY_THRESHOLD
from ..quiz import scored_near_tie, subthreshold_match

COLOR_NEAR_TIE = QColor(255, 240, 200)


def finger_cell(r):
    """(text, tooltip, tint) for the Actual finger of one event. tooltip
    and tint are None when the detected finger needs no explaining."""
    if r.actual_finger is None:
        return "unresolved", None, None

    probs = r.finger_probabilities or {}
    p_target = r.target_finger_probability
    p_actual = probs.get(r.actual_finger)
    p_t = f"{p_target:.2f}" if p_target is not None else "n/a"
    p_a = f"{p_actual:.2f}" if p_actual is not None else "n/a"

    if scored_near_tie(r):
        return (
            f"{r.target_finger} (≈{r.actual_finger})",
            f"Scored as the cued {r.target_finger}: it held p = {p_t} ≥ θ = "
            f"{FINGER_PROBABILITY_THRESHOLD:.2f}, so the benefit of the doubt goes to the "
            f"learner. {r.actual_finger} was fractionally more probable (p = {p_a}) and is what "
            "results.json stores as actual_finger - the camera cannot separate two fingertips "
            f"this close. The confusion matrix below therefore still counts this event as "
            f"{r.target_finger} → {r.actual_finger} (amber), and every statistic reads the ✓.",
            COLOR_NEAR_TIE,
        )
    if subthreshold_match(r):
        return (
            f"{r.actual_finger} (p<θ)",
            f"The most probable fingertip was {r.actual_finger}, which is the cued finger - but it "
            f"held only p = {p_t} < θ = {FINGER_PROBABILITY_THRESHOLD:.2f}, so the mass was split "
            "across several fingertips over the key and the automatic rule cannot credit the "
            "event. That is why Finger ✓ is ✗ next to a matching finger. Double-click the row to "
            "watch the keypress and rule on it.",
            None,
        )
    return r.actual_finger, None, None
