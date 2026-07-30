"""Comparing the adhesion methods - the offline half of the experiment.

`adhesion_vibration.py` measures ONE mount per run. This module takes
ANY NUMBER of those runs, groups them by adhesion method, averages each
group, and turns the result into one comparison: waveforms, rise,
steady-state strength, spectra, repeatability, and how much vibration
each method transfers RELATIVE to a chosen reference method.

SEVERAL RUNS PER METHOD, ON PURPOSE. The same adhesive applied twice is
not the same mount: layer thickness, contact area and how hard it was
pressed all move the transmitted vibration, and that spread can rival
the difference between two adhesives. So a method is characterised by
as many runs as the operator recorded, and mount-to-mount variation is
reported as a first-class number rather than hidden inside one run.

A RUN IS THE AVERAGING UNIT. A group's mean is the mean of its RUN
means, not the mean of all its trials: the trials inside one run are
repeated measurements of a single mount, so pooling them would weight a
5-trial run above a 3-trial one and would understate the real spread
(pseudo-replication). Two standard deviations are therefore reported and
never conflated:

    sd_between_runs   spread of the run means - MOUNT repeatability,
                      i.e. how reproducible applying this adhesive is
    sd_within_run     pooled spread of trials around their own run mean
                      - MEASUREMENT repeatability of one mount

With only one run in a group there is no between-run spread, so the
headline SD falls back to the within-run one and says so
(`sd_basis`).

WHAT IT REFUSES TO DO. The comparison only runs when the selection is
actually comparable:

  * at least two runs, covering at least two different adhesion methods
    (comparing a method with itself is not a comparison),
  * the same LRA drive frequency and amp,
  * the same motor port and accelerometer sensor id,
  * the same sampling settings (ACC stream interval) and the same
    vibration duration.

Any mismatch is reported by name, with each run's value, and nothing is
computed - a "comparison" of runs driven differently would be a
comparison of the drives, not of the adhesives. A method with no run at
all is simply left out of the comparison; it is never inferred.

RELATIVE, NOT ABSOLUTE. The numbers this module reports are RELATIVE
VIBRATION TRANSFER (or relative attenuation) with respect to one
reference method:

    rms_ratio        = mean_steady_vector_rms(method) / (reference)
    rms_change_pct   = 100 * (rms_ratio - 1)
    amplitude_ratio  = mean_amplitude_at_drive_freq(method) / (reference)
    dB               = 20 * log10(ratio)

They are deliberately NOT called transmissibility: the rig measures the
accelerometer side only, with no reference accelerometer at the
actuator's input, so no input/output transfer function is available.
Everything here is one mounted configuration compared with another.

STEADY-STATE STATISTICS USE SETTLED TRIALS ONLY. A trial that never
reached the steady band is stored with its steady-state numbers computed
over the window tail and flagged `window_tail_fallback`; those trials are
counted and reported, but excluded from the steady-state means, so a
start-up transient never enters a steady-state comparison.

Outputs (one Unix-epoch-seconds stamp <ts> for the whole analysis) in the
experiment's own folder,
data/validation_experiments/adhesion_vibration_comparison/:

  adhesion_comparison_<ts>_waveform.png   aligned envelopes, rise, shape
  adhesion_comparison_<ts>_spectrum.png   spectra, fundamental, harmonics
  adhesion_comparison_<ts>_metrics.png    strength, repeatability, dB
  adhesion_comparison_<ts>.csv            per-trial, per-run and
                                          per-method rows + relative
                                          figures
  adhesion_comparison_<ts>.meta.json      inputs, drive, analysis
                                          parameters, result summary

The input runs are never modified.
"""

import csv
import glob
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from ..acceleration_metrics import (
        MS2_PER_COUNT,
        SPECTRUM_MIN_HZ,
        SPECTRUM_TOLERANCE_HZ,
        counts_to_ms2,
    )
    from ..motor_acc_delay_experiment import motor_acc_delay as mad
    from .. import report
    from . import adhesion_vibration as av
except ImportError:  # direct execution rather than package import
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from acceleration_metrics import (
        MS2_PER_COUNT,
        SPECTRUM_MIN_HZ,
        SPECTRUM_TOLERANCE_HZ,
        counts_to_ms2,
    )
    from motor_acc_delay_experiment import motor_acc_delay as mad
    import report
    from adhesion_vibration_comparison import adhesion_vibration as av


#: The method every ratio is taken against unless the caller says
#: otherwise. Blu Tack is the project's usual mounting, so "the others
#: relative to Blu Tack" is the question this experiment exists to ask.
#: When the selection contains no run of it, the first selected method
#: is used instead (compare() says which in its summary).
REFERENCE_METHOD = "Blu Tack"

#: Bumped when the comparison's metric set or formulas change; recorded
#: in every analysis meta so a saved comparison says which version
#: produced it. 2 = several runs per method, run-level averaging and the
#: between/within-run SD split (1 = exactly one run per method).
ANALYSIS_VERSION = 2

#: A comparison needs at least this many runs, covering at least this
#: many different methods.
MIN_RUNS = 2
MIN_METHODS = 2

#: Waveform alignment: at the motor-on command (the same zero for every
#: trial, so onset differences stay visible) or at each trial's own
#: detected onset (which lines the RISES up and shows their shape).
ALIGN_COMMAND = "command"
ALIGN_ONSET = "onset"
ALIGNMENTS = (ALIGN_COMMAND, ALIGN_ONSET)
DEFAULT_ALIGNMENT = ALIGN_COMMAND

#: Rise-detail zoom on the waveform figure.
RISE_ZOOM_S = 0.30
#: Steady-state waveform-shape zoom (same width as the per-run figure's).
SHAPE_ZOOM_MS = av.WAVEFORM_ZOOM_MS

#: Which SD the headline figure of a group is based on.
SD_BETWEEN_RUNS = "between_runs"
SD_WITHIN_RUN = "within_run"

#: The series compared method-by-method, in report order. They are the
#: per-run series (adhesion_vibration.TRIAL_SERIES), so the comparison
#: can never define a metric the runs do not measure.
COMPARISON_SERIES: Tuple[av.Series, ...] = av.TRIAL_SERIES

#: The steady-state amplitude series a relative ratio is computed for. A
#: ratio only means something for a positive amplitude, which rules out
#: the timings (a settling-time "ratio" would be meaningless) and the
#: frequencies (a dominant-frequency ratio is not attenuation).
RATIO_SERIES: Tuple[str, ...] = ("steady_rms", "peak_robust", "peak_abs",
                                 "peak_to_peak", "fundamental")

#: One colour per method across every figure, so the methods are read
#: the same way in all of them.
METHOD_COLOURS: Dict[str, str] = {
    "Blu Tack": "tab:blue",
    "Double-sided tape": "tab:orange",
    "Cosmetic adhesive": "tab:green",
}

#: Parameters that must match across every selected run for the
#: comparison to mean anything: (parameter key, label).
COMPATIBILITY_KEYS: Tuple[Tuple[str, str], ...] = (
    ("frequency_hz", "LRA drive frequency (Hz)"),
    ("amp", "LRA drive amp"),
    ("motor_index", "motor port"),
    ("acc_sensor_id", "ACC sensor id"),
    ("acc_interval_ms", "ACC stream interval (ms)"),
    ("vib_duration_s", "vibration duration (s)"),
)

LogFn = Callable[[str], None]


# ==========================================
# Saved runs
# ==========================================

@dataclass
class RunInfo:
    """One saved run - one physical mount of one adhesive - as the
    selection list shows it and the compatibility check reads it."""
    csv_path: str
    adhesion_method: str
    saved_at: Optional[int]
    n_trials: int
    frequency_hz: Optional[float]
    amp: Optional[float]
    motor_index: Optional[int]
    acc_sensor_id: Optional[int]
    acc_interval_ms: Optional[float]
    vib_duration_s: Optional[float]
    notes: str = ""
    n_settled: int = 0

    @property
    def method_id(self) -> str:
        return av.method_id(self.adhesion_method)

    @property
    def name(self) -> str:
        return os.path.basename(self.csv_path)

    def parameter(self, key: str):
        return getattr(self, key, None)

    def label(self) -> str:
        """The one-line description the selection list shows."""
        parts = [
            report.format_epoch(self.saved_at) if self.saved_at
            else report.run_time_text(self.csv_path, None),
            self.adhesion_method,
            f"{self.n_trials} trials",
            f"{_fmt_number(self.frequency_hz, 0)} Hz",
            f"amp {_fmt_number(self.amp, 0)}",
            f"port {self.motor_index if self.motor_index is not None else '?'}",
            f"{_fmt_number(self.vib_duration_s, 1)} s",
        ]
        if self.notes:
            parts.append(self.notes)
        return "  ·  ".join(parts)


def _fmt_number(value, decimals: int = 1) -> str:
    return "?" if value is None else f"{float(value):.{decimals}f}"


def run_info(csv_path: str) -> RunInfo:
    """Read one saved run's identity and drive parameters.

    Prefers the run's .meta.json and falls back to the CSV rows, so a run
    whose meta was lost is still listed (and still checked) rather than
    silently dropped."""
    meta = av.load_meta(csv_path)
    params = (meta or {}).get("parameters", {})
    results = av.load_results(csv_path)
    first = results[0]
    return RunInfo(
        csv_path=csv_path,
        adhesion_method=av.normalise_method(
            (meta or {}).get("adhesion_method") or first.adhesion_method),
        saved_at=(meta or {}).get("saved_at") or report.stamp_from_path(csv_path),
        n_trials=len(results),
        frequency_hz=params.get("frequency_hz", first.frequency_hz),
        amp=params.get("amp", first.amp),
        motor_index=params.get("motor_index", first.motor_index),
        acc_sensor_id=params.get("acc_sensor_id", first.acc_sensor_id),
        acc_interval_ms=params.get("acc_interval_ms"),
        vib_duration_s=params.get("vib_duration_s", first.vib_duration_s),
        notes=params.get("notes", first.notes) or "",
        n_settled=sum(1 for r in results if r.settled),
    )


def list_runs(output_dir: Optional[str] = None) -> List[RunInfo]:
    """Every saved run in the output folder, NEWEST FIRST. Unreadable
    files are skipped rather than breaking the list."""
    runs: List[RunInfo] = []
    for path in av.run_csv_paths(output_dir):
        try:
            runs.append(run_info(path))
        except Exception:
            continue
    runs.reverse()
    return runs


def group_by_method(runs: Sequence[RunInfo]) -> Dict[str, List[RunInfo]]:
    """{method: [runs of it]}, in the canonical method order, containing
    only the methods that actually have a run."""
    grouped: Dict[str, List[RunInfo]] = {}
    for method in av.ADHESION_METHODS:
        matching = [r for r in runs if r.adhesion_method == method]
        if matching:
            grouped[method] = matching
    return grouped


# ==========================================
# Compatibility
# ==========================================

def check_selection(runs: Sequence[RunInfo]) -> List[str]:
    """Everything that makes this selection uncomparable, in plain words.

    An EMPTY list means the runs may be compared. Each message names the
    offending parameter and quotes every run's value, so the operator can
    see which run is the odd one out.

    Any number of runs per method is allowed - repeated runs of one
    adhesive are the point, since re-mounting is itself a source of
    variation - but at least two DIFFERENT methods must be present."""
    problems: List[str] = []
    if len(runs) < MIN_RUNS:
        problems.append(
            f"Select at least {MIN_RUNS} runs covering at least "
            f"{MIN_METHODS} different adhesion methods; "
            f"{len(runs)} selected.")

    methods = group_by_method(runs)
    if runs and len(methods) < MIN_METHODS:
        only = ", ".join(methods) or "none"
        problems.append(
            f"At least {MIN_METHODS} different adhesion methods are needed - "
            f"the selection only covers {only}. Comparing a method with "
            "itself is not a comparison.")

    if len(runs) >= 2:
        for key, label in COMPATIBILITY_KEYS:
            values = [r.parameter(key) for r in runs]
            known = [v for v in values if v is not None]
            listing = ", ".join(f"{r.adhesion_method} ({r.name}): "
                                f"{'?' if v is None else v}"
                                for r, v in zip(runs, values))
            if len(known) < len(values):
                problems.append(
                    f"{label}: not recorded in every selected run - {listing}"
                    ". A run without this parameter cannot be checked for "
                    "compatibility.")
                continue
            if len({round(float(v), 6) for v in known}) > 1:
                problems.append(
                    f"{label} differs between the selected runs: {listing}"
                    ". Every run in a comparison must be measured with "
                    "identical drive and sampling settings.")
    return problems


# ==========================================
# One run, and one method's group of runs
# ==========================================

@dataclass
class RunData:
    """One loaded run: one physical mount, its trials and its own
    per-run statistics (adhesion_vibration.run_stats)."""
    info: RunInfo
    results: List[av.AdhesionTrialResult]
    stats: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.info.name

    def mean_of(self, series: av.Series) -> Optional[float]:
        """This mount's mean for one series (None when it measured none -
        e.g. a steady-state series in a run where nothing settled)."""
        return self.stats.get(f"{series.prefix}_mean_{series.unit}")

    def trial_values(self, series: av.Series) -> List[Tuple[int, float]]:
        """(trial_id, value) - steady-state series over the SETTLED
        trials only, timing series over every trial."""
        source = (self.results if series.prefix not in av.STEADY_SERIES
                  else [r for r in self.results if r.settled])
        return [(r.trial_id, float(series.read(r))) for r in source
                if series.read(r) is not None]


@dataclass
class MethodGroup:
    """Every selected run of ONE adhesion method, averaged over runs."""
    method: str
    runs: List[RunData]
    stats: dict = field(default_factory=dict)

    @property
    def n_runs(self) -> int:
        return len(self.runs)

    @property
    def colour(self) -> str:
        return METHOD_COLOURS.get(self.method, "tab:gray")

    @property
    def results(self) -> List[av.AdhesionTrialResult]:
        """Every trial of every run in this group."""
        return [r for run in self.runs for r in run.results]

    @property
    def settled(self) -> List[av.AdhesionTrialResult]:
        return [r for r in self.results if r.settled]

    def run_means(self, series: av.Series) -> List[float]:
        """One mean per mount - the values the group mean averages."""
        return [value for value in (run.mean_of(series) for run in self.runs)
                if value is not None]

    def trial_values(self, series: av.Series) -> List[Tuple[int, int, float]]:
        """(run index, trial_id, value) over every run - the scatter
        behind the group mean."""
        return [(index, trial_id, value)
                for index, run in enumerate(self.runs)
                for trial_id, value in run.trial_values(series)]


def load_run(csv_path: str) -> RunData:
    """One run, ready to compare: trials, per-sample traces and stats."""
    results = av.load_results(csv_path)
    av.attach_traces(csv_path, results)
    return RunData(info=run_info(csv_path), results=results,
                   stats=av.run_stats(results))


def series_group_stats(group: MethodGroup, series: av.Series) -> dict:
    """One series' group statistics, with the two spreads kept apart.

    `mean` averages the RUN means (one weight per mount - see the module
    docstring); `sd_between_runs` is their spread and `sd_within_run` the
    pooled spread of trials around their own run's mean. The headline
    `sd`/`cv_percent` use the between-run spread when there are at least
    two runs, and the within-run one otherwise - `sd_basis` says which,
    so a single-run group is never read as if its mount repeatability
    had been measured."""
    run_means = group.run_means(series)
    trials = [value for _run, _trial, value in group.trial_values(series)]
    prefix, unit = series.prefix, series.unit

    stats: Dict[str, object] = {
        f"{prefix}_n": len(trials),                 # trials behind the mean
        f"{prefix}_n_runs": len(run_means),         # mounts behind the mean
        f"{prefix}_mean_{unit}": (statistics.fmean(run_means)
                                  if run_means else None),
        f"{prefix}_median_{unit}": (statistics.median(trials)
                                    if trials else None),
        f"{prefix}_min_{unit}": min(trials) if trials else None,
        f"{prefix}_max_{unit}": max(trials) if trials else None,
    }

    between = statistics.stdev(run_means) if len(run_means) >= 2 else None

    # Pooled within-run spread: each trial against ITS OWN run's mean, so
    # a difference between mounts never inflates it.
    residuals, contributing = [], 0
    for run in group.runs:
        values = [value for _trial, value in run.trial_values(series)]
        if len(values) < 2:
            continue
        centre = statistics.fmean(values)
        residuals.extend((v - centre) ** 2 for v in values)
        contributing += 1
    within = (math.sqrt(sum(residuals) / (len(residuals) - contributing))
              if residuals and len(residuals) > contributing else None)

    headline = between if between is not None else within
    basis = (SD_BETWEEN_RUNS if between is not None
             else (SD_WITHIN_RUN if within is not None else None))
    mean = stats[f"{prefix}_mean_{unit}"]
    stats.update({
        f"{prefix}_sd_{unit}": headline,
        f"{prefix}_sd_between_runs_{unit}": between,
        f"{prefix}_sd_within_run_{unit}": within,
        f"{prefix}_sd_basis": basis,
        f"{prefix}_cv_percent": (None if not mean or headline is None
                                 else 100.0 * headline / abs(mean)),
    })
    return stats


def group_stats(group: MethodGroup) -> dict:
    """Everything reported for one method: run/trial counts, detection
    statuses summed over its runs, and every series' group statistics."""
    results = group.results
    stats: Dict[str, object] = {
        "adhesion_method": group.method,
        "n_runs": group.n_runs,
        "n_trials": len(results),
        "n_ok": sum(1 for r in results if r.status == mad.STATUS_OK),
        "n_no_onset": sum(1 for r in results
                          if r.status == mad.STATUS_NO_ONSET),
        "n_not_settled": sum(1 for r in results
                             if r.status == mad.STATUS_NOT_SETTLED),
        "n_insufficient_data": sum(
            1 for r in results if r.status == mad.STATUS_INSUFFICIENT_DATA),
        "n_settled_steady_state": len(group.settled),
        "n_fallback_steady_state": sum(
            1 for r in results if r.steady_region_source == av.STEADY_FALLBACK),
        "runs": [run.name for run in group.runs],
    }
    for series in COMPARISON_SERIES:
        stats.update(series_group_stats(group, series))
    return stats


def load_groups(csv_paths: Sequence[str]) -> Dict[str, MethodGroup]:
    """{method: MethodGroup} for the selected runs, in canonical method
    order. Runs inside a group keep their chronological order."""
    runs = [load_run(path) for path in csv_paths]
    groups: Dict[str, MethodGroup] = {}
    for method in av.ADHESION_METHODS:
        matching = [r for r in runs if r.info.adhesion_method == method]
        if not matching:
            continue
        matching.sort(key=lambda r: (r.info.saved_at or 0, r.name))
        group = MethodGroup(method=method, runs=matching)
        group.stats = group_stats(group)
        groups[method] = group
    return groups


# ==========================================
# Aligned waveforms
# ==========================================

@dataclass
class MeanEnvelope:
    """The mean vibration envelope of one method on a common time grid,
    with the across-trial SD as the variation band."""
    times_s: np.ndarray
    mean_counts: np.ndarray
    sd_counts: np.ndarray
    n_trials: int
    n_runs: int
    alignment: str


def mean_envelope(group: MethodGroup, alignment: str = DEFAULT_ALIGNMENT,
                  n_points: int = 2000) -> Optional[MeanEnvelope]:
    """Average one method's trial envelopes - over every run of it - on a
    common grid.

    Aligned at the command (so a slower onset shows up as a later rise)
    or at each trial's own onset (so the rises are superimposed).
    Envelopes are interpolated onto the grid; no trial is stretched or
    resampled in time. The grid spans only the interval EVERY trial
    covers - starting at the latest first sample and ending at the
    earliest last one - so each averaged point is backed by all of them
    rather than by whichever trials happened to reach that far."""
    if alignment not in ALIGNMENTS:
        raise ValueError(f"alignment must be one of {ALIGNMENTS}")
    traces, runs_used = [], set()
    for index, run in enumerate(group.runs):
        for r in run.results:
            if not len(r.trace) or r.trace.envelope.size != len(r.trace):
                continue
            shift = 0.0
            if alignment == ALIGN_ONSET:
                if r.onset_latency_from_command_ms is None:
                    continue
                shift = r.onset_latency_from_command_ms / 1000.0
            traces.append((r.trace.rel_times - shift, r.trace.envelope))
            runs_used.add(index)
    if not traces:
        return None
    start = max(float(times[0]) for times, _ in traces)
    end = min(float(times[-1]) for times, _ in traces)
    if end <= start:
        return None
    grid = np.linspace(start, end, int(n_points))
    stack = np.vstack([np.interp(grid, times, envelope)
                       for times, envelope in traces])
    mean = np.mean(stack, axis=0)
    sd = (np.std(stack, axis=0, ddof=1) if stack.shape[0] >= 2
          else np.zeros_like(mean))
    return MeanEnvelope(times_s=grid, mean_counts=mean, sd_counts=sd,
                        n_trials=stack.shape[0], n_runs=len(runs_used),
                        alignment=alignment)


def _shape_window(group: MethodGroup):
    """A short steady-state slice of one representative trial, on the axis
    that carries the most vibration: (times_ms, values, axis name, run
    name, trial id). Used for the waveform-shape panel - a phase-random
    average across trials would cancel the carrier, so this shows ONE
    trial and says which."""
    candidates = [(run, r) for run in group.runs for r in run.results
                  if r.settled and len(r.trace)
                  and r.steady_start_rel_s is not None]
    if not candidates:
        candidates = [(run, r) for run in group.runs for r in run.results
                      if len(r.trace)]
    if not candidates:
        return None
    candidates.sort(key=lambda pair: (pair[1].steady_vector_rms_counts or 0.0))
    run, trial = candidates[len(candidates) // 2]
    start = (trial.steady_start_rel_s
             if trial.steady_start_rel_s is not None
             else float(trial.trace.rel_times[len(trial.trace) // 2]))
    start += 0.010
    mask = ((trial.trace.rel_times >= start)
            & (trial.trace.rel_times <= start + SHAPE_ZOOM_MS / 1000.0))
    if not np.any(mask):
        return None
    rms = [trial.steady_rms_x_counts or 0.0, trial.steady_rms_y_counts or 0.0,
           trial.steady_rms_z_counts or 0.0]
    axis = int(np.argmax(rms))
    return (trial.trace.rel_times[mask] * 1000.0,
            trial.trace.axes[mask, axis], "XYZ"[axis], run.name,
            trial.trial_id)


def mean_spectrum(group: MethodGroup):
    """The mean steady-state spectrum over every settled trial of every
    run of this method."""
    return av.mean_steady_spectrum(group.results)


# ==========================================
# Relative vibration transfer
# ==========================================

@dataclass
class RelativeFigure:
    """One method's amplitude relative to the reference method's, for one
    series. `db` is 20*log10(ratio) - an amplitude ratio, which is why it
    is 20 and not 10."""
    series: str
    method: str
    reference_method: str
    value: Optional[float]
    reference_value: Optional[float]
    ratio: Optional[float]
    percent_change: Optional[float]
    db: Optional[float]


def relative_figures(groups: Dict[str, MethodGroup], reference_method: str
                     ) -> Dict[str, Dict[str, RelativeFigure]]:
    """{series: {method: RelativeFigure}} for every RATIO_SERIES.

    Both sides are group means, i.e. means over mounts. Reported as
    RELATIVE VIBRATION TRANSFER against the reference method, never as
    transmissibility: the rig has no reference accelerometer at the
    actuator input, so no absolute transfer function exists."""
    out: Dict[str, Dict[str, RelativeFigure]] = {}
    for prefix in RATIO_SERIES:
        series = av.SERIES_BY_PREFIX[prefix]
        key = f"{prefix}_mean_{series.unit}"
        reference = groups[reference_method].stats.get(key)
        row: Dict[str, RelativeFigure] = {}
        for method, group in groups.items():
            value = group.stats.get(key)
            ratio = (value / reference
                     if value is not None and reference else None)
            row[method] = RelativeFigure(
                series=prefix, method=method,
                reference_method=reference_method,
                value=value, reference_value=reference, ratio=ratio,
                percent_change=None if ratio is None else (ratio - 1.0) * 100.0,
                db=(None if not ratio or ratio <= 0
                    else 20.0 * math.log10(ratio)),
            )
        out[prefix] = row
    return out


# ==========================================
# Figures
# ==========================================

def _ordered(groups: Dict[str, MethodGroup]) -> List[str]:
    """The selected methods, in canonical order."""
    return [m for m in av.ADHESION_METHODS if m in groups]


def _bar_positions(n_methods: int, n_groups: int):
    """x positions for grouped bars: (group centres, bar width)."""
    width = 0.8 / max(1, n_methods)
    return np.arange(n_groups), width


def _method_label(group: MethodGroup) -> str:
    return (f"{group.method} ({group.n_runs} run"
            f"{'s' if group.n_runs != 1 else ''})")


def _series_bar_panel(ax, groups: Dict[str, MethodGroup],
                      series_prefixes: Sequence[str], title: str,
                      ylabel: str, scatter: bool = True) -> None:
    """Grouped mean bars with an SD error bar, each RUN's own mean as a
    filled marker and every trial as a faint dot - so the mean, the
    mount-to-mount spread and the trial spread are all visible at once."""
    order = _ordered(groups)
    positions, width = _bar_positions(len(order), len(series_prefixes))
    for index, method in enumerate(order):
        group = groups[method]
        offset = (index - (len(order) - 1) / 2.0) * width
        means, errors = [], []
        for prefix in series_prefixes:
            series = av.SERIES_BY_PREFIX[prefix]
            means.append(group.stats.get(f"{prefix}_mean_{series.unit}")
                         or float("nan"))
            errors.append(group.stats.get(f"{prefix}_sd_{series.unit}") or 0.0)
        ax.bar(positions + offset, means, width, yerr=errors, capsize=3,
               color=group.colour, alpha=0.75, label=_method_label(group))
        if not scatter:
            continue
        for position, prefix in zip(positions, series_prefixes):
            series = av.SERIES_BY_PREFIX[prefix]
            trials = [v for _run, _trial, v in group.trial_values(series)]
            if trials:
                ax.plot(np.full(len(trials), position + offset), trials,
                        linestyle="none", marker=".", markersize=4,
                        color="0.35", alpha=0.55, zorder=4)
            run_means = group.run_means(series)
            if len(run_means) > 1:
                ax.plot(np.full(len(run_means), position + offset), run_means,
                        linestyle="none", marker="D", markersize=5,
                        markerfacecolor="white", markeredgecolor="black",
                        markeredgewidth=0.8, zorder=6)
    ax.set_xticks(positions)
    ax.set_xticklabels([av.SERIES_BY_PREFIX[p].label.rstrip(":")
                        for p in series_prefixes], fontsize=8)
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(ylabel)
    ax.grid(True, axis="y", alpha=0.4)
    ax.legend(fontsize=7)


#: Legend note explaining the two marker kinds every bar panel draws.
SCATTER_NOTE = ("◆ = one run's (one mount's) mean   ·   · = one trial   ·   "
                "error bar = SD between mounts where a method has several "
                "runs, else between trials")


def save_waveform_figure(path: str, groups: Dict[str, MethodGroup],
                         alignment: str, drive_freq_hz: float) -> str:
    """Aligned mean envelopes (whole window + rise zoom), the steady-state
    waveform shape, and the rise/settling comparison."""
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    (ax_full, ax_rise), (ax_shape, ax_timing) = axes
    align_label = ("the motor-on command" if alignment == ALIGN_COMMAND
                   else "each trial's own onset")

    for method in _ordered(groups):
        group = groups[method]
        envelope = mean_envelope(group, alignment)
        if envelope is None:
            continue
        times_ms = envelope.times_s * 1000.0
        label = (f"{method} ({envelope.n_runs} run"
                 f"{'s' if envelope.n_runs != 1 else ''}, "
                 f"{envelope.n_trials} trials)")
        for ax in (ax_full, ax_rise):
            ax.plot(times_ms, envelope.mean_counts, color=group.colour,
                    linewidth=1.4, label=label)
            ax.fill_between(times_ms,
                            envelope.mean_counts - envelope.sd_counts,
                            envelope.mean_counts + envelope.sd_counts,
                            color=group.colour, alpha=0.18)
    for ax in (ax_full, ax_rise):
        ax.axvline(0.0, color="black", linewidth=1.0)
        ax.set_xlabel(f"Time since {align_label} (ms)")
        ax.set_ylabel("Envelope (counts)")
        ax.grid(True, alpha=0.4)
        ax.legend(fontsize=7)
    ax_full.set_title(f"Mean vibration envelope, aligned at {align_label}\n"
                      "(band = ±1 SD across all trials of all runs)",
                      fontsize=10)
    ax_rise.set_xlim(-0.02 * RISE_ZOOM_S * 1000.0, RISE_ZOOM_S * 1000.0)
    ax_rise.set_title(f"Rise, first {RISE_ZOOM_S * 1000:.0f} ms", fontsize=10)

    # Waveform shape: ONE representative trial per method (an average
    # over phase-random trials would cancel the carrier).
    for method in _ordered(groups):
        group = groups[method]
        shape = _shape_window(group)
        if shape is None:
            continue
        times_ms, values, axis_name, run_name, trial_id = shape
        ax_shape.plot(times_ms - times_ms[0], values, color=group.colour,
                      linewidth=1.0,
                      label=f"{method} · {run_name}, trial {trial_id}, "
                            f"{axis_name} axis")
    ax_shape.set_title(f"Steady-state waveform shape, {SHAPE_ZOOM_MS:g} ms of "
                       "one trial per method\n(strongest axis; a mean over "
                       "trials would cancel the carrier)", fontsize=10)
    ax_shape.set_xlabel("Time within the steady state (ms)")
    ax_shape.set_ylabel("Acceleration (counts)")
    ax_shape.grid(True, alpha=0.4)
    ax_shape.legend(fontsize=6)

    _series_bar_panel(ax_timing, groups, ("onset", "rise", "settling"),
                      "Onset latency, rise time and settling time",
                      "Time (ms)")

    fig.suptitle("Adhesion comparison - waveform and rise "
                 f"(LRA {drive_freq_hz:g} Hz)", fontsize=12)
    fig.text(0.5, 0.005, SCATTER_NOTE, ha="center", fontsize=8,
             color="#555555")
    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def save_spectrum_figure(path: str, groups: Dict[str, MethodGroup],
                         drive_freq_hz: float) -> str:
    """Mean steady-state spectra, the amplitude at the drive frequency,
    the harmonics and the THD."""
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    (ax_spectrum, ax_fundamental), (ax_harmonics, ax_thd) = axes

    max_hz = max(4.0 * drive_freq_hz, 200.0)
    for method in _ordered(groups):
        group = groups[method]
        spectrum = mean_spectrum(group)
        if spectrum is None:
            continue
        mask = spectrum.freqs_hz <= max_hz
        ax_spectrum.semilogy(
            spectrum.freqs_hz[mask],
            np.maximum(spectrum.amplitude_counts[mask], 1e-3),
            color=group.colour, linewidth=1.0,
            label=f"{method} ({len(group.settled)} settled trials, "
                  f"{group.n_runs} run{'s' if group.n_runs != 1 else ''})")
    ax_spectrum.axvline(drive_freq_hz, color="black", linestyle="--",
                        linewidth=1, label=f"drive {drive_freq_hz:g} Hz")
    for order in range(2, av.N_HARMONICS + 1):
        ax_spectrum.axvline(drive_freq_hz * order, color="gray",
                            linestyle=":", linewidth=0.8,
                            label="harmonics" if order == 2 else "_nolegend_")
    ax_spectrum.set_title("Mean steady-state amplitude spectrum", fontsize=10)
    ax_spectrum.set_xlabel("Frequency (Hz)")
    ax_spectrum.set_ylabel("Amplitude (counts)")
    ax_spectrum.set_xlim(0, max_hz)
    ax_spectrum.grid(True, which="both", alpha=0.4)
    ax_spectrum.legend(fontsize=7)

    _series_bar_panel(ax_fundamental, groups, ("fundamental", "dominant"),
                      "Amplitude at the drive frequency, and the dominant "
                      "frequency", "Amplitude (m/s²) / frequency (Hz)")

    # Harmonic amplitudes per order, averaged the same way: per run
    # first, then over runs.
    order_list = list(range(2, av.N_HARMONICS + 1))
    methods = _ordered(groups)
    positions, width = _bar_positions(len(methods), len(order_list))
    for index, method in enumerate(methods):
        group = groups[method]
        offset = (index - (len(methods) - 1) / 2.0) * width
        means, errors = [], []
        for position, _order in enumerate(order_list):
            run_means = []
            for run in group.runs:
                values = [counts_to_ms2(r.spectrum.harmonic_amplitudes_counts[position])
                          for r in run.results if r.settled
                          and position < len(r.spectrum.harmonic_amplitudes_counts)
                          and r.spectrum.harmonic_amplitudes_counts[position]
                          is not None]
                if values:
                    run_means.append(statistics.fmean(values))
            means.append(statistics.fmean(run_means) if run_means
                         else float("nan"))
            errors.append(statistics.stdev(run_means) if len(run_means) >= 2
                          else 0.0)
        ax_harmonics.bar(positions + offset, means, width, yerr=errors,
                         capsize=3, color=group.colour, alpha=0.75,
                         label=_method_label(group))
    ax_harmonics.set_xticks(positions)
    ax_harmonics.set_xticklabels([f"{o}f₀ = {drive_freq_hz * o:g} Hz"
                                  for o in order_list], fontsize=8)
    ax_harmonics.set_title("Harmonic amplitudes (settled trials, averaged per "
                           "run then over runs)", fontsize=10)
    ax_harmonics.set_ylabel("Amplitude (m/s²)")
    ax_harmonics.grid(True, axis="y", alpha=0.4)
    ax_harmonics.legend(fontsize=7)

    _series_bar_panel(ax_thd, groups, ("thd",),
                      "Total harmonic distortion "
                      f"(harmonics 2-{av.N_HARMONICS})", "THD (%)")

    fig.suptitle("Adhesion comparison - frequency response", fontsize=12)
    fig.text(0.5, 0.005, SCATTER_NOTE, ha="center", fontsize=8,
             color="#555555")
    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def save_metrics_figure(path: str, groups: Dict[str, MethodGroup],
                        relative: Dict[str, Dict[str, RelativeFigure]],
                        reference_method: str) -> str:
    """Steady-state strength, peaks, repeatability (CV) and the relative
    vibration transfer against the reference method."""
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    (ax_strength, ax_peaks), (ax_cv, ax_relative) = axes

    _series_bar_panel(ax_strength, groups, ("steady_rms", "fundamental"),
                      "Steady-state strength", "Acceleration (m/s²)")
    _series_bar_panel(ax_peaks, groups,
                      ("peak_robust", "peak_abs", "peak_to_peak"),
                      "Peaks - robust (p99) next to the absolute maximum",
                      "Acceleration (m/s²)")

    # Repeatability: CV per method for every compared series that has one.
    methods = _ordered(groups)
    prefixes = [s.prefix for s in COMPARISON_SERIES
                if any(groups[m].stats.get(f"{s.prefix}_cv_percent")
                       is not None for m in methods)]
    positions, width = _bar_positions(len(methods), len(prefixes))
    multi_run = [m for m in methods if groups[m].n_runs >= 2]
    for index, method in enumerate(methods):
        group = groups[method]
        offset = (index - (len(methods) - 1) / 2.0) * width
        values = [group.stats.get(f"{p}_cv_percent") or float("nan")
                  for p in prefixes]
        ax_cv.bar(positions + offset, values, width, color=group.colour,
                  alpha=0.75, label=_method_label(group))
    ax_cv.set_xticks(positions)
    ax_cv.set_xticklabels([av.SERIES_BY_PREFIX[p].label.rstrip(":")
                           for p in prefixes], fontsize=7, rotation=30,
                          ha="right")
    ax_cv.set_title(
        "Repeatability - coefficient of variation (SD / mean)\n"
        + ("mount-to-mount for " + ", ".join(multi_run) if multi_run
           else "trial-to-trial (no method has more than one run)")
        + (";  trial-to-trial for the rest"
           if multi_run and len(multi_run) < len(methods) else ""),
        fontsize=9)
    ax_cv.set_ylabel("CV (%)")
    ax_cv.grid(True, axis="y", alpha=0.4)
    ax_cv.legend(fontsize=7)

    # Relative vibration transfer, in dB, against the reference method.
    positions, width = _bar_positions(len(methods), len(RATIO_SERIES))
    for index, method in enumerate(methods):
        group = groups[method]
        offset = (index - (len(methods) - 1) / 2.0) * width
        values = [(relative[p][method].db
                   if relative[p][method].db is not None else float("nan"))
                  for p in RATIO_SERIES]
        ax_relative.bar(positions + offset, values, width, color=group.colour,
                        alpha=0.75, label=_method_label(group))
    ax_relative.axhline(0.0, color="black", linewidth=1.0)
    ax_relative.set_xticks(positions)
    ax_relative.set_xticklabels([av.SERIES_BY_PREFIX[p].label.rstrip(":")
                                 for p in RATIO_SERIES], fontsize=7,
                                rotation=30, ha="right")
    ax_relative.set_title("Relative vibration transfer vs "
                          f"{reference_method}  ·  20·log₁₀(amplitude ratio)\n"
                          "NOT transmissibility - no reference accelerometer "
                          "at the actuator input", fontsize=10)
    ax_relative.set_ylabel("dB relative to the reference")
    ax_relative.grid(True, axis="y", alpha=0.4)
    ax_relative.legend(fontsize=7)

    fig.suptitle("Adhesion comparison - steady-state metrics, repeatability "
                 "and relative transfer", fontsize=12)
    fig.text(0.5, 0.005, SCATTER_NOTE, ha="center", fontsize=8,
             color="#555555")
    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ==========================================
# CSV / meta output
# ==========================================

COMPARISON_CSV_COLUMNS: Tuple[str, ...] = (
    "scope",                 # "trial" | "run" | "summary"
    "adhesion_method", "run", "trial_id", "metric", "unit", "value",
    "n_runs", "n", "mean", "sd", "sd_basis",
    "sd_between_runs", "sd_within_run", "cv_percent", "min", "max",
    "reference_method", "ratio_to_reference", "percent_change_vs_reference",
    "db_vs_reference",
)


def _cell(value, decimals: int = 6) -> str:
    return "" if value is None else f"{float(value):.{decimals}f}"


def save_comparison_csv(path: str, groups: Dict[str, MethodGroup],
                        relative: Dict[str, Dict[str, RelativeFigure]],
                        reference_method: str) -> str:
    """Per-trial, per-run AND per-method rows in one tidy table.

    `scope` says which a row is, so the mount means behind every method
    mean - and the trials behind every mount mean - stay in the same file
    as the summary. A summary a reader cannot check against the data
    underneath it is not much of a summary."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(COMPARISON_CSV_COLUMNS))
        writer.writeheader()
        for method in _ordered(groups):
            group = groups[method]
            for series in COMPARISON_SERIES:
                prefix, unit = series.prefix, series.unit
                for run in group.runs:
                    for trial_id, value in run.trial_values(series):
                        writer.writerow({
                            "scope": "trial",
                            "adhesion_method": method,
                            "run": run.name,
                            "trial_id": trial_id,
                            "metric": prefix,
                            "unit": unit,
                            "value": _cell(value),
                        })
                    writer.writerow({
                        "scope": "run",
                        "adhesion_method": method,
                        "run": run.name,
                        "metric": prefix,
                        "unit": unit,
                        "n": run.stats.get(f"{prefix}_n", 0),
                        "mean": _cell(run.mean_of(series)),
                        "sd": _cell(run.stats.get(f"{prefix}_sd_{unit}")),
                        "sd_basis": SD_WITHIN_RUN,
                        "cv_percent": _cell(
                            run.stats.get(f"{prefix}_cv_percent"), 3),
                        "min": _cell(run.stats.get(f"{prefix}_min_{unit}")),
                        "max": _cell(run.stats.get(f"{prefix}_max_{unit}")),
                    })
                figure = relative.get(prefix, {}).get(method)
                stats = group.stats
                writer.writerow({
                    "scope": "summary",
                    "adhesion_method": method,
                    "run": "",
                    "metric": prefix,
                    "unit": unit,
                    "n_runs": stats.get(f"{prefix}_n_runs", 0),
                    "n": stats.get(f"{prefix}_n", 0),
                    "mean": _cell(stats.get(f"{prefix}_mean_{unit}")),
                    "sd": _cell(stats.get(f"{prefix}_sd_{unit}")),
                    "sd_basis": stats.get(f"{prefix}_sd_basis") or "",
                    "sd_between_runs": _cell(
                        stats.get(f"{prefix}_sd_between_runs_{unit}")),
                    "sd_within_run": _cell(
                        stats.get(f"{prefix}_sd_within_run_{unit}")),
                    "cv_percent": _cell(stats.get(f"{prefix}_cv_percent"), 3),
                    "min": _cell(stats.get(f"{prefix}_min_{unit}")),
                    "max": _cell(stats.get(f"{prefix}_max_{unit}")),
                    "reference_method": reference_method if figure else "",
                    "ratio_to_reference": _cell(
                        figure.ratio if figure else None),
                    "percent_change_vs_reference": _cell(
                        figure.percent_change if figure else None, 3),
                    "db_vs_reference": _cell(figure.db if figure else None, 3),
                })
    return path


def _method_summary(group: MethodGroup) -> dict:
    """The per-method block stored in the analysis meta."""
    stats = group.stats
    summary = {
        "n_runs": group.n_runs,
        "runs": [{"csv": run.name,
                  "n_trials": run.info.n_trials,
                  "saved_at": run.info.saved_at,
                  "notes": run.info.notes} for run in group.runs],
        "n_trials": stats.get("n_trials", 0),
        "n_settled_steady_state": stats.get("n_settled_steady_state", 0),
        "n_ok": stats.get("n_ok", 0),
        "n_not_settled": stats.get("n_not_settled", 0),
        "n_no_onset": stats.get("n_no_onset", 0),
        "series": {},
    }
    for series in COMPARISON_SERIES:
        prefix, unit = series.prefix, series.unit
        summary["series"][prefix] = {
            "unit": unit,
            "n_runs": stats.get(f"{prefix}_n_runs", 0),
            "n_trials": stats.get(f"{prefix}_n", 0),
            "mean": stats.get(f"{prefix}_mean_{unit}"),
            "sd": stats.get(f"{prefix}_sd_{unit}"),
            "sd_basis": stats.get(f"{prefix}_sd_basis"),
            "sd_between_runs": stats.get(f"{prefix}_sd_between_runs_{unit}"),
            "sd_within_run": stats.get(f"{prefix}_sd_within_run_{unit}"),
            "cv_percent": stats.get(f"{prefix}_cv_percent"),
            "min": stats.get(f"{prefix}_min_{unit}"),
            "max": stats.get(f"{prefix}_max_{unit}"),
        }
    return summary


def save_comparison_meta(path: str, groups: Dict[str, MethodGroup],
                         relative: Dict[str, Dict[str, RelativeFigure]],
                         reference_method: str, alignment: str,
                         stamp: int, files: dict) -> str:
    """The analysis meta: which files went in, what they were driven
    with, how the comparison was computed, and its results."""
    first = groups[_ordered(groups)[0]].runs[0].info
    meta = {
        "analysis": "adhesion_vibration_comparison",
        "saved_at": int(stamp),
        "analysis_version": ANALYSIS_VERSION,
        "adhesion_metric_version": av.ADHESION_METRIC_VERSION,
        "software": {
            "experiment_module": "validation_experiments/"
                                 "adhesion_vibration_comparison/"
                                 "adhesion_vibration.py",
            "analysis_module": "validation_experiments/"
                               "adhesion_vibration_comparison/"
                               "adhesion_comparison.py",
            "shared_metrics": "validation_experiments/acceleration_metrics.py",
            "onset_settling_detector": "validation_experiments/"
                                       "motor_acc_delay_experiment/"
                                       "motor_acc_delay.py",
        },
        "inputs": [
            {
                "adhesion_method": group.method,
                "csv": run.name,
                "path": run.info.csv_path,
                "saved_at": run.info.saved_at,
                "n_trials": run.info.n_trials,
                "notes": run.info.notes,
            }
            for group in groups.values() for run in group.runs
        ],
        # The drive is identical across every selected run by
        # construction - the compatibility check refuses anything else -
        # so it is recorded once, from the runs themselves.
        "drive": {
            "actuator_type": av.ACTUATOR_TYPE,
            "frequency_hz": first.frequency_hz,
            "amp": first.amp,
            "motor_index": first.motor_index,
            "acc_sensor_id": first.acc_sensor_id,
            "acc_interval_ms": first.acc_interval_ms,
            "vib_duration_s": first.vib_duration_s,
        },
        "analysis_parameters": {
            "alignment": alignment,
            "alignment_note": ("mean envelopes are interpolated onto a common "
                               "grid aligned at " + (
                                   "the motor-on command"
                                   if alignment == ALIGN_COMMAND
                                   else "each trial's own detected onset")),
            "averaging": ("a method's mean is the mean of its RUN means (one "
                          "weight per physical mount), not the mean of all "
                          "its trials - trials within a run are repeated "
                          "measurements of one mount"),
            "sd_definitions": {
                SD_BETWEEN_RUNS: ("spread of the run means - mount "
                                  "repeatability; used as the headline SD "
                                  "when a method has >= 2 runs"),
                SD_WITHIN_RUN: ("pooled spread of trials around their own "
                                "run's mean - measurement repeatability of "
                                "one mount; the headline SD only when a "
                                "method has a single run"),
                "note": "sd_basis records which one a figure used",
            },
            "steady_state_definition": ("detected stable time → motor-off; "
                                        "only trials whose steady region is a "
                                        "settled one enter the steady-state "
                                        "statistics"),
            "steady_min_s": av.STEADY_MIN_S,
            "robust_peak_percentile": av.ROBUST_PEAK_PERCENTILE,
            "spectrum_min_hz": SPECTRUM_MIN_HZ,
            "spectrum_tolerance_hz": SPECTRUM_TOLERANCE_HZ,
            "n_harmonics": av.N_HARMONICS,
            "ms2_per_count": MS2_PER_COUNT,
            "compared_series": [s.prefix for s in COMPARISON_SERIES],
            "ratio_series": list(RATIO_SERIES),
        },
        "reference_method": reference_method,
        "relative_transfer_definition": {
            "ratio": "mean(method) / mean(reference_method), both over runs",
            "percent_change": "100 * (ratio - 1)",
            "db": "20 * log10(ratio)",
            "naming": ("relative vibration transfer / relative attenuation - "
                       "NOT transmissibility: no reference accelerometer "
                       "measures the actuator's input, so no input/output "
                       "transfer function is available"),
        },
        "result": {
            "methods_compared": _ordered(groups),
            "methods": {method: _method_summary(group)
                        for method, group in groups.items()},
            "relative": {
                prefix: {method: {
                    "value": figure.value,
                    "reference_value": figure.reference_value,
                    "ratio": figure.ratio,
                    "percent_change": figure.percent_change,
                    "db": figure.db,
                } for method, figure in row.items()}
                for prefix, row in relative.items()
            },
        },
        "validity": {"protocol": list(av.VALIDITY_RULES)},
        "files": files,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)
    return path


# ==========================================
# Text report
# ==========================================

def comparison_report(groups: Dict[str, MethodGroup],
                      relative: Dict[str, Dict[str, RelativeFigure]],
                      reference_method: str, alignment: str,
                      files: Optional[dict] = None) -> List[str]:
    """The comparison as text - the same numbers the figures show."""
    order = _ordered(groups)
    first = groups[order[0]].runs[0].info
    n_runs = sum(groups[m].n_runs for m in order)
    lines = [
        "",
        "===== Adhesion method comparison =====",
        f"      LRA {_fmt_number(first.frequency_hz, 0)} Hz, amp "
        f"{_fmt_number(first.amp, 0)}, motor port {first.motor_index}, "
        f"ACC sensor {first.acc_sensor_id}, "
        f"{_fmt_number(first.vib_duration_s, 1)} s drive per trial",
        f"      {n_runs} runs over {len(order)} method(s);  waveform "
        f"alignment: {alignment};  reference method: {reference_method}",
    ]
    for method in order:
        group = groups[method]
        lines.append(f"      {method} - {group.n_runs} run"
                     f"{'s' if group.n_runs != 1 else ''}, "
                     f"{group.stats.get('n_trials', 0)} trials "
                     f"({group.stats.get('n_settled_steady_state', 0)} "
                     "settled):")
        for run in group.runs:
            lines.append(f"          {run.name} ({run.info.n_trials} trials)"
                         + (f" - {run.info.notes}" if run.info.notes else ""))
    lines.append("")

    rows = []
    for series in COMPARISON_SERIES:
        prefix, unit = series.prefix, series.unit
        for method in order:
            stats = groups[method].stats
            basis = stats.get(f"{prefix}_sd_basis") or "-"
            rows.append((
                series.label.rstrip(":"),
                method,
                stats.get(f"{prefix}_n_runs", 0),
                stats.get(f"{prefix}_n", 0),
                report.number(stats.get(f"{prefix}_mean_{unit}"),
                              series.decimals),
                report.number(stats.get(f"{prefix}_sd_{unit}"),
                              series.decimals),
                report.number(stats.get(f"{prefix}_cv_percent"), 1),
                "mounts" if basis == SD_BETWEEN_RUNS
                else ("trials" if basis == SD_WITHIN_RUN else "-"),
                series.display_unit,
            ))
    lines.extend(report.table(
        ("metric", "method", "runs", "n", "mean", "SD", "CV %", "SD over",
         "unit"), rows))
    lines.append("")
    lines.append("A method's mean is the mean of its RUN means (one weight "
                 "per mount). \"SD over: mounts\" is the spread between "
                 "runs of the same adhesive - how reproducible applying it "
                 "is; \"trials\" is the spread within a single mount, used "
                 "only when that method has one run.")
    lines.append("")

    lines.append(f"Relative vibration transfer vs {reference_method} "
                 "(amplitude ratios; dB = 20·log10(ratio)):")
    ratio_rows = []
    for prefix in RATIO_SERIES:
        series = av.SERIES_BY_PREFIX[prefix]
        for method in order:
            figure = relative[prefix][method]
            ratio_rows.append((
                series.label.rstrip(":"),
                method,
                report.number(figure.ratio, 3),
                report.number(figure.percent_change, 1),
                report.number(figure.db, 2),
            ))
    lines.extend(report.table(
        ("metric", "method", "ratio", "change %", "dB"), ratio_rows))
    lines.append("")
    lines.append("These are RELATIVE figures between mounted "
                 "configurations, not absolute transmissibility: the rig "
                 "measures no reference acceleration at the actuator's "
                 "input.")
    single = [m for m in order if groups[m].n_runs == 1]
    if single:
        lines.append("Only one run for: " + ", ".join(single)
                     + " - their means rest on a single mount, so the "
                     "mount-to-mount variation of those methods is unknown. "
                     "Record more runs of them before reading small "
                     "differences as material differences.")
    fallbacks = [f"{m}: {groups[m].stats.get('n_fallback_steady_state', 0)}"
                 for m in order
                 if groups[m].stats.get("n_fallback_steady_state")]
    if fallbacks:
        lines.append("Trials that never settled (excluded from the "
                     "steady-state statistics) - " + ", ".join(fallbacks) + ".")
    if files:
        lines.append("")
        for label, path in files.items():
            if path:
                lines.append(f"{label}: {path}")
    return lines


# ==========================================
# The comparison
# ==========================================

def compare(csv_paths: Sequence[str],
            reference_method: str = REFERENCE_METHOD,
            alignment: str = DEFAULT_ALIGNMENT,
            output_dir: Optional[str] = None,
            log: Optional[LogFn] = None) -> dict:
    """Compare any number of saved runs, grouped by adhesion method.

    Several runs of the same adhesive are averaged over runs (see the
    module docstring); at least two runs covering at least two different
    methods are required. Raises ValueError, listing every reason, when
    the selection is not comparable (too few runs or methods, or a
    differing drive/sampling parameter); nothing is written in that case.
    On success writes the three figures, the CSV and the meta into
    `output_dir` (the experiment's own folder by default) and returns a
    summary dict including the report lines."""
    log = log if log is not None else (lambda _line: None)
    if alignment not in ALIGNMENTS:
        raise ValueError(f"alignment must be one of {ALIGNMENTS}")

    infos = [run_info(path) for path in csv_paths]
    problems = check_selection(infos)
    if problems:
        raise ValueError("These runs cannot be compared:\n  · "
                         + "\n  · ".join(problems))

    groups = load_groups(csv_paths)
    reference_method = av.normalise_method(reference_method)
    if reference_method not in groups:
        fallback = _ordered(groups)[0]
        log(f"No {reference_method} run selected - taking {fallback} as the "
            "reference method for the relative figures.")
        reference_method = fallback

    for method in _ordered(groups):
        group = groups[method]
        log(f"{method}: {group.n_runs} run"
            f"{'s' if group.n_runs != 1 else ''} "
            f"({', '.join(run.name for run in group.runs)}) - "
            f"{group.stats.get('n_trials', 0)} trials, "
            f"{group.stats.get('n_settled_steady_state', 0)} with a settled "
            "steady state")

    relative = relative_figures(groups, reference_method)

    folder = output_dir or av.OUTPUT_DIR
    os.makedirs(folder, exist_ok=True)
    stamp = int(time.time())
    stem = f"{av.COMPARISON_PREFIX}{stamp}"
    drive_freq = float(groups[reference_method].runs[0].info.frequency_hz
                       or av.lra_frequency_hz())

    waveform_png = save_waveform_figure(
        os.path.join(folder, f"{stem}_waveform.png"), groups, alignment,
        drive_freq)
    spectrum_png = save_spectrum_figure(
        os.path.join(folder, f"{stem}_spectrum.png"), groups, drive_freq)
    metrics_png = save_metrics_figure(
        os.path.join(folder, f"{stem}_metrics.png"), groups, relative,
        reference_method)
    csv_path = save_comparison_csv(os.path.join(folder, f"{stem}.csv"),
                                   groups, relative, reference_method)
    files = {
        "waveform_png": os.path.basename(waveform_png),
        "spectrum_png": os.path.basename(spectrum_png),
        "metrics_png": os.path.basename(metrics_png),
        "csv": os.path.basename(csv_path),
        "meta": f"{stem}.meta.json",
    }
    meta_path = save_comparison_meta(os.path.join(folder, f"{stem}.meta.json"),
                                     groups, relative, reference_method,
                                     alignment, stamp, files)

    lines = comparison_report(groups, relative, reference_method, alignment,
                              files={"Waveform": waveform_png,
                                     "Spectrum": spectrum_png,
                                     "Metrics": metrics_png,
                                     "CSV": csv_path,
                                     "Meta": meta_path})
    for line in lines:
        log(line)

    return {
        "reference_method": reference_method,
        "alignment": alignment,
        "methods_compared": _ordered(groups),
        "n_runs": {method: group.n_runs for method, group in groups.items()},
        "methods": {method: _method_summary(group)
                    for method, group in groups.items()},
        "relative": {prefix: {method: figure.__dict__
                              for method, figure in row.items()}
                     for prefix, row in relative.items()},
        "inputs": [run.info.csv_path for group in groups.values()
                   for run in group.runs],
        "waveform_png": waveform_png,
        "spectrum_png": spectrum_png,
        "metrics_png": metrics_png,
        "png_paths": [waveform_png, spectrum_png, metrics_png],
        "csv_path": csv_path,
        "meta_path": meta_path,
        "stamp": stamp,
        "report": lines,
    }


def comparison_paths(output_dir: Optional[str] = None) -> List[str]:
    """Saved comparison CSVs in the output folder, oldest first."""
    folder = output_dir or av.OUTPUT_DIR
    return av.sort_by_stamp(glob.glob(os.path.join(
        folder, f"{av.COMPARISON_PREFIX}*.csv")))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("csv", nargs="*",
                        help="the run CSVs to compare (any number, at least "
                             "two methods); omitted = every saved run")
    parser.add_argument("--reference", default=REFERENCE_METHOD)
    parser.add_argument("--align", default=DEFAULT_ALIGNMENT,
                        choices=list(ALIGNMENTS))
    args = parser.parse_args()

    paths = list(args.csv) or [info.csv_path for info in list_runs()]
    if not paths:
        parser.error("no saved runs found - record at least two first")
    compare(paths, reference_method=args.reference, alignment=args.align,
            log=print)


if __name__ == "__main__":
    main()
