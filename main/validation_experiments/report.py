"""Shared text-report helpers for the validation experiments.

A finished run prints its statistics to the console/log as it goes, but
until now RE-OPENING a saved run only redrew its chart - the numbers
behind the picture were only in the CSV and the meta. Each experiment
module therefore exposes

    summary_report(csv_path, summary=None) -> list[str]

which rebuilds that console-style block from the saved CSV (+ its
.meta.json) so the GUI can print it next to the reloaded chart. This
module holds the parts every one of those reports shares: the header,
the epoch->local-time formatting and the aligned statistics lines.

The reports are DERIVED, never stored: they are regenerated from the
saved files, so a run's text and its chart can never disagree, and
nothing here writes to the run's own outputs.
"""

import os
import re
import time
from typing import Optional, Sequence

#: Every output file is named "<prefix>_<unix-epoch-seconds>.<ext>"
#: (project-wide epoch-timestamps rule), so the run's time can be
#: recovered from the file name even when its meta is missing.
_STAMP_RE = re.compile(r"_(\d{9,11})(?:\.|$)")


def stamp_from_path(path: str) -> Optional[int]:
    """The Unix-epoch stamp embedded in a run's file name, or None."""
    match = _STAMP_RE.search(os.path.basename(path))
    return int(match.group(1)) if match else None


def format_epoch(epoch) -> str:
    """Epoch seconds as local "YYYY-MM-DD HH:MM:SS" for display only.

    Stored timestamps stay epoch seconds everywhere (the project rule);
    this is purely so a human reading the report can tell which run it
    is looking at."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(epoch)))
    except (TypeError, ValueError, OSError):
        return "unknown time"


def run_time_text(csv_path: str, meta: Optional[dict]) -> str:
    """When the run was saved: from its meta if present, else from the
    stamp in the file name, else from the file's mtime."""
    if meta and meta.get("saved_at") is not None:
        return format_epoch(meta["saved_at"])
    stamp = stamp_from_path(csv_path)
    if stamp is not None:
        return format_epoch(stamp)
    try:
        return format_epoch(os.path.getmtime(csv_path))
    except OSError:
        return "unknown time"


def header(title: str, csv_path: str, meta: Optional[dict],
           detail_lines: Sequence[str] = ()) -> list:
    """The block every report opens with: which run this is, when it was
    recorded, and the parameters it was recorded with.

    `detail_lines` are the experiment's own parameter lines. A run whose
    meta is missing still gets a header - it just says so, rather than
    inventing parameters."""
    lines = [
        "",
        f"===== {title} =====",
        f"Run:  {os.path.basename(csv_path)}   (saved "
        f"{run_time_text(csv_path, meta)})",
    ]
    lines.extend(f"      {line}" for line in detail_lines if line)
    if meta is None:
        lines.append("      (no .meta.json beside this CSV - parameters "
                     "below are read from the data itself)")
    firmware = (meta or {}).get("firmware")
    if firmware:
        lines.append(f"      firmware: {firmware}")
    return lines


def stats_line(label: str, stats: dict, prefix: str, unit: str = "ms",
               label_width: int = 28, value_width: int = 8,
               decimals: int = 2,
               display_unit: Optional[str] = None) -> Optional[str]:
    """One aligned "mean / median / SD / range (n)" line, or None when
    that series has no values.

    Reads the "<prefix>_mean_ms"-style keys the experiments' stats dicts
    already use, so the report and the live run print identical text.
    `unit` is the KEY suffix; `display_unit` is what the reader sees when
    the two differ (keys must stay identifier-friendly, so a series
    stored as "_ms2" prints as "m/s²")."""
    mean = stats.get(f"{prefix}_mean_{unit}")
    if mean is None:
        return None
    shown = display_unit if display_unit is not None else unit
    median = stats.get(f"{prefix}_median_{unit}")
    sd = stats.get(f"{prefix}_sd_{unit}")
    low = stats.get(f"{prefix}_min_{unit}")
    high = stats.get(f"{prefix}_max_{unit}")
    text = f"{label:<{label_width}} mean {mean:{value_width}.{decimals}f} {shown}"
    if median is not None:
        text += f", median {median:{value_width}.{decimals}f} {shown}"
    text += (f", SD {sd:{value_width - 1}.{decimals}f} {shown}"
             if sd is not None else ", SD       - ")
    if low is not None and high is not None:
        text += (f", range {low:.{decimals}f}-{high:.{decimals}f} {shown}")
    return text + f" (n={stats.get(f'{prefix}_n', 0)})"


def table(headings: Sequence[str], rows: Sequence[Sequence],
          aligns: Optional[Sequence[str]] = None, indent: str = "  ") -> list:
    """A plain fixed-width text table (heading row, rule, data rows).

    Used for the per-trial / per-step listings, which are what makes a
    reloaded run readable as numbers rather than only as a picture."""
    cells = [[str(c) for c in row] for row in rows]
    headings = [str(h) for h in headings]
    widths = [len(h) for h in headings]
    for row in cells:
        for i, cell in enumerate(row[:len(widths)]):
            widths[i] = max(widths[i], len(cell))
    aligns = list(aligns or ["<"] + [">"] * (len(headings) - 1))

    def render(row):
        return indent + "  ".join(
            f"{cell:{aligns[i]}{widths[i]}}" for i, cell in enumerate(row))

    lines = [render(headings), indent + "  ".join("-" * w for w in widths)]
    lines.extend(render(row) for row in cells)
    return lines


def number(value, decimals: int = 2, dash: str = "-") -> str:
    """A number for a table cell, or `dash` when it is missing - so a
    failed trial reads as a gap instead of as a zero."""
    if value is None:
        return dash
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)
