"""Effector-selection model for the Main User Study (GUI-free).

Every keypress in the study is a choice of ONE of the ten fingers.  This
module fits that choice directly, as a competition between the ten
candidate effectors, instead of scoring it as correct/incorrect after the
fact.  The point is to express the whole A/B/C pattern - free-choice
fingering, cued accuracy, the wrong-hand error class, and the
reaction-time cost - with one set of parameters that mean something.

Input rows are the exported per-event dicts
(app.participant_export.collect_participant_data /
app.group_analysis.load_participant_rows).  Nothing here re-derives
correctness or re-filters reaction times: the export already carries the
final, hand-verified verdicts, so these numbers cannot drift from the
Participant/Group Analysis windows.  The only event filter is the shared
one (app.participant_analysis.valid_events), plus dropping timeouts and
events whose finger never resolved, because those have no chosen effector
to model.

The model
---------
For event t (participant s, condition c, PRESSED key k_t, cued finger
f* = (h*, d*)) every finger f = (h, d) gets a utility

    U(t,f) =  rho[h, k_t]                                (reach)
            + w_c * ( pi[f] + pi_s[f] )                  (habitual prior)
            + mu1*1[f = f_{t-1}] + mu2*1[h = h_{t-1}]    (motor/transition)
            + kappa_H[c]*1[h = h*] + kappa_D[c]*1[d = d*]
              - gamma[c]*|d - d*|                        (cue evidence)

    P(F_t = f) = softmax_f U(t,f)

and Condition A simply has no cue term (kappa_H = kappa_D = gamma = 0),
which is what "A withholds the finger cue" means formally rather than a
separate model for A.

Read multiplicatively it is Bayes' rule,

    P(f) proportional to  Prior(f | k, s)^{w_c} x Likelihood_c(cue | f),

with the likelihood factorised into a HAND term and a DIGIT term.  The
softmax choice model and the "prior x cue likelihood" model are therefore
the same model under a log-linear likelihood, not two rival ones; the
rival hypotheses are about the STRUCTURE of the likelihood, and those are
the model comparison (see MODEL_LADDER).

Why the hand/digit split is the whole point.  A cue that names the digit
symbolically ("R2", a highlighted digit in a picture of two hands) states
digit identity in a frame that carries across the body midline: the
evidence "digit 2" fits L2 as well as R2, and only the hand term separates
them.  A cue delivered ON the acting finger has hand membership as a
property of where it is, not as something separately encoded.  So the
prediction is a channel-SELECTIVE difference - kappa_H much larger under
C, kappa_D not - and the homologous wrong-hand error (right digit, wrong
hand) falls out of the arithmetic rather than being coded in.  Nothing in
the design matrix mentions "homologous"; error_structure() checks that
prediction against the observed confusion counts.

Design decisions worth stating
------------------------------
- Every input is knowable BEFORE the response.  The reach term reads the
  CUED key, not the key actually struck (ModelSpec.key_source): the
  pressed key is part of the response, so a model that reads it can
  describe an event but cannot be said to predict it.  The previous
  keypress is fair game - it has already happened.  Conditioning on the
  pressed key remains available as a descriptive sensitivity fit, and
  changes almost nothing: key accuracy is 98.9% (B) / 99.7% (C), and the
  held-out log loss under B moves from 0.3470 to 0.3484.
- Reachability is a FITTED term (one intercept per hand x key), not a
  hard mask over the choice set.  A mask would decide by fiat which
  cross-hand actions are possible, which is exactly the quantity in
  question.
- Events at the start of a trial keep their place: their transition
  features are zero for every candidate, and a term constant across
  candidates cancels in the softmax, so they contribute normally instead
  of being dropped.
- The habitual prior enters B and C through a free scalar w_c.  Estimating
  the prior on Condition A alone and then asking how much of it still acts
  under cueing is what makes "the free-choice habit is the residual error
  mode under visual guidance" a fitted number instead of a description.
  w_A is fixed at 1 by definition (A is where the prior is estimated).

What this module deliberately does NOT do
-----------------------------------------
No drift-diffusion/LBA/race fit.  Separating drift, boundary and
non-decision time needs the RT distribution of ERRORS, and the study has
233 (B) and 73 (C) genuine substitutions - under four per participant in
C.  Its one participant-level prediction (weaker cue evidence => larger
RT cost) is testable here and is tested in app.effector_rt; it does not
hold.  Reporting the rejected constraint is the honest form of that
analysis; fitting an unidentifiable accumulator would not be.
"""

import inspect
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from .participant_analysis import valid_events

# Canonical finger identity/order.  L1..L5 then R1..R5 - the export's
# FINGER_LABELS order, kept so p_* columns and this module agree.
FINGERS: List[str] = [f"{hand}{digit}" for hand in ("L", "R") for digit in range(1, 6)]
FINGER_INDEX: Dict[str, int] = {f: i for i, f in enumerate(FINGERS)}
N_FINGERS = len(FINGERS)
IS_RIGHT = np.array([f[0] == "R" for f in FINGERS], dtype=float)
DIGIT = np.array([int(f[1]) for f in FINGERS], dtype=float)

CONDITIONS = ("A", "B", "C")
CUED_CONDITIONS = ("B", "C")
DIGIT_NAMES = {1: "thumb", 2: "index", 3: "middle", 4: "ring", 5: "little"}

# Candidate-varying features.  Each is an (n_events, 10) array: the value
# that feature takes for each of the ten candidate fingers on that event.
CUE_FEATURES = ("match_finger", "match_hand", "match_digit", "digit_distance")
MOTOR_FEATURES = ("repeat_finger", "repeat_hand")


class EffectorModelError(Exception):
    pass


def _progress_adapter(progress):
    """Normalise a progress callback to `f(message, done)`.

    These modules are called from a Qt window, from the test suite and
    from plain scripts, and each wants something different from a
    progress hook: the window needs a count to drive a bar, a script
    usually just prints the text.  The arity is read ONCE, here, rather
    than by calling the hook and catching TypeError - a TypeError raised
    inside the callback itself would otherwise be mistaken for an arity
    mismatch and the callback silently called a second time.
    """
    if progress is None:
        return lambda message, done=0: None
    try:
        parameters = [
            p for p in inspect.signature(progress).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        takes_count = len(parameters) >= 2
    except (TypeError, ValueError):
        takes_count = False
    if takes_count:
        return lambda message, done=0: progress(message, done)
    return lambda message, done=0: progress(message)


# ---------------------------------------------------------------------------
# Event table


def build_events(event_rows: List[dict]) -> pd.DataFrame:
    """Modelling-eligible events, one row each, with the derived
    within-trial context columns.

    Filtering, in order and for one reason each:
      - valid_events(): the shared carry-over exclusion, so these events
        are the same ones every other analysis counts;
      - timed_out: no keypress, so no effector was selected;
      - actual_finger / target_finger unresolved: no choice to model, and
        no cue to model it against.
    Events removed here are reported by event_accounting(), never dropped
    silently.
    """
    rows = []
    for e in valid_events(event_rows):
        if e.get("timed_out"):
            continue
        actual, target = e.get("actual_finger"), e.get("target_finger")
        if actual not in FINGER_INDEX or target not in FINGER_INDEX:
            continue
        if e.get("actual_key_id") is None or e.get("rt_s") is None:
            continue
        rows.append(e)
    if not rows:
        raise EffectorModelError("no modelling-eligible events in the selected participants")

    df = pd.DataFrame(rows)
    df = df.sort_values(["participant", "trial_index", "event_index"]).reset_index(drop=True)
    df["target_hand"] = df["target_finger"].str[0]
    df["target_digit"] = df["target_finger"].str[1].astype(int)
    df["actual_hand"] = df["actual_finger"].str[0]
    df["actual_digit"] = df["actual_finger"].str[1].astype(int)
    # Both keys are kept, and `key` - the one the model reads - defaults to
    # the CUED key. See ModelSpec.key_source: the pressed key is not known
    # until the response has happened, so a model that reads it cannot be
    # described as predicting anything before the response.
    df["key_target"] = df["target_key_id"].astype(int)
    df["key_pressed"] = df["actual_key_id"].astype(int)
    df["key"] = df["key_target"]

    by_trial = df.groupby(["participant", "trial_index"], sort=False)
    df["prev_finger"] = by_trial["actual_finger"].shift(1)
    df["prev_key"] = by_trial["actual_key_id"].shift(1)
    df["first_in_trial"] = df["prev_finger"].isna()
    df["prev_hand"] = df["prev_finger"].str[0]
    # Signed key travel is not used: what costs is how far the hand moved,
    # and direction is already carried by the reach term.
    df["key_travel"] = (df["key"] - df["prev_key"]).abs()
    df.loc[df["first_in_trial"], "key_travel"] = 0.0
    df["hand_switch"] = (df["target_hand"] != df["prev_hand"]) & (~df["first_in_trial"])
    df["correct_finger"] = df["actual_finger"] == df["target_finger"]
    return df


def event_accounting(event_rows: List[dict]) -> pd.DataFrame:
    """Where every exported event went - one row per reason, so the
    modelled n can be reconciled with the export's n by inspection."""
    total = len(event_rows)
    valid = valid_events(event_rows)
    timeouts = sum(1 for e in valid if e.get("timed_out"))
    responded = [e for e in valid if not e.get("timed_out")]
    unresolved = sum(1 for e in responded
                     if e.get("actual_finger") not in FINGER_INDEX
                     or e.get("target_finger") not in FINGER_INDEX)
    incomplete = sum(1 for e in responded
                     if e.get("actual_finger") in FINGER_INDEX
                     and e.get("target_finger") in FINGER_INDEX
                     and (e.get("actual_key_id") is None or e.get("rt_s") is None))
    modelled = len(responded) - unresolved - incomplete
    return pd.DataFrame([
        {"stage": "exported events", "n": total},
        {"stage": "excluded: confirmed carry-over", "n": total - len(valid)},
        {"stage": "excluded: no response (timeout)", "n": timeouts},
        {"stage": "excluded: finger unresolved", "n": unresolved},
        {"stage": "excluded: missing key or RT", "n": incomplete},
        {"stage": "modelled events", "n": modelled},
    ])


# ---------------------------------------------------------------------------
# Candidate-varying design


def candidate_features(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    """Every candidate-varying feature as an (n_events, 10) array.

    A feature that takes the SAME value for all ten candidates on an event
    contributes nothing to a softmax over those candidates, which is why
    trial-initial events can keep zeroed transition features instead of
    being dropped."""
    target_right = (df["target_hand"].to_numpy() == "R").astype(float)
    target_digit = df["target_digit"].to_numpy(float)
    prev = np.array([FINGER_INDEX.get(f, -1) if isinstance(f, str) else -1
                     for f in df["prev_finger"]])
    prev_right = np.where(prev >= 0, IS_RIGHT[np.clip(prev, 0, N_FINGERS - 1)], -1.0)

    same_hand = (IS_RIGHT[None, :] == target_right[:, None]).astype(float)
    same_digit = (DIGIT[None, :] == target_digit[:, None]).astype(float)
    return {
        "match_finger": same_hand * same_digit,
        "match_hand": same_hand,
        "match_digit": same_digit,
        "digit_distance": np.abs(DIGIT[None, :] - target_digit[:, None]),
        "repeat_finger": (np.arange(N_FINGERS)[None, :] == prev[:, None]).astype(float),
        "repeat_hand": (IS_RIGHT[None, :] == prev_right[:, None]).astype(float),
    }


@dataclass
class ModelSpec:
    """One model in the comparison ladder.

    cue        : which cue-evidence features get a coefficient, fitted
                 separately per cued condition (B, C).  Empty = no cue
                 term at all, the "the cue carries nothing" null.
    motor      : transition features, shared across conditions (the motor
                 cost of repeating a finger does not depend on how the
                 finger was named).
    reach      : one free intercept per (hand, key).
    prior      : group-level per-finger intercepts.
    prior_by_participant : per-participant deviations from those.
    prior_weight : free scalar w_c on the prior in B and C (two-stage
                 only; w_A == 1 by construction).
    cue_by_digit : cue coefficients additionally free per CUED digit
                 (M4).  Saturates the per-digit accuracy margin, so it is
                 judged on cross-validation, never on in-sample fit.
    """

    name: str = "M3"
    cue: Tuple[str, ...] = ("match_hand", "match_digit", "digit_distance")
    motor: Tuple[str, ...] = MOTOR_FEATURES
    reach: bool = True
    prior: bool = True
    prior_by_participant: bool = True
    prior_weight: bool = True
    cue_by_digit: bool = False
    # Ridge penalties.  The fixed-effect one keeps a coefficient finite
    # when a cell is empty - Condition C produced two cross-hand actions
    # in 5,400 events, and zero would make the hand-evidence likelihood
    # monotone with no maximum at all.  It is far too small to shrink a
    # coefficient the data actually determine (it costs 0.64 at
    # kappa = 8, against a log-likelihood in the thousands), which is why
    # it is a guard rather than regularisation: see
    # identifiability_diagnostics() for which parameters that distinction
    # matters for.  The deviation penalty is the real shrinkage prior on
    # participant effects.
    l2: float = 1e-2
    l2_prior_dev: float = 1.0
    # Explicit box on the cue coefficients, in log-odds.  When a condition
    # produces no counter-example - no cross-hand action under C in a
    # bootstrap resample, say - the likelihood is monotone in that
    # coefficient and the ridge guard alone decides where it stops, which
    # is both opaque and slow (the optimiser grinds along a flat
    # direction for thousands of iterations).  A stated bound says the
    # same thing honestly: past 20 log-odds the number is not an estimate
    # but a report that the data contain no case against it.  Every
    # coefficient this study actually estimates sits far inside it, so
    # the bound changes no fitted value - it only makes the unidentified
    # case visible and finite.  Estimates AT the bound must be read as
    # censored, which is what bootstrap_parameters flags.
    cue_bound: float = 20.0
    # The same guard for the reach block, and for the same reason.  A
    # (hand, key) cell that a hand never actually acted on has a
    # likelihood that only ever pushes it further down - nine of the
    # thirty-eight cells here are like that - so without a bound the
    # optimiser spends hundreds of iterations sliding along a flat
    # direction that the ridge stops somewhere arbitrary.  At -20
    # log-odds the hand contributes nothing to that key, which is what
    # the data are saying; the value the unbounded fit drifts to is the
    # same one, reached slowly.
    reach_bound: float = 20.0
    # Which key the reach term reads: "target" (the cued key, known as
    # soon as the cue is delivered) or "pressed" (the key actually
    # struck).
    #
    # This is the difference between a prospective prediction and a
    # retrospective description, so it is a stated choice rather than an
    # implementation detail. Everything about the event that the model
    # uses must be knowable BEFORE the response: the cue, the previous
    # keypress (which has already happened), and the participant's
    # history. The pressed key is not - it is part of the response - so
    # "target" is the default and the only setting under which the
    # prediction wording is honest.
    #
    # "pressed" remains available because the detected finger is defined
    # relative to the key actually struck, which makes it the right
    # conditioning for a purely descriptive fit. It changes almost
    # nothing either way: key accuracy is 98.9% (B) and 99.7% (C), and
    # switching moves the held-out log loss under B from 0.3470 to
    # 0.3484 and leaves the risk-enrichment counts identical.
    key_source: str = "target"


# The comparison ladder.  Each step is a hypothesis about what the cue
# supplies, not an arbitrary feature set:
#   M0  the cue supplies nothing (habit + biomechanics only)
#   M1  the cue supplies ONE undifferentiated "this finger" signal
#   M2  the cue supplies hand identity and digit identity separably
#   M3  digit identity is graded in digit distance rather than all-or-none
#   M4  the strength of both channels depends on which digit is cued
MODEL_LADDER: Tuple[ModelSpec, ...] = (
    ModelSpec(name="M0 no cue evidence", cue=()),
    ModelSpec(name="M1 unitary cue", cue=("match_finger",)),
    ModelSpec(name="M2 hand + digit", cue=("match_hand", "match_digit")),
    ModelSpec(name="M3 hand + digit + digit gradient"),
    ModelSpec(name="M4 M3, evidence free per cued digit", cue_by_digit=True),
)

# The one model the analysis reports. M0-M2 exist to show what it beats
# and M4 to show what a more flexible version buys; none of them is an
# alternative a user should have to choose between, so the window fits
# this one and runs the rest as comparison.
PRIMARY_SPEC = ModelSpec(name="M3 hand + digit + digit gradient")

# The more flexible variant, reported as secondary: it wins on AIC and on
# held-out likelihood, but it saturates the per-digit accuracy margin, so
# it is judged on cross-validation and never on in-sample fit.
SECONDARY_SPEC = ModelSpec(name="M4 evidence free per cued digit", cue_by_digit=True)

# Ablations of the preferred model - each removes one NON-cue component,
# to show the cue parameters are not absorbing habit, biomechanics or
# movement cost.
ABLATIONS: Tuple[ModelSpec, ...] = (
    ModelSpec(name="M3 without habitual prior", prior=False, prior_by_participant=False,
              prior_weight=False),
    ModelSpec(name="M3 without participant prior", prior_by_participant=False),
    ModelSpec(name="M3 without reach", reach=False),
    ModelSpec(name="M3 without motor/transition", motor=()),
    ModelSpec(name="M3 with prior weight fixed at 1", prior_weight=False),
)


# ---------------------------------------------------------------------------
# Parameter packing


@dataclass
class _Layout:
    """Where each block of parameters sits in the flat vector."""

    slices: Dict[str, slice]
    size: int
    cue_names: List[str]
    motor_names: List[str]
    participants: List[str]
    n_keys: int

    def block(self, w: np.ndarray, name: str) -> np.ndarray:
        return w[self.slices[name]]


def _cue_columns(spec: ModelSpec) -> List[str]:
    """Names of the cue coefficients, in fitted order."""
    names = []
    for condition in CUED_CONDITIONS:
        if spec.cue_by_digit:
            for digit in (1, 2, 3, 4, 5):
                for feature in spec.cue:
                    if feature == "digit_distance":
                        continue  # kept condition-level: it is a shape, not a level
                    names.append(f"{feature}[{condition},d{digit}]")
            if "digit_distance" in spec.cue:
                names.append(f"digit_distance[{condition}]")
        else:
            names.extend(f"{feature}[{condition}]" for feature in spec.cue)
    return names


def _layout(spec: ModelSpec, participants: Sequence[str], n_keys: int) -> _Layout:
    cue_names = _cue_columns(spec)
    motor_names = list(spec.motor)
    blocks: List[Tuple[str, int]] = [
        ("cue", len(cue_names)),
        ("motor", len(motor_names)),
    ]
    if spec.reach:
        blocks.append(("reach", 2 * n_keys))
    if spec.prior:
        blocks.append(("prior", N_FINGERS))
        if spec.prior_by_participant:
            blocks.append(("prior_dev", len(participants) * N_FINGERS))
    if spec.prior_weight:
        blocks.append(("prior_weight", len(CUED_CONDITIONS)))
    slices, offset = {}, 0
    for name, size in blocks:
        slices[name] = slice(offset, offset + size)
        offset += size
    return _Layout(slices=slices, size=offset, cue_names=cue_names,
                   motor_names=motor_names, participants=list(participants),
                   n_keys=n_keys)


class _Problem:
    """The design arrays for one fit, plus utilities/gradients over them.

    Held as a class only because the arrays are big enough that rebuilding
    them per likelihood evaluation would dominate the fit; there is no
    state that outlives a fit.
    """

    def __init__(self, df: pd.DataFrame, spec: ModelSpec,
                 participants: Sequence[str], n_keys: int):
        self.spec = spec
        self.layout = _layout(spec, participants, n_keys)
        self.n = len(df)
        feats = candidate_features(df)
        condition = df["condition"].to_numpy()
        digit = df["target_digit"].to_numpy(int)

        cue_blocks = []
        for cond in CUED_CONDITIONS:
            in_cond = (condition == cond).astype(float)[:, None]
            if spec.cue_by_digit:
                for d in (1, 2, 3, 4, 5):
                    mask = in_cond * (digit == d).astype(float)[:, None]
                    for feature in spec.cue:
                        if feature == "digit_distance":
                            continue
                        cue_blocks.append(feats[feature] * mask)
                if "digit_distance" in spec.cue:
                    cue_blocks.append(feats["digit_distance"] * in_cond)
            else:
                cue_blocks.extend(feats[feature] * in_cond for feature in spec.cue)
        self.cue = (np.stack(cue_blocks, axis=-1) if cue_blocks
                    else np.zeros((self.n, N_FINGERS, 0)))
        self.motor = (np.stack([feats[f] for f in spec.motor], axis=-1) if spec.motor
                      else np.zeros((self.n, N_FINGERS, 0)))

        column = {"target": "key_target", "pressed": "key_pressed"}.get(spec.key_source)
        if column is None:
            raise EffectorModelError(
                f"unknown key_source {spec.key_source!r}; expected 'target' or 'pressed'")
        if column not in df.columns:      # frames built before both were kept
            column = "key"
        key = np.clip(df[column].to_numpy(int), 0, n_keys - 1)
        # Flat index into a (2, n_keys) reach table, per candidate.
        self.reach_index = (IS_RIGHT.astype(int)[None, :] * n_keys + key[:, None])
        index_of = {p: i for i, p in enumerate(participants)}
        self.participant = np.array([index_of.get(p, -1) for p in df["participant"]])
        self.n_participants = len(participants)
        self.y = np.array([FINGER_INDEX[f] for f in df["actual_finger"]])
        self.rows = np.arange(self.n)
        # Which events the prior weight applies to (B and C, one column each).
        self.cued_mask = np.stack([(condition == c).astype(float) for c in CUED_CONDITIONS], -1)
        self.is_cued = self.cued_mask.sum(1) > 0

        # Flattened scatter indices, built once: the gradient needs them on
        # every likelihood evaluation and rebuilding them there dominated
        # the objective.
        self._reach_flat = self.reach_index.ravel()
        seen = self.participant >= 0
        self._dev_seen = seen[:, None].astype(float)
        self._dev_flat = (np.clip(self.participant, 0, max(self.n_participants - 1, 0))[:, None]
                          * N_FINGERS + np.arange(N_FINGERS)[None, :]).ravel()

        self.penalty = np.full(self.layout.size, spec.l2)
        if "prior_dev" in self.layout.slices:
            self.penalty[self.layout.slices["prior_dev"]] = spec.l2_prior_dev
        if "prior_weight" in self.layout.slices:
            self.penalty[self.layout.slices["prior_weight"]] = 0.0

    # -- utilities ---------------------------------------------------

    def _prior_logits(self, w: np.ndarray) -> Optional[np.ndarray]:
        """Per-candidate habitual-prior logits BEFORE the w_c weighting."""
        if "prior" not in self.layout.slices:
            return None
        logits = np.broadcast_to(w[self.layout.slices["prior"]][None, :],
                                 (self.n, N_FINGERS)).copy()
        if "prior_dev" in self.layout.slices:
            dev = w[self.layout.slices["prior_dev"]].reshape(self.n_participants, N_FINGERS)
            here = np.where(self.participant[:, None] >= 0,
                            dev[np.clip(self.participant, 0, self.n_participants - 1)], 0.0)
            logits = logits + here
        return logits

    def _weights(self, w: np.ndarray) -> np.ndarray:
        """w_c per event: 1 in Condition A, the fitted scalar in B and C."""
        if "prior_weight" not in self.layout.slices:
            return np.ones(self.n)
        raw = w[self.layout.slices["prior_weight"]]
        return np.where(self.is_cued, self.cued_mask @ raw, 1.0)

    def utilities(self, w: np.ndarray) -> np.ndarray:
        u = np.zeros((self.n, N_FINGERS))
        if self.cue.shape[2]:
            u += self.cue @ w[self.layout.slices["cue"]]
        if self.motor.shape[2]:
            u += self.motor @ w[self.layout.slices["motor"]]
        if "reach" in self.layout.slices:
            u += w[self.layout.slices["reach"]][self.reach_index]
        prior = self._prior_logits(w)
        if prior is not None:
            u += self._weights(w)[:, None] * prior
        return u

    def prior_utilities(self, w: np.ndarray) -> np.ndarray:
        """Utilities with the cue and the transition terms removed.

        This is the distribution the habit alone would produce on this
        event, and it is what "how far is the cued finger from what this
        person would have done anyway" is measured against.  Reach stays
        in because a key the hand cannot reach is a physical fact, not a
        habit; the transition terms come out because they describe the
        movement being made, not the standing preference.
        """
        u = np.zeros((self.n, N_FINGERS))
        if "reach" in self.layout.slices:
            u += w[self.layout.slices["reach"]][self.reach_index]
        prior = self._prior_logits(w)
        if prior is not None:
            u += self._weights(w)[:, None] * prior
        return u

    # -- objective ---------------------------------------------------

    def objective(self, w: np.ndarray) -> Tuple[float, np.ndarray]:
        u = self.utilities(w)
        top = u.max(axis=1, keepdims=True)
        exp_u = np.exp(u - top)
        denom = exp_u.sum(axis=1, keepdims=True)
        probs = exp_u / denom
        loglik = float((u[self.rows, self.y] - (top[:, 0] + np.log(denom[:, 0]))).sum())

        upstream = probs.copy()             # d(-loglik)/d(utility)
        upstream[self.rows, self.y] -= 1.0
        grad = np.zeros(self.layout.size)
        if self.cue.shape[2]:
            grad[self.layout.slices["cue"]] = np.einsum("nf,nfp->p", upstream, self.cue)
        if self.motor.shape[2]:
            grad[self.layout.slices["motor"]] = np.einsum("nf,nfp->p", upstream, self.motor)
        if "reach" in self.layout.slices:
            # np.bincount rather than np.add.at: the same scatter-add over
            # 160k candidate rows, but roughly an order of magnitude
            # faster, and this runs on every one of the several hundred
            # likelihood evaluations in every one of several hundred
            # bootstrap resamples.
            grad[self.layout.slices["reach"]] = np.bincount(
                self._reach_flat, weights=upstream.ravel(),
                minlength=2 * self.layout.n_keys)
        prior = self._prior_logits(w)
        if prior is not None:
            weights = self._weights(w)
            scaled = upstream * weights[:, None]
            grad[self.layout.slices["prior"]] = scaled.sum(axis=0)
            if "prior_dev" in self.layout.slices:
                grad[self.layout.slices["prior_dev"]] = np.bincount(
                    self._dev_flat, weights=(scaled * self._dev_seen).ravel(),
                    minlength=self.n_participants * N_FINGERS)
            if "prior_weight" in self.layout.slices:
                per_event = (upstream * prior).sum(axis=1)
                grad[self.layout.slices["prior_weight"]] = self.cued_mask.T @ per_event

        penalised = -loglik + float((self.penalty * w ** 2).sum())
        return penalised, grad + 2.0 * self.penalty * w

    def probabilities(self, w: np.ndarray) -> np.ndarray:
        u = self.utilities(w)
        u = u - u.max(axis=1, keepdims=True)
        exp_u = np.exp(u)
        return exp_u / exp_u.sum(axis=1, keepdims=True)

    def prior_probabilities(self, w: np.ndarray) -> np.ndarray:
        u = self.prior_utilities(w)
        u = u - u.max(axis=1, keepdims=True)
        exp_u = np.exp(u)
        return exp_u / exp_u.sum(axis=1, keepdims=True)


# ---------------------------------------------------------------------------
# Fitting


@dataclass
class ChoiceFit:
    spec: ModelSpec
    params: np.ndarray
    layout: _Layout
    loglik: float
    n_events: int
    n_params: int
    converged: bool
    message: str
    participants: List[str]
    n_keys: int

    @property
    def aic(self) -> float:
        return 2 * self.n_params - 2 * self.loglik

    @property
    def bic(self) -> float:
        return self.n_params * np.log(self.n_events) - 2 * self.loglik

    def coefficients(self) -> pd.DataFrame:
        """The interpretable (non-nuisance) coefficients, in log-odds."""
        rows = []
        for name, value in zip(self.layout.cue_names,
                               self.layout.block(self.params, "cue")):
            rows.append({"block": "cue evidence", "parameter": name, "estimate": float(value)})
        for name, value in zip(self.layout.motor_names,
                               self.layout.block(self.params, "motor")):
            rows.append({"block": "motor", "parameter": name, "estimate": float(value)})
        if "prior_weight" in self.layout.slices:
            for cond, value in zip(CUED_CONDITIONS,
                                   self.layout.block(self.params, "prior_weight")):
                rows.append({"block": "prior weight", "parameter": f"w[{cond}]",
                             "estimate": float(value)})
        if "prior" in self.layout.slices:
            for finger, value in zip(FINGERS, self.layout.block(self.params, "prior")):
                rows.append({"block": "habitual prior", "parameter": f"prior[{finger}]",
                             "estimate": float(value)})
        return pd.DataFrame(rows)

    def participant_priors(self) -> pd.DataFrame:
        """Group prior + each participant's deviation, as probabilities on
        a neutral key (softmax of the finger intercepts alone)."""
        if "prior" not in self.layout.slices:
            return pd.DataFrame(columns=["participant", "finger", "logit", "probability"])
        base = self.layout.block(self.params, "prior")
        rows = []
        dev = (self.layout.block(self.params, "prior_dev")
               .reshape(len(self.participants), N_FINGERS)
               if "prior_dev" in self.layout.slices else
               np.zeros((len(self.participants), N_FINGERS)))
        for i, participant in enumerate(self.participants):
            logits = base + dev[i]
            probs = np.exp(logits - logits.max())
            probs = probs / probs.sum()
            for finger, logit, p in zip(FINGERS, logits, probs):
                rows.append({"participant": participant, "finger": finger,
                             "logit": float(logit), "probability": float(p)})
        return pd.DataFrame(rows)


def parameter_bounds(layout: "_Layout", spec: ModelSpec):
    """Box constraints for L-BFGS-B: the cue block only, everything else
    free.  See ModelSpec.cue_bound for why the bound is stated rather
    than left to the ridge guard."""
    bounds = [(None, None)] * layout.size
    for block, bound in (("cue", spec.cue_bound), ("reach", spec.reach_bound)):
        if block in layout.slices:
            for index in range(layout.slices[block].start, layout.slices[block].stop):
                bounds[index] = (-bound, bound)
    return bounds


def fit_choice_model(df: pd.DataFrame, spec: Optional[ModelSpec] = None,
                     participants: Optional[Sequence[str]] = None,
                     n_keys: Optional[int] = None,
                     start: Optional[np.ndarray] = None) -> ChoiceFit:
    """Maximum penalised likelihood fit of one ModelSpec.

    Analytic gradients throughout: at ~260 parameters a finite-difference
    L-BFGS would need 260 extra likelihood evaluations per step and turn a
    one-second fit into minutes, which the cross-validation and bootstrap
    loops multiply by 20 and by several hundred.
    """
    spec = spec or ModelSpec()
    participants = list(participants) if participants is not None else sorted(df["participant"].unique())
    n_keys = int(n_keys if n_keys is not None else df["key"].max() + 1)
    problem = _Problem(df, spec, participants, n_keys)

    x0 = np.zeros(problem.layout.size) if start is None else np.asarray(start, float).copy()
    if start is None and "prior_weight" in problem.layout.slices:
        # Start from "the habit acts at full strength", so the fitted value
        # is a move away from a stated null rather than away from zero.
        x0[problem.layout.slices["prior_weight"]] = 1.0
    result = minimize(problem.objective, x0, jac=True, method="L-BFGS-B",
                      bounds=parameter_bounds(problem.layout, spec),
                      options={"maxiter": 5000, "maxfun": 15000})
    params = result.x
    probs = problem.probabilities(params)
    loglik = float(np.log(np.clip(probs[problem.rows, problem.y], 1e-300, None)).sum())
    return ChoiceFit(spec=spec, params=params, layout=problem.layout, loglik=loglik,
                     n_events=problem.n, n_params=problem.layout.size,
                     converged=bool(result.success), message=str(result.message),
                     participants=participants, n_keys=n_keys)


def predict(fit: ChoiceFit, df: pd.DataFrame, use_participant_prior: bool = True
            ) -> Tuple[np.ndarray, np.ndarray]:
    """(choice probabilities, prior-only probabilities) for these events
    under an existing fit.

    use_participant_prior=False zeroes the per-participant deviations,
    which is what a participant the model has never seen actually gets."""
    problem = _Problem(df, fit.spec, fit.participants, fit.n_keys)
    params = fit.params
    if not use_participant_prior and "prior_dev" in fit.layout.slices:
        params = params.copy()
        params[fit.layout.slices["prior_dev"]] = 0.0
    return problem.probabilities(params), problem.prior_probabilities(params)


def attach_predictions(fit: ChoiceFit, df: pd.DataFrame,
                       use_participant_prior: bool = True) -> pd.DataFrame:
    """df plus the model's per-event quantities.

    p_chosen      probability the model gave the finger actually used
    p_cued        probability it gave the cued finger (its predicted accuracy)
    entropy       entropy of the selection distribution
    conflict      -log prior probability of the CUED finger: how much
                  standing habit the cue has to overcome on this event.
                  Structurally 0 in Condition A, where no finger is cued.
    """
    probs, prior_probs = predict(fit, df, use_participant_prior)
    rows = np.arange(len(df))
    chosen = np.array([FINGER_INDEX[f] for f in df["actual_finger"]])
    cued = np.array([FINGER_INDEX[f] for f in df["target_finger"]])
    out = df.copy()
    out["p_chosen"] = probs[rows, chosen]
    out["p_cued"] = probs[rows, cued]
    out["prior_p_cued"] = prior_probs[rows, cued]
    out["entropy"] = -(probs * np.log(np.clip(probs, 1e-300, None))).sum(axis=1)
    out["conflict"] = -np.log(np.clip(prior_probs[rows, cued], 1e-12, None))
    out.loc[out["condition"] == "A", "conflict"] = 0.0
    ordered = np.sort(probs, axis=1)
    out["margin"] = ordered[:, -1] - ordered[:, -2]
    out["predicted_finger"] = [FINGERS[i] for i in probs.argmax(axis=1)]
    return out


# ---------------------------------------------------------------------------
# Validation
#
# Every split here is by PARTICIPANT or by TRIAL, never by event.  Events
# inside one trial share a sequence, a hand position and a person, so a
# random event split would put near-duplicate rows on both sides and
# report a generalisation that does not exist.


def _fold_scores(fit: ChoiceFit, held_out: pd.DataFrame,
                 use_participant_prior: bool) -> List[dict]:
    probs, _ = predict(fit, held_out, use_participant_prior)
    rows = np.arange(len(held_out))
    chosen = np.array([FINGER_INDEX[f] for f in held_out["actual_finger"]])
    loglik = np.log(np.clip(probs[rows, chosen], 1e-300, None))
    hit = probs.argmax(axis=1) == chosen
    condition = held_out["condition"].to_numpy()
    out = []
    for cond in CONDITIONS:
        mask = condition == cond
        if not mask.any():
            continue
        out.append({"condition": cond, "n": int(mask.sum()),
                    "loglik": float(loglik[mask].sum()), "hits": int(hit[mask].sum())})
    return out


def leave_one_participant_out(df: pd.DataFrame, spec: Optional[ModelSpec] = None,
                              use_participant_prior: bool = False,
                              progress=None) -> pd.DataFrame:
    """Fit on every participant but one, score that one.

    use_participant_prior=False is the honest default: a participant the
    model has not seen has no fitted deviation, so they get the group
    prior.  Setting it True is only meaningful together with
    condition_a_transfer(), which supplies the held-out person's prior
    from their OWN Condition A events - data the study already collected,
    not data anyone has to collect.
    """
    spec = spec or ModelSpec()
    participants = sorted(df["participant"].unique())
    n_keys = int(df["key"].max() + 1)
    if len(participants) < 2:
        raise EffectorModelError("leave-one-participant-out needs at least 2 participants")
    rows = []
    for participant in participants:
        if progress is not None:
            progress(participant)
        train = df[df["participant"] != participant]
        test = df[df["participant"] == participant]
        others = [p for p in participants if p != participant]
        fit = fit_choice_model(train, spec, participants=others, n_keys=n_keys)
        for score in _fold_scores(fit, test, use_participant_prior):
            rows.append({"participant": participant, **score})
    return pd.DataFrame(rows)


def leave_one_trial_out(df: pd.DataFrame, spec: Optional[ModelSpec] = None,
                        n_folds: int = 5, seed: int = 20260827,
                        progress=None) -> pd.DataFrame:
    """K-fold over TRIALS, participants held constant across folds.

    This is the split that can judge the per-participant prior: the
    held-out trials belong to people the model has seen, so a personal
    habit that generalises within a person shows up here, where
    leave-one-participant-out cannot see it by construction.
    """
    spec = spec or ModelSpec()
    participants = sorted(df["participant"].unique())
    n_keys = int(df["key"].max() + 1)
    trials = df[["participant", "trial_index"]].drop_duplicates().reset_index(drop=True)
    rng = np.random.default_rng(seed)
    trials["fold"] = rng.integers(0, n_folds, len(trials))
    tagged = df.merge(trials, on=["participant", "trial_index"], how="left")
    rows = []
    for fold in range(n_folds):
        if progress is not None:
            progress(f"fold {fold + 1}/{n_folds}")
        train = tagged[tagged["fold"] != fold]
        test = tagged[tagged["fold"] == fold]
        if train.empty or test.empty:
            continue
        fit = fit_choice_model(train, spec, participants=participants, n_keys=n_keys)
        for score in _fold_scores(fit, test, use_participant_prior=True):
            rows.append({"fold": fold, **score})
    return pd.DataFrame(rows)


def summarise_cv(scores: pd.DataFrame) -> pd.DataFrame:
    """Per-condition held-out log-likelihood per event and top-1 accuracy.

    Both are reported because they answer different questions and only one
    of them discriminates: top-1 accuracy saturates once any cue term is
    present (the cued finger is usually chosen whatever the model believes
    about the alternatives), so model comparison rests on the
    log-likelihood, which scores the whole distribution including where
    the errors go."""
    if scores.empty:
        return pd.DataFrame(columns=["condition", "n", "loglik_per_event", "top1_accuracy"])
    grouped = scores.groupby("condition", sort=True).agg(
        n=("n", "sum"), loglik=("loglik", "sum"), hits=("hits", "sum")).reset_index()
    grouped["loglik_per_event"] = grouped["loglik"] / grouped["n"]
    grouped["top1_accuracy"] = grouped["hits"] / grouped["n"]
    return grouped[["condition", "n", "loglik_per_event", "top1_accuracy"]]


def compare_models(df: pd.DataFrame, specs: Sequence[ModelSpec],
                   cross_validate: bool = True, progress=None) -> pd.DataFrame:
    """In-sample fit plus (optionally) leave-one-participant-out scores
    for each spec, one row per model.

    The progress hook is forwarded into the cross-validation loop, not
    just called once per model: the folds are where the minutes go - one
    model's twenty folds are twenty full fits - and reporting only per
    model would leave the caller silent for exactly the stretch it most
    needs to show something.
    """
    rows = []
    report = _progress_adapter(progress)
    done = 0
    for spec in specs:
        report(spec.name, done)
        done += 1
        fit = fit_choice_model(df, spec)
        row = {"model": spec.name, "n_params": fit.n_params, "loglik": fit.loglik,
               "AIC": fit.aic, "BIC": fit.bic, "converged": fit.converged}
        if cross_validate:
            def fold_progress(participant: str, _spec=spec) -> None:
                nonlocal done
                done += 1
                report(f"{_spec.name}: without {participant}", done)

            cv = summarise_cv(leave_one_participant_out(df, spec,
                                                        progress=fold_progress))
            for _, r in cv.iterrows():
                row[f"cv_loglik_{r['condition']}"] = r["loglik_per_event"]
                row[f"cv_top1_{r['condition']}"] = r["top1_accuracy"]
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Error structure: the prediction the model was never told about


def error_structure(fit: ChoiceFit, df: pd.DataFrame) -> pd.DataFrame:
    """Observed vs model-expected substitution counts, split into the
    classes the Discussion argues over.

    The expected counts marginalise the fitted distribution over every
    non-cued finger, so "cross-hand" and "homologous" are read off the
    prediction rather than fitted to.  No feature in the design matrix
    refers to either class: the model knows only whether a candidate
    matches the cued hand and whether it matches the cued digit, so
    agreement here is a genuine test of the factorisation.

    Observed counts use actual_finger, which is what the model predicts.
    The theta-rule verdict (finger_correct) forgives some adjacent-digit
    events as detector near-ties, so it counts fewer substitutions; that
    view is reported separately by genuine_error_structure().
    """
    probs, _ = predict(fit, df)
    cued = np.array([FINGER_INDEX[f] for f in df["target_finger"]])
    chosen = np.array([FINGER_INDEX[f] for f in df["actual_finger"]])
    condition = df["condition"].to_numpy()
    same_hand = IS_RIGHT[None, :] == IS_RIGHT[cued][:, None]
    same_digit = DIGIT[None, :] == DIGIT[cued][:, None]
    is_cued = np.arange(N_FINGERS)[None, :] == cued[:, None]

    cross = (~same_hand) & (~is_cued)
    homologous = (~same_hand) & same_digit
    within = same_hand & (~is_cued)

    rows = []
    for cond in CONDITIONS:
        mask = condition == cond
        if not mask.any():
            continue
        wrong = chosen[mask] != cued[mask]
        obs_cross = int((wrong & (IS_RIGHT[chosen[mask]] != IS_RIGHT[cued[mask]])).sum())
        obs_hom = int((wrong & (IS_RIGHT[chosen[mask]] != IS_RIGHT[cued[mask]])
                       & (DIGIT[chosen[mask]] == DIGIT[cued[mask]])).sum())
        rows.append({
            "condition": cond, "n_events": int(mask.sum()),
            "observed_substitutions": int(wrong.sum()),
            "predicted_substitutions": float((probs[mask] * (~is_cued[mask])).sum()),
            "observed_cross_hand": obs_cross,
            "predicted_cross_hand": float((probs[mask] * cross[mask]).sum()),
            "observed_homologous": obs_hom,
            "predicted_homologous": float((probs[mask] * homologous[mask]).sum()),
            "observed_within_hand": int(wrong.sum()) - obs_cross,
            "predicted_within_hand": float((probs[mask] * within[mask]).sum()),
        })
    return pd.DataFrame(rows)


def genuine_error_structure(df: pd.DataFrame) -> pd.DataFrame:
    """The observed confusion under the theta-rule definition the report
    already uses (a substitution counts only when finger_correct is also
    False), for reconciliation with the Group Analysis confusion tab."""
    rows = []
    for cond in CONDITIONS:
        sub = df[df["condition"] == cond]
        if sub.empty:
            continue
        wrong = sub[(sub["actual_finger"] != sub["target_finger"])
                    & (sub["finger_correct"] == False)]  # noqa: E712 (may be object dtype)
        cross = wrong[wrong["actual_hand"] != wrong["target_hand"]]
        rows.append({
            "condition": cond,
            "genuine_substitutions": len(wrong),
            "cross_hand": len(cross),
            "homologous": int((cross["actual_digit"] == cross["target_digit"]).sum()),
            "within_hand": len(wrong) - len(cross),
        })
    return pd.DataFrame(rows)


def confusion_matrix(fit: ChoiceFit, df: pd.DataFrame, condition: str
                     ) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """(observed, predicted) 10x10 cued-finger x used-finger counts."""
    sub = df[df["condition"] == condition]
    observed = pd.DataFrame(0.0, index=FINGERS, columns=FINGERS)
    for cued, used in zip(sub["target_finger"], sub["actual_finger"]):
        observed.loc[cued, used] += 1
    probs, _ = predict(fit, sub)
    predicted = pd.DataFrame(0.0, index=FINGERS, columns=FINGERS)
    cued_index = np.array([FINGER_INDEX[f] for f in sub["target_finger"]])
    for i, row in zip(cued_index, probs):
        predicted.iloc[i] += row
    return observed, predicted


# ---------------------------------------------------------------------------
# Participant-level parameters and the paired contrast
#
# The independent unit of every inferential statement in this study is the
# participant, and that does not change because the quantity is a model
# parameter rather than a mean.


def per_participant_cue_parameters(fit: ChoiceFit, df: pd.DataFrame,
                                   progress=None) -> pd.DataFrame:
    """Refit the CUE (and prior-weight) parameters for each participant
    separately, holding the reach table and the group prior at the pooled
    estimate.

    Holding reach fixed is what makes this usable.  A participant supplies
    ~780 events spread over 30 hand-by-key cells, so a per-participant
    reach table is estimated from a handful of events per cell and its
    noise leaks straight into the cue coefficients; the biomechanics of a
    keyboard are also not a personal parameter.  What stays free is
    exactly what the research question is about - how much evidence each
    cue supplied to this person - plus their own prior weight.

    The ridge guard is rescaled by this participant's share of the events.
    A penalty is a statement about the parameter, so leaving it at full
    strength against a twentieth of the likelihood would shrink every
    per-participant estimate towards zero relative to the pooled one and
    make the paired contrast systematically too small - which is a
    property of the arithmetic, not of the person.
    """
    if not fit.spec.cue:
        return pd.DataFrame()
    report = _progress_adapter(progress)
    rows = []
    for index, participant in enumerate(fit.participants):
        sub = df[df["participant"] == participant]
        if sub.empty:
            continue
        report(participant, index + 1)
        problem = _Problem(sub, fit.spec, fit.participants, fit.n_keys)
        problem.penalty = problem.penalty * (len(sub) / max(len(df), 1))
        free = np.zeros(fit.layout.size, dtype=bool)
        for block in ("cue", "motor", "prior_weight"):
            if block in fit.layout.slices:
                free[fit.layout.slices[block]] = True
        base = fit.params.copy()

        def objective(theta):
            w = base.copy()
            w[free] = theta
            value, grad = problem.objective(w)
            return value, grad[free]

        result = minimize(objective, base[free], jac=True, method="L-BFGS-B",
                          options={"maxiter": 2000, "maxfun": 6000})
        w = base.copy()
        w[free] = result.x
        row = {"participant": participant, "n_events": len(sub),
               "converged": bool(result.success)}
        for name, value in zip(fit.layout.cue_names, w[fit.layout.slices["cue"]]):
            row[name] = float(value)
        if "prior_weight" in fit.layout.slices:
            for cond, value in zip(CUED_CONDITIONS, w[fit.layout.slices["prior_weight"]]):
                row[f"w[{cond}]"] = float(value)
        rows.append(row)
    return pd.DataFrame(rows)


def paired_parameter_contrast(per_participant: pd.DataFrame,
                              pairs: Optional[Sequence[Tuple[str, str]]] = None
                              ) -> pd.DataFrame:
    """Within-participant C - B contrast on each cue parameter.

    Paired over participants, so n is the number of people, not the number
    of events - the same rule the rest of the study's inference follows.
    Reports the mean difference, its 95% t interval, Cohen's dz and the
    count of participants moving in each direction; the p value is one
    column among those, not the result.
    """
    from scipy import stats as sstats

    if per_participant.empty:
        return pd.DataFrame()
    if pairs is None:
        pairs = []
        for column in per_participant.columns:
            if column.endswith("[B]"):
                partner = column[:-3] + "[C]"
                if partner in per_participant.columns:
                    pairs.append((column, partner))
            elif "[B," in column:
                partner = column.replace("[B,", "[C,")
                if partner in per_participant.columns:
                    pairs.append((column, partner))
    rows = []
    for b_col, c_col in pairs:
        b = per_participant[b_col].to_numpy(float)
        c = per_participant[c_col].to_numpy(float)
        diff = c - b
        n = len(diff)
        mean = float(diff.mean())
        sd = float(diff.std(ddof=1)) if n > 1 else np.nan
        sem = sd / np.sqrt(n) if n > 1 else np.nan
        half = float(sstats.t.ppf(0.975, n - 1)) * sem if n > 1 else np.nan
        t_stat, p_value = (sstats.ttest_rel(c, b) if n > 1 else (np.nan, np.nan))
        try:
            _, wilcoxon_p = sstats.wilcoxon(c, b)
        except ValueError:
            wilcoxon_p = np.nan
        rows.append({
            "parameter": b_col.replace("[B]", "").replace("[B,", "["),
            "mean_B": float(b.mean()), "mean_C": float(c.mean()),
            "mean_difference": mean, "sd_difference": sd,
            "ci95_lo": mean - half, "ci95_hi": mean + half,
            "dz": mean / sd if sd else np.nan,
            "t": float(t_stat), "df": n - 1, "p": float(p_value),
            "wilcoxon_p": float(wilcoxon_p),
            "n_participants": n, "n_higher_in_C": int((diff > 0).sum()),
        })
    return pd.DataFrame(rows)


def bootstrap_parameters(df: pd.DataFrame, spec: Optional[ModelSpec] = None,
                         n_resamples: int = 400, seed: int = 20260827,
                         progress=None) -> pd.DataFrame:
    """Percentile intervals for the interpretable coefficients, resampling
    PARTICIPANTS with replacement.

    Clustering the resample on the participant is the point: events within
    a person are not independent, so an event-level bootstrap would give
    intervals several times too narrow.  Each resample warm-starts from
    the full-data solution, which is what keeps a few hundred refits to
    seconds rather than minutes.
    """
    spec = spec or ModelSpec()
    participants = sorted(df["participant"].unique())
    n_keys = int(df["key"].max() + 1)
    full = fit_choice_model(df, spec, participants=participants, n_keys=n_keys)
    names = list(full.layout.cue_names) + list(full.layout.motor_names)
    if "prior_weight" in full.layout.slices:
        names += [f"w[{c}]" for c in CUED_CONDITIONS]

    def interpretable(fit: ChoiceFit) -> np.ndarray:
        parts = [fit.layout.block(fit.params, "cue"), fit.layout.block(fit.params, "motor")]
        if "prior_weight" in fit.layout.slices:
            parts.append(fit.layout.block(fit.params, "prior_weight"))
        return np.concatenate([p for p in parts if p.size]) if names else np.array([])

    rng = np.random.default_rng(seed)
    report = _progress_adapter(progress)
    draws: List[np.ndarray] = []
    uninformative: List[Dict[str, int]] = []
    for i in range(n_resamples):
        # Every resample, not every twenty-fifth: this is the slowest
        # loop in the application and the caller needs a per-resample
        # count to show a position and a time estimate. The progress
        # callback takes (message, done) so it can compute a fraction;
        # callbacks that only want the text ignore the second argument.
        report(f"resample {i + 1}/{n_resamples}", i + 1)
        picked = rng.choice(participants, size=len(participants), replace=True)
        # Relabel duplicates so a participant drawn twice contributes two
        # independent prior deviations rather than one shared with itself.
        frames = []
        for j, participant in enumerate(picked):
            block = df[df["participant"] == participant].copy()
            block["participant"] = f"{participant}#{j}"
            frames.append(block)
        resampled = pd.concat(frames, ignore_index=True)
        labels = sorted(resampled["participant"].unique())
        # Warm start on everything, including the participant deviations.
        # A resampled participant is a RELABELLED COPY of a known original,
        # so its deviation is already estimated - copying it in starts the
        # optimiser next to the answer instead of at zero, which is the
        # difference between a fit that takes seconds and one that takes a
        # fraction of a second, over hundreds of resamples.
        start = full.params.copy()
        if "prior_dev" in full.layout.slices:
            source = full.layout.block(full.params, "prior_dev").reshape(
                len(full.participants), N_FINGERS)
            index_of = {p: i for i, p in enumerate(full.participants)}
            warm = np.zeros((len(labels), N_FINGERS))
            for i, label in enumerate(labels):
                origin = label.split("#")[0]
                if origin in index_of:
                    warm[i] = source[index_of[origin]]
            start[full.layout.slices["prior_dev"]] = warm.ravel()
        fit = fit_choice_model(resampled, spec, participants=labels, n_keys=n_keys,
                               start=start)
        draws.append(interpretable(fit))
        # Whether THIS resample contained the events that identify each cue
        # channel.  Resampling participants can drop every cross-hand
        # action under Condition C - the study has two, in two people - and
        # that resample's kappa_hand[C] is then not a maximum but wherever
        # the guard stopped it.  Counting those is the only way the
        # interval can say which end of it is real.
        uninformative.append(_uninformative_channels(resampled))
    draws = np.vstack(draws) if draws else np.zeros((0, len(names)))
    point = interpretable(full)
    # How many resamples carried no event capable of identifying each
    # coefficient.  Those draws are not maxima, so the percentile they
    # land in is not an estimate: an interval built partly from them is a
    # bound above, and the "reading" column says so rather than leaving a
    # number that looks like the other rows.
    unidentified = np.array([sum(u.get(name, 0) for u in uninformative) for name in names])
    return pd.DataFrame({
        "parameter": names,
        "estimate": point,
        "boot_mean": draws.mean(axis=0) if len(draws) else np.nan,
        "boot_sd": draws.std(axis=0, ddof=1) if len(draws) > 1 else np.nan,
        "ci95_lo": np.percentile(draws, 2.5, axis=0) if len(draws) else np.nan,
        "ci95_hi": np.percentile(draws, 97.5, axis=0) if len(draws) else np.nan,
        "n_resamples": len(draws),
        "n_resamples_unidentified": unidentified,
        "reading": ["upper end is a bound, not an estimate" if n else "interval"
                    for n in unidentified],
    })


def _uninformative_channels(df: pd.DataFrame) -> Dict[str, int]:
    """1 for each cue coefficient this sample cannot identify at all."""
    out: Dict[str, int] = {}
    diagnostics = identifiability_diagnostics(df)
    for row in diagnostics.itertuples():
        out[row.parameter] = int(row.n_identifying == 0)
    return out


# ---------------------------------------------------------------------------
# Does Condition A predict Conditions B and C?
#
# This is the question Condition A exists to answer, and it is answerable
# from data already on disk: fit the habit on a participant's OWN key-only
# trials, then predict that same participant's cued trials without the
# model ever having seen them.


def condition_a_transfer(df: pd.DataFrame, spec: Optional[ModelSpec] = None,
                         progress=None) -> pd.DataFrame:
    """Leave-one-participant-out, three ways of supplying the held-out
    person's habitual prior:

      group          the group prior only - no personal habit;
      own_A          their own deviation, estimated from their Condition A
                     events alone, entering B and C at the fitted weight
                     w_c (~0.35, i.e. attenuated);
      own_A_full     the same deviation forced in at full strength
                     (w_c = 1), the naive form of "the habit carries over".

    own_A beating group is the transfer result.  own_A_full is included
    because it is the version that fails: applied at full strength the
    free-choice habit over-predicts, and reading that failure as "the
    prior does not transfer" would be wrong.  The three columns are what
    separates those two conclusions.
    """
    spec = spec or ModelSpec()
    participants = sorted(df["participant"].unique())
    if len(participants) < 2:
        raise EffectorModelError("Condition A transfer needs at least 2 participants")
    n_keys = int(df["key"].max() + 1)
    rows = []
    for participant in participants:
        if progress is not None:
            progress(participant)
        train = df[df["participant"] != participant]
        others = [p for p in participants if p != participant]
        fit = fit_choice_model(train, spec, participants=others, n_keys=n_keys)

        held = df[df["participant"] == participant]
        held_a = held[held["condition"] == "A"]
        held_cued = held[held["condition"].isin(CUED_CONDITIONS)]
        if held_a.empty or held_cued.empty:
            continue

        # Estimate this person's prior deviation from their Condition A
        # events only, with every other parameter frozen at the group fit.
        extended = others + [participant]
        problem_a = _Problem(held_a, spec, extended, n_keys)
        params = _extend_participants(fit, extended)
        dev_slice = problem_a.layout.slices.get("prior_dev")
        own = np.zeros(N_FINGERS)
        if dev_slice is not None:
            offset = dev_slice.start + (len(extended) - 1) * N_FINGERS

            def objective(theta):
                w = params.copy()
                w[offset:offset + N_FINGERS] = theta
                value, grad = problem_a.objective(w)
                return value, grad[offset:offset + N_FINGERS]

            own = minimize(objective, np.zeros(N_FINGERS), jac=True, method="L-BFGS-B",
                           options={"maxiter": 1000}).x

        scores = {}
        for label, deviation, full_weight in (("group", np.zeros(N_FINGERS), False),
                                              ("own_A", own, False),
                                              ("own_A_full", own, True)):
            w = params.copy()
            if dev_slice is not None:
                offset = dev_slice.start + (len(extended) - 1) * N_FINGERS
                w[offset:offset + N_FINGERS] = deviation
            if full_weight and "prior_weight" in problem_a.layout.slices:
                w[problem_a.layout.slices["prior_weight"]] = 1.0
            problem = _Problem(held_cued, spec, extended, n_keys)
            probs = problem.probabilities(w)
            chosen = np.array([FINGER_INDEX[f] for f in held_cued["actual_finger"]])
            scores[label] = float(np.log(np.clip(
                probs[np.arange(len(held_cued)), chosen], 1e-300, None)).mean())
        rows.append({"participant": participant, "n_cued_events": len(held_cued),
                     "loglik_group": scores["group"],
                     "loglik_own_A": scores["own_A"],
                     "loglik_own_A_full": scores["own_A_full"],
                     "gain_own_A": scores["own_A"] - scores["group"],
                     "gain_own_A_full": scores["own_A_full"] - scores["group"]})
    return pd.DataFrame(rows)


def _extend_participants(fit: ChoiceFit, participants: Sequence[str]) -> np.ndarray:
    """The fitted vector re-laid-out for one extra participant, whose
    prior deviation starts at zero."""
    layout = _layout(fit.spec, participants, fit.n_keys)
    out = np.zeros(layout.size)
    for block, target in layout.slices.items():
        source = fit.layout.slices.get(block)
        if source is None:
            continue
        values = fit.params[source]
        out[target.start:target.start + len(values)] = values
    return out


def transfer_summary(transfer: pd.DataFrame) -> pd.DataFrame:
    """Paired test over participants on each transfer gain."""
    from scipy import stats as sstats

    rows = []
    for column, label in (("gain_own_A", "own Condition-A prior, fitted weight"),
                          ("gain_own_A_full", "own Condition-A prior at full weight")):
        values = transfer[column].to_numpy(float)
        n = len(values)
        if n < 2:
            continue
        mean = float(values.mean())
        sd = float(values.std(ddof=1))
        half = float(sstats.t.ppf(0.975, n - 1)) * sd / np.sqrt(n)
        t_stat, p_value = sstats.ttest_1samp(values, 0.0)
        rows.append({"variant": label, "mean_gain_per_event": mean,
                     "ci95_lo": mean - half, "ci95_hi": mean + half,
                     "dz": mean / sd if sd else np.nan,
                     "t": float(t_stat), "df": n - 1, "p": float(p_value),
                     "n_participants": n, "n_improved": int((values > 0).sum())})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Descriptive reference tables


def free_choice_prior(df: pd.DataFrame) -> pd.DataFrame:
    """Observed Condition-A finger usage per participant - the raw form of
    what the prior block estimates, kept for the figures and as a check
    that the fitted prior has not drifted from the data."""
    a = df[df["condition"] == "A"]
    counts = (a.groupby(["participant", "actual_finger"]).size()
              .unstack(fill_value=0).reindex(columns=FINGERS, fill_value=0))
    shares = counts.div(counts.sum(axis=1), axis=0)
    out = shares.stack().reset_index()
    out.columns = ["participant", "finger", "share"]
    out["count"] = counts.stack().reset_index(drop=True)
    return out


def observed_vs_predicted_by_digit(fit: ChoiceFit, df: pd.DataFrame) -> pd.DataFrame:
    """Per cued digit and condition: observed rate of using the cued
    finger, and the model's mean predicted probability of doing so."""
    scored = attach_predictions(fit, df)
    scored = scored[scored["condition"].isin(CUED_CONDITIONS)]
    out = (scored.groupby(["condition", "target_digit"])
           .agg(n=("p_cued", "size"),
                observed=("correct_finger", "mean"),
                predicted=("p_cued", "mean")).reset_index())
    out["digit_name"] = out["target_digit"].map(DIGIT_NAMES)
    return out


def observed_vs_predicted_by_participant(fit: ChoiceFit, df: pd.DataFrame) -> pd.DataFrame:
    """Same calibration check with the participant as the unit."""
    scored = attach_predictions(fit, df)
    return (scored.groupby(["participant", "condition"])
            .agg(n=("p_cued", "size"),
                 observed=("correct_finger", "mean"),
                 predicted=("p_cued", "mean")).reset_index())


# ---------------------------------------------------------------------------
# What the data can and cannot pin down
#
# A coefficient is only as determined as the events that distinguish it.
# For hand evidence those events are the cross-hand actions: if a
# condition produced none, the likelihood rises monotonically in
# kappa_hand and there is no maximum, and if it produced two the maximum
# is real but the upper tail is nearly flat.  Recovery simulations on
# synthetic data with a KNOWN kappa (test-script/test_effector_model.py)
# show accurate recovery while cross-hand actions stay in the handful and
# estimates scattering from 6.9 to 10.7 for the same true value of 8 once
# they fall to zero or one.  Condition C in this study has two.
#
# The consequence is directional and worth stating rather than hiding:
# the ceiling on kappa_hand[C] is weak, so the C - B contrast is a LOWER
# BOUND on the hand-evidence difference.  The sign and the ordering rest
# on 67 cross-hand actions against 2, which is not a fragile comparison.


IDENTIFYING_EVENTS = {
    "match_hand": "actions using the hand that was not cued",
    "match_digit": "actions using the cued digit on either hand",
    "digit_distance": "actions on a digit adjacent to or further from the cued one",
    "match_finger": "actions not using the cued finger",
}


def identifiability_diagnostics(df: pd.DataFrame, min_events: int = 10) -> pd.DataFrame:
    """Per condition and cue parameter: how many events actually carry
    information about it, and whether that is enough to read the estimate
    as a value or only as a bound."""
    rows = []
    for condition in CUED_CONDITIONS:
        sub = df[df["condition"] == condition]
        if sub.empty:
            continue
        wrong = sub[sub["actual_finger"] != sub["target_finger"]]
        counts = {
            "match_hand": int((wrong["actual_hand"] != wrong["target_hand"]).sum()),
            "match_digit": int((sub["actual_digit"] == sub["target_digit"]).sum()
                               - (sub["actual_finger"] == sub["target_finger"]).sum()),
            "digit_distance": int((wrong["actual_hand"] == wrong["target_hand"]).sum()),
            "match_finger": int(len(wrong)),
        }
        for parameter, n in counts.items():
            rows.append({
                "condition": condition,
                "parameter": f"{parameter}[{condition}]",
                "identifying_events": IDENTIFYING_EVENTS[parameter],
                "n_identifying": n,
                "n_condition_events": len(sub),
                "verdict": ("estimated" if n >= min_events else
                            "lower bound only" if n > 0 else
                            "not identified (no informative events)"),
            })
    return pd.DataFrame(rows)


def fit_summary(fit: ChoiceFit, df: pd.DataFrame) -> pd.DataFrame:
    """One-row-per-fact provenance block for the export and the caption."""
    return pd.DataFrame([
        {"item": "model", "value": fit.spec.name},
        {"item": "participants", "value": len(fit.participants)},
        {"item": "modelled events", "value": fit.n_events},
        {"item": "free parameters", "value": fit.n_params},
        {"item": "log-likelihood", "value": round(fit.loglik, 2)},
        {"item": "AIC", "value": round(fit.aic, 1)},
        {"item": "BIC", "value": round(fit.bic, 1)},
        {"item": "converged", "value": fit.converged},
        {"item": "events per participant (mean)",
         "value": round(fit.n_events / max(len(fit.participants), 1), 1)},
        {"item": "chance log-likelihood per event (10 fingers)",
         "value": round(float(np.log(1 / N_FINGERS)), 4)},
        {"item": "fitted log-likelihood per event",
         "value": round(fit.loglik / fit.n_events, 4)},
    ])


# ---------------------------------------------------------------------------
# Identification-robust test of the channel contrast
#
# The paired t-test over per-participant kappas is the study's usual form
# of inference, but it cannot be the primary test here: most participants
# produced NO cross-hand action under Condition C, so their individual
# kappa_hand[C] is a lower bound whose numeric value is set by the ridge
# guard rather than by their behaviour, and averaging bounds is not an
# estimate.
#
# What survives that problem is a comparison of MODELS rather than of
# parameter values.  Constrain the two conditions to supply the same
# amount of evidence on one channel and refit: if the data can be
# described that way the likelihood barely moves, and if they cannot the
# drop is large no matter where the unconstrained maximum sits.  The test
# is about whether the channels differ, which is the claim, and it does
# not need the size of the difference to be identified.


def constrained_contrast_test(df: pd.DataFrame, spec: Optional[ModelSpec] = None,
                              features: Sequence[str] = ("match_hand", "match_digit",
                                                         "digit_distance"),
                              progress=None) -> pd.DataFrame:
    """Likelihood-ratio test, per cue channel, of "B and C supply the same
    evidence on this channel".

    Each row refits the whole model with that one feature's B and C
    coefficients tied together (one free parameter instead of two) and
    compares against the unconstrained fit.  A large drop means the
    channel genuinely differs between the modalities; a negligible drop
    means the data are consistent with the two cues being equally
    informative on it.
    """
    from scipy import stats as sstats

    spec = spec or ModelSpec()
    participants = sorted(df["participant"].unique())
    n_keys = int(df["key"].max() + 1)
    full = fit_choice_model(df, spec, participants=participants, n_keys=n_keys)
    problem = _Problem(df, spec, participants, n_keys)
    names = full.layout.cue_names
    rows = []
    for feature in features:
        b_positions = [i for i, n in enumerate(names) if n.startswith(f"{feature}[B")]
        c_positions = [i for i, n in enumerate(names) if n.startswith(f"{feature}[C")]
        if not b_positions or len(b_positions) != len(c_positions):
            continue
        if progress is not None:
            progress(feature)
        cue_start = full.layout.slices["cue"].start
        pairs = [(cue_start + b, cue_start + c) for b, c in zip(b_positions, c_positions)]
        free = np.ones(full.layout.size, dtype=bool)
        for _, c_index in pairs:
            free[c_index] = False           # C is driven by B, not fitted

        def objective(theta):
            w = np.zeros(full.layout.size)
            w[free] = theta
            for b_index, c_index in pairs:
                w[c_index] = w[b_index]
            value, grad = problem.objective(w)
            tied = grad.copy()
            for b_index, c_index in pairs:
                tied[b_index] += grad[c_index]
            return value, tied[free]

        start = full.params[free].copy()
        result = minimize(objective, start, jac=True, method="L-BFGS-B",
                          options={"maxiter": 5000, "maxfun": 15000})
        w = np.zeros(full.layout.size)
        w[free] = result.x
        for b_index, c_index in pairs:
            w[c_index] = w[b_index]
        probs = problem.probabilities(w)
        loglik = float(np.log(np.clip(probs[problem.rows, problem.y], 1e-300, None)).sum())
        statistic = 2 * (full.loglik - loglik)
        df_test = len(pairs)
        rows.append({
            "channel": feature,
            "constraint": f"{feature}[B] = {feature}[C]",
            "loglik_free": full.loglik,
            "loglik_constrained": loglik,
            "delta_loglik": full.loglik - loglik,
            "chi2": statistic, "df": df_test,
            "p": float(sstats.chi2.sf(max(statistic, 0.0), df_test)),
            "delta_AIC": 2 * df_test - statistic,
        })
    return pd.DataFrame(rows)


def cross_hand_rates(df: pd.DataFrame) -> pd.DataFrame:
    """Per participant and condition, the observable that identifies
    kappa_hand: how often the action used the hand that was not cued.

    This is the raw quantity behind the channel claim, kept beside the
    fitted parameters because it needs no model and is what a reader
    should be able to check by counting.
    """
    rows = []
    for (participant, condition), sub in df.groupby(["participant", "condition"]):
        if condition == "A":
            continue
        cross = int((sub["actual_hand"] != sub["target_hand"]).sum())
        wrong = int((sub["actual_finger"] != sub["target_finger"]).sum())
        rows.append({"participant": participant, "condition": condition,
                     "n_events": len(sub), "n_substitutions": wrong,
                     "n_cross_hand": cross,
                     "cross_hand_rate": cross / len(sub) if len(sub) else np.nan})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Two-stage fit: the habit from Condition A, then how much of it survives
#
# The joint fit estimates the prior from all three conditions at once,
# which is efficient but makes "how much of the FREE-CHOICE habit still
# acts when a finger is named" partly circular - the cued trials help
# decide what the habit is.  Fitting the prior on Condition A alone and
# freezing it removes that: stage two can only rescale a habit it had no
# hand in shaping, so w_c answers the question it is supposed to answer.
#
# Both are reported.  They agree on the sign and the ordering and differ
# in magnitude, which is the expected direction: the joint prior has
# already been pulled towards the cued trials, so it needs less
# rescaling.


@dataclass
class TwoStageFit:
    prior_stage: ChoiceFit          # Condition A only, no cue term
    cue_stage: ChoiceFit            # B and C, prior frozen
    n_events_a: int
    n_events_cued: int

    def coefficients(self) -> pd.DataFrame:
        table = self.cue_stage.coefficients()
        return table[table["block"] != "habitual prior"].reset_index(drop=True)


def fit_two_stage(df: pd.DataFrame, spec: Optional[ModelSpec] = None) -> TwoStageFit:
    """Stage 1 on Condition A, stage 2 on B and C with the prior frozen."""
    spec = spec or ModelSpec()
    participants = sorted(df["participant"].unique())
    n_keys = int(df["key"].max() + 1)

    a_events = df[df["condition"] == "A"]
    cued_events = df[df["condition"].isin(CUED_CONDITIONS)]
    if a_events.empty:
        raise EffectorModelError("the two-stage fit needs Condition A events")
    if cued_events.empty:
        raise EffectorModelError("the two-stage fit needs Condition B/C events")

    prior_spec = ModelSpec(name=f"{spec.name} (prior stage, Condition A)", cue=(),
                           motor=spec.motor, reach=spec.reach, prior=spec.prior,
                           prior_by_participant=spec.prior_by_participant,
                           prior_weight=False, l2=spec.l2, l2_prior_dev=spec.l2_prior_dev)
    stage1 = fit_choice_model(a_events, prior_spec, participants=participants, n_keys=n_keys)

    stage2_problem = _Problem(cued_events, spec, participants, n_keys)
    start = np.zeros(stage2_problem.layout.size)
    for block in ("reach", "prior", "prior_dev"):
        if block in stage2_problem.layout.slices and block in stage1.layout.slices:
            start[stage2_problem.layout.slices[block]] = stage1.params[stage1.layout.slices[block]]
    if "prior_weight" in stage2_problem.layout.slices:
        start[stage2_problem.layout.slices["prior_weight"]] = 1.0

    free = np.zeros(stage2_problem.layout.size, dtype=bool)
    for block in ("cue", "motor", "prior_weight"):
        if block in stage2_problem.layout.slices:
            free[stage2_problem.layout.slices[block]] = True

    def objective(theta):
        w = start.copy()
        w[free] = theta
        value, grad = stage2_problem.objective(w)
        return value, grad[free]

    result = minimize(objective, start[free], jac=True, method="L-BFGS-B",
                      options={"maxiter": 5000, "maxfun": 15000})
    params = start.copy()
    params[free] = result.x
    probs = stage2_problem.probabilities(params)
    loglik = float(np.log(np.clip(
        probs[stage2_problem.rows, stage2_problem.y], 1e-300, None)).sum())
    stage2 = ChoiceFit(spec=spec, params=params, layout=stage2_problem.layout,
                       loglik=loglik, n_events=len(cued_events),
                       n_params=int(free.sum()), converged=bool(result.success),
                       message=str(result.message), participants=participants, n_keys=n_keys)
    return TwoStageFit(prior_stage=stage1, cue_stage=stage2,
                       n_events_a=len(a_events), n_events_cued=len(cued_events))


def prior_weight_bounds(df: pd.DataFrame, spec: Optional[ModelSpec] = None) -> pd.DataFrame:
    """The fitted prior weight against the two hypotheses it sits between.

    w = 0 is "naming a finger abolishes the standing preference"; w = 1 is
    "the preference is untouched and simply loses the competition".  Both
    are refits with w pinned, so the comparison is in log-likelihood
    rather than in whether a confidence interval happens to exclude a
    value.
    """
    spec = spec or ModelSpec()
    participants = sorted(df["participant"].unique())
    n_keys = int(df["key"].max() + 1)
    two_stage = fit_two_stage(df, spec)
    cued_events = df[df["condition"].isin(CUED_CONDITIONS)]
    problem = _Problem(cued_events, spec, participants, n_keys)
    weight_slice = problem.layout.slices.get("prior_weight")
    if weight_slice is None:
        return pd.DataFrame()

    free = np.zeros(problem.layout.size, dtype=bool)
    for block in ("cue", "motor"):
        if block in problem.layout.slices:
            free[problem.layout.slices[block]] = True

    def refit(pinned: float) -> float:
        base = two_stage.cue_stage.params.copy()
        base[weight_slice] = pinned

        def objective(theta):
            w = base.copy()
            w[free] = theta
            value, grad = problem.objective(w)
            return value, grad[free]

        result = minimize(objective, base[free], jac=True, method="L-BFGS-B",
                          options={"maxiter": 5000})
        w = base.copy()
        w[free] = result.x
        probs = problem.probabilities(w)
        return float(np.log(np.clip(probs[problem.rows, problem.y], 1e-300, None)).sum())

    fitted = two_stage.cue_stage.params[weight_slice]
    rows = [{"variant": "fitted w", "w_B": float(fitted[0]), "w_C": float(fitted[1]),
             "loglik": two_stage.cue_stage.loglik, "delta_loglik": 0.0}]
    for pinned, label in ((0.0, "w = 0 (habit abolished by the cue)"),
                          (1.0, "w = 1 (habit acts at full strength)")):
        loglik = refit(pinned)
        rows.append({"variant": label, "w_B": pinned, "w_C": pinned, "loglik": loglik,
                     "delta_loglik": two_stage.cue_stage.loglik - loglik})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Identified contrasts
#
# The raw coefficients are not all separately identified, but the
# quantities the argument actually uses are.  Writing a candidate's
# utility relative to the far cross-hand alternative:
#
#     cued finger            kappa_hand + kappa_digit
#     homologous, other hand              kappa_digit
#     same hand, next digit  kappa_hand              - gamma
#
# so the two interpretable contrasts are
#
#     hand advantage   = U(cued) - U(homologous)        = kappa_hand
#     digit sharpness  = U(cued) - U(same-hand neighbour) = kappa_digit + gamma
#
# and they are identified by DIFFERENT events: the first by cross-hand
# actions (67 in B, 2 in C), the second by within-hand actions (359 and
# 321).  That is why the digit channel can be reported confidently while
# kappa_digit on its own cannot: Condition C contains one cross-hand
# homologous action, which is what would separate kappa_digit from gamma.
#
# Report the contrasts, not the raw coefficients, wherever the claim is
# about what the cue supplied.


def evidence_contrasts(fit: ChoiceFit) -> pd.DataFrame:
    """The identified evidence contrasts per condition, in log-odds."""
    coefficients = fit.coefficients().set_index("parameter")["estimate"]
    rows = []
    for condition in CUED_CONDITIONS:
        try:
            hand = float(coefficients[f"match_hand[{condition}]"])
            digit = float(coefficients[f"match_digit[{condition}]"])
            gradient = float(coefficients[f"digit_distance[{condition}]"])
        except KeyError:
            continue
        rows.append({
            "condition": condition, "contrast": "hand advantage",
            "definition": "cued finger vs the homologous finger on the other hand",
            "log_odds": hand,
            "identified_by": "cross-hand actions",
        })
        rows.append({
            "condition": condition, "contrast": "digit sharpness",
            "definition": "cued finger vs the adjacent digit on the same hand",
            "log_odds": digit - gradient,
            "identified_by": "within-hand actions",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Plain-language headline
#
# The parameters are log-odds, which is the right scale to fit on and the
# wrong one to read off.  These convert the two identified contrasts into
# the form the finding is actually stated in - how strongly the cue
# favours the right effector over the one it is most likely to be
# confused with - so a summary can quote an odds ratio rather than ask
# anyone to exponentiate a coefficient in their head.


def headline_numbers(fit: ChoiceFit, df: pd.DataFrame) -> Dict[str, object]:
    """The few numbers a summary needs, already in readable units."""
    contrasts = evidence_contrasts(fit).set_index(["condition", "contrast"])
    diagnostics = identifiability_diagnostics(df).set_index("parameter")
    out: Dict[str, object] = {}
    for condition in CUED_CONDITIONS:
        for contrast in ("hand advantage", "digit sharpness"):
            key = (condition, contrast)
            if key not in contrasts.index:
                continue
            log_odds = float(contrasts.loc[key, "log_odds"])
            out[f"{contrast}|{condition}"] = log_odds
            # exp() of a log-odds advantage: "the cued finger is favoured
            # this many times over its closest competitor".
            out[f"{contrast}|{condition}|odds"] = float(np.exp(log_odds))
        parameter = f"match_hand[{condition}]"
        if parameter in diagnostics.index:
            out[f"cross_hand_events|{condition}"] = int(
                diagnostics.loc[parameter, "n_identifying"])
            out[f"hand_is_bound|{condition}"] = (
                diagnostics.loc[parameter, "verdict"] != "estimated")
    hand_b = out.get("hand advantage|B|odds")
    hand_c = out.get("hand advantage|C|odds")
    if hand_b and hand_c:
        out["hand_ratio"] = hand_c / hand_b
    digit_b = out.get("digit sharpness|B|odds")
    digit_c = out.get("digit sharpness|C|odds")
    if digit_b and digit_c:
        out["digit_ratio"] = digit_c / digit_b
    return out
