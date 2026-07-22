import argparse
import math
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    from scipy import stats
except Exception:
    stats = None


# ==========================================
# Configuration
# ==========================================

ACTUATOR_ORDER = ["LRA", "ERM"]
SUBJECTIVE_ITEMS = ["clarity", "comfort", "responsiveness", "satisfaction"]
VALID_RT_ONLY_LABEL = "valid_only"


# ==========================================
# Data loading
# ==========================================


def connect_db(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")
    return sqlite3.connect(db_path)



def read_table(conn: sqlite3.Connection, table_name: str) -> pd.DataFrame:
    query = f"SELECT * FROM {table_name}"
    return pd.read_sql_query(query, conn)



def load_experiment_data(db_path: str) -> Dict[str, pd.DataFrame]:
    conn = connect_db(db_path)
    try:
        tables = {
            "participants": read_table(conn, "participants"),
            "trials": read_table(conn, "trials"),
            "subjective_ratings": read_table(conn, "subjective_ratings"),
            "final_feedback": read_table(conn, "final_feedback"),
        }
    finally:
        conn.close()
    return tables


# ==========================================
# Statistics helpers
# ==========================================


def safe_mean(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.mean()) if len(values) else float("nan")



def safe_sd(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.std(ddof=1)) if len(values) > 1 else float("nan")



def proportion_ci_wilson(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = successes / n
    denom = 1 + z ** 2 / n
    center = (p + z ** 2 / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * n)) / n) / denom
    return center - margin, center + margin



def paired_effect_size_dz(x: np.ndarray, y: np.ndarray) -> float:
    diff = x - y
    if len(diff) < 2:
        return float("nan")
    sd = np.std(diff, ddof=1)
    if sd == 0:
        return float("nan")
    return float(np.mean(diff) / sd)



def sign_test_two_sided(x: np.ndarray, y: np.ndarray) -> Tuple[float, int, int]:
    diff = x - y
    pos = int(np.sum(diff > 0))
    neg = int(np.sum(diff < 0))
    n = pos + neg
    if n == 0:
        return float("nan"), pos, neg
    if stats is not None and hasattr(stats, "binomtest"):
        p = stats.binomtest(pos, n=n, p=0.5, alternative="two-sided").pvalue
        return float(p), pos, neg
    # Simple exact binomial fallback
    from math import comb
    tail = sum(comb(n, k) for k in range(0, min(pos, neg) + 1)) / (2 ** n)
    p = min(1.0, 2 * tail)
    return float(p), pos, neg



def paired_tests(x: pd.Series, y: pd.Series) -> Dict[str, float]:
    paired = pd.concat([x.reset_index(drop=True), y.reset_index(drop=True)], axis=1).dropna()
    paired.columns = ["x", "y"]
    x_vals = paired["x"].to_numpy(dtype=float)
    y_vals = paired["y"].to_numpy(dtype=float)

    result: Dict[str, float] = {
        "n": len(paired),
        "mean_x": float(np.mean(x_vals)) if len(x_vals) else float("nan"),
        "mean_y": float(np.mean(y_vals)) if len(y_vals) else float("nan"),
        "mean_diff_x_minus_y": float(np.mean(x_vals - y_vals)) if len(x_vals) else float("nan"),
        "sd_diff": float(np.std(x_vals - y_vals, ddof=1)) if len(x_vals) > 1 else float("nan"),
        "cohens_dz": paired_effect_size_dz(x_vals, y_vals),
        "t_stat": float("nan"),
        "t_p": float("nan"),
        "wilcoxon_stat": float("nan"),
        "wilcoxon_p": float("nan"),
        "sign_p": float("nan"),
        "sign_pos": float("nan"),
        "sign_neg": float("nan"),
    }

    if len(paired) >= 2 and stats is not None:
        try:
            t_res = stats.ttest_rel(x_vals, y_vals, nan_policy="omit")
            result["t_stat"] = float(t_res.statistic)
            result["t_p"] = float(t_res.pvalue)
        except Exception:
            pass

        try:
            w_res = stats.wilcoxon(x_vals, y_vals, zero_method="wilcox", alternative="two-sided")
            result["wilcoxon_stat"] = float(w_res.statistic)
            result["wilcoxon_p"] = float(w_res.pvalue)
        except Exception:
            pass

    sign_p, pos, neg = sign_test_two_sided(x_vals, y_vals)
    result["sign_p"] = sign_p
    result["sign_pos"] = float(pos)
    result["sign_neg"] = float(neg)
    return result



def significance_text(p: float) -> str:
    if pd.isna(p):
        return "not available"
    if p < 0.001:
        return "p < .001"
    if p < 0.01:
        return f"p = {p:.3f}"
    return f"p = {p:.3f}"


# ==========================================
# Summaries
# ==========================================


def prepare_trials(trials: pd.DataFrame) -> pd.DataFrame:
    out = trials.copy()
    out["actuator_type"] = out["actuator_type"].str.upper()
    out["reaction_time_ms"] = pd.to_numeric(out["reaction_time_ms"], errors="coerce")
    out["is_valid"] = pd.to_numeric(out["is_valid"], errors="coerce").fillna(0).astype(int)
    out["is_miss"] = pd.to_numeric(out["is_miss"], errors="coerce").fillna(0).astype(int)
    out["false_start_count"] = pd.to_numeric(out["false_start_count"], errors="coerce").fillna(0).astype(int)
    return out



def participant_trial_summary(trials: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for (participant_id, actuator), g in trials.groupby(["participant_id", "actuator_type"], sort=False):
        valid = g[g["is_valid"] == 1]
        rows.append(
            {
                "participant_id": participant_id,
                "actuator_type": actuator,
                "n_trials": len(g),
                "valid_trials": int(g["is_valid"].sum()),
                "miss_trials": int(g["is_miss"].sum()),
                "accuracy": float(g["is_valid"].mean()) if len(g) else float("nan"),
                "miss_rate": float(g["is_miss"].mean()) if len(g) else float("nan"),
                "mean_rt_ms_valid": safe_mean(valid["reaction_time_ms"]),
                "median_rt_ms_valid": float(valid["reaction_time_ms"].median()) if len(valid) else float("nan"),
                "sd_rt_ms_valid": safe_sd(valid["reaction_time_ms"]),
                "mean_false_starts": safe_mean(g["false_start_count"]),
                "sum_false_starts": int(g["false_start_count"].sum()),
            }
        )
    return pd.DataFrame(rows)



def block_level_summary(participant_summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for actuator, g in participant_summary.groupby("actuator_type", sort=False):
        rows.append(
            {
                "actuator_type": actuator,
                "participants": g["participant_id"].nunique(),
                "mean_rt_ms_valid": safe_mean(g["mean_rt_ms_valid"]),
                "sd_rt_ms_valid": safe_sd(g["mean_rt_ms_valid"]),
                "mean_accuracy": safe_mean(g["accuracy"]),
                "sd_accuracy": safe_sd(g["accuracy"]),
                "mean_miss_rate": safe_mean(g["miss_rate"]),
                "sd_miss_rate": safe_sd(g["miss_rate"]),
                "mean_false_starts": safe_mean(g["mean_false_starts"]),
                "sd_false_starts": safe_sd(g["mean_false_starts"]),
            }
        )
    return pd.DataFrame(rows)



def subjective_summary(subjective: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    subj = subjective.copy()
    subj["actuator_type"] = subj["actuator_type"].str.upper()
    participant_rows = []
    for (participant_id, actuator), g in subj.groupby(["participant_id", "actuator_type"], sort=False):
        row = {"participant_id": participant_id, "actuator_type": actuator}
        for item in SUBJECTIVE_ITEMS:
            row[item] = pd.to_numeric(g[item], errors="coerce").mean()
        row["overall_mean_rating"] = np.mean([row[item] for item in SUBJECTIVE_ITEMS])
        participant_rows.append(row)
    participant_df = pd.DataFrame(participant_rows)

    block_rows = []
    for actuator, g in participant_df.groupby("actuator_type", sort=False):
        row = {"actuator_type": actuator, "participants": g["participant_id"].nunique()}
        for item in SUBJECTIVE_ITEMS + ["overall_mean_rating"]:
            row[f"mean_{item}"] = safe_mean(g[item])
            row[f"sd_{item}"] = safe_sd(g[item])
        block_rows.append(row)
    block_df = pd.DataFrame(block_rows)
    return participant_df, block_df



def preference_summary(final_feedback: pd.DataFrame) -> pd.DataFrame:
    fb = final_feedback.copy()
    if fb.empty:
        return pd.DataFrame(columns=["preferred_actuator", "count", "proportion"])
    fb["preferred_actuator"] = fb["preferred_actuator"].str.upper()
    counts = fb["preferred_actuator"].value_counts().rename_axis("preferred_actuator").reset_index(name="count")
    counts["proportion"] = counts["count"] / counts["count"].sum()
    return counts



def paired_comparison_tables(participant_summary: pd.DataFrame, subjective_participant: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    trial_metrics = [
        ("mean_rt_ms_valid", "Reaction time (ms, valid only)"),
        ("accuracy", "Accuracy"),
        ("miss_rate", "Miss rate"),
        ("mean_false_starts", "False starts per trial"),
    ]

    trial_rows = []
    for metric, label in trial_metrics:
        pivot = participant_summary.pivot(index="participant_id", columns="actuator_type", values=metric)
        if all(act in pivot.columns for act in ACTUATOR_ORDER):
            res = paired_tests(pivot["LRA"], pivot["ERM"])
            res["metric"] = metric
            res["label"] = label
            trial_rows.append(res)
    trial_cmp = pd.DataFrame(trial_rows)

    subjective_rows = []
    for metric in SUBJECTIVE_ITEMS + ["overall_mean_rating"]:
        pivot = subjective_participant.pivot(index="participant_id", columns="actuator_type", values=metric)
        if all(act in pivot.columns for act in ACTUATOR_ORDER):
            res = paired_tests(pivot["LRA"], pivot["ERM"])
            res["metric"] = metric
            res["label"] = metric.replace("_", " ").title()
            subjective_rows.append(res)
    subjective_cmp = pd.DataFrame(subjective_rows)
    return trial_cmp, subjective_cmp


# ==========================================
# Plotting
# ==========================================


def save_barplot(values: pd.DataFrame, x_col: str, y_col: str, yerr_col: Optional[str], title: str, ylabel: str, out_path: Path) -> None:
    plt.figure(figsize=(6, 4))
    x = np.arange(len(values))
    y = values[y_col].to_numpy(dtype=float)
    labels = values[x_col].tolist()
    if yerr_col is not None and yerr_col in values.columns:
        yerr = values[yerr_col].to_numpy(dtype=float)
        plt.bar(x, y, yerr=yerr, capsize=5)
    else:
        plt.bar(x, y)
    plt.xticks(x, labels)
    plt.title(title)
    plt.ylabel(ylabel)
    plt.tight_layout()
    plt.savefig(out_path, dpi=300)
    plt.close()



def save_paired_lines(participant_summary: pd.DataFrame, metric: str, title: str, ylabel: str, out_path: Path) -> None:
    pivot = participant_summary.pivot(index="participant_id", columns="actuator_type", values=metric)
    pivot = pivot.reindex(columns=ACTUATOR_ORDER)

    plt.figure(figsize=(6, 4))
    x = np.arange(len(ACTUATOR_ORDER))
    for participant_id, row in pivot.iterrows():
        plt.plot(x, row.values, marker="o", label=str(participant_id))
    plt.xticks(x, ACTUATOR_ORDER)
    plt.title(title)
    plt.ylabel(ylabel)
    plt.tight_layout()
    plt.savefig(out_path, dpi=300)
    plt.close()



def save_subjective_grouped_bars(subjective_block: pd.DataFrame, out_path: Path) -> None:
    items = SUBJECTIVE_ITEMS
    x = np.arange(len(items))
    width = 0.35

    lra = subjective_block[subjective_block["actuator_type"] == "LRA"]
    erm = subjective_block[subjective_block["actuator_type"] == "ERM"]

    lra_means = [float(lra[f"mean_{item}"].iloc[0]) if not lra.empty else np.nan for item in items]
    erm_means = [float(erm[f"mean_{item}"].iloc[0]) if not erm.empty else np.nan for item in items]

    plt.figure(figsize=(8, 4.5))
    plt.bar(x - width / 2, lra_means, width, label="LRA")
    plt.bar(x + width / 2, erm_means, width, label="ERM")
    plt.xticks(x, [item.title() for item in items])
    plt.ylabel("Mean Likert rating")
    plt.ylim(0, 5.5)
    plt.title("Subjective ratings by actuator")
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_path, dpi=300)
    plt.close()



def save_preference_plot(preference: pd.DataFrame, out_path: Path) -> None:
    if preference.empty:
        return
    plt.figure(figsize=(5, 4))
    plt.bar(preference["preferred_actuator"], preference["count"])
    plt.ylabel("Participants")
    plt.title("Preferred actuator")
    plt.tight_layout()
    plt.savefig(out_path, dpi=300)
    plt.close()


# ==========================================
# Report writing
# ==========================================


def format_mean_sd(mean_value: float, sd_value: float, decimals: int = 2) -> str:
    if pd.isna(mean_value):
        return "NA"
    if pd.isna(sd_value):
        return f"{mean_value:.{decimals}f}"
    return f"{mean_value:.{decimals}f} ± {sd_value:.{decimals}f}"



def choose_primary_p(row: pd.Series) -> Tuple[str, float]:
    if not pd.isna(row.get("wilcoxon_p", np.nan)):
        return "Wilcoxon signed-rank", float(row["wilcoxon_p"])
    if not pd.isna(row.get("sign_p", np.nan)):
        return "sign test", float(row["sign_p"])
    if not pd.isna(row.get("t_p", np.nan)):
        return "paired t-test", float(row["t_p"])
    return "test", float("nan")



def generate_markdown_report(
    out_path: Path,
    trials: pd.DataFrame,
    participant_summary: pd.DataFrame,
    block_summary: pd.DataFrame,
    subjective_participant: pd.DataFrame,
    subjective_block: pd.DataFrame,
    trial_cmp: pd.DataFrame,
    subjective_cmp: pd.DataFrame,
    preference: pd.DataFrame,
    final_feedback: pd.DataFrame,
    figures: List[str],
) -> None:
    n_participants = int(participant_summary["participant_id"].nunique()) if not participant_summary.empty else 0
    n_trials_total = int(len(trials))

    lra_block = block_summary[block_summary["actuator_type"] == "LRA"]
    erm_block = block_summary[block_summary["actuator_type"] == "ERM"]

    def block_val(df: pd.DataFrame, col: str) -> float:
        return float(df[col].iloc[0]) if not df.empty and col in df.columns else float("nan")

    rt_cmp = trial_cmp[trial_cmp["metric"] == "mean_rt_ms_valid"]
    acc_cmp = trial_cmp[trial_cmp["metric"] == "accuracy"]
    miss_cmp = trial_cmp[trial_cmp["metric"] == "miss_rate"]
    false_cmp = trial_cmp[trial_cmp["metric"] == "mean_false_starts"]
    subj_overall_cmp = subjective_cmp[subjective_cmp["metric"] == "overall_mean_rating"]

    lines: List[str] = []
    lines.append("# User Reaction Experiment Statistical Report")
    lines.append("")
    lines.append("## Overview")
    lines.append(
        f"This report summarizes the user reaction experiment comparing **LRA** and **ERM** vibrotactile actuators. "
        f"The analysis includes **{n_participants} participants** and **{n_trials_total} total trials** across both actuator conditions."
    )
    lines.append(
        "The script computes descriptive statistics, participant-level paired comparisons, subjective rating summaries, preference counts, and figure outputs suitable for inclusion in a thesis or paper."
    )
    lines.append("")
    lines.append("## Main quantitative findings")

    if not rt_cmp.empty:
        row = rt_cmp.iloc[0]
        test_name, p_value = choose_primary_p(row)
        lines.append(
            f"- **Reaction time (valid responses only):** LRA = {format_mean_sd(block_val(lra_block, 'mean_rt_ms_valid'), block_val(lra_block, 'sd_rt_ms_valid'))} ms, "
            f"ERM = {format_mean_sd(block_val(erm_block, 'mean_rt_ms_valid'), block_val(erm_block, 'sd_rt_ms_valid'))} ms. "
            f"The participant-level paired comparison used {test_name} and yielded {significance_text(p_value)}."
        )
    if not acc_cmp.empty:
        row = acc_cmp.iloc[0]
        test_name, p_value = choose_primary_p(row)
        lines.append(
            f"- **Accuracy:** LRA = {block_val(lra_block, 'mean_accuracy') * 100:.1f}% ± {block_val(lra_block, 'sd_accuracy') * 100:.1f}%, "
            f"ERM = {block_val(erm_block, 'mean_accuracy') * 100:.1f}% ± {block_val(erm_block, 'sd_accuracy') * 100:.1f}%. "
            f"The paired comparison yielded {significance_text(p_value)}."
        )
    if not miss_cmp.empty:
        row = miss_cmp.iloc[0]
        test_name, p_value = choose_primary_p(row)
        lines.append(
            f"- **Miss rate:** LRA = {block_val(lra_block, 'mean_miss_rate') * 100:.1f}% ± {block_val(lra_block, 'sd_miss_rate') * 100:.1f}%, "
            f"ERM = {block_val(erm_block, 'mean_miss_rate') * 100:.1f}% ± {block_val(erm_block, 'sd_miss_rate') * 100:.1f}%. "
            f"The paired comparison yielded {significance_text(p_value)}."
        )
    if not false_cmp.empty:
        row = false_cmp.iloc[0]
        test_name, p_value = choose_primary_p(row)
        lines.append(
            f"- **False starts per trial:** LRA = {format_mean_sd(block_val(lra_block, 'mean_false_starts'), block_val(lra_block, 'sd_false_starts'))}, "
            f"ERM = {format_mean_sd(block_val(erm_block, 'mean_false_starts'), block_val(erm_block, 'sd_false_starts'))}. "
            f"The paired comparison yielded {significance_text(p_value)}."
        )

    lines.append("")
    lines.append("## Subjective ratings")
    if not subjective_block.empty:
        for item in SUBJECTIVE_ITEMS + ["overall_mean_rating"]:
            label = item.replace("_", " ").title()
            lra_mean = block_val(subjective_block[subjective_block["actuator_type"] == "LRA"], f"mean_{item}")
            erm_mean = block_val(subjective_block[subjective_block["actuator_type"] == "ERM"], f"mean_{item}")
            cmp_row = subjective_cmp[subjective_cmp["metric"] == item]
            if cmp_row.empty:
                lines.append(f"- **{label}:** LRA = {lra_mean:.2f}, ERM = {erm_mean:.2f}.")
            else:
                test_name, p_value = choose_primary_p(cmp_row.iloc[0])
                lines.append(
                    f"- **{label}:** LRA = {lra_mean:.2f}, ERM = {erm_mean:.2f}; paired comparison ({test_name}) {significance_text(p_value)}."
                )
    else:
        lines.append("No subjective rating records were found in the database.")

    lines.append("")
    lines.append("## Participant preference")
    if not preference.empty:
        total_pref = int(preference["count"].sum())
        for _, row in preference.iterrows():
            low, high = proportion_ci_wilson(int(row["count"]), total_pref)
            lines.append(
                f"- **{row['preferred_actuator']}** was preferred by {int(row['count'])}/{total_pref} participants "
                f"({row['proportion'] * 100:.1f}%, 95% CI [{low * 100:.1f}%, {high * 100:.1f}%])."
            )
    else:
        lines.append("No final preference records were found in the database.")

    if not final_feedback.empty and "reason" in final_feedback.columns:
        non_empty_reasons = final_feedback[final_feedback["reason"].fillna("").str.strip() != ""]
        if not non_empty_reasons.empty:
            lines.append("")
            lines.append("### Qualitative preference notes")
            for _, row in non_empty_reasons.iterrows():
                lines.append(f"- Participant {row['participant_id']}: preferred **{row['preferred_actuator']}** because {row['reason']}")

    lines.append("")
    lines.append("## Suggested Results-section wording")
    lines.append(
        "In the user reaction experiment, five participants completed single-finger response blocks for both the LRA and ERM actuator conditions. "
        "For each participant, block-level averages were computed and compared using paired tests. Because the sample size was small, non-parametric paired statistics "
        "(Wilcoxon signed-rank or sign test where necessary) are the most appropriate primary inferential results, while the paired t-test and Cohen's dz are included as supplementary effect-size-oriented references in the exported tables."
    )
    lines.append(
        "The descriptive summaries and plots can be used directly in the thesis to support claims about relative speed, reliability, perceived clarity, comfort, responsiveness, and overall user preference between the two actuator types."
    )

    if figures:
        lines.append("")
        lines.append("## Generated figures")
        for fig in figures:
            lines.append(f"- `{fig}`")

    out_path.write_text("\n".join(lines), encoding="utf-8")


# ==========================================
# Main entry
# ==========================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze reaction_experiment.db and generate a report with figures.")
    parser.add_argument("--db", default="reaction_experiment.db", help="Path to the SQLite database file.")
    parser.add_argument("--outdir", default="analysis_output", help="Directory for exported results.")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    figures_dir = outdir / "figures"
    outdir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)

    data = load_experiment_data(args.db)
    trials = prepare_trials(data["trials"])
    subjective = data["subjective_ratings"]
    final_feedback = data["final_feedback"]

    participant_summary = participant_trial_summary(trials)
    block_summary = block_level_summary(participant_summary)
    subjective_participant, subjective_block = subjective_summary(subjective) if not subjective.empty else (pd.DataFrame(), pd.DataFrame())
    preference = preference_summary(final_feedback)
    trial_cmp, subjective_cmp = paired_comparison_tables(participant_summary, subjective_participant) if not participant_summary.empty else (pd.DataFrame(), pd.DataFrame())

    # Export CSV tables
    data["participants"].to_csv(outdir / "participants.csv", index=False)
    trials.to_csv(outdir / "trials_clean.csv", index=False)
    participant_summary.to_csv(outdir / "participant_trial_summary.csv", index=False)
    block_summary.to_csv(outdir / "block_summary.csv", index=False)
    subjective.to_csv(outdir / "subjective_ratings_raw.csv", index=False)
    subjective_participant.to_csv(outdir / "subjective_participant_summary.csv", index=False)
    subjective_block.to_csv(outdir / "subjective_block_summary.csv", index=False)
    final_feedback.to_csv(outdir / "final_feedback.csv", index=False)
    preference.to_csv(outdir / "preference_summary.csv", index=False)
    trial_cmp.to_csv(outdir / "paired_trial_comparisons.csv", index=False)
    subjective_cmp.to_csv(outdir / "paired_subjective_comparisons.csv", index=False)

    figure_files: List[str] = []

    if not block_summary.empty:
        p = figures_dir / "mean_reaction_time_valid.png"
        save_barplot(block_summary, "actuator_type", "mean_rt_ms_valid", "sd_rt_ms_valid", "Mean reaction time (valid only)", "Reaction time (ms)", p)
        figure_files.append(str(p.relative_to(outdir)))

        p = figures_dir / "mean_accuracy.png"
        save_barplot(block_summary, "actuator_type", "mean_accuracy", "sd_accuracy", "Mean accuracy", "Accuracy", p)
        figure_files.append(str(p.relative_to(outdir)))

        p = figures_dir / "mean_miss_rate.png"
        save_barplot(block_summary, "actuator_type", "mean_miss_rate", "sd_miss_rate", "Mean miss rate", "Miss rate", p)
        figure_files.append(str(p.relative_to(outdir)))

    if not participant_summary.empty:
        p = figures_dir / "paired_reaction_time_by_participant.png"
        save_paired_lines(participant_summary, "mean_rt_ms_valid", "Participant-level mean reaction time", "Reaction time (ms)", p)
        figure_files.append(str(p.relative_to(outdir)))

        p = figures_dir / "paired_accuracy_by_participant.png"
        save_paired_lines(participant_summary, "accuracy", "Participant-level accuracy", "Accuracy", p)
        figure_files.append(str(p.relative_to(outdir)))

    if not subjective_block.empty:
        p = figures_dir / "subjective_ratings.png"
        save_subjective_grouped_bars(subjective_block, p)
        figure_files.append(str(p.relative_to(outdir)))

    if not preference.empty:
        p = figures_dir / "preferred_actuator.png"
        save_preference_plot(preference, p)
        figure_files.append(str(p.relative_to(outdir)))

    report_path = outdir / "analysis_report.md"
    generate_markdown_report(
        out_path=report_path,
        trials=trials,
        participant_summary=participant_summary,
        block_summary=block_summary,
        subjective_participant=subjective_participant,
        subjective_block=subjective_block,
        trial_cmp=trial_cmp,
        subjective_cmp=subjective_cmp,
        preference=preference,
        final_feedback=final_feedback,
        figures=figure_files,
    )

    print(f"Analysis complete. Results saved to: {outdir.resolve()}")
    print(f"Report: {report_path.resolve()}")


if __name__ == "__main__":
    main()
