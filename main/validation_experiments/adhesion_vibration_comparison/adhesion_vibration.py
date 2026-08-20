"""Adhesion vibration comparison - one adhesive per run.

How much of an LRA's vibration actually reaches the accelerometer
depends on what holds the two together. This experiment measures that,
one adhesive at a time, so three runs (Blu Tack, double-sided tape,
cosmetic adhesive) can afterwards be compared against each other by
`adhesion_comparison.py`.

ONE RUN = ONE ADHESIVE. The GUI asks which of the three adhesion methods
is mounted right now, drives the LRA with the SAME configured drive every
time, and saves that method's trials on their own. Nothing in this module
compares methods; that is deliberate, because the three measurements are
separated by a physical re-mount and must be recorded as three
independent runs.

THE DRIVE IS FIXED AND COMES FROM THE LRA CONFIG. Whatever `haptic.using`
currently says, this experiment always reads `haptic.lra.default_frequency`
and `haptic.lra.default_amp` (config.json, via common/haptic_config.py):
comparing adhesives is only meaningful when all three runs were driven
identically, so the drive is displayed read-only in the GUI rather than
offered as a control. The delivered values, the full config snapshot and
the motor port are recorded in every run's meta.

PER TRIAL the sequence is fixed (identical for every trial and every
adhesive, which is what makes the trials comparable):

    1. motor off
    2. quiet baseline window            (BASELINE_DURATION_S)
    3. "S <mask> <amp>" - the motor-on command, timestamped
    4. drive continuously               (vib_duration_s)
    5. every X/Y/Z sample of the window recorded, losslessly
    6. "X" - motor off
    7. rest                             (rest_duration_s)

WHAT IS MEASURED, and against which part of the window:

  whole drive window   both shared intensity metrics (demeaned 3-axis
                       vector RMS + legacy magnitude RMS) and the
                       per-axis statistics - acceleration_metrics'
                       standard block, so the CSV lines up with every
                       other experiment's
  rise                 onset latency, 10-90 % rise time, settling time -
                       from the SAME detector the Motor -> ACC Delay
                       experiment uses (motor_acc_delay.collect_baseline
                       + detect_onset_and_settling), not a second
                       implementation with the same words on it
  steady state         steady vector RMS, absolute peak, robust (p99)
                       peak, per-axis peak-to-peak, the amplitude
                       spectrum, the amplitude at the configured drive
                       frequency, its harmonics and the THD

The steady state is the interval from the detected stable time to
motor-off. A trial that never stabilises is recorded as `not_settled`
with an EMPTY settling time - never an invented one; its steady-state
numbers are computed over the window tail instead and flagged
`steady_region_source = window_tail_fallback`, and the comparison
excludes them from the steady-state statistics rather than mixing a
transient into a steady-state average.

PEAKS ARE REPORTED TWICE, on purpose. `peak_abs_*` is the largest single
sample in the steady state - one mechanical knock or one noisy sample
sets it. `peak_robust_p99_*` is the 99th percentile of the same series,
which a single outlier cannot move. Both are stored, and the comparison
plots both, so no conclusion rests on one anomalous sample.

Per-run outputs (Unix-epoch-seconds stamp <ts>) under
data/validation_experiments/adhesion_vibration_comparison/:
  adhesion_<method>_<ts>.csv           per-trial summary rows
  adhesion_<method>_<ts>.raw_acc.npz   lossless raw three-axis samples of
                                       every baseline and drive window
  adhesion_<method>_<ts>.png           per-run summary figure
  adhesion_<method>_<ts>.meta.json     parameters, config snapshot,
                                       firmware, files, result

Runs standalone (python adhesion_vibration.py --method "Blu Tack") or
through the launcher's "9. Validation Experiments" section; both paths
write the same files.
"""

import argparse
import csv
import glob
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import matplotlib
import numpy as np

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt

try:
    from ..acceleration_metrics import (
        DEFAULT_HARMONICS,
        METRIC_CSV_COLUMNS,
        MS2_PER_COUNT,
        PHASE_BASELINE,
        PHASE_VIBRATION,
        SPECTRUM_MIN_HZ,
        SPECTRUM_TOLERANCE_HZ,
        AccelerationMetrics,
        RawSampleRecorder,
        VibrationSpectrum,
        as_xyz_arrays,
        compute_acceleration_metrics,
        compute_vibration_spectrum,
        counts_to_ms2,
        harmonic_amplitudes,
        load_raw_acceleration_samples,
        metric_meta_block,
        raw_path_for,
        to_epoch_seconds,
        total_harmonic_distortion,
    )
    from ..motor_acc_delay_experiment import motor_acc_delay as mad
    from ..rig import Sample, SweepAborted, open_rig, parse_acc_line, send
    from .. import report
except ImportError:  # direct execution rather than package import
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from acceleration_metrics import (
        DEFAULT_HARMONICS,
        METRIC_CSV_COLUMNS,
        MS2_PER_COUNT,
        PHASE_BASELINE,
        PHASE_VIBRATION,
        SPECTRUM_MIN_HZ,
        SPECTRUM_TOLERANCE_HZ,
        AccelerationMetrics,
        RawSampleRecorder,
        VibrationSpectrum,
        as_xyz_arrays,
        compute_acceleration_metrics,
        compute_vibration_spectrum,
        counts_to_ms2,
        harmonic_amplitudes,
        load_raw_acceleration_samples,
        metric_meta_block,
        raw_path_for,
        to_epoch_seconds,
        total_harmonic_distortion,
    )
    from motor_acc_delay_experiment import motor_acc_delay as mad
    from rig import Sample, SweepAborted, open_rig, parse_acc_line, send
    import report

try:
    from common import haptic_config as hc
except ImportError:  # direct execution from this folder - add main/ to the path
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from common import haptic_config as hc


# ==========================================
# Adhesion methods
# ==========================================

#: The three adhesion methods under test, in their canonical wording -
#: the same terms the dissertation uses (a reusable adhesive putty,
#: double-sided adhesive tape, and the cosmetic eyelash adhesive, which
#: is named "Cosmetic adhesive" here as in the report's ethics section).
#: This tuple is the ONLY definition: the GUI selector, the CSV/meta
#: values and the comparison's "one of each" rule all read it, so a
#: fourth method cannot appear by typing one somewhere.
ADHESION_METHODS: Tuple[str, ...] = ("Blu Tack", "Double-sided tape",
                                     "Cosmetic adhesive")

#: File-name-safe id per method (used in the output file names).
METHOD_IDS: Dict[str, str] = {
    "Blu Tack": "blu_tack",
    "Double-sided tape": "double_sided_tape",
    "Cosmetic adhesive": "cosmetic_adhesive",
}
_METHOD_BY_ID: Dict[str, str] = {v: k for k, v in METHOD_IDS.items()}

#: Wording used for a method before a rename, mapped to its current
#: label. Kept so runs recorded under the old name still load - and load
#: as the SAME method, rather than as an unknown fourth one. Keys are in
#: the normalised form (lower case, "-"/"_" as spaces).
LEGACY_METHOD_ALIASES: Dict[str, str] = {
    "eyelash glue": "Cosmetic adhesive",
    "eyelash adhesive": "Cosmetic adhesive",
    "cosmetic eyelash adhesive": "Cosmetic adhesive",
}

#: What to physically use for each method, for the bench. The label is
#: the report's term; this says which tube to reach for.
METHOD_DESCRIPTIONS: Dict[str, str] = {
    "Blu Tack": "reusable adhesive putty",
    "Double-sided tape": "double-sided adhesive tape",
    "Cosmetic adhesive": "cosmetic eyelash adhesive (single-use)",
}


def normalise_method(value: str) -> str:
    """The canonical label for an adhesion method, accepting its id, any
    capitalisation and any superseded name ("blu_tack", "BLU TACK" ->
    "Blu Tack"; "eyelash glue" -> "Cosmetic adhesive").

    Raises ValueError for anything else - this experiment compares three
    named adhesives, so an unrecognised one is an error, never a fourth
    category quietly added to the data."""
    if isinstance(value, str):
        key = value.strip().lower().replace("-", " ").replace("_", " ")
        for label in ADHESION_METHODS:
            if key == label.lower().replace("-", " "):
                return label
        for method_id, label in _METHOD_BY_ID.items():
            if key == method_id.replace("_", " "):
                return label
        if key in LEGACY_METHOD_ALIASES:
            return LEGACY_METHOD_ALIASES[key]
    raise ValueError(
        f"unknown adhesion method {value!r} - expected one of "
        + ", ".join(f"{m!r}" for m in ADHESION_METHODS))


def method_id(method: str) -> str:
    """File-name-safe id of an adhesion method."""
    return METHOD_IDS[normalise_method(method)]


# ==========================================
# Experiment configuration
# ==========================================

#: This experiment is LRA-only. The port is the rig's wiring convention
#: (common.haptic_config); the GUI's motor-port control still overrides
#: it, because the port is a wiring fact rather than a measurement
#: parameter - but the actuator type is not selectable.
ACTUATOR_TYPE = "LRA"
MOTOR_INDEX = hc.ACTUATOR_MOTOR_PORTS[hc.LRA]
ACC_SENSOR_ID = 0

#: Firmware boot PWM frequency, restored on the port when a run ends (a
#: firmware fact - motor_driver.cpp DEFAULT_PWM_FREQ).
DEFAULT_PWM_FREQ = hc.FIRMWARE_BOOT_PWM_HZ

ACC_INTERVAL_MS = mad.ACC_INTERVAL_MS   # 1 ms stream (firmware >= v2.9.0)

NUM_TRIALS = 5
NUM_TRIALS_MIN, NUM_TRIALS_MAX = 1, 20

#: Drive/measurement window per trial. The bounds are the delay
#: experiment's, because the onset/settling detector shared with it is
#: only validated over that range.
VIB_DURATION_S = mad.VIB_DURATION_S
VIB_DURATION_MIN_S = mad.VIB_DURATION_MIN_S
VIB_DURATION_MAX_S = mad.VIB_DURATION_MAX_S
VIB_DURATION_STEP_S = mad.VIB_DURATION_STEP_S

#: Quiet window before every motor-on command. Shared with the delay
#: experiment so the rest-noise statistics both detectors use are
#: measured over the same length of quiet.
BASELINE_DURATION_S = mad.BASELINE_DURATION_S

REST_DURATION_S = 1.0
REST_DURATION_MIN_S, REST_DURATION_MAX_S = 0.0, 10.0

# -- steady state ---------------------------------------------------------
# The steady state runs from the detected stable time to motor-off. When
# a trial never settles, the same statistics are computed over the last
# STEADY_FALLBACK_FRACTION of the drive window instead and flagged as
# such - so a not_settled trial still has numbers to look at, but nothing
# reads them as a settled measurement by accident.
STEADY_FALLBACK_FRACTION = mad.STEADY_REF_FRACTION
STEADY_MIN_S = 0.20            # shorter than this -> no steady statistics
STEADY_FROM_SETTLING = "settled"
STEADY_FALLBACK = "window_tail_fallback"
STEADY_NONE = "unavailable"

#: Robust peak definition: the 99th percentile of the steady state's
#: per-sample deviation, reported next to the absolute maximum so a
#: single anomalous sample never carries a conclusion on its own.
ROBUST_PEAK_PERCENTILE = 99.0

#: Harmonics measured (fundamental + 4 more) and the search tolerance
#: around each - shared definitions from acceleration_metrics.
N_HARMONICS = DEFAULT_HARMONICS

#: Bumped when the stored per-trial field set changes.
ADHESION_METRIC_VERSION = 1

#: Width of the steady-state waveform zoom on the run figure. ~9 cycles
#: of a 224 Hz drive - enough to see the shape of individual cycles
#: (which is what "is the waveform distorted?" is about) without the
#: carrier turning into a solid block.
WAVEFORM_ZOOM_MS = 40.0

OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "adhesion_vibration_comparison"))

#: File-name prefixes. Run files are "adhesion_<method_id>_<ts>.*"; the
#: comparison writes "adhesion_comparison_<ts>.*" into the SAME folder,
#: so anything listing runs must exclude that prefix (run_csv_paths()).
RUN_PREFIX = "adhesion_"
COMPARISON_PREFIX = "adhesion_comparison_"

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]   # (trials_done, trials_total)


def lra_frequency_hz() -> int:
    """The configured LRA drive frequency (config.json
    haptic.lra.default_frequency).

    Deliberately `hc.LRA` and not the actuator in use: this experiment is
    about an LRA, and reads the LRA block even when the platform is
    currently configured for the ERM."""
    return hc.get_default_frequency(hc.LRA)


def lra_amp() -> int:
    """The configured LRA drive amp (config.json haptic.lra.default_amp);
    see lra_frequency_hz() on why it is always the LRA block."""
    return hc.get_default_amp(hc.LRA)


def estimated_duration_s(num_trials: int = NUM_TRIALS,
                         vib_duration_s: float = VIB_DURATION_S,
                         rest_duration_s: float = REST_DURATION_S) -> float:
    """Wall-clock estimate for a whole run."""
    return float(num_trials) * (BASELINE_DURATION_S + float(vib_duration_s)
                                + float(rest_duration_s))


# ==========================================
# Per-trial results
# ==========================================

@dataclass
class TrialTrace:
    """One trial's per-sample series, kept in memory for the figure and
    rebuilt from the run's raw NPZ when a saved run is re-opened.

    `axes` is the demeaned three-axis waveform (each axis minus its
    BASELINE mean, so gravity and the mounting pose are gone), `dev` its
    Euclidean norm - the detector's per-sample statistic - and `envelope`
    the centred moving RMS of `dev` that the settling test ran on."""
    rel_times: np.ndarray = field(default_factory=lambda: np.empty(0))
    axes: np.ndarray = field(default_factory=lambda: np.empty((0, 3)))
    dev: np.ndarray = field(default_factory=lambda: np.empty(0))
    envelope: np.ndarray = field(default_factory=lambda: np.empty(0))

    def __len__(self) -> int:
        return int(self.rel_times.size)


@dataclass
class SteadyRegion:
    """Which part of a drive window the steady-state numbers came from."""
    start_rel_s: float
    end_rel_s: float
    source: str          # STEADY_FROM_SETTLING / STEADY_FALLBACK / STEADY_NONE
    n_samples: int = 0

    @property
    def duration_s(self) -> float:
        return float(self.end_rel_s - self.start_rel_s)

    @property
    def usable(self) -> bool:
        return self.source != STEADY_NONE and self.n_samples > 0


@dataclass
class SpectrumMetrics:
    """The steady state's frequency-domain numbers. Every field is
    Optional: a window too short (or too quiet) to support a spectrum
    stores nothing rather than a zero."""
    dominant_frequency_hz: Optional[float] = None
    dominant_amplitude_counts: Optional[float] = None
    drive_freq_measured_hz: Optional[float] = None
    drive_freq_amplitude_counts: Optional[float] = None
    harmonic_amplitudes_counts: List[Optional[float]] = field(
        default_factory=list)     # orders 2..N_HARMONICS
    thd_ratio: Optional[float] = None
    n_samples: int = 0
    resolution_hz: Optional[float] = None

    @property
    def thd_percent(self) -> Optional[float]:
        return None if self.thd_ratio is None else self.thd_ratio * 100.0


@dataclass
class AdhesionTrialResult:
    """One trial of one adhesion method: identity, drive, timing, the
    whole-window intensity block every experiment shares, and the
    steady-state amplitude/spectrum numbers this experiment adds."""

    adhesion_method: str
    trial_id: int
    status: str                       # mad.DETECTION_STATUSES
    frequency_hz: int
    amp: int
    motor_index: int
    acc_sensor_id: int
    notes: str = ""

    # -- windows ---------------------------------------------------------
    baseline_n_samples: int = 0
    vibration_n_samples: int = 0
    sample_rate_hz: float = 0.0
    sampling_interval_ms: float = float("nan")
    vib_duration_s: float = VIB_DURATION_S
    baseline_duration_s: float = BASELINE_DURATION_S

    # -- the instants (Unix epoch seconds - the project-wide rule) -------
    command_time_s: Optional[float] = None
    onset_time_s: Optional[float] = None
    stable_time_s: Optional[float] = None
    steady_start_time_s: Optional[float] = None
    steady_end_time_s: Optional[float] = None

    # -- the derived intervals, kept separate (never summed) -------------
    onset_latency_from_command_ms: Optional[float] = None
    settling_time_from_onset_ms: Optional[float] = None
    stable_latency_from_command_ms: Optional[float] = None
    rise_time_10_90_ms: Optional[float] = None

    # -- steady state ----------------------------------------------------
    steady_region_source: str = STEADY_NONE
    steady_start_rel_s: Optional[float] = None
    steady_end_rel_s: Optional[float] = None
    steady_duration_s: Optional[float] = None
    steady_vector_rms_counts: Optional[float] = None
    steady_legacy_magnitude_rms_counts: Optional[float] = None
    steady_rms_x_counts: Optional[float] = None
    steady_rms_y_counts: Optional[float] = None
    steady_rms_z_counts: Optional[float] = None
    peak_abs_counts: Optional[float] = None
    peak_robust_p99_counts: Optional[float] = None
    peak_to_peak_x_counts: Optional[float] = None
    peak_to_peak_y_counts: Optional[float] = None
    peak_to_peak_z_counts: Optional[float] = None
    peak_to_peak_max_counts: Optional[float] = None
    #: Largest per-sample deviation over the WHOLE drive window (the
    #: transient included) - the start-up overshoot the steady-state peak
    #: deliberately excludes.
    window_peak_dev_counts: Optional[float] = None

    # -- detector state --------------------------------------------------
    steady_state_envelope_counts: Optional[float] = None
    steady_band_low_counts: Optional[float] = None
    steady_band_high_counts: Optional[float] = None
    env_noise_p95_counts: float = 0.0

    # -- frequency domain -------------------------------------------------
    spectrum: SpectrumMetrics = field(default_factory=SpectrumMetrics)

    # -- whole-window intensity (the shared block) ------------------------
    intensity: Optional[AccelerationMetrics] = None

    # -- in-memory only ---------------------------------------------------
    trace: TrialTrace = field(default_factory=TrialTrace)

    # -- m/s2 twins (single conversion, never stored twice) ---------------
    def _ms2(self, counts: Optional[float]) -> Optional[float]:
        return None if counts is None else counts_to_ms2(counts)

    @property
    def steady_vector_rms_ms2(self) -> Optional[float]:
        return self._ms2(self.steady_vector_rms_counts)

    @property
    def steady_legacy_magnitude_rms_ms2(self) -> Optional[float]:
        return self._ms2(self.steady_legacy_magnitude_rms_counts)

    @property
    def peak_abs_ms2(self) -> Optional[float]:
        return self._ms2(self.peak_abs_counts)

    @property
    def peak_robust_p99_ms2(self) -> Optional[float]:
        return self._ms2(self.peak_robust_p99_counts)

    @property
    def peak_to_peak_max_ms2(self) -> Optional[float]:
        return self._ms2(self.peak_to_peak_max_counts)

    @property
    def drive_freq_amplitude_ms2(self) -> Optional[float]:
        return self._ms2(self.spectrum.drive_freq_amplitude_counts)

    @property
    def dominant_amplitude_ms2(self) -> Optional[float]:
        return self._ms2(self.spectrum.dominant_amplitude_counts)

    @property
    def settled(self) -> bool:
        """Whether this trial's steady-state numbers describe a state the
        detector actually saw settle."""
        return self.steady_region_source == STEADY_FROM_SETTLING


# ==========================================
# Offline analysis of one trial
# ==========================================

def steady_region_for(rel_times: Sequence[float],
                      stable_rel_s: Optional[float]) -> SteadyRegion:
    """Which slice of a drive window the steady-state statistics use.

    From the detected stable time to motor-off when the trial settled;
    otherwise the last STEADY_FALLBACK_FRACTION of the window, flagged as
    a fallback. A region shorter than STEADY_MIN_S yields STEADY_NONE -
    there is no honest steady state to measure."""
    times = np.asarray(rel_times, dtype=float)
    if times.size == 0:
        return SteadyRegion(0.0, 0.0, STEADY_NONE, 0)
    start_of_window, end = float(times[0]), float(times[-1])
    if stable_rel_s is not None:
        start, source = float(stable_rel_s), STEADY_FROM_SETTLING
    else:
        span = end - start_of_window
        start = end - STEADY_FALLBACK_FRACTION * span
        source = STEADY_FALLBACK
    if end - start < STEADY_MIN_S:
        return SteadyRegion(start, end, STEADY_NONE, 0)
    n = int(np.count_nonzero((times >= start) & (times <= end)))
    if n < 2:
        return SteadyRegion(start, end, STEADY_NONE, 0)
    return SteadyRegion(start, end, source, n)


def rise_time_10_90_s(rel_times: Sequence[float], envelope: Sequence[float],
                      onset_rel_s: Optional[float],
                      steady_level: Optional[float]) -> Optional[float]:
    """Time for the vibration envelope to grow from 10 % to 90 % of the
    steady level, measured from the onset onwards.

    A shape measure of the RISE, next to (not instead of) the settling
    time: settling asks when the envelope stopped moving, this asks how
    fast it got there. None when the trial has no onset or no steady
    level to be a fraction of."""
    if onset_rel_s is None or not steady_level:
        return None
    times = np.asarray(rel_times, dtype=float)
    values = np.asarray(envelope, dtype=float)
    if times.size == 0 or values.size != times.size:
        return None
    after = np.flatnonzero(times >= float(onset_rel_s))
    if after.size == 0:
        return None
    start = int(after[0])
    low_hits = np.flatnonzero(values[start:] >= 0.10 * float(steady_level))
    high_hits = np.flatnonzero(values[start:] >= 0.90 * float(steady_level))
    if low_hits.size == 0 or high_hits.size == 0:
        return None
    t10 = float(times[start + int(low_hits[0])])
    t90 = float(times[start + int(high_hits[0])])
    return max(0.0, t90 - t10)


def peak_statistics(samples) -> Dict[str, float]:
    """Absolute peak, robust peak and per-axis peak-to-peak of one window.

    All are computed on the window's own demeaned three-axis series (the
    vector RMS convention), so they carry no gravity and no mounting
    pose:

        dev_i        = sqrt(sum_axes (a_i - mean_axis)^2)
        peak_abs     = max(dev_i)
        peak_robust  = p99(dev_i)
        peak_to_peak = max(axis) - min(axis), per axis
    """
    x, y, z = as_xyz_arrays(samples)
    dev = np.sqrt((x - x.mean()) ** 2 + (y - y.mean()) ** 2
                  + (z - z.mean()) ** 2)
    return {
        "peak_abs_counts": float(np.max(dev)),
        "peak_robust_p99_counts": float(np.percentile(dev,
                                                      ROBUST_PEAK_PERCENTILE)),
        "peak_to_peak_x_counts": float(np.ptp(x)),
        "peak_to_peak_y_counts": float(np.ptp(y)),
        "peak_to_peak_z_counts": float(np.ptp(z)),
        "peak_to_peak_max_counts": float(max(np.ptp(x), np.ptp(y), np.ptp(z))),
    }


def spectrum_metrics(samples, sample_rate_hz: float,
                     drive_freq_hz: float,
                     n_harmonics: int = N_HARMONICS
                     ) -> Tuple[SpectrumMetrics, Optional[VibrationSpectrum]]:
    """The steady state's frequency-domain block, plus the spectrum it
    came from (for plotting). Returns an empty block and None when the
    window is too short for a spectrum."""
    try:
        spectrum = compute_vibration_spectrum(samples, sample_rate_hz)
    except ValueError:
        return SpectrumMetrics(), None

    dominant_hz, dominant_amp = spectrum.dominant_frequency(SPECTRUM_MIN_HZ)
    harmonics = harmonic_amplitudes(spectrum, drive_freq_hz, n_harmonics,
                                    SPECTRUM_TOLERANCE_HZ)
    fundamental = harmonics[0]
    return SpectrumMetrics(
        dominant_frequency_hz=dominant_hz,
        dominant_amplitude_counts=dominant_amp,
        drive_freq_measured_hz=fundamental.found_hz,
        drive_freq_amplitude_counts=fundamental.amplitude_counts,
        harmonic_amplitudes_counts=[h.amplitude_counts for h in harmonics[1:]],
        thd_ratio=total_harmonic_distortion(spectrum, drive_freq_hz,
                                            n_harmonics,
                                            SPECTRUM_TOLERANCE_HZ),
        n_samples=spectrum.n_samples,
        resolution_hz=spectrum.resolution_hz,
    ), spectrum


def analyse_trial(trial_id: int, adhesion_method: str,
                  baseline: "mad.BaselineWindow",
                  raw_samples: List[Sample], rel_times: Sequence[float],
                  command_time_s: float, vib_duration_s: float,
                  frequency_hz: int, amp: int, motor_index: int,
                  acc_sensor_id: int, notes: str = "",
                  log: Optional[LogFn] = None) -> AdhesionTrialResult:
    """Turn one recorded drive window into a trial result.

    The whole offline half of a trial, kept free of serial I/O so it can
    be replayed on stored or synthetic data. Onset and settling come from
    the delay experiment's detector - this module does not re-define what
    "onset" or "settled" mean."""
    log = log if log is not None else (lambda _line: None)
    result = AdhesionTrialResult(
        adhesion_method=normalise_method(adhesion_method),
        trial_id=trial_id,
        status=mad.STATUS_INSUFFICIENT_DATA,
        frequency_hz=int(frequency_hz),
        amp=int(amp),
        motor_index=int(motor_index),
        acc_sensor_id=int(acc_sensor_id),
        notes=notes,
        baseline_n_samples=len(baseline.samples),
        vibration_n_samples=len(raw_samples),
        vib_duration_s=float(vib_duration_s),
        command_time_s=command_time_s,
        env_noise_p95_counts=baseline.noise.envelope_p95_counts,
    )
    if not raw_samples:
        log(f"Trial {trial_id}: no ACC samples arrived during the drive "
            "window - is the stream running?")
        return result

    times = np.asarray(rel_times, dtype=float)
    deltas = mad.per_axis_deltas(raw_samples, baseline.means)
    fs = mad.estimate_sample_rate_hz(times)
    result.sample_rate_hz = fs
    result.sampling_interval_ms = (1000.0 / fs) if fs > 0 else float("nan")
    result.window_peak_dev_counts = float(np.max(deltas))

    # The shared whole-window intensity block (both metrics + per-axis
    # statistics), exactly as every other accelerometer experiment
    # records it. It spans the quiet pre-onset samples and the ring-up as
    # well as the steady state, so it is NOT the steady-state intensity -
    # steady_vector_rms_counts is.
    result.intensity = compute_acceleration_metrics(
        raw_samples, baseline.magnitude_counts, MS2_PER_COUNT)

    detection = mad.detect_onset_and_settling(times, deltas, baseline.noise,
                                              vib_duration_s)
    result.status = detection.status
    result.steady_state_envelope_counts = detection.steady_level_counts
    result.steady_band_low_counts = detection.steady_low_counts
    result.steady_band_high_counts = detection.steady_high_counts

    xs, ys, zs = as_xyz_arrays(raw_samples)
    result.trace = TrialTrace(
        rel_times=times,
        axes=np.column_stack((xs - baseline.means[0], ys - baseline.means[1],
                              zs - baseline.means[2])),
        dev=np.asarray(deltas, dtype=float),
        envelope=np.asarray(detection.envelope, dtype=float),
    )

    if detection.onset_rel_s is not None:
        result.onset_latency_from_command_ms = detection.onset_rel_s * 1000.0
        result.onset_time_s = command_time_s + detection.onset_rel_s
    if detection.stable_rel_s is not None:
        result.stable_time_s = command_time_s + detection.stable_rel_s
        result.stable_latency_from_command_ms = detection.stable_rel_s * 1000.0
        settling = detection.settling_s
        result.settling_time_from_onset_ms = (None if settling is None
                                              else settling * 1000.0)
    rise = rise_time_10_90_s(times, detection.envelope, detection.onset_rel_s,
                             detection.steady_level_counts)
    if rise is not None:
        result.rise_time_10_90_ms = rise * 1000.0

    # -- steady state -----------------------------------------------------
    region = steady_region_for(times, detection.stable_rel_s)
    result.steady_region_source = region.source
    if region.usable:
        mask = (times >= region.start_rel_s) & (times <= region.end_rel_s)
        steady_samples = np.asarray(raw_samples, dtype=float)[mask]
        steady = compute_acceleration_metrics(
            steady_samples, baseline.magnitude_counts, MS2_PER_COUNT)
        result.steady_start_rel_s = region.start_rel_s
        result.steady_end_rel_s = region.end_rel_s
        result.steady_duration_s = region.duration_s
        result.steady_start_time_s = command_time_s + region.start_rel_s
        result.steady_end_time_s = command_time_s + region.end_rel_s
        result.steady_vector_rms_counts = steady.vector_rms_counts
        result.steady_legacy_magnitude_rms_counts = \
            steady.legacy_magnitude_rms_counts
        result.steady_rms_x_counts = steady.rms_x_counts
        result.steady_rms_y_counts = steady.rms_y_counts
        result.steady_rms_z_counts = steady.rms_z_counts
        for key, value in peak_statistics(steady_samples).items():
            setattr(result, key, value)
        result.spectrum, _ = spectrum_metrics(steady_samples, fs, frequency_hz)

    log(f"Trial {trial_id} ({result.adhesion_method}): status="
        f"{result.status}, onset "
        f"{report.number(result.onset_latency_from_command_ms)} ms, settling "
        f"{report.number(result.settling_time_from_onset_ms)} ms, steady RMS "
        f"{report.number(result.steady_vector_rms_ms2, 3)} m/s², peak "
        f"{report.number(result.peak_abs_ms2, 3)} m/s² (robust "
        f"{report.number(result.peak_robust_p99_ms2, 3)}), f_dom "
        f"{report.number(result.spectrum.dominant_frequency_hz, 1)} Hz, THD "
        f"{report.number(result.spectrum.thd_percent, 1)} %")
    if detection.note:
        log(f"  ({detection.note})")
    if result.steady_region_source == STEADY_FALLBACK:
        log("  (steady-state numbers come from the window tail - this trial "
            "never settled, so they are NOT a settled measurement)")
    return result


# ==========================================
# One trial on the hardware
# ==========================================

def run_single_trial(ser, trial_id: int, adhesion_method: str,
                     log: LogFn, motor_index: int, acc_sensor_id: int,
                     should_stop: Callable[[], bool],
                     frequency_hz: int, amp: int,
                     vib_duration_s: float = VIB_DURATION_S,
                     notes: str = "",
                     recorder: Optional[RawSampleRecorder] = None
                     ) -> AdhesionTrialResult:
    """Baseline -> motor on -> record the whole drive window -> motor off.

    Identical for every trial and every adhesive; the only thing that
    differs between runs is which adhesive is physically mounted."""
    log(f"\n===== Trial {trial_id} ({adhesion_method}) =====")
    ser.reset_input_buffer()
    baseline = mad.collect_baseline(ser, BASELINE_DURATION_S, acc_sensor_id)
    log(f"Baseline |a|: {baseline.magnitude_counts:.2f}; noise "
        f"{baseline.noise.mean_counts:.0f}±{baseline.noise.sd_counts:.0f} "
        f"counts (envelope p95 {baseline.noise.envelope_p95_counts:.0f})")
    baseline_window = -1
    if recorder is not None:
        baseline_window = recorder.add_window(
            baseline.samples, phase=PHASE_BASELINE, cell_id=trial_id,
            trial_id=trial_id, commanded_freq_hz=frequency_hz,
            commanded_amp=0)

    ser.reset_input_buffer()

    # Acquisition parses and stores only - every bit of detection runs
    # offline afterwards, so nothing competes with the 1.344 kHz stream.
    raw_samples: List[Sample] = []
    rel_times: List[float] = []
    t_cmd = time.monotonic()
    command_time_s = to_epoch_seconds(t_cmd)
    send(ser, f"S {1 << motor_index} {amp}", wait_s=0.0)
    t_off = t_cmd + float(vib_duration_s)

    aborted = False
    while True:
        now = time.monotonic()
        if now >= t_off:
            break
        # A drive window can be 10 s long, so Stop must be able to cut a
        # trial short; checked per block to keep the read loop lean.
        if len(raw_samples) % 64 == 0 and should_stop():
            aborted = True
            break
        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        sample = parse_acc_line(raw, acc_sensor_id)
        if sample is None:
            continue
        arrival = time.monotonic()
        if arrival >= t_off:
            # Past motor-off: keep the ring-down out of the window, so
            # the steady state stays a driven measurement.
            break
        x, y, z = sample
        raw_samples.append((arrival, x, y, z))
        rel_times.append(arrival - t_cmd)

    send(ser, "X", wait_s=0.0)
    if aborted:
        raise SweepAborted()

    if recorder is not None and raw_samples:
        recorder.add_window(raw_samples, phase=PHASE_VIBRATION,
                            cell_id=trial_id, trial_id=trial_id,
                            baseline_window_id=baseline_window,
                            commanded_freq_hz=frequency_hz, commanded_amp=amp)

    return analyse_trial(trial_id=trial_id, adhesion_method=adhesion_method,
                         baseline=baseline, raw_samples=raw_samples,
                         rel_times=rel_times, command_time_s=command_time_s,
                         vib_duration_s=vib_duration_s,
                         frequency_hz=frequency_hz, amp=amp,
                         motor_index=motor_index,
                         acc_sensor_id=acc_sensor_id, notes=notes, log=log)


# ==========================================
# Output files
# ==========================================

def run_stem(adhesion_method: str, stamp) -> str:
    """"adhesion_blu_tack_<ts>" - the stem every output file of one run
    shares (project-wide epoch-timestamp rule)."""
    return f"{RUN_PREFIX}{method_id(adhesion_method)}_{int(stamp)}"


def meta_path_for(csv_path: str) -> str:
    return os.path.splitext(csv_path)[0] + ".meta.json"


def png_path_for(csv_path: str) -> str:
    return os.path.splitext(csv_path)[0] + ".png"


def raw_file_for(csv_path: str) -> Optional[str]:
    """The run's raw three-axis sample file, or None when it is missing."""
    path = raw_path_for(csv_path)
    return path if os.path.exists(path) else None


def is_comparison_path(path: str) -> bool:
    """Whether a file in the output folder belongs to a COMPARISON rather
    than to a single-adhesive run (both live in the same folder)."""
    return os.path.basename(path).startswith(COMPARISON_PREFIX)


def sort_by_stamp(paths) -> List[str]:
    """Output files in CHRONOLOGICAL order.

    Every other experiment can just sort its file names as strings,
    because their names are "<fixed prefix>_<epoch>". This experiment's
    are "adhesion_<method>_<epoch>", so a plain string sort orders by
    METHOD first and only then by time - which would silently mislabel
    the "newest run" in the picker. The stamp is parsed out instead
    (falling back to the file's mtime when a name carries none)."""
    def key(path):
        stamp = report.stamp_from_path(path)
        if stamp is None:
            try:
                stamp = int(os.path.getmtime(path))
            except OSError:
                stamp = -1
        return (stamp, os.path.basename(path))
    return sorted(paths, key=key)


def run_csv_paths(output_dir: Optional[str] = None) -> List[str]:
    """Every saved single-adhesive run CSV, oldest first. Comparison
    outputs are excluded."""
    folder = output_dir or OUTPUT_DIR
    return sort_by_stamp(p for p in glob.glob(os.path.join(folder,
                                                           f"{RUN_PREFIX}*.csv"))
                         if not is_comparison_path(p))


#: Per-trial CSV columns. Identity and drive first, then the windows,
#: then the three instants and the intervals DERIVED from them, then the
#: steady state (amplitude, then spectrum), then the shared whole-window
#: intensity block (acceleration_metrics.METRIC_CSV_COLUMNS) so the table
#: lines up with every other experiment's.
TRIALS_CSV_COLUMNS: Tuple[str, ...] = (
    "adhesion_method", "adhesion_method_id", "trial_id", "status",
    "frequency_hz", "amp", "motor_index", "acc_sensor_id",
    "baseline_n_samples", "vibration_n_samples",
    "sample_rate_hz", "sampling_interval_ms",
    "vib_duration_s", "baseline_duration_s",
    "command_time_s", "onset_time_s", "stable_time_s",
    "steady_start_time_s", "steady_end_time_s",
    "onset_latency_from_command_ms", "settling_time_from_onset_ms",
    "stable_latency_from_command_ms", "rise_time_10_90_ms",
    "steady_region_source", "steady_start_rel_s", "steady_end_rel_s",
    "steady_duration_s",
    "steady_vector_rms_counts", "steady_vector_rms_ms2",
    "steady_legacy_magnitude_rms_counts", "steady_legacy_magnitude_rms_ms2",
    "steady_rms_x_counts", "steady_rms_y_counts", "steady_rms_z_counts",
    "peak_abs_counts", "peak_abs_ms2",
    "peak_robust_p99_counts", "peak_robust_p99_ms2",
    "peak_to_peak_x_counts", "peak_to_peak_y_counts", "peak_to_peak_z_counts",
    "peak_to_peak_max_counts", "peak_to_peak_max_ms2",
    "window_peak_dev_counts",
    "steady_state_envelope_counts", "steady_band_low_counts",
    "steady_band_high_counts", "env_noise_p95_counts",
    "dominant_frequency_hz", "dominant_amplitude_counts",
    "dominant_amplitude_ms2",
    "drive_freq_measured_hz", "drive_freq_amplitude_counts",
    "drive_freq_amplitude_ms2",
) + tuple(f"harmonic{order}_amplitude_counts"
          for order in range(2, N_HARMONICS + 1)) + (
    "thd_percent", "spectrum_n_samples", "spectrum_resolution_hz",
    "notes",
) + METRIC_CSV_COLUMNS


def _fmt(value: Optional[float], places: int = 3) -> str:
    """CSV cell for an optional float - EMPTY when the quantity was not
    measured. Never a stand-in number: an undetected settling time must
    read as missing, not as zero."""
    return "" if value is None else f"{float(value):.{places}f}"


def trial_row(r: AdhesionTrialResult) -> dict:
    """One trial as its CSV row (also the meta's per-trial record)."""
    row = {
        "adhesion_method": r.adhesion_method,
        "adhesion_method_id": method_id(r.adhesion_method),
        "trial_id": r.trial_id,
        "status": r.status,
        "frequency_hz": r.frequency_hz,
        "amp": r.amp,
        "motor_index": r.motor_index,
        "acc_sensor_id": r.acc_sensor_id,
        "baseline_n_samples": r.baseline_n_samples,
        "vibration_n_samples": r.vibration_n_samples,
        "sample_rate_hz": _fmt(r.sample_rate_hz, 1),
        "sampling_interval_ms": _fmt(r.sampling_interval_ms, 4),
        "vib_duration_s": _fmt(r.vib_duration_s, 2),
        "baseline_duration_s": _fmt(r.baseline_duration_s, 2),
        "command_time_s": _fmt(r.command_time_s, 6),
        "onset_time_s": _fmt(r.onset_time_s, 6),
        "stable_time_s": _fmt(r.stable_time_s, 6),
        "steady_start_time_s": _fmt(r.steady_start_time_s, 6),
        "steady_end_time_s": _fmt(r.steady_end_time_s, 6),
        "onset_latency_from_command_ms": _fmt(r.onset_latency_from_command_ms),
        "settling_time_from_onset_ms": _fmt(r.settling_time_from_onset_ms),
        "stable_latency_from_command_ms": _fmt(
            r.stable_latency_from_command_ms),
        "rise_time_10_90_ms": _fmt(r.rise_time_10_90_ms),
        "steady_region_source": r.steady_region_source,
        "steady_start_rel_s": _fmt(r.steady_start_rel_s, 5),
        "steady_end_rel_s": _fmt(r.steady_end_rel_s, 5),
        "steady_duration_s": _fmt(r.steady_duration_s, 5),
        "steady_vector_rms_counts": _fmt(r.steady_vector_rms_counts, 2),
        "steady_vector_rms_ms2": _fmt(r.steady_vector_rms_ms2, 5),
        "steady_legacy_magnitude_rms_counts": _fmt(
            r.steady_legacy_magnitude_rms_counts, 2),
        "steady_legacy_magnitude_rms_ms2": _fmt(
            r.steady_legacy_magnitude_rms_ms2, 5),
        "steady_rms_x_counts": _fmt(r.steady_rms_x_counts, 2),
        "steady_rms_y_counts": _fmt(r.steady_rms_y_counts, 2),
        "steady_rms_z_counts": _fmt(r.steady_rms_z_counts, 2),
        "peak_abs_counts": _fmt(r.peak_abs_counts, 2),
        "peak_abs_ms2": _fmt(r.peak_abs_ms2, 5),
        "peak_robust_p99_counts": _fmt(r.peak_robust_p99_counts, 2),
        "peak_robust_p99_ms2": _fmt(r.peak_robust_p99_ms2, 5),
        "peak_to_peak_x_counts": _fmt(r.peak_to_peak_x_counts, 2),
        "peak_to_peak_y_counts": _fmt(r.peak_to_peak_y_counts, 2),
        "peak_to_peak_z_counts": _fmt(r.peak_to_peak_z_counts, 2),
        "peak_to_peak_max_counts": _fmt(r.peak_to_peak_max_counts, 2),
        "peak_to_peak_max_ms2": _fmt(r.peak_to_peak_max_ms2, 5),
        "window_peak_dev_counts": _fmt(r.window_peak_dev_counts, 2),
        "steady_state_envelope_counts": _fmt(r.steady_state_envelope_counts, 2),
        "steady_band_low_counts": _fmt(r.steady_band_low_counts, 2),
        "steady_band_high_counts": _fmt(r.steady_band_high_counts, 2),
        "env_noise_p95_counts": _fmt(r.env_noise_p95_counts, 2),
        "dominant_frequency_hz": _fmt(r.spectrum.dominant_frequency_hz, 2),
        "dominant_amplitude_counts": _fmt(
            r.spectrum.dominant_amplitude_counts, 2),
        "dominant_amplitude_ms2": _fmt(r.dominant_amplitude_ms2, 5),
        "drive_freq_measured_hz": _fmt(r.spectrum.drive_freq_measured_hz, 2),
        "drive_freq_amplitude_counts": _fmt(
            r.spectrum.drive_freq_amplitude_counts, 2),
        "drive_freq_amplitude_ms2": _fmt(r.drive_freq_amplitude_ms2, 5),
        "thd_percent": _fmt(r.spectrum.thd_percent, 3),
        "spectrum_n_samples": r.spectrum.n_samples,
        "spectrum_resolution_hz": _fmt(r.spectrum.resolution_hz, 3),
        "notes": r.notes,
    }
    harmonics = list(r.spectrum.harmonic_amplitudes_counts)
    for index, order in enumerate(range(2, N_HARMONICS + 1)):
        value = harmonics[index] if index < len(harmonics) else None
        row[f"harmonic{order}_amplitude_counts"] = _fmt(value, 2)
    for column in METRIC_CSV_COLUMNS:
        row[column] = ("" if r.intensity is None
                       else getattr(r.intensity, column))
    return row


def save_trials_csv(path: str, results: List[AdhesionTrialResult]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(TRIALS_CSV_COLUMNS))
        writer.writeheader()
        for r in results:
            writer.writerow(trial_row(r))


# ==========================================
# Statistics
# ==========================================

def _series_stats(prefix: str, values: Sequence[float],
                  unit: str) -> Dict[str, Optional[float]]:
    """One series as the "<prefix>_mean_<unit>"-style keys report.stats_line
    reads, plus its SD-based coefficient of variation."""
    data = [float(v) for v in values if v is not None]
    mean = statistics.fmean(data) if data else None
    sd = statistics.stdev(data) if len(data) >= 2 else None
    return {
        f"{prefix}_n": len(data),
        f"{prefix}_mean_{unit}": mean,
        f"{prefix}_median_{unit}": statistics.median(data) if data else None,
        f"{prefix}_sd_{unit}": sd,
        f"{prefix}_min_{unit}": min(data) if data else None,
        f"{prefix}_max_{unit}": max(data) if data else None,
        f"{prefix}_cv_percent": (None if not mean or sd is None
                                 else 100.0 * sd / abs(mean)),
    }


@dataclass(frozen=True)
class Series:
    """One reported per-trial series: where its stats keys live, how it
    is printed, and how to read it off a trial."""
    prefix: str          # stats-dict key prefix ("<prefix>_mean_<unit>")
    unit: str            # key suffix - identifier-friendly ("ms", "ms2")
    display_unit: str    # what a reader sees ("ms", "m/s²")
    decimals: int
    label: str
    read: Callable[["AdhesionTrialResult"], Optional[float]]


#: The series every run reports. ONE table drives the run statistics, the
#: printed report, the meta and the comparison, so the four can never
#: list different numbers under the same name.
TRIAL_SERIES: Tuple[Series, ...] = (
    Series("onset", "ms", "ms", 2, "Vibration onset latency:",
           lambda r: r.onset_latency_from_command_ms),
    Series("rise", "ms", "ms", 2, "Rise time (10-90 %):",
           lambda r: r.rise_time_10_90_ms),
    Series("settling", "ms", "ms", 2, "Settling time from onset:",
           lambda r: r.settling_time_from_onset_ms),
    Series("stable", "ms", "ms", 2, "Stable latency from command:",
           lambda r: r.stable_latency_from_command_ms),
    Series("steady_rms", "ms2", "m/s²", 3, "Steady vector RMS:",
           lambda r: r.steady_vector_rms_ms2),
    Series("peak_robust", "ms2", "m/s²", 3, "Robust peak (p99):",
           lambda r: r.peak_robust_p99_ms2),
    Series("peak_abs", "ms2", "m/s²", 3, "Absolute peak:",
           lambda r: r.peak_abs_ms2),
    Series("peak_to_peak", "ms2", "m/s²", 3, "Peak-to-peak (max axis):",
           lambda r: r.peak_to_peak_max_ms2),
    Series("fundamental", "ms2", "m/s²", 3, "Amplitude at drive freq:",
           lambda r: r.drive_freq_amplitude_ms2),
    Series("dominant", "hz", "Hz", 2, "Dominant frequency:",
           lambda r: r.spectrum.dominant_frequency_hz),
    Series("thd", "percent", "%", 2, "THD:",
           lambda r: r.spectrum.thd_percent),
)

SERIES_BY_PREFIX: Dict[str, Series] = {s.prefix: s for s in TRIAL_SERIES}


def series_line(series: Series, stats: dict) -> Optional[str]:
    """One series' "mean / median / SD / range / CV (n)" report line."""
    line = report.stats_line(series.label, stats, series.prefix,
                             unit=series.unit, label_width=30,
                             decimals=series.decimals,
                             display_unit=series.display_unit)
    if line is None:
        return None
    cv = stats.get(f"{series.prefix}_cv_percent")
    return line + (f", CV {cv:.1f} %" if cv is not None else "")

#: Series measured on the STEADY STATE - only trials whose steady region
#: is a settled one contribute to them (see STEADY_FALLBACK).
STEADY_SERIES = ("steady_rms", "peak_robust", "peak_abs", "peak_to_peak",
                 "fundamental", "dominant", "thd")


def run_stats(results: List[AdhesionTrialResult]) -> dict:
    """Per-run summary: every series' mean/median/SD/range/CV, the
    detection-status counts, and how many trials the steady-state
    statistics are actually based on."""
    settled = [r for r in results if r.settled]
    stats: Dict[str, object] = {
        "n_trials": len(results),
        "n_ok": sum(1 for r in results if r.status == mad.STATUS_OK),
        "n_no_onset": sum(1 for r in results
                          if r.status == mad.STATUS_NO_ONSET),
        "n_not_settled": sum(1 for r in results
                             if r.status == mad.STATUS_NOT_SETTLED),
        "n_insufficient_data": sum(1 for r in results
                                   if r.status == mad.STATUS_INSUFFICIENT_DATA),
        "n_settled_steady_state": len(settled),
        "n_fallback_steady_state": sum(
            1 for r in results if r.steady_region_source == STEADY_FALLBACK),
        "adhesion_method": results[0].adhesion_method if results else None,
        "frequency_hz": results[0].frequency_hz if results else None,
        "amp": results[0].amp if results else None,
        "motor_index": results[0].motor_index if results else None,
        "acc_sensor_id": results[0].acc_sensor_id if results else None,
    }
    for series in TRIAL_SERIES:
        source = settled if series.prefix in STEADY_SERIES else results
        stats.update(_series_stats(series.prefix,
                                   [series.read(r) for r in source],
                                   series.unit))
    return stats


# ==========================================
# Figure
# ==========================================

def _representative(results: List[AdhesionTrialResult]
                    ) -> Optional[AdhesionTrialResult]:
    """The trial the waveform panels are drawn for: the median-steady-RMS
    settled trial, else any trial that has a trace."""
    settled = [r for r in results if r.settled and len(r.trace)
               and r.steady_vector_rms_counts is not None]
    if settled:
        settled.sort(key=lambda r: r.steady_vector_rms_counts)
        return settled[len(settled) // 2]
    traced = [r for r in results if len(r.trace)]
    return traced[0] if traced else None


def mean_steady_spectrum(results: List[AdhesionTrialResult]
                         ) -> Optional[VibrationSpectrum]:
    """The mean steady-state amplitude spectrum over the settled trials
    (each trial's spectrum interpolated onto the first one's frequency
    grid, so trials of slightly different length still average)."""
    spectra = []
    for r in results:
        if not (r.settled and len(r.trace) and r.steady_start_rel_s is not None):
            continue
        mask = ((r.trace.rel_times >= r.steady_start_rel_s)
                & (r.trace.rel_times <= r.steady_end_rel_s))
        if int(np.count_nonzero(mask)) < 32 or r.sample_rate_hz <= 0:
            continue
        try:
            spectra.append(compute_vibration_spectrum(
                r.trace.axes[mask], r.sample_rate_hz))
        except ValueError:
            continue
    if not spectra:
        return None
    grid = spectra[0].freqs_hz
    stack = [np.interp(grid, s.freqs_hz, s.amplitude_counts) for s in spectra]
    return VibrationSpectrum(
        freqs_hz=grid,
        amplitude_counts=np.mean(np.vstack(stack), axis=0),
        sample_rate_hz=spectra[0].sample_rate_hz,
        n_samples=spectra[0].n_samples,
    )


def _mean_sd_band(ax, values: Sequence[Optional[float]], colour: str,
                  label: str) -> None:
    """Mean line + ±SD band across trials on a per-trial panel."""
    data = [v for v in values if v is not None]
    if not data:
        return
    mean = statistics.fmean(data)
    ax.axhline(mean, linestyle="--", linewidth=1, color=colour,
               label=f"{label} mean = {mean:.3g}")
    if len(data) >= 2:
        sd = statistics.stdev(data)
        ax.axhspan(mean - sd, mean + sd, color=colour, alpha=0.12,
                   label=f"±SD = {sd:.3g}")


def save_plot(path: str, results: List[AdhesionTrialResult],
              adhesion_method: str, frequency_hz: int, amp: int,
              motor_index: int, notes: str = "") -> None:
    """The per-run summary figure: the raw three-axis waveform, the
    envelope with every marked instant, the steady-state spectrum, and
    the per-trial intensity / timing scatter with their mean ± SD.

    Nothing is smoothed for looks: the waveform panel draws the recorded
    samples, and a trial with no steady state simply has no point."""
    stats = run_stats(results)
    representative = _representative(results)

    fig = plt.figure(figsize=(13, 12))
    grid = fig.add_gridspec(3, 2, height_ratios=[1.0, 1.0, 1.0], hspace=0.42,
                            wspace=0.24)
    xs = [r.trial_id for r in results]
    ticks = xs if len(xs) <= 20 else None

    def series(read):
        return [read(r) if read(r) is not None else float("nan")
                for r in results]

    # -- panel 1: the demeaned three-axis waveform -----------------------
    # Left: the whole drive window, with every instant marked. Right: a
    # WAVEFORM_ZOOM_MS window inside the steady state - at full width a
    # 224 Hz carrier is a solid block, and the shape of the individual
    # cycles is exactly what "is the waveform distorted?" asks about.
    ax1 = fig.add_subplot(grid[0, 0])
    ax1z = fig.add_subplot(grid[0, 1])
    axis_styles = (("X", "tab:blue"), ("Y", "tab:orange"), ("Z", "tab:green"))
    if representative is not None and len(representative.trace):
        trace = representative.trace
        times_ms = trace.rel_times * 1000.0
        for index, (name, colour) in enumerate(axis_styles):
            ax1.plot(times_ms, trace.axes[:, index], linewidth=0.6,
                     alpha=0.85, color=colour, label=f"{name} (demeaned)")
        ax1.axvline(0.0, color="black", linewidth=1.4)
        if representative.onset_latency_from_command_ms is not None:
            ax1.axvline(representative.onset_latency_from_command_ms,
                        color="tab:red", linestyle="--", linewidth=1,
                        label="vibration onset")
        if representative.stable_latency_from_command_ms is not None:
            ax1.axvline(representative.stable_latency_from_command_ms,
                        color="tab:purple", linestyle="-.", linewidth=1,
                        label="stable state")
        if representative.steady_start_rel_s is not None:
            ax1.axvspan(representative.steady_start_rel_s * 1000.0,
                        representative.steady_end_rel_s * 1000.0,
                        color="tab:green", alpha=0.10,
                        label="steady state "
                              f"({representative.steady_region_source})")
        ax1.set_title(f"Three-axis waveform, trial {representative.trial_id} "
                      "(each axis minus its baseline mean)", fontsize=10)
        ax1.legend(fontsize=7, ncols=2)

        # The zoom starts a little inside the steady state, so it shows
        # steady cycles rather than the last of the ring-up.
        start = (representative.steady_start_rel_s
                 if representative.steady_start_rel_s is not None
                 else float(trace.rel_times[len(trace) // 2]))
        start += 0.010
        mask = ((trace.rel_times >= start)
                & (trace.rel_times <= start + WAVEFORM_ZOOM_MS / 1000.0))
        for index, (name, colour) in enumerate(axis_styles):
            ax1z.plot(trace.rel_times[mask] * 1000.0, trace.axes[mask, index],
                      linewidth=1.0, marker=".", markersize=2, color=colour,
                      label=f"{name} (demeaned)")
        ax1z.set_title(f"Steady-state waveform shape, {WAVEFORM_ZOOM_MS:g} ms "
                       f"zoom (~{WAVEFORM_ZOOM_MS * frequency_hz / 1000:.0f} "
                       "drive cycles)", fontsize=10)
        ax1z.legend(fontsize=7, ncols=3)
    else:
        ax1.set_title("Three-axis waveform (no per-sample data for this run)",
                      fontsize=10)
        ax1z.set_title("Steady-state waveform shape (no per-sample data)",
                       fontsize=10)
    for ax in (ax1, ax1z):
        ax.set_xlabel("Time since motor command (ms)")
        ax.set_ylabel("Acceleration (counts)")
        ax.grid(True, alpha=0.4)

    # -- panel 2: the envelope of every trial ----------------------------
    ax2 = fig.add_subplot(grid[1, 0])
    drew = False
    for r in results:
        if not len(r.trace) or r.trace.envelope.size != len(r.trace):
            continue
        drew = True
        is_rep = representative is not None and r.trial_id == representative.trial_id
        ax2.plot(r.trace.rel_times * 1000.0, r.trace.envelope,
                 linewidth=1.8 if is_rep else 0.7,
                 alpha=1.0 if is_rep else 0.35,
                 color="tab:blue" if is_rep else "tab:gray",
                 label=f"trial {r.trial_id}" if is_rep else "_nolegend_")
        for value, marker, colour in (
                (r.onset_latency_from_command_ms, "v", "tab:red"),
                (r.stable_latency_from_command_ms, "o", "tab:purple")):
            if value is not None:
                ax2.plot(value, np.interp(value / 1000.0, r.trace.rel_times,
                                          r.trace.envelope),
                         marker=marker, color=colour,
                         markersize=8 if is_rep else 4, zorder=5)
    ax2.axvline(0.0, color="black", linewidth=1.2)
    ax2.set_title("Vibration envelope per trial   (▼ onset, ● stable state)"
                  if drew else "Vibration envelope (no per-sample data)")
    ax2.set_xlabel("Time since motor command (ms)")
    ax2.set_ylabel(f"Per-axis deviation envelope (counts)\n"
                   f"centred {mad.ENVELOPE_WINDOW_S * 1000:.0f} ms moving RMS")
    ax2.grid(True, alpha=0.4)
    if drew:
        ax2.legend(fontsize=7)

    # -- panel 3: the steady-state spectrum ------------------------------
    ax3 = fig.add_subplot(grid[1, 1])
    spectrum = mean_steady_spectrum(results)
    if spectrum is not None:
        mask = spectrum.freqs_hz <= max(4.0 * frequency_hz, 200.0)
        ax3.semilogy(spectrum.freqs_hz[mask],
                     np.maximum(spectrum.amplitude_counts[mask], 1e-3),
                     linewidth=1.0, color="tab:blue")
        ax3.axvline(frequency_hz, color="tab:red", linestyle="--", linewidth=1,
                    label=f"drive {frequency_hz} Hz")
        for order in range(2, N_HARMONICS + 1):
            harmonic = frequency_hz * order
            if harmonic <= spectrum.freqs_hz[-1]:
                ax3.axvline(harmonic, color="tab:orange", linestyle=":",
                            linewidth=0.8,
                            label="harmonics" if order == 2 else "_nolegend_")
        ax3.set_title("Mean steady-state amplitude spectrum "
                      f"({stats['n_settled_steady_state']} settled trials)")
        ax3.legend(fontsize=7)
    else:
        ax3.set_title("Steady-state spectrum (no settled trial with samples)")
    ax3.set_xlabel("Frequency (Hz)")
    ax3.set_ylabel("Amplitude (counts)")
    ax3.grid(True, which="both", alpha=0.4)

    # -- panel 4: per-trial intensity ------------------------------------
    ax4 = fig.add_subplot(grid[2, 0])
    ax4.plot(xs, series(lambda r: r.steady_vector_rms_ms2), marker="o",
             color="tab:blue", label="Steady vector RMS")
    ax4.plot(xs, series(lambda r: r.peak_robust_p99_ms2), marker="s",
             color="tab:green", label=f"Robust peak (p{ROBUST_PEAK_PERCENTILE:g})")
    ax4.plot(xs, series(lambda r: r.peak_abs_ms2), marker="x", linestyle=":",
             color="tab:red", label="Absolute peak")
    _mean_sd_band(ax4, [r.steady_vector_rms_ms2 for r in results],
                  "tab:blue", "RMS")
    ax4.set_title("Steady-state intensity per trial")
    ax4.set_xlabel("Trial")
    ax4.set_ylabel("Acceleration (m/s²)")
    ax4.grid(True, alpha=0.4)
    ax4.legend(fontsize=7)
    if ticks:
        ax4.set_xticks(ticks)

    # -- panel 5: per-trial timing ---------------------------------------
    ax5 = fig.add_subplot(grid[2, 1])
    ax5.plot(xs, series(lambda r: r.onset_latency_from_command_ms), marker="o",
             color="tab:red", label="Onset latency (command → onset)")
    ax5.plot(xs, series(lambda r: r.rise_time_10_90_ms), marker="^",
             color="tab:orange", label="Rise time (10-90 %)")
    ax5.plot(xs, series(lambda r: r.settling_time_from_onset_ms), marker="s",
             color="tab:purple", label="Settling time (onset → stable)")
    ax5.set_yscale("symlog", linthresh=10)
    unresolved = stats["n_no_onset"] + stats["n_not_settled"]
    ax5.set_title("Timing per trial"
                  + (f"   ({unresolved} trial(s) without a stable state)"
                     if unresolved else ""))
    ax5.set_xlabel("Trial")
    ax5.set_ylabel("Time (ms, symlog)")
    ax5.grid(True, which="both", alpha=0.4)
    ax5.legend(fontsize=7)
    if ticks:
        ax5.set_xticks(ticks)

    subtitle = (f"{adhesion_method} · LRA {frequency_hz} Hz, amp {amp}, motor "
                f"port {motor_index} · {stats['n_trials']} trials "
                f"({stats['n_ok']} ok)")
    if notes:
        subtitle += f"\nNotes: {notes}"
    fig.suptitle(f"Adhesion vibration transfer - {subtitle}", fontsize=12)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ==========================================
# Meta
# ==========================================

def save_meta(csv_path: str, png_path: str, stamp, firmware,
              adhesion_method: str, results: List[AdhesionTrialResult],
              frequency_hz: int, amp: int, motor_index: int,
              acc_sensor_id: int, vib_duration_s: float,
              rest_duration_s: float, num_trials: int, notes: str,
              raw_path: Optional[str] = None,
              installation_notes: Optional[dict] = None) -> str:
    """Write the run's .meta.json: what was sent to the rig, the config it
    came from, the mounting/validity record, and the result block."""
    configured_freq, configured_amp = lra_frequency_hz(), lra_amp()
    meta = {
        "experiment": "adhesion_vibration_comparison",
        "saved_at": int(stamp),
        "firmware": firmware,
        "adhesion_method": adhesion_method,
        "adhesion_method_id": method_id(adhesion_method),
        "adhesion_methods": list(ADHESION_METHODS),
        "parameters": {
            "actuator_type": ACTUATOR_TYPE,
            "frequency_hz": frequency_hz,
            "amp": amp,
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "num_trials": num_trials,
            "vib_duration_s": vib_duration_s,
            "rest_duration_s": rest_duration_s,
            "baseline_duration_s": BASELINE_DURATION_S,
            "acc_interval_ms": ACC_INTERVAL_MS,
            "notes": notes,
        },
        # Where the delivered drive came from. This experiment ALWAYS
        # reads the LRA block, whatever haptic.using currently says, so
        # that three adhesives can only ever be compared at one drive.
        "haptic_config": {
            "snapshot": hc.config_snapshot(),
            "reads": "haptic.lra (fixed - this experiment is LRA-only)",
            "using_at_run_time": hc.get_active_haptic_type(),
            "configured_lra_frequency_hz": configured_freq,
            "configured_lra_amp": configured_amp,
            "frequency_source": hc.value_source(frequency_hz, configured_freq),
            "amp_source": hc.value_source(amp, configured_amp),
            "motor_index_source": hc.value_source(motor_index, MOTOR_INDEX),
            "note": ("'parameters' records what was sent to the rig; this "
                     "block records the config it was compared against"),
        },
        "analysis": {
            "adhesion_metric_version": ADHESION_METRIC_VERSION,
            "onset_settling": ("motor_acc_delay.detect_onset_and_settling - "
                               "the SAME detector the Motor → ACC Delay "
                               "experiment uses, not a second definition"),
            "steady_state": ("stable time → motor-off; a trial that never "
                             f"settles falls back to the last "
                             f"{STEADY_FALLBACK_FRACTION:.0%} of the drive "
                             "window and is flagged "
                             f"'{STEADY_FALLBACK}'"),
            "steady_min_s": STEADY_MIN_S,
            "robust_peak_percentile": ROBUST_PEAK_PERCENTILE,
            "spectrum": ("per-axis demeaned Hann-windowed rFFT, combined in "
                         "quadrature over the three axes; tone amplitudes "
                         "estimated from the main-lobe energy / ENBW, so "
                         "they are independent of where the tone falls "
                         "between bin centres"),
            "spectrum_min_hz": SPECTRUM_MIN_HZ,
            "spectrum_tolerance_hz": SPECTRUM_TOLERANCE_HZ,
            "n_harmonics": N_HARMONICS,
            "thd": "sqrt(sum_{k>=2} A_k^2) / A_1 over the measured harmonics",
            "statuses": list(mad.DETECTION_STATUSES),
            "detection_parameters": mad.detection_parameters(vib_duration_s),
        },
        "time_definitions": {
            "command_time_s": "Unix epoch seconds at the 'S' motor-on write",
            "onset_time_s": "Unix epoch seconds of the vibration onset",
            "stable_time_s": "Unix epoch seconds of the steady state",
            "onset_latency_from_command_ms": "onset_time - command_time",
            "settling_time_from_onset_ms": "stable_time - onset_time",
            "stable_latency_from_command_ms": "stable_time - command_time",
            "rise_time_10_90_ms": ("envelope 10 % → 90 % of the steady "
                                   "level, measured from the onset"),
        },
        "validity": {
            "protocol": VALIDITY_RULES,
            "installation": dict(installation_notes or {}),
        },
        "files": {
            "csv": os.path.basename(csv_path),
            "png": os.path.basename(png_path),
            "raw_acceleration": (os.path.basename(raw_path)
                                 if raw_path else None),
        },
        "metrics": dict(
            metric_meta_block(None, raw_path is not None,
                              os.path.basename(raw_path) if raw_path else None,
                              MS2_PER_COUNT),
            applies_to=("whole-drive-window statistics; the steady-state "
                        "figures are the steady_* columns"),
        ),
        "result": run_stats(results),
        "trials": [trial_row(r) for r in results],
    }
    path = meta_path_for(csv_path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return path


#: The mounting rules that make three runs comparable at all. Shown in
#: the GUI, repeated in the README, and stored in every run's meta so a
#: saved run carries the protocol it claims to follow.
VALIDITY_RULES: Tuple[str, ...] = (
    "the same physical LRA for all three adhesion methods",
    "the same accelerometer (sensor id) and the same motor port",
    "the same mounting position and orientation on the actuator",
    "the same mounting surface",
    "as close as possible the same adhesive area and thickness",
    "a consistent curing time for the cosmetic adhesive",
    "several trials per method - every re-mount adds variation",
    "for a stricter result, vary the ORDER the three methods are tested "
    "in, so temperature, LRA self-heating or battery level cannot bias "
    "one method systematically",
)


def load_meta(csv_path: str) -> Optional[dict]:
    path = meta_path_for(csv_path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ==========================================
# Loading a saved run
# ==========================================

def _opt_float(row: dict, *keys: str) -> Optional[float]:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _opt_int(row: dict, key: str, default: int = 0) -> int:
    value = _opt_float(row, key)
    return default if value is None else int(value)


def _intensity_from_row(row: dict) -> Optional[AccelerationMetrics]:
    if not row.get("vector_rms_counts"):
        return None
    values = {}
    for column in METRIC_CSV_COLUMNS:
        value = _opt_float(row, column)
        if value is None:
            return None
        values[column] = int(value) if column == "n_samples" else value
    return AccelerationMetrics(**values)


def load_results(csv_path: str) -> List[AdhesionTrialResult]:
    """Read back an adhesion_<method>_<ts>.csv (summary rows; the
    per-sample series live in the run's .raw_acc.npz - see
    attach_traces())."""
    results: List[AdhesionTrialResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            spectrum = SpectrumMetrics(
                dominant_frequency_hz=_opt_float(row, "dominant_frequency_hz"),
                dominant_amplitude_counts=_opt_float(
                    row, "dominant_amplitude_counts"),
                drive_freq_measured_hz=_opt_float(row,
                                                  "drive_freq_measured_hz"),
                drive_freq_amplitude_counts=_opt_float(
                    row, "drive_freq_amplitude_counts"),
                harmonic_amplitudes_counts=[
                    _opt_float(row, f"harmonic{order}_amplitude_counts")
                    for order in range(2, N_HARMONICS + 1)],
                thd_ratio=(None if _opt_float(row, "thd_percent") is None
                           else _opt_float(row, "thd_percent") / 100.0),
                n_samples=_opt_int(row, "spectrum_n_samples"),
                resolution_hz=_opt_float(row, "spectrum_resolution_hz"),
            )
            results.append(AdhesionTrialResult(
                adhesion_method=normalise_method(row["adhesion_method"]),
                trial_id=_opt_int(row, "trial_id"),
                status=row.get("status", ""),
                frequency_hz=_opt_int(row, "frequency_hz"),
                amp=_opt_int(row, "amp"),
                motor_index=_opt_int(row, "motor_index"),
                acc_sensor_id=_opt_int(row, "acc_sensor_id"),
                notes=row.get("notes", "") or "",
                baseline_n_samples=_opt_int(row, "baseline_n_samples"),
                vibration_n_samples=_opt_int(row, "vibration_n_samples"),
                sample_rate_hz=_opt_float(row, "sample_rate_hz") or 0.0,
                sampling_interval_ms=_opt_float(
                    row, "sampling_interval_ms") or float("nan"),
                vib_duration_s=_opt_float(row, "vib_duration_s") or 0.0,
                baseline_duration_s=_opt_float(
                    row, "baseline_duration_s") or BASELINE_DURATION_S,
                command_time_s=_opt_float(row, "command_time_s"),
                onset_time_s=_opt_float(row, "onset_time_s"),
                stable_time_s=_opt_float(row, "stable_time_s"),
                steady_start_time_s=_opt_float(row, "steady_start_time_s"),
                steady_end_time_s=_opt_float(row, "steady_end_time_s"),
                onset_latency_from_command_ms=_opt_float(
                    row, "onset_latency_from_command_ms"),
                settling_time_from_onset_ms=_opt_float(
                    row, "settling_time_from_onset_ms"),
                stable_latency_from_command_ms=_opt_float(
                    row, "stable_latency_from_command_ms"),
                rise_time_10_90_ms=_opt_float(row, "rise_time_10_90_ms"),
                steady_region_source=row.get("steady_region_source",
                                             STEADY_NONE) or STEADY_NONE,
                steady_start_rel_s=_opt_float(row, "steady_start_rel_s"),
                steady_end_rel_s=_opt_float(row, "steady_end_rel_s"),
                steady_duration_s=_opt_float(row, "steady_duration_s"),
                steady_vector_rms_counts=_opt_float(
                    row, "steady_vector_rms_counts"),
                steady_legacy_magnitude_rms_counts=_opt_float(
                    row, "steady_legacy_magnitude_rms_counts"),
                steady_rms_x_counts=_opt_float(row, "steady_rms_x_counts"),
                steady_rms_y_counts=_opt_float(row, "steady_rms_y_counts"),
                steady_rms_z_counts=_opt_float(row, "steady_rms_z_counts"),
                peak_abs_counts=_opt_float(row, "peak_abs_counts"),
                peak_robust_p99_counts=_opt_float(row,
                                                  "peak_robust_p99_counts"),
                peak_to_peak_x_counts=_opt_float(row, "peak_to_peak_x_counts"),
                peak_to_peak_y_counts=_opt_float(row, "peak_to_peak_y_counts"),
                peak_to_peak_z_counts=_opt_float(row, "peak_to_peak_z_counts"),
                peak_to_peak_max_counts=_opt_float(
                    row, "peak_to_peak_max_counts"),
                window_peak_dev_counts=_opt_float(row,
                                                  "window_peak_dev_counts"),
                steady_state_envelope_counts=_opt_float(
                    row, "steady_state_envelope_counts"),
                steady_band_low_counts=_opt_float(row,
                                                  "steady_band_low_counts"),
                steady_band_high_counts=_opt_float(row,
                                                   "steady_band_high_counts"),
                env_noise_p95_counts=_opt_float(
                    row, "env_noise_p95_counts") or 0.0,
                spectrum=spectrum,
                intensity=_intensity_from_row(row),
            ))
    if not results:
        raise ValueError(f"No trial rows found in {csv_path}")
    return results


def attach_traces(csv_path: str, results: List[AdhesionTrialResult]) -> int:
    """Rebuild every trial's per-sample series from the run's raw NPZ, so
    a re-opened run redraws the same waveform/envelope panels as the live
    run did. Returns how many trials got a trace (0 when the run has no
    raw file). The saved data is never modified."""
    raw_path = raw_file_for(csv_path)
    if raw_path is None:
        return 0
    raw = load_raw_acceleration_samples(raw_path)
    by_trial = {r.trial_id: r for r in results}
    attached = 0
    for window_id in raw.window_ids(phase=PHASE_VIBRATION):
        info = raw.window_info(window_id)
        result = by_trial.get(int(info["trial_id"]))
        if result is None:
            continue
        samples = raw.samples_for_window(window_id)
        if samples.shape[0] < 2:
            continue
        baseline_samples = None
        base_id = info["baseline_window_id"]
        if base_id >= 0:
            baseline_samples = raw.samples_for_window(base_id)
        if baseline_samples is not None and baseline_samples.shape[0]:
            bx, by, bz = as_xyz_arrays(baseline_samples)
            means = (float(bx.mean()), float(by.mean()), float(bz.mean()))
        else:
            # No baseline window stored: fall back to the drive window's
            # own means, which removes gravity just as well but cannot
            # show the static shift the baseline would have revealed.
            wx, wy, wz = as_xyz_arrays(samples)
            means = (float(wx.mean()), float(wy.mean()), float(wz.mean()))
        times = samples[:, 0]
        rel_times = times - (result.command_time_s
                             if result.command_time_s else times[0])
        deltas = mad.per_axis_deltas(samples, means)
        fs = result.sample_rate_hz or mad.estimate_sample_rate_hz(rel_times)
        window = max(1, int(round(mad.ENVELOPE_WINDOW_S * fs))) if fs else 1
        x, y, z = as_xyz_arrays(samples)
        result.trace = TrialTrace(
            rel_times=np.asarray(rel_times, dtype=float),
            axes=np.column_stack((x - means[0], y - means[1], z - means[2])),
            dev=np.asarray(deltas, dtype=float),
            envelope=np.asarray(mad.moving_rms(deltas, window), dtype=float),
        )
        attached += 1
    return attached


def render_csv(csv_path: str, out_png: str) -> dict:
    """Re-render a saved run's figure from its own files (CSV + meta +
    raw NPZ) and return its summary statistics. Saved outputs are never
    modified - the figure goes wherever the caller asks."""
    meta = load_meta(csv_path)
    params = (meta or {}).get("parameters", {})
    results = load_results(csv_path)
    attach_traces(csv_path, results)

    first = results[0]
    method = (meta or {}).get("adhesion_method") or first.adhesion_method
    frequency_hz = params.get("frequency_hz") or first.frequency_hz
    amp = params.get("amp") if params.get("amp") is not None else first.amp
    motor_index = (params.get("motor_index") if params.get("motor_index")
                   is not None else first.motor_index)
    notes = params.get("notes", first.notes)

    save_plot(out_png, results, method, int(frequency_hz), int(amp),
              int(motor_index), notes=notes)
    stats = run_stats(results)
    stats.update({"adhesion_method": method, "frequency_hz": int(frequency_hz),
                  "amp": int(amp), "motor_index": int(motor_index),
                  "notes": notes, "csv_path": csv_path, "png_path": out_png})
    return stats


# ==========================================
# Text report
# ==========================================

def _trial_rows(results: List[AdhesionTrialResult]) -> list:
    return [(r.trial_id, r.status,
             report.number(r.onset_latency_from_command_ms),
             report.number(r.rise_time_10_90_ms),
             report.number(r.settling_time_from_onset_ms),
             report.number(r.steady_vector_rms_ms2, 3),
             report.number(r.peak_robust_p99_ms2, 3),
             report.number(r.peak_abs_ms2, 3),
             report.number(r.drive_freq_amplitude_ms2, 3),
             report.number(r.spectrum.dominant_frequency_hz, 1),
             report.number(r.spectrum.thd_percent, 1),
             r.steady_region_source)
            for r in results]


def summary_report(csv_path: str, summary: Optional[dict] = None) -> list:
    """The run's statistics as text, rebuilt from its saved files - the
    same wording the live run prints."""
    results = load_results(csv_path)
    meta = load_meta(csv_path)
    params = (meta or {}).get("parameters", {})
    stats = dict(summary) if summary is not None else run_stats(results)

    method = ((meta or {}).get("adhesion_method")
              or stats.get("adhesion_method") or results[0].adhesion_method)
    details = [
        f"Adhesion method: {method}",
        f"LRA on motor port {params.get('motor_index', results[0].motor_index)}"
        f", {params.get('frequency_hz', results[0].frequency_hz)} Hz, amp "
        f"{params.get('amp', results[0].amp)}"
        + (f", {params['vib_duration_s']:g} s drive"
           if params.get("vib_duration_s") else ""),
        f"{stats.get('n_trials', len(results))} trials, ACC sensor "
        f"{params.get('acc_sensor_id', results[0].acc_sensor_id)}, "
        f"{params.get('acc_interval_ms', ACC_INTERVAL_MS)} ms ACC interval",
    ]
    haptic = (meta or {}).get("haptic_config", {})
    if haptic.get("frequency_source") or haptic.get("amp_source"):
        details.append(
            f"drive source: frequency {haptic.get('frequency_source', '?')}, "
            f"amp {haptic.get('amp_source', '?')} (always read from "
            "haptic.lra)")
    if params.get("notes"):
        details.append(f"notes: {params['notes']}")

    lines = report.header("Statistics", csv_path, meta, details)
    lines.append("")
    lines.extend(report.table(
        ("trial", "status", "onset ms", "rise ms", "settling ms",
         "RMS m/s²", "p99 peak", "abs peak", f"A@{results[0].frequency_hz}Hz",
         "f_dom Hz", "THD %", "steady from"),
        _trial_rows(results)))
    lines.append("")
    for series in TRIAL_SERIES:
        line = series_line(series, stats)
        if line:
            lines.append(line)
    lines.append(
        f"Detected: {stats.get('n_ok', 0)} ok, "
        f"{stats.get('n_no_onset', 0)} no_onset, "
        f"{stats.get('n_not_settled', 0)} not_settled, "
        f"{stats.get('n_insufficient_data', 0)} insufficient_data "
        f"(of {stats.get('n_trials', len(results))} trials)")
    lines.append(
        f"Steady-state statistics use the {stats.get('n_settled_steady_state', 0)}"
        " settled trial(s)"
        + (f"; {stats['n_fallback_steady_state']} trial(s) never settled and "
           "their steady-state numbers come from the window tail "
           f"({STEADY_FALLBACK}) - excluded from the statistics above."
           if stats.get("n_fallback_steady_state") else "."))
    lines.append("This run measures ONE adhesion method. Use \"Analyse "
                 "adhesion methods\" to compare three runs; a single run "
                 "says nothing about relative vibration transfer.")
    return lines


# ==========================================
# Experiment
# ==========================================

def run_experiment(log: Optional[LogFn] = None,
                   progress: Optional[ProgressFn] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   interactive: bool = True,
                   motor_index: int = MOTOR_INDEX,
                   acc_sensor_id: int = ACC_SENSOR_ID,
                   adhesion_method: str = ADHESION_METHODS[0],
                   num_trials: int = NUM_TRIALS,
                   vib_duration_s: float = VIB_DURATION_S,
                   rest_duration_s: float = REST_DURATION_S,
                   notes: str = "",
                   installation_notes: Optional[dict] = None) -> dict:
    """Measure ONE adhesion method and write its output files.

    The drive is not a parameter: the LRA's configured frequency and amp
    (config.json haptic.lra) are read here and delivered unchanged, so
    every run of every adhesive is driven identically. log/progress/
    should_stop let a GUI wrapper stream the console output, drive a
    progress bar and abort between trials."""
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

    method = normalise_method(adhesion_method)
    num_trials = int(min(max(int(num_trials), NUM_TRIALS_MIN), NUM_TRIALS_MAX))
    vib_duration_s = float(min(max(float(vib_duration_s), VIB_DURATION_MIN_S),
                               VIB_DURATION_MAX_S))
    rest_duration_s = float(min(max(float(rest_duration_s),
                                    REST_DURATION_MIN_S), REST_DURATION_MAX_S))
    frequency_hz, amp = lra_frequency_hz(), lra_amp()

    log(f"Adhesion vibration comparison - {method}")
    log(f"LRA on motor port {motor_index}, {frequency_hz} Hz, amp {amp} "
        f"(config.json haptic.lra; haptic.using is currently "
        f"'{hc.get_active_haptic_type()}' and is deliberately ignored here).")
    log(f"{num_trials} trials x ({BASELINE_DURATION_S:g} s baseline + "
        f"{vib_duration_s:g} s vibration + {rest_duration_s:g} s rest); "
        f"estimated duration ~"
        f"{estimated_duration_s(num_trials, vib_duration_s, rest_duration_s):.0f} s.")
    log("Mount rules for a valid comparison:")
    for rule in VALIDITY_RULES:
        log(f"  · {rule}")
    if notes:
        log(f"Notes: {notes}")
    log("")

    stamp = str(int(time.time()))
    recorder = RawSampleRecorder(
        run_id=run_stem(method, stamp),
        experiment="adhesion_vibration_comparison",
        ms2_per_count=MS2_PER_COUNT,
        sensor_id=acc_sensor_id,
        meta={"adhesion_method": method,
              "adhesion_method_id": method_id(method),
              "actuator_type": ACTUATOR_TYPE,
              "motor_index": motor_index,
              "frequency_hz": frequency_hz, "amp": amp,
              "acc_interval_ms": ACC_INTERVAL_MS,
              "baseline_duration_s": BASELINE_DURATION_S,
              "vib_duration_s": vib_duration_s,
              "rest_duration_s": rest_duration_s,
              "notes": notes,
              "window_note": ("'vibration' windows span the whole drive "
                              "period, from the motor-on command to "
                              "motor-off - the quiet pre-onset samples, the "
                              "ring-up and the steady state, and no "
                              "ring-down")},
    )

    ser = open_rig(log=log, interactive=interactive)
    results: List[AdhesionTrialResult] = []
    try:
        send(ser, "X")
        send(ser, f"F {motor_index} {frequency_hz}")
        log(f"PWM frequency on port {motor_index}: {frequency_hz} Hz (LRA)")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        for trial_id in range(1, num_trials + 1):
            if should_stop():
                raise SweepAborted()
            results.append(run_single_trial(
                ser, trial_id, method, log, motor_index, acc_sensor_id,
                should_stop, frequency_hz=frequency_hz, amp=amp,
                vib_duration_s=vib_duration_s, notes=notes,
                recorder=recorder))
            progress(trial_id, num_trials)
            if trial_id < num_trials and rest_duration_s > 0:
                time.sleep(rest_duration_s)

        stats = run_stats(results)
        log("\n===== Summary =====")
        for series in TRIAL_SERIES:
            line = series_line(series, stats)
            if line:
                log(line)
        log(f"Detected: {stats['n_ok']} ok, {stats['n_no_onset']} no_onset, "
            f"{stats['n_not_settled']} not_settled, "
            f"{stats['n_insufficient_data']} insufficient_data "
            f"(of {stats['n_trials']} trials)")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stem = run_stem(method, stamp)
        csv_path = os.path.join(OUTPUT_DIR, f"{stem}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"{stem}.png")
        raw_pth = raw_path_for(csv_path) if recorder.n_windows else None
        save_trials_csv(csv_path, results)
        if raw_pth is not None:
            recorder.save(raw_pth)
        save_plot(png_path, results, method, frequency_hz, amp, motor_index,
                  notes=notes)
        meta_path = save_meta(csv_path, png_path, stamp,
                              getattr(ser, "rig_identity", None), method,
                              results, frequency_hz, amp, motor_index,
                              acc_sensor_id, vib_duration_s, rest_duration_s,
                              num_trials, notes, raw_path=raw_pth,
                              installation_notes=installation_notes)
        log(f"Data:  {csv_path}")
        if raw_pth is not None:
            log(f"Raw ACC: {raw_pth} ({recorder.n_samples} samples, "
                f"{recorder.n_windows} windows)")
        log(f"Plot:  {png_path}")
        log(f"Meta:  {meta_path}")
        log("\nThis run covers ONE adhesion method. Record the other two the "
            "same way, then press \"Analyse adhesion methods\".")

        stats.update({"adhesion_method": method, "frequency_hz": frequency_hz,
                      "amp": amp, "motor_index": motor_index, "notes": notes,
                      "vib_duration_s": vib_duration_s,
                      "csv_path": csv_path, "png_path": png_path,
                      "raw_path": raw_pth})
        return stats
    finally:
        try:
            send(ser, "X")
            send(ser, f"F {motor_index} {DEFAULT_PWM_FREQ}")
            send(ser, "A STOP", wait_s=0.1)
        except Exception:
            pass
        ser.close()
        log("Serial port closed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--method", default=ADHESION_METHODS[0],
                        help="adhesion method: "
                             + " | ".join(ADHESION_METHODS))
    parser.add_argument("--trials", type=int, default=NUM_TRIALS)
    parser.add_argument("--duration", type=float, default=VIB_DURATION_S,
                        help="vibration duration per trial (s)")
    parser.add_argument("--rest", type=float, default=REST_DURATION_S,
                        help="rest between trials (s)")
    parser.add_argument("--notes", default="",
                        help="free-text notes (adhesive thickness, curing "
                             "time, mounting anomalies)")
    parser.add_argument("--motor", type=int, default=MOTOR_INDEX)
    parser.add_argument("--sensor", type=int, default=ACC_SENSOR_ID)
    args = parser.parse_args()
    run_experiment(adhesion_method=args.method, num_trials=args.trials,
                   vib_duration_s=args.duration, rest_duration_s=args.rest,
                   notes=args.notes, motor_index=args.motor,
                   acc_sensor_id=args.sensor)


if __name__ == "__main__":
    main()
