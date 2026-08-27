"""Reaction-time decomposition on top of the effector-selection model.

app.effector_model fits WHICH finger acted.  This module asks what the
same fitted quantities say about HOW LONG the action took, and it is kept
separate because it answers a different question and reaches a weaker
conclusion - the choice model explains the error structure outright,
while only part of the reaction time follows from it.

The decomposition
-----------------
    log RT = participant
           + beta  * conflict            selection: overriding the habit
           + tau_c                       cue transformation, per condition
           + eps_d                       execution, per cued digit
           + eta1 * key travel + eta2 * hand switch
           + error

`conflict` is -log P_prior(cued finger), taken straight from the choice
fit (app.effector_model.attach_predictions), so the two layers share
parameters rather than being two unrelated regressions.  It is
structurally zero in Condition A, where no finger is cued and there is no
habit to override - which is also why Condition A can sit in the same
model as B and C instead of being dropped.

Why not entropy
---------------
The obvious latent quantity is the entropy of the selection distribution,
and on this data it has the WRONG SIGN: mean entropy is highest in
Condition A (1.25 nats) and Condition A is the fastest condition, so
entropy alone predicts that key-only trials should be the slowest.  The
reason is that entropy conflates two situations a softmax cannot tell
apart - many options are acceptable (free choice: fast) and the evidence
is fighting the prior (cued choice: slow).  Conflict separates them,
because it is measured against the CUED finger and is undefined-hence-zero
when nothing is cued.  entropy_check() recomputes that comparison from
whatever data is loaded rather than asking anyone to trust this
paragraph.

What this does not establish
----------------------------
tau_B and tau_C are estimated, not derived.  With two cue modalities and
one coefficient each they are saturated: they measure the modality cost
but do not explain it, and calling them a mechanism would be renaming the
condition dummy.  Two things are worth knowing about them and both are
computed here: adding `conflict` leaves the B - C gap essentially
unchanged (so the reaction-time difference between the modalities is not
habit override), and the Condition x Digit interaction survives it too
(so the modality cost is itself digit-dependent, and the clean "selection
is cue-dependent, execution is not" split holds only for the habit term).

The accumulator prediction that would have derived tau from the choice
parameters - weaker cue evidence should cost more time, so RT extra
proportional to 1/kappa - is testable from the existing data at the
participant level, and evidence_time_link() tests it.  It does not hold,
which is why no drift-diffusion or race model is fitted anywhere in this
analysis.
"""

from typing import Sequence

import numpy as np
import pandas as pd

from .effector_model import CUED_CONDITIONS, DIGIT_NAMES

# Reaction times below this are the carry-over threshold app.quiz already
# flags; confirmed ones are gone before the model sees an event, and the
# rest are kept exactly as the other analyses keep them.  Stated here so
# nothing in this module silently re-filters what the export decided.
_MIN_MODELLED_RT_S = 0.0


def _design(scored: pd.DataFrame, terms: Sequence[str]) -> pd.DataFrame:
    df = scored.copy()
    df["log_rt"] = np.log(df["rt_s"].astype(float))
    df["digit"] = df["target_digit"].astype(int).astype(str)
    df["hand_switch"] = df["hand_switch"].astype(float)
    df["key_travel"] = df["key_travel"].astype(float)
    keep = ["log_rt", "rt_s", "participant", "condition", "digit",
            "hand_switch", "key_travel", "conflict", "entropy", "margin",
            "target_digit", "correct_finger"]
    return df[[c for c in keep if c in df.columns] + [t for t in terms if t not in keep]]


def _fit_ols(formula: str, data: pd.DataFrame):
    """OLS with participant-clustered standard errors.

    Clustered because events within a participant share that person's
    baseline speed and their fitted prior; ordinary errors would treat
    16,000 events as 16,000 independent observations, which is the exact
    mistake the rest of this study's inference avoids.
    """
    import statsmodels.formula.api as smf

    model = smf.ols(formula, data=data)
    return model.fit(cov_type="cluster", cov_kwds={"groups": data["participant"]})


def _coefficient_table(result, drop_prefixes=("Intercept", "C(participant)")) -> pd.DataFrame:
    rows = []
    conf = result.conf_int()
    for name in result.params.index:
        if any(name.startswith(p) for p in drop_prefixes):
            continue
        rows.append({"term": name, "estimate": float(result.params[name]),
                     "se": float(result.bse[name]), "t": float(result.tvalues[name]),
                     "p": float(result.pvalues[name]),
                     "ci95_lo": float(conf.loc[name, 0]), "ci95_hi": float(conf.loc[name, 1])})
    return pd.DataFrame(rows)


BASE_TERMS = "C(participant) + C(condition) + C(digit) + hand_switch + key_travel"


def decomposition(scored: pd.DataFrame) -> pd.DataFrame:
    """The three-component model's coefficients, on log reaction time.

    `scored` is the output of app.effector_model.attach_predictions, so
    `conflict` is the choice model's own quantity and not recomputed here.
    """
    data = _design(scored, [])
    result = _fit_ols(f"log_rt ~ {BASE_TERMS} + conflict", data)
    table = _coefficient_table(result)
    table["component"] = table["term"].map(_component_of)
    table["interpretation"] = table["term"].map(_interpretation_of)
    table.attrs["r_squared"] = float(result.rsquared)
    table.attrs["n"] = int(result.nobs)
    return table


def _component_of(term: str) -> str:
    if term.startswith("conflict"):
        return "selection (habit override)"
    if term.startswith("C(condition)"):
        return "cue transformation (measured, not derived)"
    if term.startswith("C(digit)"):
        return "execution (per cued digit)"
    return "movement"


def _interpretation_of(term: str) -> str:
    if term == "conflict":
        return "log-RT cost per nat of habitual-prior surprisal on the cued finger"
    if term.startswith("C(condition)"):
        return f"log-RT relative to Condition A (key-only), condition {term[-2]}"
    if term.startswith("C(digit)"):
        return f"log-RT relative to the thumb, cued digit {term[-2]} ({DIGIT_NAMES.get(int(term[-2]), '')})"
    if term == "hand_switch":
        return "log-RT cost of the cued hand differing from the previous event's"
    if term == "key_travel":
        return "log-RT cost per key of travel from the previous keypress"
    return ""


def component_comparison(scored: pd.DataFrame) -> pd.DataFrame:
    """Nested models showing exactly what `conflict` does and does not
    absorb - the honest form of "the model explains reaction time".

    Each row is the same regression with one term added, so the movement
    of the Condition coefficients between rows is the quantity of
    interest: conflict takes a real bite out of the A -> B/C rise and
    almost none out of the B - C gap.
    """
    data = _design(scored, [])
    specs = [
        ("condition + digit only", f"log_rt ~ {BASE_TERMS}"),
        ("+ selection conflict", f"log_rt ~ {BASE_TERMS} + conflict"),
        ("+ entropy instead of conflict", f"log_rt ~ {BASE_TERMS} + entropy"),
    ]
    rows = []
    for label, formula in specs:
        result = _fit_ols(formula, data)
        row = {"model": label, "r_squared": float(result.rsquared), "n": int(result.nobs)}
        for name in ("C(condition)[T.B]", "C(condition)[T.C]", "conflict", "entropy"):
            if name in result.params.index:
                row[name] = float(result.params[name])
        cued = data[data["condition"].isin(CUED_CONDITIONS)]
        gap = _fit_ols(formula.replace("C(condition)", "C(condition)"), cued)
        if "C(condition)[T.C]" in gap.params.index:
            row["B_minus_C_gap_log"] = -float(gap.params["C(condition)[T.C]"])
        rows.append(row)
    return pd.DataFrame(rows)


def selection_vs_execution(scored: pd.DataFrame) -> pd.DataFrame:
    """Per cued digit: the raw reaction-time profile, and the profile that
    survives removing the habit-override term.

    The difference between the two columns is the part of a digit's
    apparent slowness that was really the cost of being an unusual choice
    rather than a hard movement.  The index finger is the clearest case
    and the little finger the clearest counter-case; both are read off
    this table rather than asserted.
    """
    data = _design(scored, [])
    without = _fit_ols(f"log_rt ~ {BASE_TERMS}", data)
    with_conflict = _fit_ols(f"log_rt ~ {BASE_TERMS} + conflict", data)

    # The same thing in milliseconds, which is the unit the finding should
    # be read in. `adjusted_rt_ms` is the model's fitted reaction time
    # with the habit-override term held at its GRAND MEAN for every event
    # - so every digit is scored as though it had been an equally usual
    # choice, and what is left is the cost of making the movement. Duan's
    # smearing factor converts the log-scale fit back to an arithmetic
    # mean rather than a geometric one.
    smearing = float(np.mean(np.exp(with_conflict.resid)))
    levelled = data.assign(conflict=data["conflict"].mean())
    adjusted_ms = np.exp(with_conflict.predict(levelled)) * smearing * 1000.0
    fitted_ms = np.exp(with_conflict.fittedvalues) * smearing * 1000.0

    rows = []
    for digit in (1, 2, 3, 4, 5):
        term = f"C(digit)[T.{digit}]"
        raw = float(without.params.get(term, 0.0))
        adjusted = float(with_conflict.params.get(term, 0.0))
        mask = (data["target_digit"] == digit).to_numpy()
        subset = data[mask]
        rows.append({
            "digit": digit, "digit_name": DIGIT_NAMES[digit], "n": len(subset),
            "mean_rt_ms": float(subset["rt_s"].mean() * 1000.0),
            "fitted_rt_ms": float(fitted_ms[mask].mean()),
            "adjusted_rt_ms": float(adjusted_ms[mask].mean()),
            "selection_cost_ms": float(fitted_ms[mask].mean() - adjusted_ms[mask].mean()),
            "mean_conflict": float(subset["conflict"].mean()),
            "log_rt_vs_thumb_raw": raw,
            "log_rt_vs_thumb_after_selection": adjusted,
            "selection_share": raw - adjusted,
        })
    frame = pd.DataFrame(rows)
    thumb = frame.loc[frame["digit"] == 1]
    if len(thumb):
        frame["vs_thumb_raw_ms"] = frame["fitted_rt_ms"] - float(thumb["fitted_rt_ms"].iloc[0])
        frame["vs_thumb_adjusted_ms"] = (frame["adjusted_rt_ms"]
                                         - float(thumb["adjusted_rt_ms"].iloc[0]))
    return frame


def condition_by_digit(scored: pd.DataFrame) -> pd.DataFrame:
    """Whether the modality cost is the same for every digit, before and
    after the selection term.

    If the cue transformation were a fixed per-event overhead, these
    interaction coefficients would be zero.  They are not, and they barely
    move when conflict enters, which is the finding that stops tau being
    describable as a pure non-decision constant.
    """
    data = _design(scored, [])
    cued = data[data["condition"].isin(CUED_CONDITIONS)]
    rows = []
    for label, formula in (
        ("without selection term",
         "log_rt ~ C(participant) + C(condition)*C(digit) + hand_switch + key_travel"),
        ("with selection term",
         "log_rt ~ C(participant) + C(condition)*C(digit) + hand_switch + key_travel + conflict"),
    ):
        result = _fit_ols(formula, cued)
        for name in result.params.index:
            if ":" not in name:
                continue
            rows.append({"model": label, "term": name,
                         "estimate": float(result.params[name]),
                         "se": float(result.bse[name]),
                         "t": float(result.tvalues[name]),
                         "p": float(result.pvalues[name])})
    return pd.DataFrame(rows)


def entropy_check(scored: pd.DataFrame) -> pd.DataFrame:
    """Mean latent quantities against mean reaction time, per condition.

    Included because it is the table that rules a candidate mechanism out:
    entropy runs opposite to reaction time across the three conditions,
    conflict runs with it.
    """
    out = (scored.groupby("condition")
           .agg(n=("rt_s", "size"), mean_rt_s=("rt_s", "mean"),
                mean_entropy=("entropy", "mean"), mean_conflict=("conflict", "mean"),
                mean_margin=("margin", "mean")).reset_index())
    return out


def evidence_time_link(per_participant_cue: pd.DataFrame, scored: pd.DataFrame,
                       parameter: str = "match_hand") -> pd.DataFrame:
    """Test the accumulator prediction that would have derived the
    modality cost from the choice parameters.

    If reaching a decision criterion takes time inversely proportional to
    the evidence rate, a participant whose fitted cue evidence is weak
    should pay a larger reaction-time cost over Condition A in that
    condition.  Correlating each participant's kappa against their own
    Condition-A-referenced cost tests exactly that, WITHIN condition, so
    the condition difference cannot manufacture the correlation (pooling B
    and C together does manufacture one, which is why the pooled value is
    reported alongside and labelled).
    """
    from scipy import stats as sstats

    means = (scored.groupby(["participant", "condition"])["rt_s"].mean().unstack())
    rows = []
    for condition in CUED_CONDITIONS:
        column = f"{parameter}[{condition}]"
        if column not in per_participant_cue.columns or condition not in means.columns:
            continue
        merged = per_participant_cue[["participant", column]].merge(
            means.reset_index()[["participant", "A", condition]], on="participant")
        cost = merged[condition] - merged["A"]
        kappa = merged[column].to_numpy(float)
        if len(merged) < 3:
            continue
        # A correlation is undefined when either side has no variance,
        # which happens when every participant's parameter sat at the
        # bound. Report that as the reason rather than as a NaN nobody
        # can interpret.
        if np.std(kappa) == 0 or np.std(cost) == 0:
            rows.append({"condition": condition, "parameter": column,
                         "n_participants": len(merged),
                         "pearson_r": np.nan, "p": np.nan,
                         "spearman_rho": np.nan, "spearman_p": np.nan,
                         "prediction": "not testable: no variation between "
                                       "participants in this parameter"})
            continue
        r, p = sstats.pearsonr(kappa, cost)
        rho, rho_p = sstats.spearmanr(kappa, cost)
        rows.append({"condition": condition, "parameter": column,
                     "n_participants": len(merged),
                     "pearson_r": float(r), "p": float(p),
                     "spearman_rho": float(rho), "spearman_p": float(rho_p),
                     "prediction": "negative r if RT cost is set by evidence rate"})
    return pd.DataFrame(rows)


def observed_vs_predicted_rt(scored: pd.DataFrame) -> pd.DataFrame:
    """Participant x condition observed and fitted mean reaction time,
    with the participant as the unit of the calibration plot.

    The model is fitted on log RT, so exp(fitted) is a geometric mean and
    sits systematically below the arithmetic mean it would be plotted
    against - about 4% here, which would read as the model
    under-predicting every cell.  Duan's smearing factor, the mean of
    exp(residual), is the standard retransformation correction and puts
    both axes on the same scale.  The factor is returned so a caption can
    state it rather than leaving a silent adjustment in the figure.
    """
    data = _design(scored, [])
    result = _fit_ols(f"log_rt ~ {BASE_TERMS} + conflict", data)
    smearing = float(np.mean(np.exp(result.resid)))
    data = data.assign(predicted_rt_s=np.exp(result.fittedvalues) * smearing)
    out = (data.groupby(["participant", "condition"])
           .agg(n=("rt_s", "size"), observed_rt_s=("rt_s", "mean"),
                predicted_rt_s=("predicted_rt_s", "mean")).reset_index())
    out.attrs["smearing_factor"] = smearing
    return out


def residual_diagnostics(scored: pd.DataFrame) -> pd.DataFrame:
    """Residual centre and spread per condition and per digit - the check
    that the fit is not carrying a systematic miss in one cell."""
    data = _design(scored, [])
    result = _fit_ols(f"log_rt ~ {BASE_TERMS} + conflict", data)
    data = data.assign(residual=result.resid)
    by_condition = (data.groupby("condition")["residual"]
                    .agg(n="size", mean="mean", sd="std").reset_index()
                    .rename(columns={"condition": "cell"}).assign(grouping="condition"))
    by_digit = (data.groupby("digit")["residual"]
                .agg(n="size", mean="mean", sd="std").reset_index()
                .rename(columns={"digit": "cell"}).assign(grouping="cued digit"))
    return pd.concat([by_condition, by_digit], ignore_index=True)
