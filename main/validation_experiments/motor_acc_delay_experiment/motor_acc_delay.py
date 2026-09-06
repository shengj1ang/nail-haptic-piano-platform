"""Motor command -> accelerometer onset and settling latency.

Measures, per trial, how long the LIS3DH glued to the motor takes to
(a) START moving and (b) reach a STEADY vibration state after the host
issues a motor-on command ("S mask amp") - the electromechanical part
of the haptic-cue latency chain.

THREE TIMES ARE RECORDED PER TRIAL and the two latencies derived from
them are kept strictly separate (never merged into one number):

    command_time   the host clock at the "S" write
    onset_time     the first sustained departure from the rest noise
    stable_time    the first instant the vibration envelope enters the
                   steady band AND stays there for a hold window

    onset_latency_from_command  = onset_time  - command_time
    settling_time_from_onset    = stable_time - onset_time
    stable_latency_from_command = stable_time - command_time

Both settling definitions are stored (`settling_time_from_onset_ms` and
`stable_latency_from_command_ms`) so a reader never has to guess which
zero a "settling time" was measured from.

A third, historical metric is still reported alongside them:

  delay_ms  - DETECTION-LEVEL CROSSING: the response reaching
              NOISE_MULT x the baseline noise p95, sub-sample refined by
              linear interpolation. Kept unchanged so every number in
              this experiment's README stays comparable; it is neither
              the onset nor the settling time.

Detection runs OFFLINE on the trial's recorded window (the acquisition
loop only parses and stores samples, so nothing competes with the
1.344 kHz stream). Each trial drives the motor continuously for a
user-chosen VIBRATION DURATION (VIB_DURATION_S, 0.5-10 s) and the FULL
three-axis series of that whole window is saved, so any trial can be
re-analysed offline with a different detector.

Detection outline (all tuning constants live together under
"Onset / settling detection" below - nothing assumes a 2 s window):

  1. The quiet pre-command baseline gives the rest-noise statistics:
     mean/SD of the per-sample per-axis deviation (the CUSUM's
     distribution) and the p95 of its short-time moving RMS envelope.
  2. ONSET = CUSUM change-point (Page 1954) on the per-sample deviation,
     dated at the start of the alarmed excursion, with a PERSISTENCE
     GATE: an alarm is only accepted when the deviation stays above the
     noise slack for ONSET_SUSTAIN_FRACTION of the following
     ONSET_SUSTAIN_S. A lone spike alarms but does not persist, so the
     search resumes past it instead of reporting it as the onset.
  3. STEADY LEVEL = the median envelope over the last
     STEADY_REF_FRACTION of the drive window (motor still on, so no
     ring-down is included).
  4. STABLE TIME = the first sample from which the envelope stays inside
     steady_level x (1 +/- STEADY_TOL_FRACTION) for a hold window of
     max(STEADY_HOLD_MIN_S, STEADY_HOLD_FRACTION x vibration duration),
     capped at STEADY_HOLD_MAX_S.

The envelope is a CENTRED moving RMS of the per-axis deviation, i.e.
exactly the shared demeaned three-axis vector RMS evaluated over a short
sliding window - the same statistic acceleration_metrics defines, in its
per-window form, so the two never disagree.

Every trial ends with an explicit status - `ok`, `no_onset`,
`not_settled` or `insufficient_data`. A trial that started but never
settled keeps its onset latency and stores an EMPTY settling time; no
stable time is ever invented.

Per-run outputs (Unix-epoch-seconds stamp <ts>) under
data/validation_experiments/motor_acc_delay_experiment/:
  delay_trials_<ts>.csv          per-trial summary rows (three times,
                                 both latencies, steady level, status,
                                 detection parameters + offline metrics)
  delay_samples_<ts>.csv         per-sample detector delta + envelope
  delay_trials_<ts>.raw_acc.npz  lossless raw three-axis samples of
                                 every baseline and drive window
  delay_summary_<ts>.png         latency / settling / envelope figure
  delay_trials_<ts>.meta.json    parameters, firmware, files, result

ONSET DETECTION IS DELIBERATELY NOT UNIFIED WITH THE INTENSITY SWEEPS.
This experiment shares the sweeps' raw-sample storage and their offline
vibration-intensity statistics (validation_experiments/
acceleration_metrics.py), but its detector keeps its own statistic - the
per-sample per-axis deviation from the per-trial baseline means. The
reason is that the two answer different questions: an RMS is an average
over a whole window and cannot date an event, while onset detection
needs a per-sample quantity that responds within one sample. (The
demeaned vector RMS is in fact the RMS of exactly this per-axis
deviation over a window, so the two are consistent by construction - the
detector is the per-sample form, the metric the windowed form, and the
settling envelope is the sliding-window form.) Only the storage and the
offline statistics are shared.

Runs standalone (python motor_acc_delay.py) or through the launcher's
"Validation Experiments" section; both paths write the same files.
"""

import csv
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

matplotlib.use("Agg")  # save PNG without needing a display
import matplotlib.pyplot as plt

try:
    from ..acceleration_metrics import (
        METRIC_CSV_COLUMNS,
        MS2_PER_COUNT,
        PHASE_BASELINE,
        PHASE_VIBRATION,
        AccelerationMetrics,
        RawSampleRecorder,
        as_xyz_arrays,
        compute_acceleration_metrics,
        compute_baseline_magnitude,
        counts_to_ms2,
        metric_meta_block,
        raw_path_for,
        to_epoch_seconds,
    )
    from ..rig import SweepAborted, Sample, open_rig, parse_acc_line, send
    from .. import report
except ImportError:  # direct execution rather than package import
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from acceleration_metrics import (
        METRIC_CSV_COLUMNS,
        MS2_PER_COUNT,
        PHASE_BASELINE,
        PHASE_VIBRATION,
        AccelerationMetrics,
        RawSampleRecorder,
        as_xyz_arrays,
        compute_acceleration_metrics,
        compute_baseline_magnitude,
        counts_to_ms2,
        metric_meta_block,
        raw_path_for,
        to_epoch_seconds,
    )
    from rig import SweepAborted, Sample, open_rig, parse_acc_line, send
    import report

try:
    from common import haptic_config as hc
except ImportError:  # direct execution from this folder - add main/ to the path
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from common import haptic_config as hc


# ==========================================
# Experiment configuration
# ==========================================

# The rig's two actuator test channels (see teensy_driver README wiring).
# The mapping itself lives in common.haptic_config so every window and
# experiment agrees on one wiring convention; the GUI's motor-port spin
# box still overrides it per run.
ACTUATOR_MOTORS = {"LRA": hc.ACTUATOR_MOTOR_PORTS[hc.LRA],
                   "ERM": hc.ACTUATOR_MOTOR_PORTS[hc.ERM]}

# The firmware's boot PWM frequency - restored on the port when the run
# ends. A FIRMWARE fact (motor_driver.cpp DEFAULT_PWM_FREQ), not a
# preference: it must keep matching the firmware even if the configured
# LRA default changes.
DEFAULT_PWM_FREQ = hc.FIRMWARE_BOOT_PWM_HZ

# Amp assumed for a run saved before the amp was recorded in its meta -
# every historical run used the calibrated cue level. Used ONLY when
# re-rendering such a run, so an old chart keeps its own label instead of
# picking up today's configured default.
HISTORICAL_AMP = 64

MOTOR_INDEX = ACTUATOR_MOTORS["LRA"]   # default: the LRA test channel
ACC_SENSOR_ID = 0

ACC_INTERVAL_MS = 1      # firmware >= v2.9.0: LIS3DH at 1.344 kHz, so a
                         # 1 ms stream carries fresh samples (older
                         # firmware streams duplicates beyond 2.5 ms)


def actuator_amp(actuator_type: str = "") -> int:
    """The configured default cue amp for this actuator (config.json's
    haptic block). The study-representative latency is the one measured
    at the amp the study actually delivers, so the run defaults to it -
    the GUI still lets the operator drive at any other amp."""
    return hc.get_default_amp(actuator_type or None)


def actuator_pwm_hz(actuator_type: str = "") -> int:
    """The configured default PWM frequency for this actuator.

    Why it is per-actuator: the firmware boot default (the LRA's
    resonance) is right for the LRA but WRONG for an ERM - a DC motor
    chopped that slowly (at amp 64, ~1.1 ms on / 3.3 ms off) cannot
    overcome stiction and never starts. ERMs need a kHz-range carrier so
    the drive behaves like smooth DC. The experiment sets the port's
    frequency before the trials and restores the boot default after."""
    return hc.get_default_frequency(actuator_type or None)


# Vibration (= measurement) duration per trial: the motor runs
# continuously for this long and the WHOLE window is recorded, so the
# settling detector has steady-state data to define "stable" against.
# User-adjustable in the GUI within [MIN, MAX]; nothing in the detector
# assumes the default (every window length is derived from it).
VIB_DURATION_S = 2.0
VIB_DURATION_MIN_S = 0.5
VIB_DURATION_MAX_S = 10.0
VIB_DURATION_STEP_S = 0.5

NUM_TRIALS = 10
BASELINE_DURATION_S = 1.0
INTER_TRIAL_REST_S = 1.0

# Detection level (delay_ms - the historical crossing metric): adaptive,
# derived from the quiet baseline:
#   threshold = max(MIN_THRESHOLD, NOISE_MULT * p95(baseline deviations))
# The deviation is computed PER AXIS against the per-axis baseline means
# (| |a| - baseline | is only second-order sensitive to vibration
# perpendicular to gravity and is not used).
MIN_THRESHOLD = 40       # counts; floor so noise can't set it absurdly low
NOISE_MULT = 3.0         # detection level = this many times baseline p95
CONSECUTIVE_HITS = 1     # samples above the level required for a crossing


# ==========================================
# Onset / settling detection (all tuning in one place)
# ==========================================
#
# Every window length below is a DURATION in seconds and is converted to
# a sample count with the trial's measured sample rate, so the detector
# works unchanged at any stream rate and at any vibration duration
# between VIB_DURATION_MIN_S and VIB_DURATION_MAX_S.

# -- onset ---------------------------------------------------------------
# CUSUM change-point detection (Page 1954). A fixed-amplitude onset
# criterion is biased against gradually ringing actuators: an ERM's
# impulsive start crosses any level instantly while an LRA's exponential
# ring-up is only "admitted" once it has grown to the level, milliseconds
# after motion truly began. CUSUM instead accumulates each sample's
# departure from the baseline-noise distribution,
# S_i = max(0, S_{i-1} + (d_i - mu - k*sigma)), alarms when S exceeds
# h*sigma, and dates the onset at the sample where the alarmed excursion
# STARTED - the first instant the curve departs from noise, regardless of
# how slowly the amplitude grows.
CUSUM_SLACK_SIGMA = 1.0   # k: per-sample slack above the noise mean
CUSUM_ALARM_SIGMA = 10.0  # h: accumulated evidence needed to alarm

# Persistence gate on the alarm, so a single mechanical knock (or one
# noisy sample) is never reported as the onset: from the ALARM sample
# onward, the deviation must stay above the CUSUM slack level for at
# least ONSET_SUSTAIN_FRACTION of ONSET_SUSTAIN_S. A lone spike is
# followed by pure noise, which only clears the slack level ~16-20% of
# the time, so it fails; a genuine start (however slow) has already
# raised the mean above the slack level by the time it alarms, so it
# passes. The test is on the alarm - not on the dated onset - precisely
# so that a slow ERM ramp is not required to be large at its own onset.
ONSET_SUSTAIN_S = 0.030
ONSET_SUSTAIN_FRACTION = 0.40

# -- envelope ------------------------------------------------------------
# Short-time moving RMS of the per-sample deviation - the vibration
# envelope the settling test runs on. 20 ms is ~4.5 cycles of the LRA's
# 224 Hz drive, enough to average the carrier away while still resolving
# a ring-up. CENTRED (not trailing) so the envelope carries no
# systematic lag that would inflate every settling time.
ENVELOPE_WINDOW_S = 0.020

#: The y-axis label EVERY envelope panel carries, written out once
#: here. Single panels get cropped out of these summary figures for
#: the write-up, so each one has to name its own axis on its own -
#: including what a count is worth, because the intensity panels
#: next to them are plotted in m/s².
ENVELOPE_AXIS_LABEL = (
    "Per-axis deviation envelope (counts)\n"
    f"centred {ENVELOPE_WINDOW_S * 1000:.0f} ms moving RMS\n"
    f"(1 count = 1 mg = {MS2_PER_COUNT:.4f} m/s²)")

# -- figure --------------------------------------------------------------
# X span of the "zoom on the rise" panel. FIXED, not fitted to each
# run: the ERM and the LRA panels are read side by side, and an axis
# that shrinks to whatever the actuator needed makes the fast one
# look exactly as slow as the slow one - the eye reads the shape of
# the rise across the panel, not the tick labels. A run whose rise
# needs longer still gets the room (nothing is ever cut off), and the
# span never exceeds the drive window.
ZOOM_END_S = 1.000

# -- steady state --------------------------------------------------------
# The steady level is the MEDIAN envelope over the last
# STEADY_REF_FRACTION of the drive window (median, so a late glitch
# cannot move it). The window ends at motor-off, so no ring-down is
# included.
STEADY_REF_FRACTION = 0.30
STEADY_REF_MIN_S = 0.05     # too short a reference -> not_settled

# "Stable" band around that level, and how long the envelope must hold
# inside it. The hold scales with the vibration duration (a 10 s run
# should demand more evidence than a 0.5 s one) between a floor and a
# cap, and tolerates STEADY_HOLD_INSIDE_FRACTION < 1 so a single-sample
# envelope wobble does not restart the search.
STEADY_TOL_FRACTION = 0.20
STEADY_HOLD_FRACTION = 0.10
STEADY_HOLD_MIN_S = 0.05
STEADY_HOLD_MAX_S = 0.50
STEADY_HOLD_INSIDE_FRACTION = 0.95

# Sanity guards on the steady level itself: if the "steady" vibration is
# indistinguishable from the rest noise the motor never really ran, and
# the trial is not_settled rather than ok.
STEADY_MIN_SNR = 1.5                 # x baseline envelope p95
MIN_STEADY_ENVELOPE_COUNTS = 20.0    # absolute floor, counts

# Fewer samples than this in a drive window means the stream stalled.
MIN_DETECTION_SAMPLES = 64

# -- per-trial detection status ------------------------------------------
STATUS_OK = "ok"                              # onset and stable time found
STATUS_NO_ONSET = "no_onset"                  # no sustained departure
STATUS_NOT_SETTLED = "not_settled"            # onset only; never stabilised
STATUS_INSUFFICIENT_DATA = "insufficient_data"  # window too short/empty
DETECTION_STATUSES = (STATUS_OK, STATUS_NO_ONSET, STATUS_NOT_SETTLED,
                      STATUS_INSUFFICIENT_DATA)

# Mounting protocol: the motor+sensor pair hangs FREELY IN THE AIR
# (desk mounting damps the vibration below reliable detection). The
# starting orientation is arbitrary - the per-axis baseline absorbs
# whatever direction gravity points - but each trial first waits for
# the rig to hang still.
STILL_WINDOW_S = 0.5     # stillness is judged over windows this long
# A genuinely still hanging rig still shows a p95 deviation of ~30-45
# counts (wire-borne micro-vibration + sensor noise), so the gate sits
# above that floor; actual swinging reads well over 100.
STILL_MAX_DEV = 60.0     # p95 of in-window per-axis deviations (counts)
STILL_TIMEOUT_S = 60.0   # give up if the rig never settles

OUTPUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "data", "validation_experiments", "motor_acc_delay_experiment"))

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (trials_done, trials_total)


@dataclass
class TrialResult:
    trial_id: int
    status: str                    # one of DETECTION_STATUSES ("timeout"
                                   # in pre-settling runs)
    delay_ms: Optional[float]      # detection-level crossing (interpolated)
    baseline_magnitude_counts: float
    peak_axis_delta_counts: float  # largest per-axis deviation (detector units)
    threshold_counts: float = 0.0  # per-trial detection level (0 = unknown)
    onset_ms: Optional[float] = None       # motion onset (= onset latency)
    onset_threshold_counts: float = 0.0    # per-trial CUSUM alarm (0 = unknown)
    # Offline vibration-intensity statistics of the trial's drive window,
    # computed with the shared acceleration_metrics module (both metrics).
    # None for trials loaded from a pre-refactor CSV.
    intensity: Optional[AccelerationMetrics] = None
    # Per-sample trace (host-relative time vs per-axis delta and its
    # envelope); empty when loaded from a summary CSV without its
    # samples file.
    rel_times: List[float] = field(default_factory=list)
    deltas: List[float] = field(default_factory=list)
    envelopes: List[float] = field(default_factory=list)
    detect_rel_time: Optional[float] = None

    # -- settling ---------------------------------------------------------
    actuator_type: str = ""
    # The three instants, in Unix epoch seconds (project-wide rule).
    command_time_s: Optional[float] = None
    onset_time_s: Optional[float] = None
    stable_time_s: Optional[float] = None
    # The two latencies, kept separate on purpose.
    settling_time_from_onset_ms: Optional[float] = None
    stable_latency_from_command_ms: Optional[float] = None
    # Steady-state vibration level and the band it had to hold.
    steady_state_envelope_counts: Optional[float] = None
    steady_band_low_counts: Optional[float] = None
    steady_band_high_counts: Optional[float] = None
    env_noise_p95_counts: float = 0.0
    # Detection parameters that actually applied to THIS trial.
    vib_duration_s: float = VIB_DURATION_S
    envelope_window_s: float = ENVELOPE_WINDOW_S
    steady_tolerance_frac: float = STEADY_TOL_FRACTION
    steady_hold_s: float = 0.0
    onset_sustain_s: float = ONSET_SUSTAIN_S
    sample_rate_hz: float = 0.0
    onset_rel_time: Optional[float] = None   # seconds since command
    stable_rel_time: Optional[float] = None  # seconds since command

    @property
    def steady_state_envelope_ms2(self) -> Optional[float]:
        if self.steady_state_envelope_counts is None:
            return None
        return counts_to_ms2(self.steady_state_envelope_counts)


# ==========================================
# Detection
# ==========================================

def steady_hold_s(vib_duration_s: float) -> float:
    """How long the envelope must hold inside the steady band, for a
    given vibration duration - scaled, floored and capped (never a
    hard-coded window: the duration is user-selectable 0.5-10 s)."""
    return float(min(max(STEADY_HOLD_FRACTION * float(vib_duration_s),
                         STEADY_HOLD_MIN_S), STEADY_HOLD_MAX_S))


def per_axis_deltas(samples, means: Tuple[float, float, float]) -> np.ndarray:
    """The detector's per-sample statistic for a window of raw samples:
    the Euclidean distance of each sample from `means` (the per-axis
    baseline means). First-order sensitive to vibration in ANY direction,
    unlike | |a| - baseline | which nearly cancels for motion
    perpendicular to gravity."""
    xs, ys, zs = as_xyz_arrays(samples)
    bx, by, bz = means
    return np.sqrt((xs - bx) ** 2 + (ys - by) ** 2 + (zs - bz) ** 2)


def moving_rms(values: Sequence[float], window_samples: int) -> np.ndarray:
    """Centred moving RMS - the vibration envelope.

    At the two ends the window is SHIFTED inwards rather than truncated
    or zero-padded, so every envelope sample averages the same number of
    points. Zero-padding would pull the ends toward zero and fake both a
    slow start and a late settle; truncating would leave the last
    samples noisier than the rest, which is exactly where the steady
    reference is measured."""
    arr = np.asarray(values, dtype=float)
    n = arr.size
    window = max(1, int(window_samples))
    if n == 0:
        return arr
    if window <= 1:
        return np.abs(arr)
    cumulative = np.concatenate(([0.0], np.cumsum(arr * arr)))
    half = window // 2
    idx = np.arange(n)
    starts = np.clip(idx - half, 0, n)
    ends = np.clip(starts + window, 0, n)
    starts = np.clip(ends - window, 0, n)
    counts = np.maximum(ends - starts, 1)
    return np.sqrt((cumulative[ends] - cumulative[starts]) / counts)


def estimate_sample_rate_hz(rel_times: Sequence[float]) -> float:
    """Mean sample rate of a window, from its host arrival times."""
    times = np.asarray(rel_times, dtype=float)
    if times.size < 2:
        return 0.0
    span = float(times[-1] - times[0])
    if span <= 0:
        return 0.0
    return float(times.size - 1) / span


@dataclass
class NoiseStats:
    """Rest-noise statistics of one quiet baseline window, in counts."""
    mean_counts: float           # mean per-sample deviation
    sd_counts: float             # its SD (the CUSUM's sigma)
    p95_counts: float            # its p95 (the detection level's basis)
    envelope_p95_counts: float   # p95 of the moving-RMS envelope
    sample_rate_hz: float = 0.0


def noise_stats(baseline_deltas: Sequence[float],
                sample_rate_hz: float) -> NoiseStats:
    """Everything the onset/settling detector needs to know about rest."""
    deltas = np.asarray(baseline_deltas, dtype=float)
    if deltas.size < 2:
        raise ValueError("baseline window has too few samples for noise "
                         "statistics")
    window = max(1, int(round(ENVELOPE_WINDOW_S * float(sample_rate_hz))))
    envelope = moving_rms(deltas, window)
    return NoiseStats(
        mean_counts=float(np.mean(deltas)),
        sd_counts=float(np.std(deltas, ddof=1)),
        p95_counts=float(np.percentile(deltas, 95)),
        envelope_p95_counts=float(np.percentile(envelope, 95)),
        sample_rate_hz=float(sample_rate_hz),
    )


@dataclass
class OnsetSettling:
    """One trial's detection outcome, in window-relative seconds."""
    status: str
    onset_rel_s: Optional[float] = None
    stable_rel_s: Optional[float] = None
    steady_level_counts: Optional[float] = None
    steady_low_counts: Optional[float] = None
    steady_high_counts: Optional[float] = None
    envelope: np.ndarray = field(default_factory=lambda: np.empty(0))
    hold_s: float = 0.0
    sample_rate_hz: float = 0.0
    note: str = ""

    @property
    def settling_s(self) -> Optional[float]:
        if self.onset_rel_s is None or self.stable_rel_s is None:
            return None
        return self.stable_rel_s - self.onset_rel_s


def _find_onset_index(deltas: np.ndarray, noise: NoiseStats,
                      sustain_samples: int) -> Optional[int]:
    """CUSUM change-point onset index, with the persistence gate.

    Returns the index of the sample where the first ACCEPTED alarmed
    excursion started, or None. An alarm whose following
    ONSET_SUSTAIN_S is mostly back at rest is a transient (a knock, a
    single noisy sample): the accumulator is reset and the search
    continues after it, so a spike is never dated as the onset."""
    if noise.sd_counts <= 0 or deltas.size == 0:
        return None
    slack_level = noise.mean_counts + CUSUM_SLACK_SIGMA * noise.sd_counts
    alarm_level = CUSUM_ALARM_SIGMA * noise.sd_counts
    above = deltas > slack_level
    # Prefix sums make the persistence test O(1) per alarm.
    above_cumulative = np.concatenate(([0], np.cumsum(above.astype(np.int64))))
    sustain = max(1, int(sustain_samples))
    needed = max(1, int(math.ceil(ONSET_SUSTAIN_FRACTION * sustain)))

    accumulator = 0.0
    excursion_start = 0
    i = 0
    n = deltas.size
    while i < n:
        previous = accumulator
        accumulator = max(0.0, accumulator + (deltas[i] - slack_level))
        if accumulator > 0.0 and previous == 0.0:
            excursion_start = i
        if accumulator > alarm_level:
            end = i + sustain
            if end > n:
                # Not enough record left to tell a start from a knock.
                return None
            if above_cumulative[end] - above_cumulative[i] >= needed:
                return int(excursion_start)
            # Transient: drop the evidence and keep looking past it.
            accumulator = 0.0
        i += 1
    return None


def _first_sustained_index(inside: np.ndarray, hold_samples: int,
                           start: int, min_fraction: float) -> Optional[int]:
    """First index >= `start` that is itself inside the band and from
    which at least `min_fraction` of the next `hold_samples` are too."""
    n = inside.size
    hold = max(1, int(hold_samples))
    if start >= n or n - start < hold:
        return None
    cumulative = np.concatenate(([0], np.cumsum(inside.astype(np.int64))))
    sums = cumulative[hold:] - cumulative[:-hold]      # length n - hold + 1
    needed = max(1, int(math.ceil(min_fraction * hold)))
    ok = (sums >= needed) & inside[:sums.size]
    hits = np.flatnonzero(ok[start:])
    return int(start + hits[0]) if hits.size else None


def detect_onset_and_settling(rel_times: Sequence[float],
                              deltas: Sequence[float],
                              noise: NoiseStats,
                              vib_duration_s: float) -> OnsetSettling:
    """Date the vibration onset and the steady state in one drive window.

    `rel_times`/`deltas` are the window's host-relative sample times
    (seconds since the motor-on command) and its per-sample per-axis
    deviations; `noise` comes from the trial's quiet baseline. Pure
    function - no hardware, no I/O - which is what the synthetic-signal
    tests exercise.
    """
    times = np.asarray(rel_times, dtype=float)
    values = np.asarray(deltas, dtype=float)
    hold = steady_hold_s(vib_duration_s)
    fs = estimate_sample_rate_hz(times)

    if (values.size < MIN_DETECTION_SAMPLES or fs <= 0
            or float(times[-1] - times[0]) < 2 * ENVELOPE_WINDOW_S + hold):
        return OnsetSettling(
            status=STATUS_INSUFFICIENT_DATA, hold_s=hold, sample_rate_hz=fs,
            note=(f"{values.size} samples over "
                  f"{0.0 if times.size < 2 else times[-1] - times[0]:.3f} s - "
                  "too short to date an onset and a steady state"))

    envelope = moving_rms(values, max(1, int(round(ENVELOPE_WINDOW_S * fs))))
    result = OnsetSettling(status=STATUS_NO_ONSET, envelope=envelope,
                           hold_s=hold, sample_rate_hz=fs)

    onset_idx = _find_onset_index(values, noise,
                                  int(round(ONSET_SUSTAIN_S * fs)))
    if onset_idx is None:
        result.note = ("no sustained departure from the rest noise "
                       f"(CUSUM alarm {CUSUM_ALARM_SIGMA:g} sigma, "
                       f"persistence {ONSET_SUSTAIN_FRACTION:.0%} of "
                       f"{ONSET_SUSTAIN_S * 1000:.0f} ms)")
        return result

    result.onset_rel_s = float(times[onset_idx])
    result.status = STATUS_NOT_SETTLED

    # Steady reference: the tail of the drive window, never earlier than
    # the onset (the motor is still on, so there is no ring-down here).
    ref_start = max(onset_idx, int(values.size * (1.0 - STEADY_REF_FRACTION)))
    if (ref_start >= values.size
            or float(times[-1] - times[ref_start]) < STEADY_REF_MIN_S):
        result.note = ("the onset leaves less than "
                       f"{STEADY_REF_MIN_S * 1000:.0f} ms of drive window to "
                       "estimate a steady level from")
        return result

    steady = float(np.median(envelope[ref_start:]))
    result.steady_level_counts = steady
    floor = max(MIN_STEADY_ENVELOPE_COUNTS,
                STEADY_MIN_SNR * noise.envelope_p95_counts)
    if steady < floor:
        result.note = (f"steady envelope {steady:.1f} counts is below the "
                       f"{floor:.1f}-count floor - the vibration never "
                       "established")
        return result

    low = steady * (1.0 - STEADY_TOL_FRACTION)
    high = steady * (1.0 + STEADY_TOL_FRACTION)
    result.steady_low_counts, result.steady_high_counts = low, high

    inside = (envelope >= low) & (envelope <= high)
    stable_idx = _first_sustained_index(
        inside, int(round(hold * fs)), onset_idx, STEADY_HOLD_INSIDE_FRACTION)
    if stable_idx is None:
        result.note = (f"the envelope never held within +/-"
                       f"{STEADY_TOL_FRACTION:.0%} of {steady:.1f} counts for "
                       f"{hold * 1000:.0f} ms")
        return result

    result.stable_rel_s = float(times[stable_idx])
    result.status = STATUS_OK
    return result


def _interp_crossing(rel_times: Sequence[float], deltas: Sequence[float],
                     idx: int, level: float) -> float:
    """Linearly interpolated time at which the series crossed `level`
    between sample idx-1 and idx (standard sub-sample onset estimate);
    falls back to the sample time when interpolation is ill-posed."""
    if idx == 0:
        return float(rel_times[0])
    t0, d0 = float(rel_times[idx - 1]), float(deltas[idx - 1])
    t1, d1 = float(rel_times[idx]), float(deltas[idx])
    if d1 <= d0 or d0 >= level:
        return t1
    frac = (level - d0) / (d1 - d0)
    return t0 + frac * (t1 - t0)


def find_detection_crossing(deltas: Sequence[float],
                            threshold: float) -> Optional[int]:
    """Index of the first sample of CONSECUTIVE_HITS above `threshold`
    (the historical detection-level crossing), or None."""
    hits = 0
    for i, value in enumerate(deltas):
        if value > threshold:
            hits += 1
            if hits >= CONSECUTIVE_HITS:
                return i - (CONSECUTIVE_HITS - 1)
        else:
            hits = 0
    return None


# ==========================================
# Trial
# ==========================================

def _collect_window(ser, duration_s: float,
                    acc_sensor_id: int) -> List[Sample]:
    """Read the ACC stream for duration_s as (t, x, y, z) tuples.

    Local rather than rig.collect_samples because that helper flushes the
    input buffer at the start of every window: here the stillness gate
    flushes once and then reads back-to-back windows, and the baseline
    must continue straight from the flush the caller performed."""
    samples: List[Sample] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        sample = parse_acc_line(raw, acc_sensor_id)
        if sample is not None:
            x, y, z = sample
            samples.append((time.monotonic(), x, y, z))
    return samples


def _p95_dev(samples: List[Sample]) -> float:
    """p95 of the per-axis deviation from the window's own per-axis means
    - the detector's own statistic (see the module docstring on why this
    is not replaced by a windowed RMS)."""
    xs, ys, zs = as_xyz_arrays(samples)
    devs = per_axis_deltas(samples, (float(xs.mean()), float(ys.mean()),
                                     float(zs.mean())))
    return float(np.percentile(devs, 95))


def wait_until_still(ser, acc_sensor_id: int, log: LogFn,
                     should_stop: Callable[[], bool],
                     still_max_dev: float = STILL_MAX_DEV) -> None:
    """Block until the suspended rig hangs still (see STILL_* constants;
    still_max_dev is per-run adjustable for rigs with a different noise
    floor). Orientation is irrelevant - only in-window motion matters.
    Raises SweepAborted on a stop request, RuntimeError on timeout."""
    log("Waiting for the rig to hang still...")
    t0 = time.monotonic()
    ser.reset_input_buffer()
    min_p95 = float("inf")
    windows = 0
    hinted = False
    while True:
        if should_stop():
            raise SweepAborted()
        if time.monotonic() - t0 > STILL_TIMEOUT_S:
            raise RuntimeError(
                f"Rig did not settle within {STILL_TIMEOUT_S:.0f} s "
                f"(lowest p95 deviation seen: {min_p95:.0f} counts vs "
                f"limit {still_max_dev:.0f}) - steady the hanging assembly "
                "and start again, or raise the stillness limit if the rig "
                "is genuinely still")
        samples = _collect_window(ser, STILL_WINDOW_S, acc_sensor_id)
        if len(samples) < 10:
            continue
        p95 = _p95_dev(samples)
        min_p95 = min(min_p95, p95)
        windows += 1
        if p95 <= still_max_dev:
            log(f"Rig still (p95 deviation {p95:.0f} counts) "
                f"after {time.monotonic() - t0:.1f} s.")
            return
        log(f"  ...still moving (p95 deviation {p95:.0f} counts)")
        # If the readings plateau just above the limit, the problem is
        # this rig's noise floor, not motion - say so instead of letting
        # the operator chase a stillness that already happened.
        if not hinted and windows >= 10 and min_p95 <= still_max_dev * 1.5:
            hinted = True
            log(f"  (p95 has plateaued around {min_p95:.0f} counts - if the "
                "rig looks still, that is this rig's noise floor; consider "
                f"raising the stillness limit above {min_p95:.0f})")


@dataclass
class BaselineWindow:
    """One trial's quiet pre-command window and everything derived from
    it: the raw samples (for the raw store), the per-axis means the
    detector measures against, the |a| mean the legacy metric subtracts,
    the detection level and the rest-noise statistics."""
    samples: List[Sample]
    means: Tuple[float, float, float]
    magnitude_counts: float
    threshold_counts: float
    noise: NoiseStats


def collect_baseline(ser, duration_s: float,
                     acc_sensor_id: int) -> BaselineWindow:
    """Measure the quiet window every per-trial statistic derives from."""
    samples = _collect_window(ser, duration_s, acc_sensor_id)
    if len(samples) < 20:
        raise RuntimeError("Not enough ACC samples for the baseline - "
                           "is the stream running?")

    xs, ys, zs = as_xyz_arrays(samples)
    means = (float(xs.mean()), float(ys.mean()), float(zs.mean()))
    deltas = per_axis_deltas(samples, means)
    fs = estimate_sample_rate_hz([s[0] for s in samples])
    noise = noise_stats(deltas, fs)
    return BaselineWindow(
        samples=samples,
        means=means,
        magnitude_counts=compute_baseline_magnitude(samples),
        threshold_counts=max(MIN_THRESHOLD, NOISE_MULT * noise.p95_counts),
        noise=noise,
    )


def run_single_trial(ser, trial_id: int, log: LogFn,
                     motor_index: int, acc_sensor_id: int,
                     should_stop: Callable[[], bool],
                     still_max_dev: float = STILL_MAX_DEV,
                     amp: Optional[int] = None,
                     recorder: Optional[RawSampleRecorder] = None,
                     pwm_freq_hz: Optional[float] = None,
                     vib_duration_s: float = VIB_DURATION_S,
                     actuator_type: str = "") -> TrialResult:
    # None = the configured default for this actuator (run_experiment
    # always passes explicit values; this is for direct callers).
    if amp is None:
        amp = actuator_amp(actuator_type)
    if pwm_freq_hz is None:
        pwm_freq_hz = actuator_pwm_hz(actuator_type)
    log(f"\n===== Trial {trial_id} =====")
    # The previous trial's buzz leaves the hanging rig swinging - gate
    # every trial on stillness before taking its baseline.
    wait_until_still(ser, acc_sensor_id, log, should_stop,
                     still_max_dev=still_max_dev)
    baseline = collect_baseline(ser, BASELINE_DURATION_S, acc_sensor_id)
    log(f"Baseline |a|: {baseline.magnitude_counts:.2f}; noise "
        f"{baseline.noise.mean_counts:.0f}±{baseline.noise.sd_counts:.0f} "
        f"counts (envelope p95 {baseline.noise.envelope_p95_counts:.0f}); "
        f"detection level: {baseline.threshold_counts:.0f} counts")
    baseline_window = -1
    if recorder is not None:
        baseline_window = recorder.add_window(
            baseline.samples, phase=PHASE_BASELINE, cell_id=trial_id,
            trial_id=trial_id, commanded_freq_hz=pwm_freq_hz,
            commanded_amp=0)

    ser.reset_input_buffer()

    # ---- acquisition: parse and store only -----------------------------
    # Detection runs offline (below) so this loop never competes with the
    # 1.344 kHz stream; the motor stays on for the WHOLE window and every
    # sample of it is kept.
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
        # trial short; checked per block rather than per sample to keep
        # the 1.3 kHz loop lean.
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
            # Past motor-off: keep the window free of the ring-down so
            # the steady-state reference stays a driven measurement.
            break
        x, y, z = sample
        raw_samples.append((arrival, x, y, z))
        rel_times.append(arrival - t_cmd)

    send(ser, "X", wait_s=0.0)
    if aborted:
        # Motor is off; the caller's finally-block restores the rest.
        raise SweepAborted()

    if recorder is not None and raw_samples:
        recorder.add_window(raw_samples, phase=PHASE_VIBRATION,
                            cell_id=trial_id, trial_id=trial_id,
                            baseline_window_id=baseline_window,
                            commanded_freq_hz=pwm_freq_hz, commanded_amp=amp)

    return analyse_trial(
        trial_id=trial_id, log=log, baseline=baseline,
        raw_samples=raw_samples, rel_times=rel_times,
        command_time_s=command_time_s, vib_duration_s=vib_duration_s,
        actuator_type=actuator_type)


def analyse_trial(trial_id: int, log: LogFn, baseline: BaselineWindow,
                  raw_samples: List[Sample], rel_times: List[float],
                  command_time_s: float, vib_duration_s: float,
                  actuator_type: str = "") -> TrialResult:
    """Turn one recorded drive window into a TrialResult - the whole
    offline half of a trial, separated from the serial I/O so it can be
    replayed on stored or synthetic data."""
    result = TrialResult(
        trial_id=trial_id,
        status=STATUS_INSUFFICIENT_DATA,
        delay_ms=None,
        baseline_magnitude_counts=baseline.magnitude_counts,
        peak_axis_delta_counts=0.0,
        threshold_counts=baseline.threshold_counts,
        onset_threshold_counts=CUSUM_ALARM_SIGMA * baseline.noise.sd_counts,
        env_noise_p95_counts=baseline.noise.envelope_p95_counts,
        actuator_type=actuator_type,
        command_time_s=command_time_s,
        vib_duration_s=float(vib_duration_s),
        steady_hold_s=steady_hold_s(vib_duration_s),
        rel_times=list(rel_times),
    )
    if not raw_samples:
        log(f"Trial {trial_id}: no ACC samples arrived during the drive "
            "window - is the stream running?")
        return result

    deltas = per_axis_deltas(raw_samples, baseline.means)
    result.deltas = [float(d) for d in deltas]
    result.peak_axis_delta_counts = float(np.max(deltas))

    # Offline only, and never fed back into detection: the intensity of
    # the whole recorded drive window under BOTH shared metrics. The
    # window spans the quiet pre-onset samples and the ring-up as well as
    # the steady state, so this is NOT a steady-state intensity
    # comparable with a sweep cell (the settling detector's
    # steady_state_envelope_counts is).
    result.intensity = compute_acceleration_metrics(
        raw_samples, baseline.magnitude_counts, MS2_PER_COUNT)

    detection = detect_onset_and_settling(rel_times, deltas, baseline.noise,
                                          vib_duration_s)
    result.status = detection.status
    result.envelopes = [float(v) for v in detection.envelope]
    result.sample_rate_hz = detection.sample_rate_hz
    result.steady_hold_s = detection.hold_s
    result.steady_state_envelope_counts = detection.steady_level_counts
    result.steady_band_low_counts = detection.steady_low_counts
    result.steady_band_high_counts = detection.steady_high_counts

    if detection.onset_rel_s is not None:
        result.onset_rel_time = detection.onset_rel_s
        result.onset_ms = detection.onset_rel_s * 1000.0
        result.onset_time_s = command_time_s + detection.onset_rel_s
    if detection.stable_rel_s is not None:
        result.stable_rel_time = detection.stable_rel_s
        result.stable_time_s = command_time_s + detection.stable_rel_s
        result.stable_latency_from_command_ms = detection.stable_rel_s * 1000.0
        settling = detection.settling_s
        result.settling_time_from_onset_ms = (None if settling is None
                                              else settling * 1000.0)

    # The historical detection-level crossing, unchanged.
    crossing_idx = find_detection_crossing(deltas, baseline.threshold_counts)
    if crossing_idx is not None:
        result.detect_rel_time = _interp_crossing(
            rel_times, deltas, crossing_idx, baseline.threshold_counts)
        result.delay_ms = result.detect_rel_time * 1000.0

    onset_str = ("-" if result.onset_ms is None
                 else f"{result.onset_ms:.2f} ms")
    settle_str = ("-" if result.settling_time_from_onset_ms is None
                  else f"{result.settling_time_from_onset_ms:.2f} ms")
    stable_str = ("-" if result.stable_latency_from_command_ms is None
                  else f"{result.stable_latency_from_command_ms:.2f} ms")
    steady_str = ("-" if result.steady_state_envelope_counts is None else
                  f"{result.steady_state_envelope_counts:.1f} counts")
    log(f"Trial {trial_id}: status={result.status}, onset {onset_str}, "
        f"settling {settle_str}, stable-from-command {stable_str}, "
        f"steady {steady_str}")
    if detection.note:
        log(f"  ({detection.note})")
    return result


# ==========================================
# Output
# ==========================================

def samples_path_for(csv_path: str) -> str:
    """delay_trials_<ts>.csv -> delay_samples_<ts>.csv (same folder)."""
    folder, name = os.path.split(csv_path)
    return os.path.join(folder, name.replace("delay_trials_", "delay_samples_", 1))


def meta_path_for(csv_path: str) -> str:
    """delay_trials_<ts>.csv -> delay_trials_<ts>.meta.json."""
    return os.path.splitext(csv_path)[0] + ".meta.json"


def png_path_for(csv_path: str) -> str:
    """delay_trials_<ts>.csv -> delay_summary_<ts>.png (same folder) - the
    run's saved figure, so a re-render can overwrite it in place."""
    folder, name = os.path.split(csv_path)
    stem = os.path.splitext(name)[0]
    return os.path.join(folder,
                        stem.replace("delay_trials_", "delay_summary_", 1)
                        + ".png")


# Timing columns first (the three instants, then the three latencies that
# are DERIVED from them and are never merged), then the steady-state and
# detection-parameter columns, then the historical crossing columns, then
# the shared offline intensity columns every accelerometer experiment
# writes (acceleration_metrics.METRIC_CSV_COLUMNS) so the tables line up
# across experiments.
#
# `onset_ms` and `onset_latency_from_command_ms` are the SAME number: the
# first is the column name every historical run and every loader already
# uses, the second spells out its reference instant the way
# `settling_time_from_onset_ms` and `stable_latency_from_command_ms` do.
TRIALS_CSV_COLUMNS = (
    "trial_id", "status", "actuator_type",
    "command_time_s", "onset_time_s", "stable_time_s",
    "onset_ms", "onset_latency_from_command_ms",
    "settling_time_from_onset_ms", "stable_latency_from_command_ms",
    "steady_state_envelope_counts", "steady_state_envelope_ms2",
    "steady_band_low_counts", "steady_band_high_counts",
    "env_noise_p95_counts",
    "delay_ms", "peak_axis_delta_counts",
    "onset_threshold_counts", "threshold_counts",
    "vib_duration_s", "envelope_window_s", "steady_tolerance_frac",
    "steady_hold_s", "onset_sustain_s", "sample_rate_hz",
) + METRIC_CSV_COLUMNS


def _fmt(value: Optional[float], places: int = 3) -> str:
    """CSV cell for an optional float - EMPTY when the quantity was not
    measured. Never a stand-in number: an undetected settling time must
    read as missing, not as zero."""
    return "" if value is None else f"{float(value):.{places}f}"


def save_trials_csv(path: str, results: List[TrialResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(TRIALS_CSV_COLUMNS))
        writer.writeheader()
        for r in results:
            row = {
                "trial_id": r.trial_id,
                "status": r.status,
                "actuator_type": r.actuator_type,
                "command_time_s": _fmt(r.command_time_s, 6),
                "onset_time_s": _fmt(r.onset_time_s, 6),
                "stable_time_s": _fmt(r.stable_time_s, 6),
                "onset_ms": _fmt(r.onset_ms),
                "onset_latency_from_command_ms": _fmt(r.onset_ms),
                "settling_time_from_onset_ms": _fmt(
                    r.settling_time_from_onset_ms),
                "stable_latency_from_command_ms": _fmt(
                    r.stable_latency_from_command_ms),
                "steady_state_envelope_counts": _fmt(
                    r.steady_state_envelope_counts, 2),
                "steady_state_envelope_ms2": _fmt(
                    r.steady_state_envelope_ms2, 5),
                "steady_band_low_counts": _fmt(r.steady_band_low_counts, 2),
                "steady_band_high_counts": _fmt(r.steady_band_high_counts, 2),
                "env_noise_p95_counts": f"{r.env_noise_p95_counts:.2f}",
                "delay_ms": _fmt(r.delay_ms),
                "peak_axis_delta_counts": f"{r.peak_axis_delta_counts:.2f}",
                "onset_threshold_counts": f"{r.onset_threshold_counts:.1f}",
                "threshold_counts": f"{r.threshold_counts:.1f}",
                "vib_duration_s": f"{r.vib_duration_s:.2f}",
                "envelope_window_s": f"{r.envelope_window_s:.4f}",
                "steady_tolerance_frac": f"{r.steady_tolerance_frac:.3f}",
                "steady_hold_s": f"{r.steady_hold_s:.4f}",
                "onset_sustain_s": f"{r.onset_sustain_s:.4f}",
                "sample_rate_hz": f"{r.sample_rate_hz:.1f}",
            }
            for column in METRIC_CSV_COLUMNS:
                row[column] = ("" if r.intensity is None
                               else getattr(r.intensity, column))
            # The legacy metric's baseline is the trial's quiet-window
            # |a| mean, which is recorded even for a trial with no
            # intensity block (an empty drive window).
            if r.intensity is None:
                row["baseline_magnitude_counts"] = (
                    f"{r.baseline_magnitude_counts:.2f}")
            writer.writerow(row)


def save_samples_csv(path: str, results: List[TrialResult]) -> None:
    """Per-sample detector traces: the raw per-axis deviation and the
    moving-RMS envelope the settling test ran on, so the figure can be
    rebuilt from the CSVs alone. (The lossless three-axis samples live in
    the .raw_acc.npz next to it.)"""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["trial_id", "rel_time_s", "delta", "envelope"])
        for r in results:
            envelopes = r.envelopes or [float("nan")] * len(r.deltas)
            for t, d, e in zip(r.rel_times, r.deltas, envelopes):
                writer.writerow([r.trial_id, f"{t:.5f}", f"{d:.2f}",
                                 "" if e != e else f"{e:.2f}"])


def _series_stats(values: List[float]) -> dict:
    """Mean, median, SD and range of one latency series (empty -> None)."""
    return {
        "n": len(values),
        "mean_ms": statistics.fmean(values) if values else None,
        "median_ms": statistics.median(values) if values else None,
        "sd_ms": statistics.stdev(values) if len(values) >= 2 else None,
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
    }


def _prefixed(prefix: str, stats: dict) -> dict:
    return {f"{prefix}_{key}": value for key, value in stats.items()}


def delay_stats(results: List[TrialResult]) -> dict:
    """Per-run summary. Onset latency and settling time are reported as
    SEPARATE series (never merged), each with mean/median/SD/range, next
    to the counts of every detection status."""
    onsets = [r.onset_ms for r in results if r.onset_ms is not None]
    settlings = [r.settling_time_from_onset_ms for r in results
                 if r.settling_time_from_onset_ms is not None]
    stables = [r.stable_latency_from_command_ms for r in results
               if r.stable_latency_from_command_ms is not None]
    crossings = [r.delay_ms for r in results if r.delay_ms is not None]
    steadies = [r.steady_state_envelope_counts for r in results
                if r.steady_state_envelope_counts is not None]

    onset = _series_stats(onsets)
    settling = _series_stats(settlings)
    stable = _series_stats(stables)
    crossing = _series_stats(crossings)

    stats = {
        "n_trials": len(results),
        "n_ok": sum(1 for r in results if r.status == STATUS_OK),
        "n_no_onset": sum(1 for r in results if r.status == STATUS_NO_ONSET),
        "n_not_settled": sum(1 for r in results
                             if r.status == STATUS_NOT_SETTLED),
        "n_insufficient_data": sum(1 for r in results
                                   if r.status == STATUS_INSUFFICIENT_DATA),
        # Pre-settling runs used a single "timeout" status.
        "n_timeout": sum(1 for r in results if r.status == "timeout"),
        "n_onset": len(onsets),
        "n_settling": len(settlings),
        "steady_state_envelope_mean_counts": (statistics.fmean(steadies)
                                              if steadies else None),
    }
    stats.update(_prefixed("onset", onset))
    stats.update(_prefixed("settling", settling))
    stats.update(_prefixed("stable", stable))
    stats.update(_prefixed("crossing", crossing))
    # Long-standing key names other code/READMEs already use.
    stats.update({
        "onset_mean_ms": onset["mean_ms"],
        "onset_sd_ms": onset["sd_ms"],
        "onset_min_ms": onset["min_ms"],
        "onset_max_ms": onset["max_ms"],
        "mean_delay_ms": crossing["mean_ms"],
        "sd_delay_ms": crossing["sd_ms"],
        "min_delay_ms": crossing["min_ms"],
        "max_delay_ms": crossing["max_ms"],
    })
    return stats


def intensity_stats(results: List[TrialResult]) -> dict:
    """Across-trial mean of each offline intensity metric of the drive
    windows. Reported for completeness only - it plays no part in onset
    or settling detection, and the windows include the quiet pre-onset
    samples and the ring-up, so these are not steady-state intensities
    (steady_state_envelope_counts is)."""
    blocks = [r.intensity for r in results if r.intensity is not None]
    if not blocks:
        return {"n_trials": 0}
    return {
        "n_trials": len(blocks),
        "mean_vector_rms_ms2": statistics.fmean(
            b.vector_rms_ms2 for b in blocks),
        "mean_legacy_magnitude_rms_ms2": statistics.fmean(
            b.legacy_magnitude_rms_ms2 for b in blocks),
        "mean_baseline_magnitude_counts": statistics.fmean(
            b.baseline_magnitude_counts for b in blocks),
        "note": ("whole-drive-window intensities (quiet pre-onset samples "
                 "and ring-up included); offline only, not used for "
                 "detection - see steady_state_envelope for the settled "
                 "level"),
    }


def _representative(results: List[TrialResult]) -> Optional[TrialResult]:
    """The trial the detail annotations are drawn for: the median-onset
    successful trial, else any trial that has a trace."""
    ok = [r for r in results if r.status == STATUS_OK and r.envelopes
          and r.onset_rel_time is not None]
    if ok:
        ok.sort(key=lambda r: r.onset_rel_time)
        return ok[len(ok) // 2]
    traced = [r for r in results if r.envelopes or r.deltas]
    return traced[0] if traced else None


def _draw_envelopes(ax, results: List[TrialResult],
                    representative: Optional[TrialResult],
                    annotate: bool, ylim_top: Optional[float] = None) -> bool:
    """Envelope traces with the four instants marked. `annotate` adds the
    representative trial's detail (steady band, onset/stable lines, the
    rise arrow) - used on the zoomed panel, where there is room.

    `ylim_top` is applied BEFORE the annotations are placed, because the
    rise arrow is positioned as a fraction of the axis height."""
    any_trace = False
    for r in results:
        envelope = r.envelopes if r.envelopes else r.deltas
        if not (r.rel_times and envelope):
            continue
        any_trace = True
        is_rep = (representative is not None
                  and r.trial_id == representative.trial_id)
        times_ms = [t * 1000.0 for t in r.rel_times]
        # Only the annotated trial is named: ten "Trial n" entries would
        # crowd out the four things this panel is actually explaining.
        ax.plot(times_ms, envelope,
                linewidth=2.0 if is_rep else 0.8,
                alpha=1.0 if is_rep else 0.30,
                color="tab:blue" if is_rep else "tab:gray",
                label=(f"Trial {r.trial_id} (annotated)" if is_rep
                       else "_nolegend_"))
        for rel_time, marker, colour in ((r.onset_rel_time, "v", "tab:red"),
                                         (r.stable_rel_time, "o", "tab:green")):
            if rel_time is not None:
                ax.plot(rel_time * 1000.0,
                        np.interp(rel_time, r.rel_times, envelope),
                        marker=marker, color=colour,
                        markersize=8 if is_rep else 4, zorder=5)

    if ylim_top is not None:
        ax.set_ylim(0, ylim_top)
    ax.axvline(0.0, color="black", linewidth=1.4)
    ax.annotate("motor command", xy=(0, 0), xycoords=("data", "axes fraction"),
                xytext=(4, 6), textcoords="offset points", fontsize=7,
                rotation=90)
    if representative is None:
        return any_trace

    rep = representative
    if (rep.steady_band_low_counts is not None
            and rep.steady_band_high_counts is not None):
        ax.axhspan(rep.steady_band_low_counts, rep.steady_band_high_counts,
                   color="tab:green", alpha=0.12,
                   label=f"Steady band ±{STEADY_TOL_FRACTION:.0%} "
                         f"(trial {rep.trial_id})")
    if rep.steady_state_envelope_counts is not None:
        ax.axhline(rep.steady_state_envelope_counts, color="tab:green",
                   linestyle="--", linewidth=1,
                   label=f"Steady level = "
                         f"{rep.steady_state_envelope_counts:.0f} counts")
    if not annotate:
        return any_trace

    if rep.onset_rel_time is not None:
        ax.axvline(rep.onset_rel_time * 1000.0, color="tab:red",
                   linestyle="--", linewidth=1,
                   label=f"Vibration onset = {rep.onset_ms:.2f} ms")
    if rep.stable_rel_time is not None:
        ax.axvline(rep.stable_rel_time * 1000.0, color="tab:green",
                   linestyle="-.", linewidth=1,
                   label=f"Stable state = "
                         f"{rep.stable_latency_from_command_ms:.1f} ms")
    # The rise: the interval the settling time actually measures.
    if (rep.onset_rel_time is not None and rep.stable_rel_time is not None
            and rep.settling_time_from_onset_ms is not None):
        low, high = ax.get_ylim()
        y = low + 0.80 * (high - low)
        ax.annotate("", xy=(rep.stable_rel_time * 1000.0, y),
                    xytext=(rep.onset_rel_time * 1000.0, y),
                    arrowprops=dict(arrowstyle="<->", color="tab:orange",
                                    lw=1.6))
        # Centred on the arrow, unless the arrow sits so close to the
        # left edge (a fast actuator on the shared span) that a
        # centred caption would spill over the y axis: then it
        # starts just past the arrow instead.
        x_left, x_right = ax.get_xlim()
        span = x_right - x_left
        middle_ms = (rep.onset_rel_time + rep.stable_rel_time) * 500.0
        crowded = (middle_ms - x_left) < 0.22 * span
        ax.text(rep.stable_rel_time * 1000.0 + 0.015 * span if crowded
                else middle_ms,
                low + 0.83 * (high - low),
                f"rise / settling = "
                f"{rep.settling_time_from_onset_ms:.1f} ms",
                ha="left" if crowded else "center", fontsize=8,
                color="tab:orange")
    return any_trace


def _envelope_ylim(results: List[TrialResult]) -> Optional[float]:
    """Top of the envelope axes: high enough to show every settled trace
    in full, but not so high that one runaway trial (whose whole point is
    that it never settled) flattens all the others."""
    steadies = [r.steady_band_high_counts for r in results
                if r.status == STATUS_OK
                and r.steady_band_high_counts is not None]
    if not steadies:
        steadies = [r.steady_band_high_counts for r in results
                    if r.steady_band_high_counts is not None]
    if not steadies:
        return None
    return 1.6 * max(steadies)


def save_plot(path: str, results: List[TrialResult], actuator_type: str,
              motor_index: int, threshold_hint: Optional[float] = None,
              amp: Optional[int] = None,
              vib_duration_s: Optional[float] = None) -> None:
    """Five panels: the two latencies per trial (separately), the
    steady-state level per trial, and the envelope traces over the whole
    drive window plus a zoom on the rise - with the motor command, the
    vibration onset, the stable state, the rise between them and the
    steady band all marked."""
    stats = delay_stats(results)
    if amp is None:
        amp = actuator_amp(actuator_type)
    if vib_duration_s is None:
        durations = [r.vib_duration_s for r in results if r.vib_duration_s]
        vib_duration_s = max(durations) if durations else VIB_DURATION_S

    fig = plt.figure(figsize=(13, 11))
    grid = fig.add_gridspec(3, 2, height_ratios=[1.0, 1.0, 1.3], hspace=0.42,
                            wspace=0.22)
    xs = [r.trial_id for r in results]
    ticks = xs if len(xs) <= 20 else None

    def series(attr):
        return [getattr(r, attr) if getattr(r, attr) is not None
                else float("nan") for r in results]

    # -- panel 1: latencies measured from the motor command --------------
    ax1 = fig.add_subplot(grid[0, :])
    ax1.plot(xs, series("onset_ms"), marker="o", color="tab:blue",
             label="Vibration onset latency (command → onset)")
    ax1.plot(xs, series("stable_latency_from_command_ms"), marker="^",
             color="tab:green",
             label="Stable latency (command → stable state)")
    if any(r.delay_ms is not None for r in results):
        ax1.plot(xs, series("delay_ms"), marker="x", linestyle=":",
                 color="tab:gray",
                 label=f"Detection-level crossing ({NOISE_MULT:g}× noise p95)")
    if stats["onset_mean_ms"] is not None:
        ax1.axhline(stats["onset_mean_ms"], linestyle="--", color="tab:blue",
                    linewidth=1,
                    label=f"Onset mean = {stats['onset_mean_ms']:.2f} ms")
    if stats["stable_mean_ms"] is not None:
        ax1.axhline(stats["stable_mean_ms"], linestyle="--", color="tab:green",
                    linewidth=1,
                    label=f"Stable mean = {stats['stable_mean_ms']:.2f} ms")
    ax1.set_yscale("symlog", linthresh=10)
    ax1.set_title(f"Motor command → vibration latency "
                  f"({actuator_type}, motor port {motor_index}, amp={amp}, "
                  f"{vib_duration_s:g} s drive)")
    ax1.set_xlabel("Trial")
    ax1.set_ylabel("Latency from command (ms, symlog)")
    ax1.grid(True, which="both", alpha=0.4)
    ax1.legend(fontsize=7, ncols=2)
    if ticks:
        ax1.set_xticks(ticks)

    # -- panel 2: settling time, reported on its own ---------------------
    ax2 = fig.add_subplot(grid[1, 0])
    ax2.plot(xs, series("settling_time_from_onset_ms"), marker="s",
             color="tab:orange", label="Settling time (onset → stable)")
    if stats["settling_mean_ms"] is not None:
        ax2.axhline(stats["settling_mean_ms"], linestyle="--",
                    color="tab:orange", linewidth=1,
                    label=f"Mean = {stats['settling_mean_ms']:.1f} ms")
    if stats["settling_median_ms"] is not None:
        ax2.axhline(stats["settling_median_ms"], linestyle="-.",
                    color="tab:red", linewidth=1,
                    label=f"Median = {stats['settling_median_ms']:.1f} ms")
    unresolved = stats["n_no_onset"] + stats["n_not_settled"]
    ax2.set_title("Settling time from onset"
                  + (f"  ({stats['n_settling']}/{len(results)} trials; "
                     f"{unresolved} without a stable state)"
                     if unresolved else ""))
    ax2.set_xlabel("Trial")
    ax2.set_ylabel("Settling time (ms)")
    ax2.grid(True, alpha=0.4)
    ax2.legend(fontsize=7)
    if ticks:
        ax2.set_xticks(ticks)

    # -- panel 3: the steady level each settling time was judged against -
    ax3 = fig.add_subplot(grid[1, 1])
    ax3.plot(xs, series("steady_state_envelope_counts"), marker="D",
             color="tab:purple", label="Steady-state envelope")
    for r in results:
        if (r.steady_band_low_counts is not None
                and r.steady_band_high_counts is not None):
            ax3.vlines(r.trial_id, r.steady_band_low_counts,
                       r.steady_band_high_counts, color="tab:purple",
                       alpha=0.35, linewidth=4)
    noise_levels = [r.env_noise_p95_counts for r in results
                    if r.env_noise_p95_counts > 0]
    if noise_levels:
        ax3.axhline(statistics.fmean(noise_levels), linestyle=":",
                    color="tab:gray",
                    label="Rest-noise envelope p95 (mean)")
    ax3.set_title(f"Steady-state vibration level "
                  f"(band = ±{STEADY_TOL_FRACTION:.0%})")
    ax3.set_xlabel("Trial")
    ax3.set_ylabel("Envelope (counts)")
    ax3.set_yscale("log")
    ax3.grid(True, which="both", alpha=0.4)
    ax3.legend(fontsize=7)
    if ticks:
        ax3.set_xticks(ticks)

    # -- panels 4+5: the traces, whole window and a zoom on the rise -----
    # The rise is milliseconds long inside a drive window of seconds, so
    # one axis cannot show both; the left panel proves the vibration held,
    # the right one is where the four instants are actually readable.
    representative = _representative(results)
    ax4 = fig.add_subplot(grid[2, 0])
    ax5 = fig.add_subplot(grid[2, 1])
    top = _envelope_ylim(results)

    any_trace = _draw_envelopes(ax4, results, representative, annotate=False,
                                ylim_top=top)
    ax4.set_title("Vibration envelope, whole drive window"
                  if any_trace else
                  "Vibration envelope (no per-sample traces for this run)",
                  fontsize=10)

    # Zoom: the shared ZOOM_END_S span. It only widens for a run that
    # would otherwise have its own marked instants off the panel -
    # margin around the rise is what the fixed span already buys - and
    # it never runs past motor-off.
    stables = [r.stable_rel_time for r in results
               if r.stable_rel_time is not None]
    onsets = [r.onset_rel_time for r in results if r.onset_rel_time is not None]
    marked_s = max(max(stables) if stables else 0.0,
                   max(onsets) if onsets else 0.0)
    zoom_end_s = min(max(ZOOM_END_S, 1.25 * marked_s),
                     float(vib_duration_s))
    ax5.set_xlim(-0.04 * zoom_end_s * 1000.0, zoom_end_s * 1000.0)
    _draw_envelopes(ax5, results, representative, annotate=True, ylim_top=top)
    ax5.set_title(f"Zoom on the rise (first {zoom_end_s * 1000:.0f} ms)",
                  fontsize=10)

    for ax in (ax4, ax5):
        ax.set_xlabel("Time since motor command (ms)")
        ax.grid(True, alpha=0.4)
        if threshold_hint is not None:
            ax.axhline(threshold_hint, linestyle=":", color="tab:gray",
                       label=f"Detection level ≈ {threshold_hint:.0f}")
    # The detector's own statistic - deliberately not one of the shared
    # windowed intensity metrics (see the module docstring). BOTH
    # panels are labelled: the zoom is the one the write-up crops
    # out, and a cropped panel keeps only its own axis labels.
    for ax in (ax4, ax5):
        ax.set_ylabel(ENVELOPE_AXIS_LABEL, fontsize=9)
    if any_trace or threshold_hint is not None:
        ax4.legend(fontsize=6, loc="upper left")
        ax5.legend(fontsize=6, loc="lower right")
    # ▼ = vibration onset, ● = stable state, on every trial's trace.
    fig.text(0.5, 0.005,
             "▼ vibration onset   ● stable state   │ motor command (t = 0)",
             ha="center", fontsize=8, color="#555555")

    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def detection_parameters(vib_duration_s: float = VIB_DURATION_S) -> dict:
    """Every constant the onset/settling detector used, for the meta."""
    return {
        "detection": "per_axis_delta CUSUM onset + moving-RMS envelope settling",
        "min_threshold_counts": MIN_THRESHOLD,
        "noise_mult": NOISE_MULT,
        "consecutive_hits": CONSECUTIVE_HITS,
        "onset_criterion": ("CUSUM change-point (Page 1954): slack "
                            f"{CUSUM_SLACK_SIGMA} sigma, alarm "
                            f"{CUSUM_ALARM_SIGMA} sigma of baseline "
                            "deviation noise; onset = start of the alarmed "
                            "excursion, accepted only when the deviation "
                            f"stays above the slack for "
                            f"{ONSET_SUSTAIN_FRACTION:.0%} of the following "
                            f"{ONSET_SUSTAIN_S * 1000:.0f} ms (spike guard)"),
        "cusum_slack_sigma": CUSUM_SLACK_SIGMA,
        "cusum_alarm_sigma": CUSUM_ALARM_SIGMA,
        "onset_sustain_s": ONSET_SUSTAIN_S,
        "onset_sustain_fraction": ONSET_SUSTAIN_FRACTION,
        "settling_criterion": ("first sample from which the centred "
                               f"{ENVELOPE_WINDOW_S * 1000:.0f} ms moving-RMS "
                               "envelope stays within +/-"
                               f"{STEADY_TOL_FRACTION:.0%} of the steady "
                               "level for the hold window "
                               f"({STEADY_HOLD_INSIDE_FRACTION:.0%} of its "
                               "samples inside)"),
        "envelope_window_s": ENVELOPE_WINDOW_S,
        "steady_ref_fraction": STEADY_REF_FRACTION,
        "steady_ref_min_s": STEADY_REF_MIN_S,
        "steady_tolerance_fraction": STEADY_TOL_FRACTION,
        "steady_hold_s": steady_hold_s(vib_duration_s),
        "steady_hold_fraction_of_duration": STEADY_HOLD_FRACTION,
        "steady_hold_min_s": STEADY_HOLD_MIN_S,
        "steady_hold_max_s": STEADY_HOLD_MAX_S,
        "steady_hold_inside_fraction": STEADY_HOLD_INSIDE_FRACTION,
        "steady_min_snr": STEADY_MIN_SNR,
        "min_steady_envelope_counts": MIN_STEADY_ENVELOPE_COUNTS,
        "min_detection_samples": MIN_DETECTION_SAMPLES,
        "crossing_estimate": "linear_interpolation",
        "statuses": list(DETECTION_STATUSES),
    }


def save_meta(csv_path: str, samples_path: Optional[str], png_path: str,
              stamp: str, firmware, motor_index: int, acc_sensor_id: int,
              actuator_type: str, results: List[TrialResult],
              still_max_dev: float = STILL_MAX_DEV,
              pwm_freq_hz: Optional[int] = None,
              amp: Optional[int] = None, raw_path: Optional[str] = None,
              vib_duration_s: float = VIB_DURATION_S) -> str:
    # amp/pwm_freq_hz are what was ACTUALLY sent to the rig. They default
    # to the configured values only when a caller omitted them; the
    # config snapshot below is recorded separately and never substituted
    # for the delivered numbers.
    if amp is None:
        amp = actuator_amp(actuator_type)
    if pwm_freq_hz is None:
        pwm_freq_hz = actuator_pwm_hz(actuator_type)
    configured_amp = actuator_amp(actuator_type)
    configured_freq = actuator_pwm_hz(actuator_type)
    meta = {
        "experiment": "motor_acc_delay",
        "saved_at": int(stamp),
        "firmware": firmware,
        "parameters": dict({
            "actuator_type": actuator_type,
            "motor_index": motor_index,
            "acc_sensor_id": acc_sensor_id,
            "amp": amp,
            "vib_duration_s": vib_duration_s,
            "vib_duration_min_s": VIB_DURATION_MIN_S,
            "vib_duration_max_s": VIB_DURATION_MAX_S,
            "num_trials": NUM_TRIALS,
            "baseline_duration_s": BASELINE_DURATION_S,
            "inter_trial_rest_s": INTER_TRIAL_REST_S,
            "pwm_freq_hz": pwm_freq_hz,
            "mounting": "suspended_free_hanging",
            "still_window_s": STILL_WINDOW_S,
            "still_max_dev": still_max_dev,
            "acc_interval_ms": ACC_INTERVAL_MS,
        }, **detection_parameters(vib_duration_s)),
        # Where the delivered drive came from: the configured default for
        # this actuator, or an operator override typed in the window. The
        # "parameters" block above is always what the hardware actually
        # got - this only says how it was chosen.
        "haptic_config": {
            "snapshot": hc.config_snapshot(),
            "configured_amp": configured_amp,
            "configured_pwm_freq_hz": configured_freq,
            "amp_source": hc.value_source(amp, configured_amp),
            "pwm_freq_source": hc.value_source(pwm_freq_hz, configured_freq),
            "motor_index_source": hc.value_source(
                motor_index, ACTUATOR_MOTORS.get(actuator_type)),
            "note": ("'parameters' records what was sent to the rig; this "
                     "block records the config it was compared against"),
        },
        "time_definitions": {
            "command_time_s": "Unix epoch seconds at the 'S' motor-on write",
            "onset_time_s": "Unix epoch seconds of the vibration onset",
            "stable_time_s": "Unix epoch seconds of the steady state",
            "onset_latency_from_command_ms": "onset_time - command_time",
            "settling_time_from_onset_ms": "stable_time - onset_time",
            "stable_latency_from_command_ms": "stable_time - command_time",
            "note": ("onset latency and settling time are separate metrics "
                     "and are never summed or averaged together"),
        },
        "files": {
            "csv": os.path.basename(csv_path),
            "samples_csv": (os.path.basename(samples_path)
                            if samples_path else None),
            "png": os.path.basename(png_path),
            "raw_acceleration": (os.path.basename(raw_path)
                                 if raw_path else None),
        },
        # The shared metric block. selected_plot_metric is recorded for
        # consistency with the intensity sweeps, but this experiment's
        # FIGURE is a latency plot: the metric does not choose what is
        # drawn here, it only labels the offline intensity statistics.
        "metrics": dict(
            metric_meta_block(None, raw_path is not None,
                              os.path.basename(raw_path) if raw_path else None,
                              MS2_PER_COUNT),
            applies_to="offline_drive_window_statistics_only",
            onset_detection=("per-sample per-axis deviation + CUSUM, and its "
                             "sliding-window RMS for settling - deliberately "
                             "NOT one of the windowed intensity metrics; see "
                             "the module docstring"),
        ),
        "result": delay_stats(results),
        "drive_window_intensity": intensity_stats(results),
    }
    path = meta_path_for(csv_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return path


def load_meta(csv_path: str) -> Optional[dict]:
    path = meta_path_for(csv_path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _opt_float(row: dict, *keys: str) -> Optional[float]:
    """First present, non-empty column among `keys`, as a float."""
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return float(value)
    return None


def _intensity_from_row(row: dict) -> Optional[AccelerationMetrics]:
    """The offline intensity block of a trial row, or None for a
    pre-refactor CSV that has no such columns."""
    if not row.get("vector_rms_counts"):
        return None
    values = {}
    for column in METRIC_CSV_COLUMNS:
        value = _opt_float(row, column)
        if value is None:
            return None
        values[column] = int(value) if column == "n_samples" else value
    return AccelerationMetrics(**values)


def load_results(csv_path: str) -> List[TrialResult]:
    """Read back a delay_trials_*.csv (summary rows, no traces).

    Accepts every historical column layout: the pre-refactor names
    (baseline_mag, peak_delta, threshold, onset_threshold), the
    onset/crossing-only runs that predate the settling metrics (their
    settling columns simply come back as None), and the current one."""
    results: List[TrialResult] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            onset_ms = _opt_float(row, "onset_ms",
                                  "onset_latency_from_command_ms")
            stable_ms = _opt_float(row, "stable_latency_from_command_ms")
            results.append(TrialResult(
                trial_id=int(row["trial_id"]),
                status=row["status"],
                delay_ms=_opt_float(row, "delay_ms"),
                baseline_magnitude_counts=_opt_float(
                    row, "baseline_magnitude_counts", "baseline_mag") or 0.0,
                peak_axis_delta_counts=_opt_float(
                    row, "peak_axis_delta_counts", "peak_delta") or 0.0,
                # Columns below were added over time; absent in older runs.
                threshold_counts=_opt_float(
                    row, "threshold_counts", "threshold") or 0.0,
                onset_ms=onset_ms,
                onset_threshold_counts=_opt_float(
                    row, "onset_threshold_counts", "onset_threshold") or 0.0,
                intensity=_intensity_from_row(row),
                actuator_type=row.get("actuator_type", "") or "",
                command_time_s=_opt_float(row, "command_time_s"),
                onset_time_s=_opt_float(row, "onset_time_s"),
                stable_time_s=_opt_float(row, "stable_time_s"),
                settling_time_from_onset_ms=_opt_float(
                    row, "settling_time_from_onset_ms"),
                stable_latency_from_command_ms=stable_ms,
                steady_state_envelope_counts=_opt_float(
                    row, "steady_state_envelope_counts"),
                steady_band_low_counts=_opt_float(row, "steady_band_low_counts"),
                steady_band_high_counts=_opt_float(
                    row, "steady_band_high_counts"),
                env_noise_p95_counts=_opt_float(row, "env_noise_p95_counts") or 0.0,
                vib_duration_s=_opt_float(row, "vib_duration_s") or 0.0,
                envelope_window_s=_opt_float(
                    row, "envelope_window_s") or ENVELOPE_WINDOW_S,
                steady_tolerance_frac=_opt_float(
                    row, "steady_tolerance_frac") or STEADY_TOL_FRACTION,
                steady_hold_s=_opt_float(row, "steady_hold_s") or 0.0,
                onset_sustain_s=_opt_float(
                    row, "onset_sustain_s") or ONSET_SUSTAIN_S,
                sample_rate_hz=_opt_float(row, "sample_rate_hz") or 0.0,
                onset_rel_time=None if onset_ms is None else onset_ms / 1000.0,
                stable_rel_time=(None if stable_ms is None
                                 else stable_ms / 1000.0),
            ))
    if not results:
        raise ValueError(f"No trial rows found in {csv_path}")
    return results


@dataclass
class TrialTrace:
    """One trial's per-sample series as read back from a samples CSV."""
    rel_times: List[float] = field(default_factory=list)
    deltas: List[float] = field(default_factory=list)
    envelopes: List[float] = field(default_factory=list)


def load_samples(samples_path: str) -> Dict[int, TrialTrace]:
    """Read back a delay_samples_*.csv. The `envelope` column was added
    with the settling metrics; older files carry deltas only and their
    envelope is recomputed here so those runs still re-render."""
    traces: Dict[int, TrialTrace] = {}
    with open(samples_path, newline="") as f:
        for row in csv.DictReader(f):
            trace = traces.setdefault(int(row["trial_id"]), TrialTrace())
            trace.rel_times.append(float(row["rel_time_s"]))
            trace.deltas.append(float(row["delta"]))
            envelope = row.get("envelope")
            if envelope:
                trace.envelopes.append(float(envelope))
    for trace in traces.values():
        if len(trace.envelopes) != len(trace.deltas):
            fs = estimate_sample_rate_hz(trace.rel_times)
            window = max(1, int(round(ENVELOPE_WINDOW_S * fs))) if fs else 1
            trace.envelopes = [float(v)
                               for v in moving_rms(trace.deltas, window)]
    return traces


def render_csv(csv_path: str, out_png: str) -> dict:
    """Re-render the summary figure from a saved delay_trials_*.csv.
    Labels come from the sibling .meta.json when it exists; per-sample
    traces come from the sibling delay_samples file when it exists
    (backfilled pre-refactor runs only have summary rows)."""
    meta = load_meta(csv_path)
    params = meta.get("parameters", {}) if meta else {}
    results = load_results(csv_path)
    actuator_type = params.get("actuator_type") or (
        results[0].actuator_type or "LRA")
    motor_index = params.get("motor_index", MOTOR_INDEX)
    amp = params.get("amp", HISTORICAL_AMP)
    vib_duration_s = params.get("vib_duration_s") or None
    # Old fixed-threshold runs recorded "threshold"; adaptive runs carry
    # per-trial thresholds in the CSV instead.
    threshold_hint = params.get("threshold")

    samples_path = samples_path_for(csv_path)
    if os.path.exists(samples_path):
        traces = load_samples(samples_path)
        for r in results:
            trace = traces.get(r.trial_id)
            if trace is None:
                continue
            r.rel_times = trace.rel_times
            r.deltas = trace.deltas
            r.envelopes = trace.envelopes
            if r.delay_ms is not None:
                r.detect_rel_time = r.delay_ms / 1000.0

    save_plot(out_png, results, actuator_type, motor_index,
              threshold_hint=threshold_hint, amp=amp,
              vib_duration_s=vib_duration_s)
    stats = delay_stats(results)
    stats.update({"actuator_type": actuator_type, "amp": amp,
                  "vib_duration_s": vib_duration_s,
                  "csv_path": csv_path, "png_path": out_png})
    return stats


# ==========================================
# Experiment
# ==========================================

def estimated_duration_s(vib_duration_s: float = VIB_DURATION_S) -> float:
    """Wall-clock estimate for a whole run, excluding the per-trial
    stillness wait (which depends on how much the rig is swinging)."""
    return NUM_TRIALS * (BASELINE_DURATION_S + float(vib_duration_s)
                         + INTER_TRIAL_REST_S)


def _format_stats_line(label: str, stats: dict, prefix: str) -> Optional[str]:
    """One aligned latency line. Shared with summary_report() so a
    reloaded run prints exactly what the live run printed."""
    return report.stats_line(label, stats, prefix, unit="ms")


#: The four latency series, in the order they are always reported. Onset
#: latency and settling time are SEPARATE measurements and are never
#: summed or averaged together - see the module docstring.
STATS_SERIES = (
    ("onset", "Vibration onset latency:"),
    ("settling", "Settling time from onset:"),
    ("stable", "Stable latency from command:"),
    ("crossing", "Detection-level crossing:"),
)


def _trial_rows(results: List[TrialResult]) -> list:
    return [(r.trial_id, r.status,
             report.number(r.onset_ms),
             report.number(r.settling_time_from_onset_ms),
             report.number(r.stable_latency_from_command_ms),
             report.number(r.delay_ms),
             report.number(r.steady_state_envelope_counts, decimals=1))
            for r in results]


def summary_report(csv_path: str, summary: Optional[dict] = None) -> list:
    """The run's statistics as text, rebuilt from a saved
    delay_trials_*.csv (+ its .meta.json).

    Same numbers, same wording and same column alignment as the live
    run's console output, so re-opening a saved run gives the reader the
    figures as well as the picture. Everything comes from THAT run's own
    files - never from the current configuration."""
    results = load_results(csv_path)
    meta = load_meta(csv_path)
    params = (meta or {}).get("parameters", {})
    stats = dict(summary) if summary is not None else delay_stats(results)

    actuator = params.get("actuator_type") or stats.get("actuator_type") or "?"
    duration = params.get("vib_duration_s") or stats.get("vib_duration_s")
    amp = params.get("amp", stats.get("amp"))
    details = [
        f"{actuator} on motor port {params.get('motor_index', '?')}, "
        f"amp {amp if amp is not None else '?'}, "
        f"{params.get('pwm_freq_hz', '?')} Hz PWM"
        + (f", {duration:g} s drive" if duration else ""),
        f"{stats.get('n_trials', len(results))} trials, ACC sensor "
        f"{params.get('acc_sensor_id', '?')}, mounting "
        f"{params.get('mounting', '?')}",
    ]
    # How the delivered drive was chosen (configured default vs. an
    # operator override), when the run recorded it.
    haptic = (meta or {}).get("haptic_config", {})
    if haptic.get("amp_source") or haptic.get("pwm_freq_source"):
        details.append(
            f"drive source: amp {haptic.get('amp_source', '?')}, "
            f"frequency {haptic.get('pwm_freq_source', '?')}")

    lines = report.header("Statistics", csv_path, meta, details)
    lines.append("")
    lines.extend(report.table(
        ("trial", "status", "onset ms", "settling ms", "stable ms",
         "crossing ms", "steady counts"),
        _trial_rows(results)))
    lines.append("")
    for prefix, label in STATS_SERIES:
        line = _format_stats_line(label, stats, prefix)
        if line:
            lines.append(line)
    lines.append(
        f"Detected: {stats.get('n_ok', 0)} ok, "
        f"{stats.get('n_no_onset', 0)} no_onset, "
        f"{stats.get('n_not_settled', 0)} not_settled, "
        f"{stats.get('n_insufficient_data', 0)} insufficient_data "
        f"(of {stats.get('n_trials', len(results))} trials)")
    if not stats.get("n_onset"):
        lines.append("No vibration onset was detected in any trial, so no "
                     "latency was computed.")
    lines.append("Onset latency and settling time are separate measurements "
                 "and are never summed or averaged together.")
    return lines


def run_experiment(log: Optional[LogFn] = None,
                   progress: Optional[ProgressFn] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   interactive: bool = True,
                   motor_index: int = MOTOR_INDEX,
                   acc_sensor_id: int = ACC_SENSOR_ID,
                   actuator_type: str = "LRA",
                   still_max_dev: float = STILL_MAX_DEV,
                   amp: Optional[int] = None,
                   pwm_freq_hz: Optional[int] = None,
                   vib_duration_s: float = VIB_DURATION_S) -> dict:
    """Run the delay + settling measurement and write the output files.

    log/progress/should_stop let a GUI wrapper stream the console
    output, drive a progress bar, and abort between trials;
    motor_index/acc_sensor_id/actuator_type pick the actuator under
    test (LRA on port 11, ERM on port 10 by wiring convention);
    still_max_dev adjusts the stillness gate for the rig's noise floor;
    amp is the PWM drive amplitude (None = this actuator's configured
    default cue level - runs at any other amp measure a DIFFERENT cue
    than the study delivers); pwm_freq_hz overrides the per-actuator drive frequency
    (None = the actuator's configured default frequency: an LRA's
    resonance, an ERM's kHz carrier - config.json "haptic");
    vib_duration_s is how long the motor is driven - and the full
    three-axis window recorded - per trial (VIB_DURATION_MIN_S..MAX_S).
    Returns a summary dict with separate onset and settling statistics.
    """
    log = log if log is not None else print
    progress = progress if progress is not None else (lambda done, total: None)
    should_stop = should_stop if should_stop is not None else (lambda: False)

    vib_duration_s = float(min(max(float(vib_duration_s), VIB_DURATION_MIN_S),
                               VIB_DURATION_MAX_S))

    # The configured defaults for THIS actuator: used when the caller
    # passed nothing, and kept alongside so the log and the meta can say
    # whether the run used the default or an operator override.
    configured_amp = actuator_amp(actuator_type)
    configured_freq = actuator_pwm_hz(actuator_type)
    if amp is None:
        amp = configured_amp

    log(f"Motor -> ACC delay test: {actuator_type} on motor port "
        f"{motor_index}, amp={amp}, {NUM_TRIALS} trials, "
        f"{vib_duration_s:g} s vibration per trial")
    if amp != configured_amp:
        log(f"NOTE: amp {amp} differs from the configured cue level "
            f"({configured_amp}) "
            "- this run measures a different cue than the study delivers, "
            "so its latency does not bound the study's timestamp error.")
    log("Suspend the motor+sensor pair freely in the air (any orientation); "
        "each trial waits until it hangs still before measuring.")
    log(f"Per trial: onset latency (command -> first sustained motion) and "
        f"settling time (onset -> steady state held for "
        f"{steady_hold_s(vib_duration_s) * 1000:.0f} ms).")
    log(f"Estimated duration: ~{estimated_duration_s(vib_duration_s):.0f} s "
        "plus settling time.\n")

    stamp = str(int(time.time()))
    # ERM needs kHz-range PWM to start (see actuator_pwm_hz); the LRA
    # sits at its resonance.
    if pwm_freq_hz is None:
        pwm_freq_hz = configured_freq
    recorder = RawSampleRecorder(
        run_id=f"motor_acc_delay_{stamp}",
        experiment="motor_acc_delay",
        ms2_per_count=MS2_PER_COUNT,
        sensor_id=acc_sensor_id,
        meta={"actuator_type": actuator_type, "motor_index": motor_index,
              "amp": amp, "pwm_freq_hz": pwm_freq_hz,
              "acc_interval_ms": ACC_INTERVAL_MS,
              "baseline_duration_s": BASELINE_DURATION_S,
              "vib_duration_s": vib_duration_s,
              "window_note": ("'vibration' windows span the whole drive "
                              "period, from the motor-on command to "
                              "motor-off, so they include the quiet "
                              "pre-onset samples, the ring-up and the "
                              "steady state - and no ring-down")},
    )

    ser = open_rig(log=log, interactive=interactive)
    results: List[TrialResult] = []
    try:
        send(ser, "X")
        send(ser, f"F {motor_index} {pwm_freq_hz}")
        log(f"PWM frequency on port {motor_index}: {pwm_freq_hz} Hz "
            f"({actuator_type})")
        send(ser, "A STOP", wait_s=0.2)
        ser.reset_input_buffer()
        send(ser, f"A START {ACC_INTERVAL_MS}", wait_s=0.2)

        for trial_id in range(1, NUM_TRIALS + 1):
            if should_stop():
                raise SweepAborted()
            results.append(run_single_trial(ser, trial_id, log,
                                            motor_index, acc_sensor_id,
                                            should_stop,
                                            still_max_dev=still_max_dev,
                                            amp=amp, recorder=recorder,
                                            pwm_freq_hz=pwm_freq_hz,
                                            vib_duration_s=vib_duration_s,
                                            actuator_type=actuator_type))
            progress(trial_id, NUM_TRIALS)
            if trial_id < NUM_TRIALS:
                time.sleep(INTER_TRIAL_REST_S)

        stats = delay_stats(results)
        log("\n===== Summary =====")
        for r in results:
            onset = "-" if r.onset_ms is None else f"{r.onset_ms:.2f}"
            settling = ("-" if r.settling_time_from_onset_ms is None
                        else f"{r.settling_time_from_onset_ms:.2f}")
            stable = ("-" if r.stable_latency_from_command_ms is None
                      else f"{r.stable_latency_from_command_ms:.2f}")
            steady = ("-" if r.steady_state_envelope_counts is None
                      else f"{r.steady_state_envelope_counts:.1f}")
            log(f"Trial {r.trial_id}: status={r.status}, onset={onset} ms, "
                f"settling={settling} ms, stable={stable} ms, "
                f"steady={steady} counts")
        for prefix, label in STATS_SERIES:
            line = _format_stats_line(label, stats, prefix)
            if line:
                log(line)
        log(f"Detected: {stats['n_ok']} ok, {stats['n_no_onset']} no_onset, "
            f"{stats['n_not_settled']} not_settled, "
            f"{stats['n_insufficient_data']} insufficient_data "
            f"(of {stats['n_trials']} trials)")
        if stats["n_onset"] == 0:
            log("No vibration onset was detected in any trial, so no "
                "latency was computed.")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # <stamp> is Unix epoch seconds, taken when the run started, per
        # the project-wide epoch-timestamps rule.
        csv_path = os.path.join(OUTPUT_DIR, f"delay_trials_{stamp}.csv")
        samples_path = os.path.join(OUTPUT_DIR, f"delay_samples_{stamp}.csv")
        png_path = os.path.join(OUTPUT_DIR, f"delay_summary_{stamp}.png")
        raw_pth = raw_path_for(csv_path) if recorder.n_windows else None
        save_trials_csv(csv_path, results)
        save_samples_csv(samples_path, results)
        if raw_pth is not None:
            recorder.save(raw_pth)
        save_plot(png_path, results, actuator_type, motor_index, amp=amp,
                  vib_duration_s=vib_duration_s)
        meta_path = save_meta(csv_path, samples_path, png_path, stamp,
                              getattr(ser, "rig_identity", None),
                              motor_index, acc_sensor_id, actuator_type,
                              results, still_max_dev=still_max_dev,
                              pwm_freq_hz=pwm_freq_hz, amp=amp,
                              raw_path=raw_pth,
                              vib_duration_s=vib_duration_s)
        log(f"Data:  {csv_path}")
        if raw_pth is not None:
            log(f"Raw ACC: {raw_pth} ({recorder.n_samples} samples, "
                f"{recorder.n_windows} windows)")
        log(f"Plot:  {png_path}")
        log(f"Meta:  {meta_path}")

        stats.update({"actuator_type": actuator_type, "amp": amp,
                      "vib_duration_s": vib_duration_s,
                      "csv_path": csv_path, "png_path": png_path,
                      "raw_path": raw_pth})
        return stats
    finally:
        try:
            send(ser, "X")
            # A non-default PWM frequency (ERM runs) must not outlive the
            # experiment - restore the boot default.
            send(ser, f"F {motor_index} {DEFAULT_PWM_FREQ}")
            send(ser, "A STOP", wait_s=0.1)
        except Exception:
            pass
        ser.close()
        log("Serial port closed.")


def main() -> None:
    run_experiment()


if __name__ == "__main__":
    main()
