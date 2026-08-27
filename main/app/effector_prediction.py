"""Out-of-sample prediction for the effector-selection model (GUI-free).

Everything here answers one question: with a participant, a trial or an
event REMOVED from the training data, can a model fitted on the rest say
which finger will act, whether the hand will be wrong, and how long the
response will take?

Nothing in this module reports an in-sample fitted value as a prediction.
Every number is produced by a model that never saw the row it is scoring.
That is enforced structurally rather than by convention: predictions only
ever come out of _fold(), which is handed a disjoint (train, test) pair,
and the returned frame carries the fold_id that produced each row, so a
prediction can always be traced back to the fit that had no access to it.

Two prediction tasks, deliberately separated
--------------------------------------------
POPULATION - a participant nobody has seen.  Only the group-level prior
and the event's own observable features are available; the held-out
person contributes nothing at all.

PERSONALISED - the same held-out participant, except that their own
Condition A trials may be used as calibration.  Condition A carries no
finger cue, so using it to characterise someone's habitual fingering
before predicting their cued trials leaks nothing about the events being
predicted.  Their B and C events are never touched.

The personalised variant is NOT assumed to be better.  Phase-2 work
already found that a participant's own Condition-A prior did not improve
cross-participant prediction, so this is set up as a formal comparison
with a real possibility of a null, and population_vs_personalised()
reports whichever way it falls.

Splits
------
LOPO is primary: one participant out, model fitted on the other
nineteen, every one of their events predicted, twenty folds concatenated.
Trial-level K-fold within participants is secondary and splits on WHOLE
TRIALS - a random event split would put events from the same trial,
sharing a sequence, a hand position and a moment in the session, on both
sides of the split and report a generalisation that is really memory.

What the metrics are chosen for
-------------------------------
Top-1 accuracy saturates: once any cue term is in the model the cued
finger is usually chosen, and every cue model scores about the same.  Log
loss is the metric that separates them, because it scores the whole
ten-way distribution including where the probability mass for the errors
went.  Both are reported, with the saturation stated, so the comparison
is not read off the column that cannot make it.

Wrong-hand events are rare - 67 under B and 2 under C out of 5,400 each -
so accuracy on that class is meaningless (always-predict-correct scores
99%).  Precision, recall, average precision and a decile calibration
table are reported instead, and any class with too few positives is
labelled exploratory by rare_event_warning() rather than being given a
confident-looking number.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from . import effector_model as em
from .effector_model import (
    CONDITIONS,
    CUED_CONDITIONS,
    DIGIT,
    DIGIT_NAMES,
    FINGER_INDEX,
    FINGERS,
    IS_RIGHT,
    N_FINGERS,
    ModelSpec,
)

# Below this many positive cases a rare-event metric is reported but
# labelled exploratory.  Twenty is not a magic number; it is the point
# below which a precision/recall pair moves by more than a tenth when one
# case changes, which is the property that makes a figure misleading.
MIN_POSITIVES_FOR_INFERENCE = 20

# Folds in the secondary within-participant split. Named so the GUI can
# size its progress bar from the same number the split actually uses.
DEFAULT_TRIAL_FOLDS = 5

POPULATION = "population"
PERSONALISED = "personalised"
WITHIN_PARTICIPANT = "within-participant"

# The four mutually exclusive outcome classes the error-risk task
# predicts, defined on the CUED finger, so every event has exactly one.
OUTCOME_CORRECT = "correct finger"
OUTCOME_WITHIN_HAND = "within-hand wrong finger"
OUTCOME_HOMOLOGOUS = "wrong hand, homologous digit"
OUTCOME_CROSS_OTHER = "wrong hand, other digit"
OUTCOME_CLASSES = [OUTCOME_CORRECT, OUTCOME_WITHIN_HAND,
                   OUTCOME_HOMOLOGOUS, OUTCOME_CROSS_OTHER]


# ---------------------------------------------------------------------------
# Baselines
#
# A complex model earns its parameters only against something simpler on
# the SAME held-out data.  These are the alternatives it has to beat.


class _Baseline:
    """A predictor with the same fit/predict shape as the choice model."""

    name = "baseline"

    def fit(self, train: pd.DataFrame) -> "_Baseline":
        raise NotImplementedError

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError


class EmpiricalFingerFrequency(_Baseline):
    """P(finger) = how often that finger was used in training, full stop.

    The floor: it ignores the key, the cue and the condition, so it says
    what "no model at all, just the base rates" is worth.
    """

    name = "empirical finger frequency"

    def fit(self, train):
        counts = np.ones(N_FINGERS)  # Laplace, so a held-out finger is never p=0
        for finger in train["actual_finger"]:
            counts[FINGER_INDEX[finger]] += 1
        self.probs = counts / counts.sum()
        return self

    def predict(self, test):
        return np.tile(self.probs, (len(test), 1))


class EmpiricalByKeyAndCue(_Baseline):
    """P(finger | pressed key, cued finger, condition) as a lookup table.

    The strong non-parametric baseline: it has access to exactly the
    information the model has, but can only memorise cells rather than
    share structure across them.  Beating it is what "the model has found
    structure" means.
    """

    name = "empirical by key x cue x condition"

    def fit(self, train):
        self.table: Dict[tuple, np.ndarray] = {}
        self.backoff = np.ones(N_FINGERS)
        for row in train.itertuples():
            self.backoff[FINGER_INDEX[row.actual_finger]] += 1
            key = (row.condition, int(row.key), row.target_finger)
            cell = self.table.setdefault(key, np.ones(N_FINGERS) * 0.5)
            cell[FINGER_INDEX[row.actual_finger]] += 1
        self.backoff = self.backoff / self.backoff.sum()
        return self

    def predict(self, test):
        out = np.empty((len(test), N_FINGERS))
        for i, row in enumerate(test.itertuples()):
            cell = self.table.get((row.condition, int(row.key), row.target_finger))
            probs = cell / cell.sum() if cell is not None else self.backoff
            out[i] = probs
        return out


class MultinomialLogistic(_Baseline):
    """Flat multinomial logistic regression on the same observables.

    A standard off-the-shelf classifier, included because "would a
    generic model have done as well?" is the first question a reader
    asks.  It sees one-hot key, cued hand, cued digit, condition and
    previous finger - the same information - but as a flat feature
    vector, with no candidate structure and no notion that a finger is a
    (hand, digit) pair.
    """

    name = "multinomial logistic"

    def _design(self, df: pd.DataFrame) -> np.ndarray:
        key = np.clip(df["key"].to_numpy(int), 0, self.n_keys - 1)
        blocks = [np.eye(self.n_keys)[key]]
        blocks.append(np.eye(len(CONDITIONS))[[CONDITIONS.index(c) for c in df["condition"]]])
        target = np.array([FINGER_INDEX[f] for f in df["target_finger"]])
        blocks.append(np.eye(N_FINGERS)[target])
        prev = np.array([FINGER_INDEX[f] if isinstance(f, str) else N_FINGERS
                         for f in df["prev_finger"]])
        blocks.append(np.eye(N_FINGERS + 1)[prev])
        # Cue information is only present in B and C; interact it with the
        # condition so the model is not told the cue in Condition A.
        cued = np.eye(N_FINGERS)[target]
        for condition in CUED_CONDITIONS:
            blocks.append(cued * (df["condition"].to_numpy() == condition)[:, None])
        return np.hstack(blocks)

    def fit(self, train):
        from sklearn.linear_model import LogisticRegression

        self.n_keys = int(train["key"].max()) + 1
        X = self._design(train)
        y = np.array([FINGER_INDEX[f] for f in train["actual_finger"]])
        self.classes = np.unique(y)
        self.model = LogisticRegression(max_iter=2000, C=1.0)
        self.model.fit(X, y)
        return self

    def predict(self, test):
        probs = self.model.predict_proba(self._design(test))
        out = np.full((len(test), N_FINGERS), 1e-6)
        out[:, self.classes] = probs
        return out / out.sum(axis=1, keepdims=True)


class ChoiceModelPredictor(_Baseline):
    """The effector-selection model itself, wrapped so it competes with
    the baselines through the identical interface."""

    def __init__(self, spec: ModelSpec, personalised: bool = False,
                 seen_participants: bool = False):
        self.spec = spec
        self.personalised = personalised
        # True only for the within-participant split, where the people
        # being predicted ARE in the training set and their fitted prior
        # deviation is legitimately theirs.  Under leave-one-participant-out
        # it must stay False: a held-out person has no deviation, and using
        # one would be predicting them from themselves.
        self.seen_participants = seen_participants
        self.name = spec.name + (" (personalised)" if personalised else "")

    def fit(self, train, participants=None, n_keys=None):
        self.participants = participants or sorted(train["participant"].unique())
        self.n_keys = n_keys or int(train["key"].max()) + 1
        self.fit_result = em.fit_choice_model(train, self.spec,
                                              participants=self.participants,
                                              n_keys=self.n_keys)
        return self

    def calibrate(self, calibration: pd.DataFrame, participant: str) -> None:
        """Estimate one unseen participant's prior deviation from their own
        Condition A events, leaving every other parameter frozen.

        This is the whole of what "personalised" is allowed to mean: the
        cued trials being predicted contribute nothing, and neither does
        any other participant's cued behaviour beyond the group fit.
        """
        fit = self.fit_result
        extended = list(fit.participants) + [participant]
        params = em._extend_participants(fit, extended)
        problem = em._Problem(calibration, self.spec, extended, self.n_keys)
        dev_slice = problem.layout.slices.get("prior_dev")
        if dev_slice is None or calibration.empty:
            self._calibrated = (extended, params)
            return
        offset = dev_slice.start + (len(extended) - 1) * N_FINGERS

        def objective(theta):
            w = params.copy()
            w[offset:offset + N_FINGERS] = theta
            value, grad = problem.objective(w)
            return value, grad[offset:offset + N_FINGERS]

        deviation = minimize(objective, np.zeros(N_FINGERS), jac=True,
                             method="L-BFGS-B", options={"maxiter": 1000}).x
        params[offset:offset + N_FINGERS] = deviation
        self._calibrated = (extended, params)

    def predict(self, test):
        if self.personalised and getattr(self, "_calibrated", None) is not None:
            participants, params = self._calibrated
            problem = em._Problem(test, self.spec, participants, self.n_keys)
            return problem.probabilities(params)
        probs, _ = em.predict(self.fit_result, test,
                              use_participant_prior=self.seen_participants)
        return probs

    def prior_probabilities(self, test) -> np.ndarray:
        if self.personalised and getattr(self, "_calibrated", None) is not None:
            participants, params = self._calibrated
            problem = em._Problem(test, self.spec, participants, self.n_keys)
            return problem.prior_probabilities(params)
        _, prior = em.predict(self.fit_result, test,
                              use_participant_prior=self.seen_participants)
        return prior


def default_predictors() -> List[Tuple[str, Callable[[], _Baseline]]]:
    """The comparison set, simplest first.

    M3 is the primary model and M4 the secondary one; the four entries
    above them are what they have to beat on held-out data for their
    parameters to be worth anything.
    """
    return [
        ("empirical finger frequency", EmpiricalFingerFrequency),
        ("empirical by key x cue x condition", EmpiricalByKeyAndCue),
        ("multinomial logistic", MultinomialLogistic),
        ("M3 without habitual prior",
         lambda: ChoiceModelPredictor(ModelSpec(name="M3 without habitual prior",
                                                prior=False, prior_by_participant=False,
                                                prior_weight=False))),
        ("M1 unitary cue (no channel split)",
         lambda: ChoiceModelPredictor(ModelSpec(name="M1 unitary cue",
                                                cue=("match_finger",)))),
        ("M3 (primary)", lambda: ChoiceModelPredictor(ModelSpec(name="M3"))),
        ("M4 per-digit evidence (secondary)",
         lambda: ChoiceModelPredictor(ModelSpec(name="M4", cue_by_digit=True))),
    ]


# ---------------------------------------------------------------------------
# The fold machinery
#
# One function produces every prediction in this module, so there is
# exactly one place where a train/test split is made and exactly one
# place where "the model has not seen this row" can be got wrong.


def _outcome_class(target_finger: str, actual_finger: str) -> str:
    if actual_finger == target_finger:
        return OUTCOME_CORRECT
    same_hand = target_finger[0] == actual_finger[0]
    if same_hand:
        return OUTCOME_WITHIN_HAND
    return (OUTCOME_HOMOLOGOUS if target_finger[1] == actual_finger[1]
            else OUTCOME_CROSS_OTHER)


def _class_probabilities(probs: np.ndarray, cued: np.ndarray) -> Dict[str, np.ndarray]:
    """Fold the ten-way distribution into the four outcome classes.

    These are predictions in the strict sense - the model assigns mass to
    ten fingers and the classes are read off that mass, so nothing about
    "wrong hand" or "homologous" is fitted."""
    same_hand = IS_RIGHT[None, :] == IS_RIGHT[cued][:, None]
    same_digit = DIGIT[None, :] == DIGIT[cued][:, None]
    is_cued = np.arange(N_FINGERS)[None, :] == cued[:, None]
    return {
        OUTCOME_CORRECT: probs[np.arange(len(cued)), cued],
        OUTCOME_WITHIN_HAND: (probs * (same_hand & ~is_cued)).sum(axis=1),
        OUTCOME_HOMOLOGOUS: (probs * (~same_hand & same_digit)).sum(axis=1),
        OUTCOME_CROSS_OTHER: (probs * (~same_hand & ~same_digit)).sum(axis=1),
    }


def _rt_features(df: pd.DataFrame, conflict: np.ndarray) -> np.ndarray:
    """Design for the held-out RT model.

    No participant dummies: a participant who was not in the training set
    has no fitted intercept, and inventing one would be exactly the leak
    this module exists to avoid.  The personalised variant adds an
    intercept offset estimated from that person's Condition A trials,
    which is calibration data rather than a fitted effect.
    """
    blocks = [np.ones((len(df), 1)), conflict[:, None]]
    condition = df["condition"].to_numpy()
    for c in CUED_CONDITIONS:
        blocks.append((condition == c).astype(float)[:, None])
    digit = df["target_digit"].to_numpy(int)
    for d in (2, 3, 4, 5):
        blocks.append((digit == d).astype(float)[:, None])
    blocks.append(df["hand_switch"].to_numpy(float)[:, None])
    blocks.append(df["key_travel"].to_numpy(float)[:, None])
    return np.hstack(blocks)


def _fit_rt(train: pd.DataFrame, conflict: np.ndarray):
    X = _rt_features(train, conflict)
    y = np.log(train["rt_s"].to_numpy(float))
    coefficients, *_ = np.linalg.lstsq(X, y, rcond=None)
    residuals = y - X @ coefficients
    # Duan's smearing factor, so a log-scale fit predicts an arithmetic
    # mean rather than a geometric one.
    return coefficients, float(np.mean(np.exp(residuals)))


def _fold(train: pd.DataFrame, test: pd.DataFrame, predictor: _Baseline,
          fold_id: str, variant: str, model_name: str,
          calibration: Optional[pd.DataFrame] = None,
          participant: Optional[str] = None) -> pd.DataFrame:
    """Fit on `train`, predict `test`, return one tidy row per test event.

    `calibration` is the held-out participant's own Condition A events for
    the personalised variant, and is never allowed to contain a cued
    event - the assertion below is the guard, not a comment.
    """
    def _keys(frame):
        return set(zip(frame["participant"], frame["trial_index"], frame["event_index"]))

    assert not (_keys(train) & _keys(test)), "train and test share events"
    if calibration is not None and not calibration.empty:
        assert (calibration["condition"] == "A").all(), \
            "personalised calibration may only use Condition A events"

    participants = sorted(train["participant"].unique())
    n_keys = int(max(train["key"].max(), test["key"].max())) + 1
    if isinstance(predictor, ChoiceModelPredictor):
        predictor.fit(train, participants=participants, n_keys=n_keys)
        if predictor.personalised and calibration is not None and participant is not None:
            predictor.calibrate(calibration, participant)
    else:
        predictor.fit(train)

    probs = predictor.predict(test)
    probs = np.clip(probs, 1e-12, None)
    probs = probs / probs.sum(axis=1, keepdims=True)

    cued = np.array([FINGER_INDEX[f] for f in test["target_finger"]])
    chosen = np.array([FINGER_INDEX[f] for f in test["actual_finger"]])
    rows = np.arange(len(test))
    order = np.argsort(-probs, axis=1)

    classes = _class_probabilities(probs, cued)
    out = pd.DataFrame({
        "participant": test["participant"].to_numpy(),
        "trial": test["trial_index"].to_numpy(),
        "event": test["event_index"].to_numpy(),
        "condition": test["condition"].to_numpy(),
        "level": test["level"].to_numpy(),
        "target_key": test["key"].to_numpy(),
        "target_finger": test["target_finger"].to_numpy(),
        "actual_finger": test["actual_finger"].to_numpy(),
        "predicted_finger": [FINGERS[i] for i in order[:, 0]],
        "second_choice": [FINGERS[i] for i in order[:, 1]],
    })
    for i, finger in enumerate(FINGERS):
        out[f"p_{finger}"] = probs[:, i]
    out["p_correct"] = classes[OUTCOME_CORRECT]
    out["p_within_hand"] = classes[OUTCOME_WITHIN_HAND]
    out["p_wrong_hand"] = classes[OUTCOME_HOMOLOGOUS] + classes[OUTCOME_CROSS_OTHER]
    out["p_homologous"] = classes[OUTCOME_HOMOLOGOUS]
    out["observed_outcome"] = [_outcome_class(t, a) for t, a
                               in zip(test["target_finger"], test["actual_finger"])]
    out["correct"] = out["actual_finger"] == out["target_finger"]
    out["log_loss"] = -np.log(probs[rows, chosen])
    out["top1_hit"] = order[:, 0] == chosen
    out["top2_hit"] = (order[:, 0] == chosen) | (order[:, 1] == chosen)

    # Reaction time, fitted on the same training events and predicted here.
    if isinstance(predictor, ChoiceModelPredictor):
        train_prior = predictor.prior_probabilities(train)
        test_prior = predictor.prior_probabilities(test)
    else:
        train_prior = test_prior = None
    if train_prior is not None:
        train_cued = np.array([FINGER_INDEX[f] for f in train["target_finger"]])
        train_conflict = -np.log(np.clip(
            train_prior[np.arange(len(train)), train_cued], 1e-12, None))
        train_conflict = np.where(train["condition"].to_numpy() == "A", 0.0, train_conflict)
        test_conflict = -np.log(np.clip(test_prior[rows, cued], 1e-12, None))
        test_conflict = np.where(test["condition"].to_numpy() == "A", 0.0, test_conflict)
        coefficients, smearing = _fit_rt(train, train_conflict)
        offset = 0.0
        if variant == PERSONALISED and calibration is not None and not calibration.empty:
            # This person's own speed, from their key-only trials only.
            # Condition A carries no cued finger, so its conflict is zero
            # by construction - the offset is purely this person's baseline
            # speed relative to the group.
            predicted = _rt_features(calibration, np.zeros(len(calibration))) @ coefficients
            offset = float(np.mean(np.log(calibration["rt_s"].to_numpy(float)) - predicted))
        out["conflict"] = test_conflict
        out["predicted_rt"] = np.exp(_rt_features(test, test_conflict) @ coefficients
                                     + offset) * smearing
    else:
        out["conflict"] = np.nan
        out["predicted_rt"] = np.nan
    out["actual_rt"] = test["rt_s"].to_numpy(float)
    out["rt_residual"] = out["actual_rt"] - out["predicted_rt"]
    out["fold_id"] = fold_id
    out["model_name"] = model_name
    out["variant"] = variant
    return out


def leave_one_participant_out_predictions(
        df: pd.DataFrame, predictor_factory: Callable[[], _Baseline],
        model_name: str, variants: Sequence[str] = (POPULATION,),
        progress=None) -> pd.DataFrame:
    """Twenty folds, one per participant, concatenated.

    For each fold the held-out participant contributes nothing to the fit.
    Under the personalised variant their Condition A events are then used
    as calibration and their B/C events predicted; under the population
    variant nothing of theirs is used at all.
    """
    participants = sorted(df["participant"].unique())
    if len(participants) < 2:
        raise em.EffectorModelError("prediction needs at least 2 participants")
    frames = []
    for participant in participants:
        if progress is not None:
            progress(f"{model_name}: holding out {participant}")
        train = df[df["participant"] != participant]
        test = df[df["participant"] == participant]
        calibration = test[test["condition"] == "A"]
        for variant in variants:
            predictor = predictor_factory()
            if isinstance(predictor, ChoiceModelPredictor):
                predictor.personalised = variant == PERSONALISED
            elif variant == PERSONALISED:
                continue  # baselines have nothing to personalise
            scored = test if variant == POPULATION else test[test["condition"] != "A"]
            if scored.empty:
                continue
            frames.append(_fold(train, scored, predictor,
                                fold_id=f"LOPO/{participant}", variant=variant,
                                model_name=model_name,
                                calibration=calibration, participant=participant))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def trial_cv_predictions(df: pd.DataFrame, predictor_factory: Callable[[], _Baseline],
                         model_name: str, n_folds: int = DEFAULT_TRIAL_FOLDS,
                         seed: int = 20260827,
                         progress=None) -> pd.DataFrame:
    """K-fold over WHOLE trials, every participant present in every fold.

    The secondary split: it asks whether the model can predict trials the
    same person has not done yet, which is a different and easier question
    than predicting a new person, and the two are reported separately
    rather than averaged into one "cross-validation" number.
    """
    trials = df[["participant", "trial_index"]].drop_duplicates().reset_index(drop=True)
    rng = np.random.default_rng(seed)
    trials["fold"] = rng.permutation(np.arange(len(trials)) % n_folds)
    tagged = df.merge(trials, on=["participant", "trial_index"], how="left")
    frames = []
    for fold in range(n_folds):
        if progress is not None:
            progress(f"{model_name}: trial fold {fold + 1}/{n_folds}")
        train = tagged[tagged["fold"] != fold].drop(columns="fold")
        test = tagged[tagged["fold"] == fold].drop(columns="fold")
        if train.empty or test.empty:
            continue
        predictor = predictor_factory()
        if isinstance(predictor, ChoiceModelPredictor):
            predictor.seen_participants = True
        frames.append(_fold(train.reset_index(drop=True), test.reset_index(drop=True),
                            predictor, fold_id=f"trial/{fold}",
                            variant=WITHIN_PARTICIPANT, model_name=model_name))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# Scoring


def choice_metrics(predictions: pd.DataFrame,
                   by: Sequence[str] = ("model_name", "variant", "condition")
                   ) -> pd.DataFrame:
    """Held-out log loss, top-1 and top-2 accuracy.

    Log loss is the column that ranks models.  Top-1 saturates: any model
    carrying a cue term puts the cued finger first on the great majority
    of events, so several very different models score the same to three
    decimals, and reading the comparison off that column would conclude
    they are equivalent when their error distributions are not.
    """
    grouped = predictions.groupby(list(by), sort=True)
    out = grouped.agg(n=("log_loss", "size"),
                      log_loss=("log_loss", "mean"),
                      top1_accuracy=("top1_hit", "mean"),
                      top2_accuracy=("top2_hit", "mean"),
                      mean_p_correct=("p_correct", "mean"),
                      observed_correct=("correct", "mean")).reset_index()
    out["chance_log_loss"] = float(-np.log(1 / N_FINGERS))
    return out


def choice_metrics_by_participant(predictions: pd.DataFrame) -> pd.DataFrame:
    """The same scores with the participant as the unit, so a model
    comparison can be tested the way everything else in this study is -
    paired over people, not over events."""
    return (predictions.groupby(["model_name", "variant", "condition", "participant"])
            .agg(n=("log_loss", "size"), log_loss=("log_loss", "mean"),
                 top1_accuracy=("top1_hit", "mean"),
                 top2_accuracy=("top2_hit", "mean")).reset_index())


def compare_models_paired(by_participant: pd.DataFrame, reference: str,
                          metric: str = "log_loss",
                          condition: Optional[str] = None) -> pd.DataFrame:
    """Paired over participants: each model against the reference model on
    the same held-out people."""
    from scipy import stats as sstats

    frame = by_participant
    if condition is not None:
        frame = frame[frame["condition"] == condition]
    pivot = (frame.groupby(["model_name", "participant"])[metric].mean()
             .unstack("model_name"))
    if reference not in pivot.columns:
        return pd.DataFrame()
    rows = []
    for model in pivot.columns:
        if model == reference:
            continue
        pair = pivot[[reference, model]].dropna()
        if len(pair) < 2:
            continue
        difference = pair[model].to_numpy() - pair[reference].to_numpy()
        n = len(difference)
        sd = float(difference.std(ddof=1))
        half = float(sstats.t.ppf(0.975, n - 1)) * sd / np.sqrt(n) if sd else np.nan
        t_stat, p_value = sstats.ttest_rel(pair[model], pair[reference])
        rows.append({
            "model": model, "reference": reference, "metric": metric,
            "mean_model": float(pair[model].mean()),
            "mean_reference": float(pair[reference].mean()),
            "mean_difference": float(difference.mean()),
            "ci95_lo": float(difference.mean() - half),
            "ci95_hi": float(difference.mean() + half),
            "t": float(t_stat), "df": n - 1, "p": float(p_value),
            "n_participants": n,
            "better": ("model" if difference.mean() < 0 else "reference")
            if metric == "log_loss" else ("model" if difference.mean() > 0 else "reference"),
        })
    return pd.DataFrame(rows)


def prediction_confusion(predictions: pd.DataFrame, condition: str,
                         model_name: Optional[str] = None,
                         variant: str = POPULATION) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """(observed, predicted) 10x10 cued x used counts on HELD-OUT events.

    The predicted table sums the held-out probability mass rather than the
    argmax, so it is the model's full expectation and can be compared cell
    by cell with the observed counts.
    """
    frame = predictions[(predictions["condition"] == condition)
                        & (predictions["variant"] == variant)]
    if model_name is not None:
        frame = frame[frame["model_name"] == model_name]
    observed = pd.DataFrame(0.0, index=FINGERS, columns=FINGERS)
    predicted = pd.DataFrame(0.0, index=FINGERS, columns=FINGERS)
    probability_columns = [f"p_{finger}" for finger in FINGERS]
    for row in frame.itertuples():
        observed.loc[row.target_finger, row.actual_finger] += 1
    if len(frame):
        mass = frame[probability_columns].to_numpy(float)
        for cued, row in zip(frame["target_finger"], mass):
            predicted.loc[cued] += row
    return observed, predicted


def calibration_table(predictions: pd.DataFrame, column: str = "p_correct",
                      outcome: Optional[str] = None, n_bins: int = 10,
                      by: Sequence[str] = ("model_name", "variant", "condition")
                      ) -> pd.DataFrame:
    """Predicted probability against observed frequency, in bins.

    Calibration is what makes a probability usable rather than merely
    ranked: a model can order events perfectly and still say 0.9 where the
    truth is 0.5.  Bins are equal-width on the predicted probability so
    empty regions stay visible instead of being hidden by equal-count
    binning.
    """
    frame = predictions.copy()
    if outcome is None:
        frame["hit"] = frame["correct"]
    else:
        frame["hit"] = frame["observed_outcome"] == outcome
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    frame["bin"] = np.clip(np.digitize(frame[column], edges) - 1, 0, n_bins - 1)
    grouped = frame.groupby(list(by) + ["bin"], sort=True)
    out = grouped.agg(n=("hit", "size"), predicted=(column, "mean"),
                      observed=("hit", "mean")).reset_index()
    out["bin_lo"] = edges[out["bin"].to_numpy()]
    out["bin_hi"] = edges[out["bin"].to_numpy() + 1]
    out["gap"] = out["observed"] - out["predicted"]
    return out


def expected_calibration_error(calibration: pd.DataFrame,
                               by: Sequence[str] = ("model_name", "variant", "condition")
                               ) -> pd.DataFrame:
    """One number per cell: the bin-count-weighted mean |observed -
    predicted|, so calibration can be compared across models."""
    rows = []
    for key, group in calibration.groupby(list(by), sort=True):
        key = key if isinstance(key, tuple) else (key,)
        weight = group["n"] / group["n"].sum()
        rows.append({**dict(zip(by, key)),
                     "expected_calibration_error": float((weight * group["gap"].abs()).sum()),
                     "n": int(group["n"].sum())})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Error-risk prediction
#
# The question is whether the model can flag, BEFORE the keypress, that
# this event is likely to go wrong and in which way.  Accuracy is
# worthless here: predicting "correct" on every event scores 98.7% under
# B and 99.96% on the wrong-hand class under C.  What matters is whether
# the events the model calls risky are the ones that actually fail.


def rare_event_warning(n_positives: int, class_name: str) -> Optional[str]:
    """The label a rare class has to carry, or None when it has enough
    cases to support ordinary inference."""
    if n_positives == 0:
        return f"{class_name}: no positive cases held out - nothing to score"
    if n_positives < MIN_POSITIVES_FOR_INFERENCE:
        return (f"{class_name}: only {n_positives} positive case"
                f"{'s' if n_positives != 1 else ''} held out - exploratory, "
                f"precision and recall move by more than a tenth per case")
    return None


def error_risk_metrics(predictions: pd.DataFrame,
                       by: Sequence[str] = ("model_name", "variant", "condition")
                       ) -> pd.DataFrame:
    """Ranking and calibration for each outcome class, per cell.

    Average precision is the summary: it is the area under the
    precision-recall curve, which unlike ROC area does not flatter a
    classifier on a class that is 1% of the data.  A `warning` column
    carries the rare-event label rather than leaving a confident number
    beside two positive cases.
    """
    from sklearn.metrics import average_precision_score

    probability_of = {
        OUTCOME_CORRECT: "p_correct",
        OUTCOME_WITHIN_HAND: "p_within_hand",
        OUTCOME_HOMOLOGOUS: "p_homologous",
    }
    rows = []
    for key, group in predictions.groupby(list(by), sort=True):
        key = key if isinstance(key, tuple) else (key,)
        base = dict(zip(by, key))
        wrong_hand_observed = group["observed_outcome"].isin(
            [OUTCOME_HOMOLOGOUS, OUTCOME_CROSS_OTHER]).to_numpy()
        targets = {
            **{name: (group["observed_outcome"] == name).to_numpy()
               for name in probability_of},
            "wrong hand (any digit)": wrong_hand_observed,
        }
        scores = {**{name: group[column].to_numpy()
                     for name, column in probability_of.items()},
                  "wrong hand (any digit)": group["p_wrong_hand"].to_numpy()}
        for name, actual in targets.items():
            positives = int(actual.sum())
            score = scores[name]
            row = {**base, "outcome": name, "n_events": len(group),
                   "n_positives": positives,
                   "base_rate": positives / len(group) if len(group) else np.nan,
                   "mean_predicted": float(score.mean()),
                   "warning": rare_event_warning(positives, name)}
            if 0 < positives < len(group):
                row["average_precision"] = float(average_precision_score(actual, score))
                row["lift_over_base_rate"] = (row["average_precision"]
                                              / (positives / len(group)))
                # Precision and recall at the decision the model itself
                # implies: flag the event when the class is its modal
                # prediction among the four outcome classes.
                flagged = score >= 0.5
                if flagged.sum():
                    row["precision_at_0.5"] = float(actual[flagged].mean())
                else:
                    row["precision_at_0.5"] = np.nan
                row["recall_at_0.5"] = (float(flagged[actual].mean())
                                        if positives else np.nan)
                # The operationally useful number for a rare class: among
                # the events the model ranks riskiest, how many really failed.
                for k in (10, 50):
                    if len(score) >= k:
                        top = np.argsort(-score)[:k]
                        row[f"precision_in_top_{k}"] = float(actual[top].mean())
            else:
                row["average_precision"] = np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def error_risk_deciles(predictions: pd.DataFrame, outcome: str = OUTCOME_HOMOLOGOUS,
                       column: str = "p_homologous", n_bins: int = 10,
                       by: Sequence[str] = ("model_name", "variant", "condition")
                       ) -> pd.DataFrame:
    """Predicted risk against observed failure rate, in equal-count bins.

    Equal-count rather than equal-width here, because a rare class puts
    almost every event in the lowest width-bin and the table would say
    nothing.  This is the "are the events it calls risky actually risky"
    check in its most direct form.
    """
    rows = []
    for key, group in predictions.groupby(list(by), sort=True):
        key = key if isinstance(key, tuple) else (key,)
        actual = (group["observed_outcome"] == outcome).to_numpy()
        score = group[column].to_numpy()
        if len(group) < n_bins:
            continue
        order = np.argsort(score)
        bins = np.array_split(order, n_bins)
        for index, chunk in enumerate(bins):
            rows.append({**dict(zip(by, key)), "risk_bin": index + 1,
                         "n": len(chunk),
                         "mean_predicted": float(score[chunk].mean()),
                         "observed_rate": float(actual[chunk].mean()),
                         "n_observed": int(actual[chunk].sum())})
    return pd.DataFrame(rows)


def highest_risk_events(predictions: pd.DataFrame, column: str = "p_homologous",
                        condition: str = "B", model_name: Optional[str] = None,
                        variant: str = POPULATION, top: int = 25) -> pd.DataFrame:
    """The held-out events the model flagged as most likely to go wrong,
    with what actually happened on each.

    This is the table that makes the error-risk claim checkable one event
    at a time instead of only in aggregate.
    """
    frame = predictions[(predictions["condition"] == condition)
                        & (predictions["variant"] == variant)]
    if model_name is not None:
        frame = frame[frame["model_name"] == model_name]
    columns = ["participant", "trial", "event", "condition", "target_key",
               "target_finger", "actual_finger", "predicted_finger",
               "p_correct", "p_within_hand", "p_wrong_hand", "p_homologous",
               "observed_outcome", "actual_rt", "predicted_rt", "fold_id"]
    return (frame.sort_values(column, ascending=False)
            .head(top)[columns].reset_index(drop=True))


# ---------------------------------------------------------------------------
# Reaction-time prediction


def rt_metrics(predictions: pd.DataFrame,
               by: Sequence[str] = ("model_name", "variant", "condition")
               ) -> pd.DataFrame:
    """MAE, RMSE and R^2 on held-out reaction times.

    R^2 is computed against the mean of the HELD-OUT reaction times, so it
    is the honest "how much better than predicting the overall mean"
    rather than a within-fit statistic.  A negative value therefore means
    the model did worse than that constant, which is information and is
    not clipped away.

    Whatever these numbers turn out to be, they do not license a
    mechanistic claim about the B - C difference: the condition term is a
    fitted per-modality constant, so predicting reaction time accurately
    is not the same as having explained why the modalities differ.  See
    app.effector_rt for the tests that separate the two.
    """
    rows = []
    for key, group in predictions.groupby(list(by), sort=True):
        key = key if isinstance(key, tuple) else (key,)
        actual = group["actual_rt"].to_numpy(float)
        predicted = group["predicted_rt"].to_numpy(float)
        usable = np.isfinite(actual) & np.isfinite(predicted)
        actual, predicted = actual[usable], predicted[usable]
        if not len(actual):
            continue
        residual = actual - predicted
        total = ((actual - actual.mean()) ** 2).sum()
        rows.append({**dict(zip(by, key)), "n": len(actual),
                     "mae_s": float(np.abs(residual).mean()),
                     "rmse_s": float(np.sqrt((residual ** 2).mean())),
                     "r2": float(1 - (residual ** 2).sum() / total) if total else np.nan,
                     "mean_observed_s": float(actual.mean()),
                     "mean_predicted_s": float(predicted.mean()),
                     "bias_s": float(residual.mean())})
    return pd.DataFrame(rows)


def rt_pooled_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    """The same reaction-time scores over all conditions at once.

    Reported beside the per-condition table because the two answer
    different questions and the difference between them is the finding.
    Pooled, the model is scored partly on separating the conditions, which
    it does well.  Within a condition that variance is already removed by
    the grouping, so what is left is event-to-event variation - and that
    is where a low R^2 says the model predicts WHICH finger far better
    than HOW LONG any single response took.
    """
    return rt_metrics(predictions, by=("model_name", "variant"))


def rt_by_participant(predictions: pd.DataFrame, model_name: Optional[str] = None,
                      variant: str = POPULATION) -> pd.DataFrame:
    """Observed against predicted mean reaction time, participant by
    participant - the unit every other statistic in this study uses."""
    frame = predictions[predictions["variant"] == variant]
    if model_name is not None:
        frame = frame[frame["model_name"] == model_name]
    return (frame.groupby(["participant", "condition"])
            .agg(n=("actual_rt", "size"),
                 observed_rt_s=("actual_rt", "mean"),
                 predicted_rt_s=("predicted_rt", "mean")).reset_index())


def rt_residual_diagnostics(predictions: pd.DataFrame, model_name: Optional[str] = None,
                            variant: str = POPULATION) -> pd.DataFrame:
    """Residual centre and spread per condition and per cued digit, to
    show whether the error is even or concentrated in one cell."""
    frame = predictions[predictions["variant"] == variant]
    if model_name is not None:
        frame = frame[frame["model_name"] == model_name]
    frame = frame[np.isfinite(frame["rt_residual"])]
    if frame.empty:
        return pd.DataFrame()
    frame = frame.assign(digit=frame["target_finger"].str[1].astype(int))
    by_condition = (frame.groupby("condition")["rt_residual"]
                    .agg(n="size", mean="mean", sd="std",
                         p05=lambda v: v.quantile(0.05),
                         p95=lambda v: v.quantile(0.95)).reset_index()
                    .rename(columns={"condition": "cell"}).assign(grouping="condition"))
    by_digit = (frame.groupby("digit")["rt_residual"]
                .agg(n="size", mean="mean", sd="std",
                     p05=lambda v: v.quantile(0.05),
                     p95=lambda v: v.quantile(0.95)).reset_index()
                .rename(columns={"digit": "cell"}).assign(grouping="cued digit"))
    by_digit["cell"] = by_digit["cell"].map(lambda d: f"{d} ({DIGIT_NAMES[d]})")
    return pd.concat([by_condition, by_digit], ignore_index=True)


# ---------------------------------------------------------------------------
# Population vs personalised, and the top-level driver


def population_vs_personalised(predictions: pd.DataFrame,
                               model_name: Optional[str] = None) -> pd.DataFrame:
    """Paired over participants: does that person's own Condition A data
    help predict their cued trials?

    Set up as a real comparison with a real null.  Phase-2 work found no
    cross-participant gain from a personal habitual prior, so a null here
    is an expected outcome and is reported as one - the interesting
    quantity is the interval, not whether it clears a threshold.
    """
    from scipy import stats as sstats

    frame = predictions[predictions["condition"].isin(CUED_CONDITIONS)]
    if model_name is not None:
        frame = frame[frame["model_name"] == model_name]
    pivot = (frame.groupby(["model_name", "variant", "participant"])
             .agg(log_loss=("log_loss", "mean"), top1=("top1_hit", "mean"),
                  mae=("rt_residual", lambda v: float(np.abs(v).mean())))
             .reset_index())
    rows = []
    for model, group in pivot.groupby("model_name"):
        wide = group.pivot(index="participant", columns="variant",
                           values=["log_loss", "top1", "mae"])
        for metric, lower_is_better in (("log_loss", True), ("top1", False), ("mae", True)):
            if (metric, POPULATION) not in wide.columns or \
               (metric, PERSONALISED) not in wide.columns:
                continue
            pair = wide[metric][[POPULATION, PERSONALISED]].dropna()
            if len(pair) < 2:
                continue
            difference = pair[PERSONALISED].to_numpy() - pair[POPULATION].to_numpy()
            n = len(difference)
            sd = float(difference.std(ddof=1))
            half = float(sstats.t.ppf(0.975, n - 1)) * sd / np.sqrt(n) if sd else np.nan
            t_stat, p_value = sstats.ttest_rel(pair[PERSONALISED], pair[POPULATION])
            improved = ((difference < 0).sum() if lower_is_better else (difference > 0).sum())
            rows.append({
                "model": model, "metric": metric,
                "population": float(pair[POPULATION].mean()),
                "personalised": float(pair[PERSONALISED].mean()),
                "mean_difference": float(difference.mean()),
                "ci95_lo": float(difference.mean() - half),
                "ci95_hi": float(difference.mean() + half),
                "t": float(t_stat), "df": n - 1, "p": float(p_value),
                "n_participants": n, "n_improved": int(improved),
                "verdict": _personalisation_verdict(difference.mean(), half,
                                                    p_value, lower_is_better),
            })
    return pd.DataFrame(rows)


def _personalisation_verdict(mean: float, half: float, p_value: float,
                             lower_is_better: bool) -> str:
    if not np.isfinite(half):
        return "not enough participants"
    helps = (mean < 0) if lower_is_better else (mean > 0)
    if p_value < 0.05:
        return "personalisation helps" if helps else "personalisation hurts"
    # A null with a tight interval says something; a null with a wide one
    # does not, and the two must not be reported the same way.
    return ("no detectable difference (interval tight)" if abs(half) < abs(mean) + 0.02
            else "no detectable difference (interval wide - inconclusive)")


@dataclass
class PredictionRun:
    """Everything one prediction run produced, ready for tables and export."""

    predictions: pd.DataFrame
    primary_model: str
    n_participants: int
    n_events: int

    def for_model(self, model_name: str, variant: str = POPULATION) -> pd.DataFrame:
        return self.predictions[(self.predictions["model_name"] == model_name)
                                & (self.predictions["variant"] == variant)]


EXPORT_COLUMNS = (["participant", "trial", "event", "condition", "target_finger",
                   "actual_finger", "predicted_finger"]
                  + [f"p_{finger}" for finger in FINGERS]
                  + ["p_correct", "p_wrong_hand", "p_homologous",
                     "predicted_rt", "actual_rt", "fold_id", "model_name"])


def export_frame(predictions: pd.DataFrame) -> pd.DataFrame:
    """The per-event prediction table in the agreed export schema, with
    the extra diagnostic columns kept after the required ones."""
    extra = [c for c in ("variant", "level", "target_key", "second_choice",
                         "p_within_hand", "observed_outcome", "log_loss",
                         "top1_hit", "top2_hit", "conflict", "rt_residual")
             if c in predictions.columns]
    columns = [c for c in EXPORT_COLUMNS if c in predictions.columns] + extra
    return predictions[columns].copy()


def primary_export_frame(predictions: pd.DataFrame,
                         primary_model: str = "M3 (primary)") -> pd.DataFrame:
    """The same table cut down to one row per modelled event.

    export_frame() carries every model in the comparison set and both
    prediction variants, which is what makes the model comparison
    auditable and also what makes it about eighty megabytes - ten times
    larger than anything else this project versions, for a file that is
    fully regenerable from the tracked inputs and a fixed seed.

    Almost every practical use is the primary model's population
    predictions: one row per event, which is what "why was this event
    flagged" is answered from. That subset is a few megabytes and opens
    in a spreadsheet. The full table stays exactly as it was for anyone
    who needs the other models.
    """
    frame = predictions[(predictions["model_name"] == primary_model)
                        & (predictions["variant"] == POPULATION)]
    return export_frame(frame)


def run_predictions(df: pd.DataFrame,
                    predictors: Optional[Sequence[Tuple[str, Callable]]] = None,
                    include_personalised: bool = True,
                    include_trial_cv: bool = True,
                    primary_model: str = "M3 (primary)",
                    progress=None) -> PredictionRun:
    """Every held-out prediction in one pass.

    Leave-one-participant-out for each predictor (population, plus
    personalised for the model-based ones), then the secondary
    within-participant trial split for the primary model.  Progress is
    reported per fold because a full run is a few hundred fits.
    """
    predictors = list(predictors if predictors is not None else default_predictors())
    frames = []
    for name, factory in predictors:
        probe = factory()
        variants = [POPULATION]
        # Personalisation means calibrating this participant's habitual
        # prior, so a model without one has nothing to calibrate: running
        # it would duplicate the population pass and then appear as a
        # zero-difference row with no interval, which reads like a result.
        personalisable = (isinstance(probe, ChoiceModelPredictor)
                          and probe.spec.prior and probe.spec.prior_by_participant)
        if include_personalised and personalisable:
            variants.append(PERSONALISED)
        frames.append(leave_one_participant_out_predictions(
            df, factory, model_name=name, variants=variants, progress=progress))
    if include_trial_cv:
        for name, factory in predictors:
            if name != primary_model:
                continue
            frames.append(trial_cv_predictions(df, factory, model_name=name,
                                               progress=progress))
    predictions = pd.concat([f for f in frames if len(f)], ignore_index=True)
    return PredictionRun(predictions=predictions, primary_model=primary_model,
                         n_participants=df["participant"].nunique(),
                         n_events=len(df))


def prediction_summary(run: PredictionRun) -> pd.DataFrame:
    """The headline table: one row per model, held-out log loss and
    accuracy on the cued conditions, ordered by log loss."""
    frame = run.predictions[(run.predictions["variant"] == POPULATION)
                            & (run.predictions["condition"].isin(CUED_CONDITIONS))]
    out = (frame.groupby("model_name")
           .agg(n=("log_loss", "size"), log_loss=("log_loss", "mean"),
                top1_accuracy=("top1_hit", "mean"),
                top2_accuracy=("top2_hit", "mean"),
                rt_mae_s=("rt_residual", lambda v: float(np.abs(v).mean())))
           .reset_index().sort_values("log_loss"))
    out["rank"] = np.arange(1, len(out) + 1)
    return out


def leakage_audit(run: PredictionRun, df: pd.DataFrame) -> pd.DataFrame:
    """Evidence that every prediction really is out of sample.

    Each row states a property that would be false if the split had
    leaked, and whether it holds.  This is here because "these are
    predictions, not fitted values" is the one claim in the module that a
    reader cannot check by looking at a number.
    """
    predictions = run.predictions
    lopo = predictions[predictions["fold_id"].str.startswith("LOPO/")]
    rows = []
    if len(lopo):
        held_out = lopo["fold_id"].str.split("/").str[1]
        rows.append({
            "check": "every leave-one-participant-out row was predicted by the fold "
                     "that excluded that participant",
            "holds": bool((held_out.to_numpy() == lopo["participant"].to_numpy()).all()),
        })
        rows.append({
            "check": "every participant was held out exactly once per model and variant",
            "holds": bool(lopo.groupby(["model_name", "variant", "participant"])["fold_id"]
                          .nunique().eq(1).all()),
        })
    personalised = predictions[predictions["variant"] == PERSONALISED]
    rows.append({
        "check": "personalised predictions cover only cued (B/C) events, so the "
                 "Condition A calibration data are never also scored",
        "holds": bool(personalised.empty
                      or personalised["condition"].isin(CUED_CONDITIONS).all()),
    })
    trial = predictions[predictions["fold_id"].str.startswith("trial/")]
    if len(trial):
        per_trial_folds = (trial.groupby(["model_name", "participant", "trial"])["fold_id"]
                           .nunique())
        rows.append({
            "check": "each trial appears in exactly one within-participant fold "
                     "(no trial split across the boundary)",
            "holds": bool(per_trial_folds.eq(1).all()),
        })
    rows.append({
        "check": "no prediction row is missing its fold identifier",
        "holds": bool(predictions["fold_id"].notna().all()),
    })
    counts = (predictions[predictions["variant"] == POPULATION]
              .groupby("model_name").size())
    rows.append({
        "check": f"each model predicted all {len(df)} modelled events under the "
                 f"population variant",
        "holds": bool((counts == len(df)).all()) if len(counts) else False,
    })
    return pd.DataFrame(rows)


def risk_concentration(predictions: pd.DataFrame, outcome: str = OUTCOME_HOMOLOGOUS,
                       column: str = "p_homologous", condition: str = "B",
                       model_name: Optional[str] = None, top_fraction: float = 0.2,
                       variant: str = POPULATION) -> Dict[str, object]:
    """How many of a rare error's cases fell in the riskiest slice of events.

    The summary form of the error-risk result, and the one that can be
    stated without any statistics: rank every held-out event by the
    probability the model gave this outcome BEFORE the keypress, take the
    riskiest fraction, and count how many of the failures are in there
    against how many would be if the ranking carried no information.
    """
    frame = predictions[(predictions["condition"] == condition)
                        & (predictions["variant"] == variant)]
    if model_name is not None:
        frame = frame[frame["model_name"] == model_name]
    if frame.empty:
        return {}
    actual = (frame["observed_outcome"] == outcome).to_numpy()
    score = frame[column].to_numpy(float)
    n_top = max(int(round(len(frame) * top_fraction)), 1)
    top = np.argsort(-score)[:n_top]
    captured = int(actual[top].sum())
    total = int(actual.sum())
    return {
        "condition": condition, "outcome": outcome,
        "n_events": len(frame), "n_top_events": n_top,
        "top_fraction": top_fraction,
        "n_errors": total, "n_errors_in_top": captured,
        "share_captured": captured / total if total else np.nan,
        "expected_if_uninformative": total * top_fraction,
        "lift": (captured / (total * top_fraction)) if total else np.nan,
    }
