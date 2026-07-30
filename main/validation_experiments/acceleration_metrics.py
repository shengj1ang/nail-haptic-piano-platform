"""Shared accelerometer analysis for every validation experiment.

One place for the vibration-intensity formulas, the counts -> m/s^2
conversion, the metric naming, and the lossless raw-sample store. Before
this module each sweep script carried its own copy of
`sqrt(mean((|a| - baseline)^2))`, its own `MS2_PER_COUNT`, and saved only
the final scalar per cell - so a run could never be re-analysed with a
different intensity definition without re-doing the hardware sweep.

Two intensity metrics are supported, and every experiment computes BOTH
for every measurement window:

1. Legacy magnitude RMS ("legacy_magnitude_rms") - the original project
   formula, kept so old and new runs stay comparable:

       magnitude_i = sqrt(x_i^2 + y_i^2 + z_i^2)
       legacy      = sqrt(mean((magnitude_i - baseline_magnitude)^2))

   It needs a separately measured quiet-window `baseline_magnitude`, and
   it is only second-order sensitive to vibration perpendicular to
   gravity (sqrt(g^2 + v^2) ~ g + v^2/2g), which is exactly why the
   motor->ACC delay experiment never used it for detection.

2. Demeaned three-axis vector RMS ("vector_rms") - the RECOMMENDED
   metric, computed per window with no external baseline:

       vector_rms = sqrt(mean((x_i - mean_x)^2
                             + (y_i - mean_y)^2
                             + (z_i - mean_z)^2))
                  = sqrt(rms_x^2 + rms_y^2 + rms_z^2)

   Removing each axis's own mean removes gravity, the sensor's static
   offset and the mounting orientation in one step, so the number is the
   AC energy of the vibration regardless of how the rig is oriented.

Both are computed in raw LIS3DH counts and converted once, at the end,
with `value_ms2 = value_counts * ms2_per_count`. Nothing in this project
should convert twice or store an unlabelled number: every stored field
name ends in `_counts` or `_ms2`.

A third group of functions answers the other half of the question - not
how much a window vibrates but AT WHAT FREQUENCIES:
`compute_vibration_spectrum`, `harmonic_amplitudes` and
`total_harmonic_distortion` (see "Spectral analysis" below). They use
the same demeaned three-axis convention as the vector RMS, so the
spectrum and the intensity describe the same signal.

The raw store (`save_raw_acceleration_samples` /
`load_raw_acceleration_samples`) is a compressed NPZ holding the FULL
three-axis time series of every measurement window in long format, with
the commanded frequency/amp, the cell/trial identity, the sensor id and
a Unix-epoch timestamp per sample. Given that file alone, both metrics
and every plot can be reproduced offline
(`recompute_window_metrics`) - which is the whole point.
"""

import json
import math
import os
import time
from dataclasses import dataclass, field, fields
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

# ==========================================
# Units
# ==========================================

# LIS3DH high-resolution mode, +/-2 g: 1 count = 1 mg. The single
# definition for the whole project - experiments import it from here.
MS2_PER_COUNT = 0.001 * 9.80665


def counts_to_ms2(value_counts: float,
                  ms2_per_count: float = MS2_PER_COUNT) -> float:
    """counts -> m/s^2. The ONLY conversion in the codebase; call it once
    per quantity (a `_ms2` field is never converted again)."""
    return float(value_counts) * float(ms2_per_count)


# `time.monotonic()` is what the sample collectors stamp with (it cannot
# jump backwards mid-window), but the project stores Unix epoch seconds
# everywhere. Capture the offset once so a monotonic stamp can be mapped
# to epoch without losing the monotonic clock's stability inside a run.
_EPOCH_MINUS_MONOTONIC = time.time() - time.monotonic()


def to_epoch_seconds(monotonic_s: float) -> float:
    """Map a `time.monotonic()` stamp to Unix epoch seconds."""
    return float(monotonic_s) + _EPOCH_MINUS_MONOTONIC


# ==========================================
# Metric registry
# ==========================================

METRIC_LEGACY_MAGNITUDE_RMS = "legacy_magnitude_rms"
METRIC_VECTOR_RMS = "vector_rms"

# New runs default to the demeaned vector RMS; the legacy metric stays
# available for historical comparison and for old CSVs.
DEFAULT_METRIC = METRIC_VECTOR_RMS

# Bumped whenever the stored metric field set changes. 1 = the original
# single `rms_delta*` columns (pre-refactor runs), 2 = both metrics plus
# per-axis statistics plus a raw-sample file.
METRIC_VERSION = 2


@dataclass(frozen=True)
class MetricSpec:
    name: str
    short_label: str          # plot/axis wording
    gui_label: str            # dropdown wording
    counts_field: str
    ms2_field: str
    requires_raw_samples: bool  # can it be derived from an old scalar CSV?
    description: str


METRICS: Dict[str, MetricSpec] = {
    METRIC_LEGACY_MAGNITUDE_RMS: MetricSpec(
        name=METRIC_LEGACY_MAGNITUDE_RMS,
        short_label="Legacy magnitude RMS",
        gui_label="Legacy magnitude RMS",
        counts_field="legacy_magnitude_rms_counts",
        ms2_field="legacy_magnitude_rms_ms2",
        # Old CSVs already carry this one under its old column names.
        requires_raw_samples=False,
        description=("sqrt(mean((|a_i| - baseline_magnitude)^2)); the "
                     "original project formula, kept for comparability "
                     "with historical runs"),
    ),
    METRIC_VECTOR_RMS: MetricSpec(
        name=METRIC_VECTOR_RMS,
        short_label="Demeaned 3-axis vector RMS",
        gui_label="Demeaned 3-axis vector RMS (Recommended)",
        counts_field="vector_rms_counts",
        ms2_field="vector_rms_ms2",
        # Cannot be reconstructed from a scalar magnitude RMS - it needs
        # the per-axis samples.
        requires_raw_samples=True,
        description=("sqrt(mean(sum_axes((a_i - mean_axis)^2))); removes "
                     "gravity, static sensor bias and mounting "
                     "orientation - the recommended metric"),
    ),
}

METRIC_NAMES: Tuple[str, ...] = (METRIC_VECTOR_RMS, METRIC_LEGACY_MAGNITUDE_RMS)

# Column names earlier revisions of each experiment wrote for what is now
# `legacy_magnitude_rms_*`. Used only when reading an old CSV.
LEGACY_COUNTS_ALIASES = ("legacy_magnitude_rms_counts", "rms_delta_counts",
                         "rms_delta", "rms_counts")
LEGACY_MS2_ALIASES = ("legacy_magnitude_rms_ms2", "rms_ms2")
BASELINE_ALIASES = ("baseline_magnitude_counts", "baseline_mag")
PEAK_ALIASES = ("peak_magnitude_delta_counts", "peak_delta_counts",
                "peak_delta")


def metric_spec(name: Optional[str]) -> MetricSpec:
    """Look up a metric by name; unknown/None falls back to the default."""
    return METRICS.get(name or "", METRICS[DEFAULT_METRIC])


def normalise_metric(name: Optional[str]) -> str:
    return metric_spec(name).name


def metric_field(name: Optional[str], unit: str = "ms2") -> str:
    spec = metric_spec(name)
    if unit == "counts":
        return spec.counts_field
    if unit == "ms2":
        return spec.ms2_field
    raise ValueError(f"unit must be 'counts' or 'ms2', got {unit!r}")


def metric_axis_label(name: Optional[str], unit: str = "ms2") -> str:
    """Axis / colour-bar wording for a metric, e.g.
    "Demeaned 3-axis vector RMS acceleration (m/s²)"."""
    unit_label = "m/s²" if unit == "ms2" else "raw LIS3DH counts"
    return f"{metric_spec(name).short_label} acceleration ({unit_label})"


def available_metrics(has_raw_samples: bool) -> List[str]:
    """Metric names a data set can actually be plotted with. Without the
    per-sample three-axis data only the legacy metric is available - the
    vector RMS is NOT derivable from a stored scalar magnitude RMS."""
    return [n for n in METRIC_NAMES
            if has_raw_samples or not METRICS[n].requires_raw_samples]


# ==========================================
# Sample handling
# ==========================================

# A "sample" is either (x, y, z) or (timestamp, x, y, z) - rig.Sample,
# the 4-tuple the collectors produce. Both are accepted everywhere.

def as_xyz_arrays(samples) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(x, y, z) float arrays from a sequence of 3- or 4-element samples
    (or an (N, 3) / (N, 4) array). 4-element samples are (t, x, y, z)."""
    arr = np.asarray(samples, dtype=float)
    if arr.ndim != 2 or arr.shape[0] == 0:
        raise ValueError("expected a non-empty sequence of acceleration samples")
    if arr.shape[1] == 4:
        arr = arr[:, 1:]
    elif arr.shape[1] != 3:
        raise ValueError("samples must be (x, y, z) or (t, x, y, z), got "
                         f"{arr.shape[1]} columns")
    return arr[:, 0], arr[:, 1], arr[:, 2]


def magnitudes(samples) -> np.ndarray:
    """Per-sample |a| = sqrt(x^2 + y^2 + z^2), in the samples' own units."""
    x, y, z = as_xyz_arrays(samples)
    return np.sqrt(x * x + y * y + z * z)


def compute_baseline_magnitude(samples) -> float:
    """Mean |a| over a quiet (motor-off) window - the rest floor the
    legacy metric is measured against. Dominated by gravity (~1000
    counts at +/-2 g)."""
    return float(np.mean(magnitudes(samples)))


# ==========================================
# The two intensity metrics
# ==========================================

def compute_legacy_magnitude_rms(samples, baseline_magnitude: float) -> float:
    """Legacy metric, in the samples' units (counts):

        sqrt(mean((|a_i| - baseline_magnitude)^2))

    Kept unchanged from the original sweep scripts so historical results
    remain directly comparable. Needs a baseline measured in a separate
    quiet window; pass the value from `compute_baseline_magnitude`."""
    mags = magnitudes(samples)
    delta = mags - float(baseline_magnitude)
    return float(math.sqrt(float(np.mean(delta * delta))))


def compute_peak_magnitude_delta(samples, baseline_magnitude: float) -> float:
    """Largest single-sample |a| deviation from the baseline (counts) -
    the legacy secondary metric, logged alongside the RMS."""
    mags = magnitudes(samples)
    return float(np.max(np.abs(mags - float(baseline_magnitude))))


def compute_demeaned_vector_rms(samples) -> float:
    """Recommended metric, in the samples' units (counts):

        vector_rms = sqrt(mean((x-mean_x)^2 + (y-mean_y)^2 + (z-mean_z)^2))

    Each axis is demeaned over THIS window, so gravity, the sensor's
    static bias and the mounting orientation all drop out and no
    separate baseline window is needed. Equivalent to
    sqrt(rms_x^2 + rms_y^2 + rms_z^2) - see `compute_axis_statistics`."""
    x, y, z = as_xyz_arrays(samples)
    dx = x - x.mean()
    dy = y - y.mean()
    dz = z - z.mean()
    return float(math.sqrt(float(np.mean(dx * dx + dy * dy + dz * dz))))


@dataclass
class AxisStatistics:
    """Per-axis mean and demeaned RMS over one measurement window, in
    raw counts. `vector_rms_counts` is the quadrature sum of the three
    axis RMS values - the same number `compute_demeaned_vector_rms`
    returns, exposed here so a caller can see the axis breakdown."""
    n_samples: int
    mean_x_counts: float
    mean_y_counts: float
    mean_z_counts: float
    rms_x_counts: float
    rms_y_counts: float
    rms_z_counts: float

    @property
    def vector_rms_counts(self) -> float:
        return float(math.sqrt(self.rms_x_counts ** 2
                               + self.rms_y_counts ** 2
                               + self.rms_z_counts ** 2))


def compute_axis_statistics(samples) -> AxisStatistics:
    """Per-axis means and demeaned RMS values (counts) for one window."""
    x, y, z = as_xyz_arrays(samples)
    return AxisStatistics(
        n_samples=int(x.size),
        mean_x_counts=float(x.mean()),
        mean_y_counts=float(y.mean()),
        mean_z_counts=float(z.mean()),
        rms_x_counts=float(np.sqrt(np.mean((x - x.mean()) ** 2))),
        rms_y_counts=float(np.sqrt(np.mean((y - y.mean()) ** 2))),
        rms_z_counts=float(np.sqrt(np.mean((z - z.mean()) ** 2))),
    )


@dataclass
class AccelerationMetrics:
    """Everything the experiments store per measurement window. The field
    names are the CSV column names; each ends in `_counts` or `_ms2` so a
    unit is never ambiguous."""
    n_samples: int
    mean_x_counts: float
    mean_y_counts: float
    mean_z_counts: float
    rms_x_counts: float
    rms_y_counts: float
    rms_z_counts: float
    legacy_magnitude_rms_counts: float
    legacy_magnitude_rms_ms2: float
    vector_rms_counts: float
    vector_rms_ms2: float
    baseline_magnitude_counts: float
    peak_magnitude_delta_counts: float


#: The metric/statistic columns every experiment's summary CSV carries,
#: in a fixed order, so the tables line up across experiments.
METRIC_CSV_COLUMNS: Tuple[str, ...] = tuple(
    f.name for f in fields(AccelerationMetrics))


def compute_acceleration_metrics(
        samples,
        baseline_magnitude: Optional[float] = None,
        ms2_per_count: float = MS2_PER_COUNT) -> AccelerationMetrics:
    """Both intensity metrics plus the per-axis statistics for one window.

    `baseline_magnitude` is the quiet-window mean |a| the LEGACY metric
    subtracts; when it is None the window's own mean |a| is used, which
    makes the legacy value a self-baselined magnitude RMS (documented
    fallback for data recorded without a baseline window - the vector
    RMS is unaffected either way)."""
    axes = compute_axis_statistics(samples)
    if baseline_magnitude is None:
        baseline_magnitude = compute_baseline_magnitude(samples)
    legacy_counts = compute_legacy_magnitude_rms(samples, baseline_magnitude)
    vector_counts = compute_demeaned_vector_rms(samples)
    return AccelerationMetrics(
        n_samples=axes.n_samples,
        mean_x_counts=axes.mean_x_counts,
        mean_y_counts=axes.mean_y_counts,
        mean_z_counts=axes.mean_z_counts,
        rms_x_counts=axes.rms_x_counts,
        rms_y_counts=axes.rms_y_counts,
        rms_z_counts=axes.rms_z_counts,
        legacy_magnitude_rms_counts=legacy_counts,
        legacy_magnitude_rms_ms2=counts_to_ms2(legacy_counts, ms2_per_count),
        vector_rms_counts=vector_counts,
        vector_rms_ms2=counts_to_ms2(vector_counts, ms2_per_count),
        baseline_magnitude_counts=float(baseline_magnitude),
        peak_magnitude_delta_counts=compute_peak_magnitude_delta(
            samples, baseline_magnitude),
    )


# ==========================================
# Reading a metric off a result
# ==========================================

def _lookup(result, names: Iterable[str]):
    """First present, non-empty attribute/key among `names`."""
    for name in names:
        value = None
        if isinstance(result, dict):
            if name in result:
                value = result[name]
        elif hasattr(result, name):
            value = getattr(result, name)
        if value is None or value == "":
            continue
        return value
    return None


def get_metric_value(result, metric_name: Optional[str] = None,
                     unit: str = "ms2") -> Optional[float]:
    """The chosen metric's value from a result row - a dataclass (e.g. a
    sweep's CellResult/StepResult), an AccelerationMetrics, or a raw CSV
    dict. Returns None when the row does not carry that metric (an old
    CSV asked for the vector RMS), never a silently wrong number.

    Old column names are accepted for the legacy metric only; a stored
    scalar magnitude RMS can NOT be turned into a vector RMS."""
    spec = metric_spec(metric_name)
    if spec.name == METRIC_VECTOR_RMS:
        names = (spec.counts_field,) if unit == "counts" else (spec.ms2_field,)
    else:
        names = LEGACY_COUNTS_ALIASES if unit == "counts" else LEGACY_MS2_ALIASES
    value = _lookup(result, names)
    if value is None:
        # A `_ms2` field can always be derived from its `_counts` twin
        # (single conversion), which is how the frequency sweep's old
        # counts-only rows still plot in m/s².
        if unit == "ms2":
            counts = get_metric_value(result, spec.name, unit="counts")
            ms2_per_count = _lookup(result, ("ms2_per_count",))
            if counts is not None:
                return counts_to_ms2(counts, float(ms2_per_count)
                                     if ms2_per_count else MS2_PER_COUNT)
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def has_metric(result, metric_name: Optional[str] = None) -> bool:
    return get_metric_value(result, metric_name) is not None


def metrics_available_in(results, metric_names=METRIC_NAMES) -> List[str]:
    """Which metrics an already-loaded result list can be plotted with -
    a metric counts as available only when every row carries it."""
    rows = list(results)
    if not rows:
        return []
    return [name for name in metric_names
            if all(has_metric(r, name) for r in rows)]


def select_metric(results, requested: Optional[str] = None) -> str:
    """The metric to actually plot `results` with: the requested one when
    the data supports it, else the first supported one (legacy for old
    CSVs). Raises when the data supports neither."""
    supported = metrics_available_in(results)
    if not supported:
        raise ValueError("These results carry neither intensity metric.")
    name = normalise_metric(requested)
    return name if name in supported else supported[0]


# ==========================================
# Raw three-axis sample store
# ==========================================

RAW_DATA_FORMAT = "acceleration_raw_npz_v1"
RAW_FILE_SUFFIX = ".raw_acc.npz"

PHASE_BASELINE = "baseline"
PHASE_VIBRATION = "vibration"

#: Long-format columns of the raw store; one row per accelerometer sample.
RAW_COLUMNS: Tuple[str, ...] = (
    "window_id",           # unique id of the measurement window
    "cell_id",             # sweep cell / trial this window belongs to (-1 none)
    "trial_id",            # repetition within the cell (-1 when unused)
    "baseline_window_id",  # window whose mean |a| is this window's baseline
    "sweep_pass",          # "", "coarse", "fine", ...
    "phase",               # "baseline" | "vibration"
    "commanded_freq_hz",   # NaN when the experiment does not command one
    "commanded_amp",
    "sensor_id",           # accelerometer (LIS3DH) sensor id in the ACC stream
    "timestamp_s",         # Unix epoch seconds, host arrival time
    "x", "y", "z",         # raw LIS3DH counts
)


@dataclass
class RawAccelerationSamples:
    """The full three-axis time series of one run, in long format.

    Every array has one entry per accelerometer sample and they are all
    the same length, so `raw.x[mask]` and friends slice consistently.
    `run_id`/`experiment`/`ms2_per_count` identify the run and make the
    file self-describing; `meta` carries whatever extra the experiment
    stored (motor port, timing constants, ...)."""

    run_id: str
    experiment: str
    ms2_per_count: float
    window_id: np.ndarray
    cell_id: np.ndarray
    trial_id: np.ndarray
    baseline_window_id: np.ndarray
    sweep_pass: np.ndarray
    phase: np.ndarray
    commanded_freq_hz: np.ndarray
    commanded_amp: np.ndarray
    sensor_id: np.ndarray
    timestamp_s: np.ndarray
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.x.size)

    def window_ids(self, phase: Optional[str] = None) -> List[int]:
        """Window ids in recording order, optionally only one phase."""
        ids = self.window_id
        if phase is not None:
            ids = ids[self.phase == phase]
        # np.unique sorts; window ids are assigned in recording order so
        # sorted order IS recording order.
        return [int(i) for i in np.unique(ids)]

    def samples_for_window(self, window_id: int) -> np.ndarray:
        """(N, 4) array of [timestamp_s, x, y, z] for one window - the
        shape `compute_*` accepts directly."""
        m = self.window_id == int(window_id)
        return np.column_stack((self.timestamp_s[m], self.x[m],
                                self.y[m], self.z[m]))

    def window_info(self, window_id: int) -> dict:
        """The window's identity columns (they are constant within it)."""
        idx = int(np.flatnonzero(self.window_id == int(window_id))[0])
        return {
            "window_id": int(window_id),
            "cell_id": int(self.cell_id[idx]),
            "trial_id": int(self.trial_id[idx]),
            "baseline_window_id": int(self.baseline_window_id[idx]),
            "sweep_pass": str(self.sweep_pass[idx]),
            "phase": str(self.phase[idx]),
            "commanded_freq_hz": float(self.commanded_freq_hz[idx]),
            "commanded_amp": float(self.commanded_amp[idx]),
            "sensor_id": int(self.sensor_id[idx]),
            "n_samples": int(np.count_nonzero(self.window_id == int(window_id))),
        }

    def baseline_magnitude_for(self, window_id: int) -> Optional[float]:
        """Mean |a| of the baseline window linked to `window_id`, or None
        when the run recorded no baseline for it."""
        info = self.window_info(window_id)
        base_id = info["baseline_window_id"]
        if base_id < 0:
            return None
        base = self.samples_for_window(base_id)
        if base.shape[0] == 0:
            return None
        return compute_baseline_magnitude(base)


@dataclass
class WindowMetrics:
    """One measurement window's identity plus its recomputed metrics."""
    window_id: int
    cell_id: int
    trial_id: int
    sweep_pass: str
    commanded_freq_hz: float
    commanded_amp: float
    sensor_id: int
    metrics: AccelerationMetrics


class RawSampleRecorder:
    """Accumulates the raw three-axis windows of one run, then writes them
    as a single compressed NPZ.

    Experiments call `add_window()` once per collected window (baselines
    included) and `save()` at the end; the recorder assigns the window
    ids and keeps the baseline linkage, so a reader can recompute the
    legacy metric with the very baseline the live run used."""

    def __init__(self, run_id: str, experiment: str,
                 ms2_per_count: float = MS2_PER_COUNT,
                 sensor_id: int = 0, meta: Optional[dict] = None):
        self.run_id = str(run_id)
        self.experiment = str(experiment)
        self.ms2_per_count = float(ms2_per_count)
        self.default_sensor_id = int(sensor_id)
        self.meta = dict(meta or {})
        self._windows: List[dict] = []
        self._next_window_id = 0

    # -- recording ------------------------------------------------------

    def add_window(self, samples, phase: str = PHASE_VIBRATION,
                   cell_id: int = -1, trial_id: int = -1,
                   baseline_window_id: int = -1, sweep_pass: str = "",
                   commanded_freq_hz: float = float("nan"),
                   commanded_amp: float = float("nan"),
                   sensor_id: Optional[int] = None,
                   timestamps: str = "monotonic") -> int:
        """Record one window and return its window id (-1 when empty).

        `samples` are (t, x, y, z) tuples as produced by
        rig.collect_samples, or (x, y, z) tuples when the collector kept
        no time stamps (then the timestamps are stored as NaN).
        `timestamps` says what t means: "monotonic" (converted to Unix
        epoch seconds here) or "epoch" (stored as-is)."""
        rows = list(samples)
        if not rows:
            return -1
        arr = np.asarray(rows, dtype=float)
        if arr.shape[1] == 4:
            times = arr[:, 0]
            if timestamps == "monotonic":
                times = times + _EPOCH_MINUS_MONOTONIC
            elif timestamps != "epoch":
                raise ValueError("timestamps must be 'monotonic' or 'epoch'")
            xyz = arr[:, 1:]
        elif arr.shape[1] == 3:
            times = np.full(arr.shape[0], np.nan)
            xyz = arr
        else:
            raise ValueError("samples must be (x, y, z) or (t, x, y, z)")

        window_id = self._next_window_id
        self._next_window_id += 1
        self._windows.append({
            "window_id": window_id,
            "cell_id": int(cell_id),
            "trial_id": int(trial_id),
            "baseline_window_id": int(baseline_window_id),
            "sweep_pass": str(sweep_pass),
            "phase": str(phase),
            "commanded_freq_hz": float(commanded_freq_hz),
            "commanded_amp": float(commanded_amp),
            "sensor_id": int(self.default_sensor_id if sensor_id is None
                             else sensor_id),
            "timestamp_s": times,
            "xyz": xyz,
        })
        return window_id

    @property
    def n_windows(self) -> int:
        return len(self._windows)

    @property
    def n_samples(self) -> int:
        return sum(w["xyz"].shape[0] for w in self._windows)

    # -- output ---------------------------------------------------------

    def build(self) -> RawAccelerationSamples:
        if not self._windows:
            raise ValueError("no raw acceleration windows were recorded")
        counts = [w["xyz"].shape[0] for w in self._windows]

        def repeated(key, dtype):
            return np.concatenate([
                np.full(n, w[key], dtype=dtype)
                for w, n in zip(self._windows, counts)])

        def repeated_str(key):
            return np.concatenate([
                np.full(n, w[key], dtype=object)
                for w, n in zip(self._windows, counts)]).astype(str)

        xyz = np.concatenate([w["xyz"] for w in self._windows])
        return RawAccelerationSamples(
            run_id=self.run_id,
            experiment=self.experiment,
            ms2_per_count=self.ms2_per_count,
            window_id=repeated("window_id", np.int32),
            cell_id=repeated("cell_id", np.int32),
            trial_id=repeated("trial_id", np.int32),
            baseline_window_id=repeated("baseline_window_id", np.int32),
            sweep_pass=repeated_str("sweep_pass"),
            phase=repeated_str("phase"),
            commanded_freq_hz=repeated("commanded_freq_hz", np.float64),
            commanded_amp=repeated("commanded_amp", np.float64),
            sensor_id=repeated("sensor_id", np.int16),
            timestamp_s=np.concatenate([w["timestamp_s"]
                                        for w in self._windows]),
            # The LIS3DH reports integer counts, so int32 is lossless for
            # real data; rint (not truncation) keeps synthetic or already
            # scaled inputs unbiased around zero.
            x=np.rint(xyz[:, 0]).astype(np.int32),
            y=np.rint(xyz[:, 1]).astype(np.int32),
            z=np.rint(xyz[:, 2]).astype(np.int32),
            meta=dict(self.meta),
        )

    def save(self, path: str) -> str:
        return save_raw_acceleration_samples(path, self.build())


def save_raw_acceleration_samples(path: str,
                                  raw: RawAccelerationSamples) -> str:
    """Write the run's full three-axis series to a compressed NPZ.

    Lossless: x/y/z are the integer LIS3DH counts as received, and the
    identity columns keep every sample attached to its window, cell,
    commanded frequency/amp, trial and sensor. Categorical columns are
    stored as small integer codes plus their label table, which is what
    keeps a multi-hundred-thousand-sample sweep to a few MB."""
    sweep_labels, sweep_codes = np.unique(raw.sweep_pass, return_inverse=True)
    phase_labels, phase_codes = np.unique(raw.phase, return_inverse=True)
    meta = dict(raw.meta)
    meta.update({
        "raw_data_format": RAW_DATA_FORMAT,
        "run_id": raw.run_id,
        "experiment": raw.experiment,
        "ms2_per_count": raw.ms2_per_count,
        "metric_version": METRIC_VERSION,
        "columns": list(RAW_COLUMNS),
        "n_samples": int(raw.x.size),
        "timestamp_units": "unix_epoch_seconds",
        "acceleration_units": "raw_lis3dh_counts",
    })
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    np.savez_compressed(
        path,
        meta_json=np.array(json.dumps(meta)),
        window_id=raw.window_id.astype(np.int32),
        cell_id=raw.cell_id.astype(np.int32),
        trial_id=raw.trial_id.astype(np.int32),
        baseline_window_id=raw.baseline_window_id.astype(np.int32),
        sweep_pass_code=sweep_codes.astype(np.int16),
        sweep_pass_labels=sweep_labels,
        phase_code=phase_codes.astype(np.int16),
        phase_labels=phase_labels,
        commanded_freq_hz=raw.commanded_freq_hz.astype(np.float64),
        commanded_amp=raw.commanded_amp.astype(np.float64),
        sensor_id=raw.sensor_id.astype(np.int16),
        timestamp_s=raw.timestamp_s.astype(np.float64),
        x=raw.x.astype(np.int32),
        y=raw.y.astype(np.int32),
        z=raw.z.astype(np.int32),
    )
    return path


def load_raw_acceleration_samples(path: str) -> RawAccelerationSamples:
    """Read back a file written by `save_raw_acceleration_samples`."""
    with np.load(path, allow_pickle=False) as data:
        meta = json.loads(str(data["meta_json"]))
        fmt = meta.get("raw_data_format")
        if fmt != RAW_DATA_FORMAT:
            raise ValueError(
                f"{os.path.basename(path)} is not a {RAW_DATA_FORMAT} raw "
                f"acceleration file (found {fmt!r})")
        sweep_labels = data["sweep_pass_labels"]
        phase_labels = data["phase_labels"]
        return RawAccelerationSamples(
            run_id=meta.get("run_id", ""),
            experiment=meta.get("experiment", ""),
            ms2_per_count=float(meta.get("ms2_per_count", MS2_PER_COUNT)),
            window_id=data["window_id"],
            cell_id=data["cell_id"],
            trial_id=data["trial_id"],
            baseline_window_id=data["baseline_window_id"],
            sweep_pass=sweep_labels[data["sweep_pass_code"]],
            phase=phase_labels[data["phase_code"]],
            commanded_freq_hz=data["commanded_freq_hz"],
            commanded_amp=data["commanded_amp"],
            sensor_id=data["sensor_id"],
            timestamp_s=data["timestamp_s"],
            x=data["x"], y=data["y"], z=data["z"],
            meta=meta,
        )


def recompute_window_metrics(raw: RawAccelerationSamples,
                             phase: str = PHASE_VIBRATION,
                             baselines: Optional[Dict[int, float]] = None
                             ) -> List[WindowMetrics]:
    """Recompute BOTH metrics for every window of `phase` from the raw
    samples alone - the offline path that lets a saved run be re-analysed
    without touching the hardware.

    The legacy metric uses the baseline window the run linked to each
    measurement window (`baselines` overrides it per window id); when
    neither exists the window's own mean |a| is used, which is stated in
    `compute_acceleration_metrics`."""
    out: List[WindowMetrics] = []
    for window_id in raw.window_ids(phase=phase):
        info = raw.window_info(window_id)
        samples = raw.samples_for_window(window_id)
        baseline = None
        if baselines is not None and window_id in baselines:
            baseline = baselines[window_id]
        if baseline is None:
            baseline = raw.baseline_magnitude_for(window_id)
        out.append(WindowMetrics(
            window_id=window_id,
            cell_id=info["cell_id"],
            trial_id=info["trial_id"],
            sweep_pass=info["sweep_pass"],
            commanded_freq_hz=info["commanded_freq_hz"],
            commanded_amp=info["commanded_amp"],
            sensor_id=info["sensor_id"],
            metrics=compute_acceleration_metrics(samples, baseline,
                                                 raw.ms2_per_count),
        ))
    return out


# ==========================================
# Spectral analysis (shared)
# ==========================================
#
# The intensity metrics above say HOW MUCH a window vibrates; these say
# AT WHAT FREQUENCIES. They are used by the adhesion comparison (is the
# drive still a clean 224 Hz tone through this glue, or is it distorted?)
# and are written here rather than in one experiment so a second
# experiment cannot grow a second, subtly different FFT.
#
# The spectrum is computed the same way the vector RMS is: each axis is
# demeaned over the window, so gravity and the mounting pose drop out,
# and the three axes are combined in QUADRATURE
#
#     amplitude(f) = sqrt(amp_x(f)^2 + amp_y(f)^2 + amp_z(f)^2)
#
# which makes the result independent of how the sensor is oriented -
# exactly the property the demeaned vector RMS has (and, by Parseval,
# the same energy: sum_f amplitude(f)^2 / 2 ~ vector_rms^2).
#
# Amplitudes are SCALED so that a pure sinusoid of amplitude A counts
# reads A counts: the analysis window's coherent gain is divided out
# (2 * |X_k| / sum(window)). The DC and Nyquist bins are not corrected
# for the factor 2 and are meaningless here anyway - the series is
# demeaned, and everything below SPECTRUM_MIN_HZ is ignored.

#: Frequencies below this are ignored when searching for a dominant
#: frequency. Demeaning removes DC, but slow drift and rig sway still
#: leave energy in the first few bins, which would otherwise win every
#: time and report a "dominant frequency" of a couple of Hz.
SPECTRUM_MIN_HZ = 20.0

#: How far either side of a requested frequency an amplitude is read.
#: A mounted LRA does not vibrate at exactly the commanded PWM frequency,
#: and a finite window leaks energy across neighbouring bins, so "the
#: amplitude at 224 Hz" is the strongest bin within this tolerance - and
#: the frequency it was actually found at is always returned with it,
#: never silently substituted for the requested one.
SPECTRUM_TOLERANCE_HZ = 5.0

#: Harmonics (2f0, 3f0, ...) measured for the THD by default.
DEFAULT_HARMONICS = 5

#: Windows shorter than this cannot support a meaningful spectrum.
MIN_SPECTRUM_SAMPLES = 32


#: Half-width, in bins, of the main lobe summed by the energy-based
#: amplitude estimate below. A Hann window's main lobe is 4 bins wide,
#: so +/-2 bins captures it for any offset between bin centres.
_LOBE_HALF_BINS = 2


@dataclass
class VibrationSpectrum:
    """One measurement window's single-sided amplitude spectrum, in raw
    counts, combined over the three axes (see the section header).

    `freqs_hz` and `amplitude_counts` are the same length;
    `sample_rate_hz` is the rate the window was assumed to be sampled at
    (the host stream's mean rate) and `n_samples` how many samples went
    in, so the resolution is reproducible from the stored numbers.
    `enbw_bins` is the analysis window's equivalent noise bandwidth
    (1.5 for Hann), used by the energy-based amplitude estimate."""

    freqs_hz: np.ndarray
    amplitude_counts: np.ndarray
    sample_rate_hz: float
    n_samples: int
    window: str = "hann"
    enbw_bins: float = 1.5

    @property
    def resolution_hz(self) -> float:
        """Bin spacing: sample_rate / n_samples."""
        if self.n_samples <= 0:
            return float("nan")
        return float(self.sample_rate_hz) / float(self.n_samples)

    def _tone_amplitude(self, peak_idx: int) -> float:
        """Amplitude of the tone around bin `peak_idx`, from the ENERGY
        of the window's main lobe rather than the single peak bin.

        A single bin under-reads a tone that falls between bin centres by
        up to ~1.4 dB (Hann scalloping); the main-lobe energy divided by
        the window's ENBW is offset-independent - for an on-bin sine the
        corrected bins read (0.5, 1, 0.5)·A, whose sum of squares is
        exactly ENBW·A²."""
        low = max(0, peak_idx - _LOBE_HALF_BINS)
        high = min(self.amplitude_counts.size, peak_idx + _LOBE_HALF_BINS + 1)
        energy = float(np.sum(self.amplitude_counts[low:high] ** 2))
        return math.sqrt(energy / float(self.enbw_bins))

    def amplitude_at(self, freq_hz: float,
                     tolerance_hz: float = SPECTRUM_TOLERANCE_HZ
                     ) -> Tuple[Optional[float], Optional[float]]:
        """(frequency actually found, amplitude in counts) of the
        strongest tone within `tolerance_hz` of `freq_hz` - the peak bin
        locates the tone, the main-lobe energy sizes it (see
        `_tone_amplitude`).

        Returns (None, None) when the requested frequency is outside the
        spectrum - never a zero, which would read as "measured, and it
        was silent"."""
        if self.freqs_hz.size == 0:
            return None, None
        low = float(freq_hz) - float(tolerance_hz)
        high = float(freq_hz) + float(tolerance_hz)
        mask = (self.freqs_hz >= low) & (self.freqs_hz <= high)
        if not np.any(mask):
            return None, None
        idx = int(np.flatnonzero(mask)[int(np.argmax(self.amplitude_counts[mask]))])
        return float(self.freqs_hz[idx]), self._tone_amplitude(idx)

    def dominant_frequency(self, min_hz: float = SPECTRUM_MIN_HZ
                           ) -> Tuple[Optional[float], Optional[float]]:
        """(frequency, amplitude) of the strongest tone at or above
        `min_hz`, or (None, None) when the spectrum reaches no such bin.
        Sized like `amplitude_at` - main-lobe energy, not the peak bin."""
        mask = self.freqs_hz >= float(min_hz)
        if not np.any(mask):
            return None, None
        idx = int(np.flatnonzero(mask)[int(np.argmax(self.amplitude_counts[mask]))])
        return float(self.freqs_hz[idx]), self._tone_amplitude(idx)


def compute_vibration_spectrum(samples, sample_rate_hz: float,
                               window: str = "hann") -> VibrationSpectrum:
    """Single-sided, axis-combined amplitude spectrum of one window.

    `sample_rate_hz` must be supplied by the caller (the stream's mean
    rate over that window) - the host timestamps jitter per sample, so
    the samples are treated as uniformly sampled at that mean rate, which
    is the same assumption every dominant-frequency estimate in this
    project makes (see rig.collect_samples)."""
    x, y, z = as_xyz_arrays(samples)
    n = int(x.size)
    fs = float(sample_rate_hz)
    if n < MIN_SPECTRUM_SAMPLES:
        raise ValueError(f"need at least {MIN_SPECTRUM_SAMPLES} samples for a "
                         f"spectrum, got {n}")
    if not (fs > 0):
        raise ValueError("sample_rate_hz must be positive")
    if window == "hann":
        taper = np.hanning(n)
    elif window in ("none", "rect", "boxcar"):
        taper = np.ones(n)
    else:
        raise ValueError("window must be 'hann' or 'none'")
    gain = float(taper.sum())
    # ENBW in bins: N * sum(w^2) / sum(w)^2 (1.5 for Hann, 1.0 for none).
    enbw = float(n * np.sum(taper ** 2) / (gain * gain))
    power = np.zeros(n // 2 + 1, dtype=float)
    for axis in (x, y, z):
        spectrum = np.fft.rfft((axis - axis.mean()) * taper)
        power += (2.0 * np.abs(spectrum) / gain) ** 2
    return VibrationSpectrum(
        freqs_hz=np.fft.rfftfreq(n, d=1.0 / fs),
        amplitude_counts=np.sqrt(power),
        sample_rate_hz=fs,
        n_samples=n,
        window=window,
        enbw_bins=enbw,
    )


@dataclass
class HarmonicAmplitude:
    """One harmonic of a driven vibration: which multiple of the
    fundamental it is, where it was looked for, where it was found and
    how strong it was (None/None when it falls outside the spectrum -
    e.g. above Nyquist)."""
    order: int
    requested_hz: float
    found_hz: Optional[float]
    amplitude_counts: Optional[float]


def harmonic_amplitudes(spectrum: VibrationSpectrum, fundamental_hz: float,
                        n_harmonics: int = DEFAULT_HARMONICS,
                        tolerance_hz: float = SPECTRUM_TOLERANCE_HZ
                        ) -> List[HarmonicAmplitude]:
    """The fundamental (order 1) and its next `n_harmonics - 1` multiples.

    The tolerance widens with the order, because a harmonic of a slightly
    off-nominal fundamental is off by the same factor: order k is
    searched within k * tolerance_hz."""
    out: List[HarmonicAmplitude] = []
    for order in range(1, max(1, int(n_harmonics)) + 1):
        target = float(fundamental_hz) * order
        found, amplitude = spectrum.amplitude_at(target,
                                                 tolerance_hz * order)
        out.append(HarmonicAmplitude(order=order, requested_hz=target,
                                     found_hz=found,
                                     amplitude_counts=amplitude))
    return out


def total_harmonic_distortion(spectrum: VibrationSpectrum,
                              fundamental_hz: float,
                              n_harmonics: int = DEFAULT_HARMONICS,
                              tolerance_hz: float = SPECTRUM_TOLERANCE_HZ
                              ) -> Optional[float]:
    """THD as a RATIO (multiply by 100 for percent):

        THD = sqrt(sum_{k>=2} A_k^2) / A_1

    over the harmonics `harmonic_amplitudes` could measure. Returns None
    when the fundamental itself could not be measured or is zero - a THD
    referred to a fundamental that is not there is meaningless, and a 0
    would read as "perfectly clean"."""
    harmonics = harmonic_amplitudes(spectrum, fundamental_hz, n_harmonics,
                                    tolerance_hz)
    fundamental = harmonics[0].amplitude_counts
    if not fundamental:
        return None
    higher = [h.amplitude_counts for h in harmonics[1:]
              if h.amplitude_counts is not None]
    if not higher:
        return None
    return float(math.sqrt(sum(a * a for a in higher)) / float(fundamental))


# ==========================================
# Meta helpers (shared by every experiment)
# ==========================================

def metric_meta_block(selected_metric: str, has_raw_samples: bool,
                      raw_file: Optional[str],
                      ms2_per_count: float = MS2_PER_COUNT) -> dict:
    """The metric/raw-data keys every experiment's .meta.json carries."""
    return {
        "metric_version": METRIC_VERSION,
        "available_metrics": available_metrics(has_raw_samples),
        "selected_plot_metric": normalise_metric(selected_metric),
        "default_plot_metric": DEFAULT_METRIC,
        "raw_acceleration_file": raw_file,
        "raw_data_format": RAW_DATA_FORMAT if raw_file else None,
        "ms2_per_count": ms2_per_count,
        "metric_definitions": {name: spec.description
                               for name, spec in METRICS.items()},
    }


def raw_path_for(csv_path: str) -> str:
    """<anything>.csv -> <anything>.raw_acc.npz, next to the CSV. Every
    experiment names its raw file this way, so a summary CSV always
    points at its own raw data."""
    return os.path.splitext(csv_path)[0] + RAW_FILE_SUFFIX
