#!/usr/bin/env python3
import argparse
import itertools
import math
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

try:
    from scipy.stats import friedmanchisquare, wilcoxon
except Exception:
    friedmanchisquare = None
    wilcoxon = None


ATTACHMENT_ORDER = ["tape", "putty", "eyelash_glue"]
METRICS = ["peak_delta", "rms_delta", "onset_delay_ms"]


def holm_adjust(pvals):
    indexed = sorted(enumerate(pvals), key=lambda x: x[1])
    adjusted = [None] * len(pvals)
    m = len(pvals)
    running = 0.0
    for rank, (idx, p) in enumerate(indexed, start=1):
        adj = min(1.0, p * (m - rank + 1))
        running = max(running, adj)
        adjusted[idx] = running
    return adjusted


def ensure_outdir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def load_data(db_path: Path):
    conn = sqlite3.connect(db_path)
    trials = pd.read_sql_query("SELECT * FROM trials", conn)
    conditions = pd.read_sql_query("SELECT * FROM conditions", conn)
    conn.close()

    if trials.empty:
        raise RuntimeError("No rows found in trials table.")

    for col in ["participant_id", "actuator_type", "attachment_method"]:
        if col not in trials.columns:
            legacy = {"participant": "participant_id", "actuator": "actuator_type", "attachment": "attachment_method"}
            if col in legacy.values():
                continue
        if col not in trials.columns and col.replace("_id", "") in trials.columns:
            trials[col] = trials[col.replace("_id", "")]
    if "participant_id" not in trials.columns and "participant" in trials.columns:
        trials["participant_id"] = trials["participant"]
    if "actuator_type" not in trials.columns and "actuator" in trials.columns:
        trials["actuator_type"] = trials["actuator"]
    if "attachment_method" not in trials.columns and "attachment" in trials.columns:
        trials["attachment_method"] = trials["attachment"]

    if not conditions.empty:
        if "participant_id" not in conditions.columns and "participant" in conditions.columns:
            conditions["participant_id"] = conditions["participant"]
        if "actuator_type" not in conditions.columns and "actuator" in conditions.columns:
            conditions["actuator_type"] = conditions["actuator"]
        if "attachment_method" not in conditions.columns and "attachment" in conditions.columns:
            conditions["attachment_method"] = conditions["attachment"]

    return trials, conditions


def summarize_trials(trials: pd.DataFrame):
    summary = (
        trials.groupby(["participant_id", "actuator_type", "attachment_method"], as_index=False)
        .agg(
            n_trials=("trial_index", "count"),
            mean_peak_delta=("peak_delta", "mean"),
            sd_peak_delta=("peak_delta", "std"),
            mean_rms_delta=("rms_delta", "mean"),
            sd_rms_delta=("rms_delta", "std"),
            mean_onset_delay_ms=("onset_delay_ms", "mean"),
            sd_onset_delay_ms=("onset_delay_ms", "std"),
            valid_onset_count=("onset_delay_ms", lambda s: s.notna().sum()),
        )
    )
    return summary


def friedman_by_actuator(summary: pd.DataFrame, metric_col: str):
    results = []
    if friedmanchisquare is None:
        return pd.DataFrame(columns=["actuator_type", "metric", "n_blocks", "statistic", "p_value"])

    for actuator in sorted(summary["actuator_type"].unique()):
        sub = summary[summary["actuator_type"] == actuator]
        pivot = sub.pivot_table(
            index="participant_id",
            columns="attachment_method",
            values=metric_col,
            aggfunc="mean",
        )
        needed = [a for a in ATTACHMENT_ORDER if a in pivot.columns]
        if len(needed) < 3:
            continue
        pivot = pivot[ATTACHMENT_ORDER].dropna()
        if len(pivot) < 2:
            continue
        stat, p = friedmanchisquare(*[pivot[c].values for c in ATTACHMENT_ORDER])
        results.append(
            {
                "actuator_type": actuator,
                "metric": metric_col,
                "n_blocks": len(pivot),
                "statistic": stat,
                "p_value": p,
            }
        )
    return pd.DataFrame(results)


def friedman_pooled(summary: pd.DataFrame, metric_col: str):
    if friedmanchisquare is None:
        return None
    tmp = summary.copy()
    tmp["block_id"] = tmp["participant_id"].astype(str) + "_" + tmp["actuator_type"].astype(str)
    pivot = tmp.pivot_table(index="block_id", columns="attachment_method", values=metric_col, aggfunc="mean")
    if not all(a in pivot.columns for a in ATTACHMENT_ORDER):
        return None
    pivot = pivot[ATTACHMENT_ORDER].dropna()
    if len(pivot) < 2:
        return None
    stat, p = friedmanchisquare(*[pivot[c].values for c in ATTACHMENT_ORDER])
    return {"metric": metric_col, "n_blocks": len(pivot), "statistic": stat, "p_value": p}


def pairwise_wilcoxon(summary: pd.DataFrame, metric_col: str):
    rows = []
    if wilcoxon is None:
        return pd.DataFrame(columns=["actuator_type", "metric", "comparison", "n_pairs", "statistic", "p_value", "p_holm"])

    comparisons = [("tape", "putty"), ("tape", "eyelash_glue"), ("putty", "eyelash_glue")]
    for actuator in sorted(summary["actuator_type"].unique()):
        sub = summary[summary["actuator_type"] == actuator]
        pivot = sub.pivot_table(
            index="participant_id", columns="attachment_method", values=metric_col, aggfunc="mean"
        )
        pvals = []
        temp_rows = []
        for a, b in comparisons:
            if a not in pivot.columns or b not in pivot.columns:
                continue
            pair = pivot[[a, b]].dropna()
            if len(pair) < 2:
                continue
            stat, p = wilcoxon(pair[a], pair[b], zero_method="wilcox", alternative="two-sided", method="auto")
            temp_rows.append(
                {
                    "actuator_type": actuator,
                    "metric": metric_col,
                    "comparison": f"{a} vs {b}",
                    "n_pairs": len(pair),
                    "statistic": stat,
                    "p_value": p,
                }
            )
            pvals.append(p)
        if temp_rows:
            adjusted = holm_adjust(pvals)
            for row, p_adj in zip(temp_rows, adjusted):
                row["p_holm"] = p_adj
                rows.append(row)
    return pd.DataFrame(rows)


def descriptive_by_attachment(summary: pd.DataFrame):
    out_rows = []
    mapping = {
        "mean_peak_delta": "Peak delta",
        "mean_rms_delta": "RMS delta",
        "mean_onset_delay_ms": "Onset delay (ms)",
    }
    for actuator in sorted(summary["actuator_type"].unique()):
        sub = summary[summary["actuator_type"] == actuator]
        for attachment in ATTACHMENT_ORDER:
            s2 = sub[sub["attachment_method"] == attachment]
            if s2.empty:
                continue
            for col, label in mapping.items():
                vals = s2[col].dropna()
                if vals.empty:
                    continue
                out_rows.append(
                    {
                        "actuator_type": actuator,
                        "attachment_method": attachment,
                        "metric": label,
                        "n": len(vals),
                        "mean": vals.mean(),
                        "sd": vals.std(ddof=1) if len(vals) > 1 else 0.0,
                        "median": vals.median(),
                    }
                )
    return pd.DataFrame(out_rows)


def reliability_summary(conditions: pd.DataFrame):
    if conditions.empty or "failure_cycle" not in conditions.columns:
        return pd.DataFrame()
    out = (
        conditions.groupby(["actuator_type", "attachment_method"], as_index=False)
        .agg(
            n=("failure_cycle", "count"),
            mean_failure_cycle=("failure_cycle", "mean"),
            median_failure_cycle=("failure_cycle", "median"),
            never_fell_off=("failure_cycle", lambda s: int((s == 0).sum())),
        )
    )
    return out


def plot_metric(summary: pd.DataFrame, metric_col: str, ylabel: str, outpath: Path):
    for actuator in sorted(summary["actuator_type"].unique()):
        sub = summary[summary["actuator_type"] == actuator]
        stats = (
            sub.groupby("attachment_method")[metric_col]
            .agg(["mean", "std", "count"])
            .reindex(ATTACHMENT_ORDER)
        )
        x_labels = [a for a in ATTACHMENT_ORDER if a in stats.index]
        means = [stats.loc[a, "mean"] for a in x_labels]
        errs = []
        for a in x_labels:
            sd = stats.loc[a, "std"]
            n = stats.loc[a, "count"]
            errs.append(0.0 if pd.isna(sd) or n <= 1 else sd / math.sqrt(n))

        plt.figure(figsize=(6, 4))
        plt.bar(x_labels, means, yerr=errs, capsize=4)
        for pid, psub in sub.groupby("participant_id"):
            psub = psub.set_index("attachment_method").reindex(x_labels)
            plt.plot(x_labels, psub[metric_col].values, marker="o", alpha=0.7)
        plt.title(f"{actuator}: {ylabel}")
        plt.ylabel(ylabel)
        plt.xlabel("Attachment method")
        plt.tight_layout()
        plt.savefig(outpath.with_name(f"{outpath.stem}_{actuator}{outpath.suffix}"), dpi=200)
        plt.close()


def plot_reliability(conditions: pd.DataFrame, outpath: Path):
    if conditions.empty or "failure_cycle" not in conditions.columns:
        return
    for actuator in sorted(conditions["actuator_type"].unique()):
        sub = conditions[conditions["actuator_type"] == actuator]
        stats = (
            sub.groupby("attachment_method")["failure_cycle"]
            .mean()
            .reindex(ATTACHMENT_ORDER)
        )
        x_labels = [a for a in ATTACHMENT_ORDER if a in stats.index]
        values = [stats.loc[a] for a in x_labels]
        plt.figure(figsize=(6, 4))
        plt.bar(x_labels, values)
        plt.title(f"{actuator}: Mean failure cycle")
        plt.ylabel("Failure cycle (0 = never fell off)")
        plt.xlabel("Attachment method")
        plt.tight_layout()
        plt.savefig(outpath.with_name(f"{outpath.stem}_{actuator}{outpath.suffix}"), dpi=200)
        plt.close()


def write_report(
    outdir: Path,
    summary: pd.DataFrame,
    desc: pd.DataFrame,
    friedman_results: pd.DataFrame,
    pairwise_results: pd.DataFrame,
    pooled_results: pd.DataFrame,
    reliability: pd.DataFrame,
):
    lines = []
    lines.append("# Attachment Verification Analysis Report")
    lines.append("")
    lines.append("This report evaluates whether attachment method changes the measured vibration transmission.")
    lines.append("")
    lines.append("## Data structure")
    lines.append(f"- Condition-level rows: {len(summary)}")
    lines.append(f"- Participants: {summary['participant_id'].nunique()}")
    lines.append(f"- Actuators: {', '.join(sorted(summary['actuator_type'].unique()))}")
    lines.append("")

    lines.append("## Descriptive statistics")
    if desc.empty:
        lines.append("No descriptive statistics available.")
    else:
        for actuator in sorted(desc["actuator_type"].unique()):
            lines.append(f"### {actuator}")
            sub = desc[desc["actuator_type"] == actuator]
            for metric in sub["metric"].unique():
                lines.append(f"**{metric}**")
                sm = sub[sub["metric"] == metric]
                for att in ATTACHMENT_ORDER:
                    row = sm[sm["attachment_method"] == att]
                    if row.empty:
                        continue
                    r = row.iloc[0]
                    lines.append(
                        f"- {att}: mean={r['mean']:.2f}, sd={r['sd']:.2f}, median={r['median']:.2f}, n={int(r['n'])}"
                    )
            lines.append("")

    lines.append("## Friedman tests by actuator")
    if friedman_results.empty:
        lines.append("Friedman tests could not be computed.")
    else:
        for _, r in friedman_results.iterrows():
            lines.append(
                f"- {r['actuator_type']} | {r['metric']}: chi-square={r['statistic']:.3f}, p={r['p_value']:.4f}, n blocks={int(r['n_blocks'])}"
            )
    lines.append("")

    lines.append("## Pooled Friedman tests")
    if pooled_results.empty:
        lines.append("Pooled tests could not be computed.")
    else:
        for _, r in pooled_results.iterrows():
            lines.append(
                f"- {r['metric']}: chi-square={r['statistic']:.3f}, p={r['p_value']:.4f}, n blocks={int(r['n_blocks'])}"
            )
    lines.append("")

    lines.append("## Pairwise Wilcoxon tests")
    if pairwise_results.empty:
        lines.append("Pairwise tests could not be computed.")
    else:
        for _, r in pairwise_results.iterrows():
            lines.append(
                f"- {r['actuator_type']} | {r['metric']} | {r['comparison']}: W={r['statistic']:.3f}, p={r['p_value']:.4f}, Holm-adjusted p={r['p_holm']:.4f}, n pairs={int(r['n_pairs'])}"
            )
    lines.append("")

    if not reliability.empty:
        lines.append("## Reliability summary")
        for actuator in sorted(reliability["actuator_type"].unique()):
            lines.append(f"### {actuator}")
            sub = reliability[reliability["actuator_type"] == actuator]
            for att in ATTACHMENT_ORDER:
                row = sub[sub["attachment_method"] == att]
                if row.empty:
                    continue
                r = row.iloc[0]
                lines.append(
                    f"- {att}: mean failure cycle={r['mean_failure_cycle']:.2f}, median={r['median_failure_cycle']:.2f}, never fell off={int(r['never_fell_off'])}/{int(r['n'])}"
                )
        lines.append("")

    lines.append("## Interpretation guide")
    lines.append("- Start with the Friedman test for each actuator and metric.")
    lines.append("- If p < 0.05, attachment method likely affected that vibration metric.")
    lines.append("- Use the pairwise Wilcoxon results to see which attachment methods differed.")
    lines.append("- For onset delay, missing values can reduce the number of analyzable blocks.")
    lines.append("- Reliability is descriptive here unless you want a separate survival-style analysis later.")

    (outdir / "analysis_report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--outdir", default="attachment_analysis_output", help="Output directory")
    args = parser.parse_args()

    db_path = Path(args.db)
    outdir = Path(args.outdir)
    ensure_outdir(outdir)

    trials, conditions = load_data(db_path)
    summary = summarize_trials(trials)
    summary.to_csv(outdir / "condition_level_summary.csv", index=False)

    desc = descriptive_by_attachment(summary)
    desc.to_csv(outdir / "descriptive_statistics.csv", index=False)

    friedman_frames = []
    pairwise_frames = []
    pooled_rows = []
    for metric_col in ["mean_peak_delta", "mean_rms_delta", "mean_onset_delay_ms"]:
        friedman_frames.append(friedman_by_actuator(summary, metric_col))
        pairwise_frames.append(pairwise_wilcoxon(summary, metric_col))
        pooled = friedman_pooled(summary, metric_col)
        if pooled is not None:
            pooled_rows.append(pooled)

    friedman_results = pd.concat(friedman_frames, ignore_index=True) if friedman_frames else pd.DataFrame()
    pairwise_results = pd.concat(pairwise_frames, ignore_index=True) if pairwise_frames else pd.DataFrame()
    pooled_results = pd.DataFrame(pooled_rows)

    friedman_results.to_csv(outdir / "friedman_results.csv", index=False)
    pairwise_results.to_csv(outdir / "pairwise_wilcoxon_results.csv", index=False)
    pooled_results.to_csv(outdir / "pooled_friedman_results.csv", index=False)

    reliability = reliability_summary(conditions)
    reliability.to_csv(outdir / "reliability_summary.csv", index=False)

    plot_metric(summary, "mean_peak_delta", "Mean peak delta", outdir / "peak_delta.png")
    plot_metric(summary, "mean_rms_delta", "Mean RMS delta", outdir / "rms_delta.png")
    plot_metric(summary, "mean_onset_delay_ms", "Mean onset delay (ms)", outdir / "onset_delay.png")
    plot_reliability(conditions, outdir / "reliability.png")

    write_report(outdir, summary, desc, friedman_results, pairwise_results, pooled_results, reliability)

    print(f"Done. Results saved to: {outdir}")


if __name__ == "__main__":
    main()
