"""Shared "Export figures + data" for the two analysis windows.

app/gui/participant_analysis_window.py and app/gui/group_analysis_window.py
register every figure and tidy table their tabs build, then hand the pair
here. The write itself is slow enough to look like a freeze - a full group
export is 28 figures and 55 tables, and re-rendering those figures at
300 dpi takes about six of the roughly eight seconds - so it runs through
app.gui.progress_task, which names the file currently being written.

The two windows differ only in whether their filenames carry a participant
prefix, so that is the one parameter; everything else - the PNG+SVG pair,
the per-file failure handling, the self-identifying manifest - is the same
export and lives here once.
"""

from pathlib import Path
from typing import Dict, List

import pandas as pd

from .progress_task import Step, run_with_progress

# Report-quality raster; the SVG beside it is what actually gets edited.
FIGURE_DPI = 300


def _stem(prefix: str, slug: str) -> str:
    return f"{prefix}_{slug}" if prefix else slug


def manifest_name(prefix: str) -> str:
    """`P12__manifest.csv` for a participant export, `_manifest.csv` for the
    group one - the leading underscore sorts it to the top of the folder."""
    return f"{prefix}__manifest.csv" if prefix else "_manifest.csv"


def export_analysis(parent, out_dir: Path, figures: Dict[str, object],
                    datasets: Dict[str, object], provenance: Dict[str, object],
                    prefix: str = "") -> str:
    """Write every figure as PNG + SVG and every tidy table as CSV into
    out_dir, then a manifest. Returns the status line for the caller to
    show.

    One unwritable file is collected and skipped rather than aborting the
    other hundred - a single locked file should not cost the whole export.
    Cancelling stops between files and deliberately skips the manifest: a
    manifest that lists files nobody wrote is worse than none, and the
    message says the folder is incomplete.

    provenance supplies the manifest's first row ("rows" and "columns"),
    which is what makes a CSV lifted into a report appendix identify whose
    data it holds and on what denominator.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: List[dict] = []
    failures: List[str] = []
    written: Dict[str, int] = {"figures": 0, "tables": 0}

    def save_figure(slug: str, fig, stem: str) -> None:
        ok = False
        for suffix, kwargs in ((".png", {"dpi": FIGURE_DPI}), (".svg", {})):
            try:
                fig.savefig(out_dir / f"{stem}{suffix}", bbox_inches="tight", **kwargs)
                ok = True
            except Exception as e:
                failures.append(f"{stem}{suffix} ({type(e).__name__}: {e})")
        if ok:
            written["figures"] += 1
            manifest.append({"file": f"{stem}.png / {stem}.svg", "kind": "figure",
                             "rows": "", "columns": ""})

    def save_table(slug: str, df, stem: str) -> None:
        try:
            df.to_csv(out_dir / f"{stem}.csv", index=False)
        except Exception as e:
            failures.append(f"{stem}.csv ({type(e).__name__}: {e})")
            return
        written["tables"] += 1
        manifest.append({"file": f"{stem}.csv", "kind": "table",
                         "rows": len(df), "columns": ", ".join(map(str, df.columns))})

    steps: List[Step] = []
    for slug, fig in sorted(figures.items()):
        stem = _stem(prefix, slug)
        steps.append((f"{stem}.png + .svg",
                      lambda slug=slug, fig=fig, stem=stem: save_figure(slug, fig, stem)))
    for slug, df in sorted(datasets.items()):
        stem = _stem(prefix, slug)
        steps.append((f"{stem}.csv",
                      lambda slug=slug, df=df, stem=stem: save_table(slug, df, stem)))

    name = manifest_name(prefix)

    def save_manifest() -> None:
        header = pd.DataFrame([{"file": "(export)", "kind": "provenance", **provenance}])
        try:
            pd.concat([header, pd.DataFrame(manifest)], ignore_index=True).to_csv(
                out_dir / name, index=False)
        except Exception as e:
            failures.append(f"{name} ({type(e).__name__}: {e})")

    steps.append((name, save_manifest))

    completed = run_with_progress(parent, "Exporting figures and data", steps)

    if completed:
        msg = (f"Exported {written['figures']} figures ({FIGURE_DPI} dpi PNG + SVG) + "
               f"{written['tables']} CSVs + {name} to {out_dir}")
    else:
        msg = (f"Export CANCELLED after {written['figures']} figures + "
               f"{written['tables']} CSVs — {out_dir} is incomplete and has no manifest. "
               "Run the export again to finish it.")
    if failures:
        msg += "  —  FAILED: " + "; ".join(failures)
    return msg
