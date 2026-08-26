"""One axis policy for every exported figure.

**Latency axes start at zero** and are never cropped to the data, and
panels that share a metric inside one figure share their limits.  A
cropped RT baseline makes the gap between two conditions look like
whatever the crop chooses, which is the thing a reader cannot check.

**Accuracy axes are deliberately NOT pinned to 0-100%,** and there is no
helper here for doing so.  This study's accuracy sits against the ceiling
(94-99%), so a full-range axis collapses every condition into one flat
line and the per-level movement the figure exists to show disappears.
They autoscale instead, and the captions say so, because a legible
near-ceiling panel with honest tick labels beats an unreadable one.  The
few panels that were already pinned before this policy existed keep their
own ``set_ylim`` call at the call site.
"""

from typing import Iterable

import numpy as np

def _data_extreme(axes: Iterable, attribute: str) -> float:
    """Largest finite value the given axes actually have artists for."""
    top = -np.inf
    for ax in axes:
        value = float(getattr(ax.dataLim, attribute))
        if np.isfinite(value):
            top = max(top, value)
    return top


def zero_based_ylim(*axes, headroom: float = 0.05) -> None:
    """Share one 0 -> data-maximum y-range across the given axes.

    The top comes from what was drawn (individual trajectories included,
    not only the means), so nothing is clipped and no panel is expanded
    relative to its neighbour.
    """
    top = _data_extreme(axes, "y1")
    if not np.isfinite(top) or top <= 0:
        return
    for ax in axes:
        ax.set_ylim(0.0, top * (1.0 + headroom))


def zero_based_xlim(*axes, headroom: float = 0.05) -> None:
    """zero_based_ylim for a horizontal quantity axis (RT scatter plots)."""
    right = _data_extreme(axes, "x1")
    if not np.isfinite(right) or right <= 0:
        return
    for ax in axes:
        ax.set_xlim(0.0, right * (1.0 + headroom))


def shared_ylim(*axes) -> None:
    """Give the axes the union of their autoscaled ranges.

    For signed quantities (paired differences) where zero is inside the
    range rather than at the bottom.
    """
    lows, highs = [], []
    for ax in axes:
        low, high = ax.get_ylim()
        lows.append(low)
        highs.append(high)
    if not lows:
        return
    for ax in axes:
        ax.set_ylim(min(lows), max(highs))
