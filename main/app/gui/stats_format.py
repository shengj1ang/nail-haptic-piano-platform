"""Number formatting shared by the analysis windows' rich-text captions.

One home for these, because every caption in the analysis windows is
rendered as Qt RichText and a bare "<" there opens a tag: Qt then
silently swallows the rest of the line, so a p < .001 result would be
exactly the one that disappears from the report. Anything emitting a
comparison sign must go through fmt_p (or use &lt; itself).
"""

import numpy as np

__all__ = ["fmt", "fmt_p", "fmt_ci", "fmt_signed"]


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and np.isnan(x))


def fmt(x, decimals=0, suffix="") -> str:
    """A number, or "n/a" for None/NaN - never a fabricated 0."""
    if _missing(x):
        return "n/a"
    return f"{x:.{decimals}f}{suffix}"


def fmt_signed(x, decimals=1, suffix="") -> str:
    """Same, but always carrying an explicit + or - (differences and
    benefits are unreadable without the sign)."""
    if _missing(x):
        return "n/a"
    return f"{x:+.{decimals}f}{suffix}"


def fmt_p(p) -> str:
    """p as "= 0.031" / "&lt; 0.001" / "n/a". The entity, not "<"."""
    if _missing(p):
        return "n/a"
    return "&lt; 0.001" if p < 0.001 else f"= {p:.3f}"


def fmt_ci(lo, hi, decimals=1, suffix="") -> str:
    """A 95% interval, or "n/a" when it could not be formed (n < 2)."""
    if _missing(lo) or _missing(hi):
        return "n/a"
    return f"[{lo:.{decimals}f}, {hi:.{decimals}f}]{suffix}"
