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
