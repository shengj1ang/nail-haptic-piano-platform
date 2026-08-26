"""The "Finger Benefit" tab of the Group Analysis window: is the
vibrotactile advantage compensatory rather than uniform?

Three questions, three modules, one tab:

  app.finger_benefit       does the benefit grow with the finger's
                           baseline RT? (compensation)
  app.finger_equalisation  does the spread across the five fingers
                           shrink from B to C? (equalisation)
  app.finger_weakest       does each participant's OWN weakest finger
                           gain most? (self-selected targeting)

All the statistics live in those modules; this file only renders them.
Each section shows the naive estimate the question is usually written
with NEXT TO the coupling-free one, because in all three cases the naive
version is biased in the direction of the hypothesis and the gap between
the two is the part of the result that is arithmetic rather than finding.

build() returns (caption_html, figures, datasets) so the window can add
the tab and register the figures/CSVs for "Export figures + data"
without knowing anything about what is on it.
"""

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from .. import finger_benefit as fb
from ..figure_axes import zero_based_xlim, zero_based_ylim
from .. import finger_common as fc
from .. import finger_equalisation as fe
from .. import finger_weakest as fw
from .stats_format import fmt, fmt_ci, fmt_p, fmt_signed

# Reaction times are stored in seconds and read in milliseconds.
MS = 1000.0
PARTICIPANT_CMAP = "tab10"


def _participant_colors(participants) -> Dict[str, tuple]:
    import matplotlib
    # matplotlib.colormaps is the current API; cm.get_cmap is deprecated
    # since 3.7 and removed in 3.11, which requirements.txt already pins.
    if hasattr(matplotlib, "colormaps"):
        cmap = matplotlib.colormaps[PARTICIPANT_CMAP]
    else:  # matplotlib < 3.5
        from matplotlib import cm
        cmap = cm.get_cmap(PARTICIPANT_CMAP)
    return {p: cmap(i % cmap.N) for i, p in enumerate(sorted(participants))}


# ---------------------------------------------------------------------------
# Section 1 - compensation


def _compensation_html(res: dict) -> str:
    head = ["<h3>1. Is the benefit compensatory?</h3>",
            "Correlation, over (participant × finger) cells, between the vibrotactile benefit "
            "(RT<sub>B</sub> − RT<sub>C</sub>, positive = C faster) and how slow that finger "
            "already was under B. A positive correlation means the slow digits gained most, "
            "i.e. the cue compensates rather than uniformly accelerates."]
    if res["reason"]:
        return "".join(f"<p>{p}</p>" for p in head + [f"<i>Not computed: {res['reason']}.</i>"])
    if res["dropped_note"]:
        head.append(res["dropped_note"] + ".")

    rows = ["<table border='0' cellspacing='0' cellpadding='4'>"
            "<tr><th align='left'>Estimator</th><th>x-axis</th><th>r (two-stage)</th>"
            "<th>95% CI</th><th>t</th><th>p</th><th>slope (ms/ms)</th>"
            "<th>rm_corr r (p)</th><th>N</th></tr>"]
    for e in res["estimators"]:
        g, s, rc = e["group"], e["slope"], e["rm_corr"]
        if rc and rc.get("available"):
            rm_cell = f"{rc['r']:.3f} (p {fmt_p(rc['p'])})"
        elif rc:
            rm_cell = "n/a"
        else:
            rm_cell = "n/a"
        rows.append(
            f"<tr><td><b>{e['label']}</b></td>"
            f"<td>{e['x_label']}</td>"
            f"<td align='center'>{fmt(g['r'], 3)}</td>"
            f"<td align='center'>{fmt_ci(g['ci95_lo_r'], g['ci95_hi_r'], 3)}</td>"
            f"<td align='center'>{fmt(g['t'], 2)}</td>"
            f"<td align='center'>{fmt_p(g['p']).lstrip('= ')}</td>"
            f"<td align='center'>{fmt_signed(s['mean'], 3)}<br>"
            f"<span style='color:#888'>{fmt_ci(s['ci95_lo'], s['ci95_hi'], 3)}</span></td>"
            f"<td align='center'>{rm_cell}</td>"
            f"<td align='center'>{g['n']}</td></tr>")
    rows.append("</table>")

    caveats = "<br>".join(f"<b>{e['label'].split('—')[0].strip()}:</b> {e['caveat']}"
                          for e in res["estimators"])
    gap = fb.artefact_gap(res)
    gap_line = ""
    if gap:
        gap_line = (f"<b>Artefact size:</b> naive r = {gap['naive_r']:.3f} vs coupling-free "
                    f"split-half r = {gap['split_half_r']:.3f}; the difference of "
                    f"{gap['gap']:+.3f} is what the shared-noise term contributes on its own. "
                    + ("The split-half estimate is the one to report."
                       if abs(gap["gap"]) > 0.1 else
                       "The two agree closely, so coupling is not driving this result."))
    note = ("Inference: one Pearson r per participant over their own five fingers, Fisher "
            "z-transformed, one-sample t-test of the mean z against 0 (df = N−1). The "
            "participant is the unit — the 35 cells are not 35 independent observations. "
            "rm_corr is the more powerful pooled alternative (separate intercept per "
            "participant, common slope assumed); it is secondary.")
    return "".join(f"<p>{p}</p>" for p in
                   head + ["".join(rows), f"<i>{caveats}</i>", gap_line, f"<i>{note}</i>"] if p)


def _compensation_figure(res: dict) -> Figure:
    ests = res["estimators"]
    fig = Figure(figsize=(10.5, 3.9))
    axes = fig.subplots(1, len(ests))
    axes = np.atleast_1d(axes)
    colors = _participant_colors(res["pairs"]["participant"].unique())
    for ax, e in zip(axes, ests):
        pts = e["points"]
        ax.axhline(0, color="#bbbbbb", linewidth=1, linestyle="--", zorder=1)
        for participant, sub in pts.groupby("participant"):
            x = sub["x"].to_numpy(dtype=float) * MS
            y = sub["y"].to_numpy(dtype=float) * MS
            ax.scatter(x, y, s=26, color=colors.get(participant, "#3a76c4"),
                       alpha=0.85, zorder=3, label=participant)
            if x.size >= 2 and np.std(x) > 0:
                fit = np.polyfit(x, y, 1)
                xs = np.array([x.min(), x.max()])
                ax.plot(xs, np.polyval(fit, xs), "-", linewidth=1.0,
                        color=colors.get(participant, "#3a76c4"), alpha=0.55, zorder=2)
        g = e["group"]
        title = e["label"].split("—")[0].strip()
        ax.set_title(f"{title}\nr = {fmt(g['r'], 3)}, p {fmt_p(g['p']).replace('&lt;', '<')}",
                     fontsize=9)
        ax.set_xlabel(e["x_label"] + " (ms)", fontsize=8)
        ax.set_ylabel("benefit B − C (ms)", fontsize=8)
        ax.tick_params(labelsize=8)
        # RT on the x axis starts at zero here too. r and p are printed in
        # the panel title, so nothing is lost by refusing to crop, and a
        # cropped baseline is what makes a modest slope look steep.
        zero_based_xlim(ax)
    handles, labels = axes[0].get_legend_handles_labels()
    seen, uniq = set(), []
    for h, l in zip(handles, labels):
        if l not in seen:
            seen.add(l)
            uniq.append((h, l))
    if uniq:
        axes[-1].legend([h for h, _ in uniq], [l for _, l in uniq], fontsize=6,
                        loc="best", title="participant", title_fontsize=6)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Section 2 - equalisation


def _equalisation_html(res: dict) -> str:
    head = ["<h3>2. Does the cue even out the five fingers?</h3>",
            "Per participant, the spread of the five finger cell means under each condition; "
            "the test is on the participants' paired B → C change. Negative = the haptic "
            "condition is the more even one."]
    if res["reason"]:
        return "".join(f"<p>{p}</p>" for p in head + [f"<i>Not computed: {res['reason']}.</i>"])

    ratio = res.get("proportional_null_ratio", np.nan)
    head.append(
        f"<b>Reference to beat:</b> C's overall mean is {ratio * 100:.0f}% of B's "
        f"({res['means'][res['baseline']]['mean'] * MS:.0f} → "
        f"{res['means'][res['cued']]['mean'] * MS:.0f} ms). A purely proportional speed-up — "
        "no equalisation at all — would multiply SD and range by exactly that factor and leave "
        "the coefficient of variation unchanged. So an SD ratio near "
        f"{ratio:.2f} is the null, not the finding; only the CV separates the two.")

    rows = ["<table border='0' cellspacing='0' cellpadding='4'>"
            "<tr><th align='left'>Dispersion measure</th><th>B</th><th>C</th><th>C/B</th>"
            "<th>mean diff (C − B)</th><th>95% CI</th><th>dz</th><th>t</th>"
            "<th>p (t)</th><th>p (Wilcoxon)</th><th>↓ in</th></tr>"]
    for m in res["measures"]:
        t = m["test"]
        scale = 1.0 if m["scale_free"] else MS
        dec = 3 if m["scale_free"] else 0
        tag = " <i>(scale-free)</i>" if m["scale_free"] else ""
        rows.append(
            f"<tr><td>{m['label']}{tag}</td>"
            f"<td align='center'>{fmt(m['baseline_mean'] * scale, dec)}</td>"
            f"<td align='center'>{fmt(m['cued_mean'] * scale, dec)}</td>"
            f"<td align='center'>{fmt(m['ratio'], 2)}</td>"
            f"<td align='center'>{fmt_signed(t['mean'] * scale, dec)}</td>"
            f"<td align='center'>{fmt_ci(t['ci95_lo'] * scale, t['ci95_hi'] * scale, dec)}</td>"
            f"<td align='center'>{fmt_signed(t['dz'], 2)}</td>"
            f"<td align='center'>{fmt(t['t'], 2)}</td>"
            f"<td align='center'>{fmt_p(t['p_t']).lstrip('= ')}</td>"
            f"<td align='center'>{fmt_p(t['p_wilcoxon']).lstrip('= ')}</td>"
            f"<td align='center'>{m['n_shrunk']}/{m['n_pairs']}</td></tr>")
    rows.append("</table>")

    # Which condition's cells are the noisier ones is a property of the
    # data, not something to assert in prose: the raw SD only flatters
    # the equalisation story when the BASELINE is the noisier side.
    sem_b = res["means"][res["baseline"]]["mean_cell_sem"]
    sem_c = res["means"][res["cued"]]["mean_cell_sem"]
    if np.isfinite(sem_b) and np.isfinite(sem_c) and sem_b > sem_c * 1.05:
        sem_reading = (f"{res['baseline']}'s cells are the noisier ones, so the raw SD "
                       f"overstates {res['baseline']}'s true spread and flatters the "
                       "equalisation story — read the corrected row.")
    elif np.isfinite(sem_b) and np.isfinite(sem_c) and sem_c > sem_b * 1.05:
        sem_reading = (f"{res['cued']}'s cells are the noisier ones, which biases the raw SD "
                       f"AGAINST equalisation — the corrected row is the conservative one here.")
    else:
        sem_reading = ("The two conditions' cells carry comparable noise, so the correction "
                       "shifts both sides alike and does not change the direction.")
    sems = (f"Mean per-cell standard error: {res['baseline']} {sem_b * MS:.0f} ms, "
            f"{res['cued']} {sem_c * MS:.0f} ms — the noise the corrected SD removes. "
            + sem_reading)
    v = fe.verdict(res)
    return "".join(f"<p>{p}</p>" for p in
                   head + ["".join(rows), f"<i>{sems}</i>",
                           f"<b>Reading:</b> {v}" if v else ""] if p)


def _equalisation_figure(res: dict) -> Figure:
    table = res["table"]
    baseline, cued = res["baseline"], res["cued"]
    fig = Figure(figsize=(10.5, 3.9))
    ax_sd, ax_cv = fig.subplots(1, 2)
    colors = _participant_colors(table["participant"].unique())
    specs = [(ax_sd, "sd_corrected", MS, "SD across fingers, noise-corrected (ms)"),
             (ax_cv, "cv_raw", 1.0, "Coefficient of variation (SD / mean)")]
    for ax, key, scale, ylabel in specs:
        wide = table.pivot(index="participant", columns="condition", values=key)
        for participant, row in wide.iterrows():
            ys = [row.get(baseline, np.nan) * scale, row.get(cued, np.nan) * scale]
            ax.plot([0, 1], ys, "-o", markersize=5, linewidth=1.1,
                    color=colors.get(participant, "#888888"), alpha=0.8, zorder=2)
        means = [float(wide[baseline].mean()) * scale, float(wide[cued].mean()) * scale]
        ax.plot([0, 1], means, "-D", markersize=11, linewidth=2.4, color="black",
                zorder=4, label="group mean")
        if key == "sd_corrected" and np.isfinite(res.get("proportional_null_ratio", np.nan)):
            null = means[0] * res["proportional_null_ratio"]
            ax.plot([1], [null], marker="_", markersize=26, color="#c23b22", zorder=5,
                    label="proportional-speed-up null")
        ax.set_xticks([0, 1], [baseline, cued])
        ax.set_xlim(-0.3, 1.3)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_title(ylabel.split("(")[0].strip(), fontsize=10)
        ax.legend(fontsize=7)
        zero_based_ylim(ax)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Section 3 - weakest finger


def _weakest_html(results: List[dict]) -> str:
    head = ["<h3>3. Does each participant's own weakest finger gain most?</h3>",
            "The weakest finger is identified per participant under B, then the benefit at "
            "that finger is compared with the mean benefit at their other four. Positive "
            "advantage = the weakest finger gained more than the rest."]
    blocks = []
    for res in results:
        title = (f"<b>Weakest = {res['criterion_label']} under {res['baseline']}</b> "
                 f"(selection on {res['select_metric']}, benefit on {res['benefit_metric']})")
        if res["reason"]:
            blocks.append(f"{title}<br><i>Not computed: {res['reason']}.</i>")
            continue
        counts = ", ".join(f"{fc.finger_label(f)}: {n}" for f, n in res["finger_counts"].items() if n)
        stability = fw.stability_verdict(res)
        rows = ["<table border='0' cellspacing='0' cellpadding='4'>"
                "<tr><th align='left'>Estimate</th><th>mean advantage</th><th>95% CI</th>"
                "<th>dz</th><th>t</th><th>p (t)</th><th>p (Wilcoxon)</th><th>N</th></tr>"]
        for e in res["estimates"]:
            t = e["test"]
            rows.append(
                f"<tr><td>{e['label']}</td>"
                f"<td align='center'>{fmt_signed(t['mean'] * MS, 0, ' ms')}</td>"
                f"<td align='center'>{fmt_ci(t['ci95_lo'] * MS, t['ci95_hi'] * MS, 0, ' ms')}</td>"
                f"<td align='center'>{fmt_signed(t['dz'], 2)}</td>"
                f"<td align='center'>{fmt(t['t'], 2)}</td>"
                f"<td align='center'>{fmt_p(t['p_t']).lstrip('= ')}</td>"
                f"<td align='center'>{fmt_p(t['p_wilcoxon']).lstrip('= ')}</td>"
                f"<td align='center'>{t['n']}</td></tr>")
        rows.append("</table>")
        gap = fw.regression_to_mean_gap(res)
        gap_line = ""
        if gap:
            gap_line = (f"<i>Regression to the mean accounts for "
                        f"{(gap['gap']) * MS:+.0f} ms of the naive advantage "
                        f"({gap['naive'] * MS:+.0f} → {gap['split_half'] * MS:+.0f} ms once "
                        f"selection and measurement use disjoint trials).</i>")
        blocks.append(f"{title}<br>Weakest-finger distribution: {counts or 'n/a'}."
                      f"<br><b>Selection stability:</b> {stability}"
                      + "".join(rows) + gap_line)
    return "".join(f"<p>{p}</p>" for p in head + blocks if p)


def _weakest_figure(results: List[dict]) -> Figure:
    usable = [r for r in results if not r["reason"]]
    fig = Figure(figsize=(10.5, 3.9))
    axes = np.atleast_1d(fig.subplots(1, max(len(usable), 1)))
    for ax, res in zip(axes, usable):
        ax.axhline(0, color="#bbbbbb", linewidth=1, linestyle="--", zorder=1)
        for xi, e in enumerate(res["estimates"]):
            vals = e["table"]["advantage"].to_numpy(dtype=float) * MS if len(e["table"]) else np.array([])
            jitter = (np.arange(vals.size) - (vals.size - 1) / 2) * (0.28 / max(vals.size, 1))
            ax.scatter(xi + jitter, vals, s=28, color="#3a76c4", alpha=0.8, zorder=3)
            t = e["test"]
            if np.isfinite(t["mean"]):
                mean = t["mean"] * MS
                if np.isfinite(t["ci95_lo"]):
                    ax.errorbar([xi], [mean],
                                yerr=[[mean - t["ci95_lo"] * MS], [t["ci95_hi"] * MS - mean]],
                                color="black", capsize=4, linewidth=1.3, zorder=4)
                ax.scatter([xi], [mean], s=150, marker="D", color="#d9663d",
                           edgecolor="black", zorder=5)
        ax.set_xticks(range(len(res["estimates"])),
                      ["naive\n(biased)", "split-half\n(unbiased)"], fontsize=8)
        ax.set_ylabel("weakest − others benefit (ms)", fontsize=9)
        ax.set_title(f"Weakest = {res['criterion_label']}", fontsize=10)
    for ax in axes[len(usable):]:
        ax.axis("off")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------


def build(event_rows: List[dict], metric: str = "rt_complete_s"
          ) -> Tuple[str, List[Figure], Dict[str, pd.DataFrame]]:
    """(caption_html, figures, datasets) for the Finger Benefit tab."""
    comp = fb.compensation_analysis(event_rows, metric)
    equal = fe.equalisation_analysis(event_rows, metric)
    weak = [fw.weakest_finger_analysis(event_rows, criterion, metric)
            for criterion, *_ in fw.CRITERIA]

    intro = (
        "<h3>Finger Benefit — is the vibrotactile advantage compensatory?</h3>"
        "<p>The RM-ANOVA tab asks whether the B → C benefit differs across fingers at all. "
        "These three analyses ask the directed version of that question: does the cue help "
        "the digits that need it most? All three run on the same complete-case "
        f"{fc.BASELINE_CONDITION}/{fc.CUED_CONDITION} × {len(fc.FINGER_IDS)}-finger cell grid, "
        f"on <b>{metric}</b>, with the participant as the independent unit.</p>"
        "<p><b>Read the paired columns, not the headline.</b> Each question is naturally "
        "written in a form that is biased towards confirming it: correlating a difference "
        "against one of its own terms, and selecting an extreme cell before re-measuring it. "
        "Every section therefore shows the naive estimate beside a coupling-free one computed "
        "from disjoint halves of the trials, and the gap between them is the artefact.</p>")

    sections = [intro,
                _compensation_html(comp),
                _equalisation_html(equal),
                _weakest_html(weak)]

    figures, datasets = {}, {}
    if not comp["reason"]:
        figures["group_finger_benefit_compensation"] = _compensation_figure(comp)
        datasets["finger_benefit_cells"] = comp["pairs"]
        for e in comp["estimators"]:
            datasets[f"finger_benefit_{e['key']}_per_participant"] = e["per_participant"]
        datasets["finger_benefit_summary"] = _compensation_summary(comp)
    if not equal["reason"]:
        figures["group_finger_equalisation"] = _equalisation_figure(equal)
        datasets["finger_equalisation_dispersion"] = equal["table"]
        datasets["finger_equalisation_tests"] = _equalisation_summary(equal)
    usable_weak = [r for r in weak if not r["reason"]]
    if usable_weak:
        figures["group_finger_weakest"] = _weakest_figure(weak)
        for res in usable_weak:
            for e in res["estimates"]:
                if len(e["table"]):
                    datasets[f"finger_weakest_{res['criterion']}_{e['key']}"] = e["table"]
        datasets["finger_weakest_summary"] = _weakest_summary(usable_weak)

    return "".join(sections), figures, datasets


# ---------------------------------------------------------------------------
# Tidy summaries: the numbers the three tables render, as exportable rows.
# The per-participant frames above are the raw material; these are what a
# results chapter quotes, so both go into the export.


def _compensation_summary(res: dict) -> pd.DataFrame:
    rows = []
    for e in res["estimators"]:
        g, s, rc = e["group"], e["slope"], e["rm_corr"]
        rows.append({
            "metric": res["metric"], "estimator": e["key"], "x_axis": e["x_label"],
            "r_two_stage": g["r"], "r_ci95_lo": g["ci95_lo_r"], "r_ci95_hi": g["ci95_hi_r"],
            "t": g["t"], "df": g["df"], "p": g["p"], "n_participants": g["n"],
            "slope_mean": s["mean"], "slope_ci95_lo": s["ci95_lo"],
            "slope_ci95_hi": s["ci95_hi"], "slope_p": s.get("p", np.nan),
            "rm_corr_r": rc.get("r", np.nan) if rc else np.nan,
            "rm_corr_p": rc.get("p", np.nan) if rc else np.nan,
            "rm_corr_dof": rc.get("dof", np.nan) if rc else np.nan,
            "coupling_caveat": e["caveat"],
        })
    return pd.DataFrame(rows)


def _equalisation_summary(res: dict) -> pd.DataFrame:
    rows = []
    for m in res["measures"]:
        t = m["test"]
        rows.append({
            "metric": res["metric"], "measure": m["key"], "scale_free": m["scale_free"],
            "baseline_mean": m["baseline_mean"], "cued_mean": m["cued_mean"],
            "ratio": m["ratio"], "proportional_null_ratio": res["proportional_null_ratio"],
            "mean_diff": t["mean"], "ci95_lo": t["ci95_lo"], "ci95_hi": t["ci95_hi"],
            "dz": t["dz"], "t": t["t"], "p_t": t["p_t"], "w": t["w"],
            "p_wilcoxon": t["p_wilcoxon"], "n_shrunk": m["n_shrunk"],
            "n_pairs": m["n_pairs"], "note": t["note"],
        })
    return pd.DataFrame(rows)


def _weakest_summary(results: List[dict]) -> pd.DataFrame:
    rows = []
    for res in results:
        s = res["stability"]
        for e in res["estimates"]:
            t = e["test"]
            rows.append({
                "criterion": res["criterion"], "select_metric": res["select_metric"],
                "benefit_metric": res["benefit_metric"], "estimator": e["key"],
                "mean_advantage": t["mean"], "ci95_lo": t["ci95_lo"],
                "ci95_hi": t["ci95_hi"], "dz": t["dz"], "t": t["t"], "p_t": t["p_t"],
                "p_wilcoxon": t["p_wilcoxon"], "n_participants": t["n"],
                "selection_agreement_rate": s["agreement_rate"],
                "selection_chance_rate": s["chance_rate"],
                "selection_p_vs_chance": s["p_vs_chance"],
                "note": t["note"],
            })
    return pd.DataFrame(rows)
