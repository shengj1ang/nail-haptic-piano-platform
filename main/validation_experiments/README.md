# Validation Experiments — shared layer

Small hardware-validation / calibration experiments that inform the main
user study's design constants but are not part of the main protocol
(launcher section **9. Validation Experiments**). One subfolder per
experiment, each with its own full write-up:

| Folder | Experiment |
|---|---|
| `lra_resonance_intensity_calibration/` | LRA frequency sweep (resonance) + amplitude sweep (cue intensity) |
| `actuator_spectrogram/` | 2-D drive-frequency × amp vibration-intensity map (ERM/LRA) |
| `motor_acc_delay_experiment/` | command → vibration onset latency **and** settling time (LRA/ERM) |

Two modules at this level are shared by all of them:

| Module | Contents |
|---|---|
| `rig.py` | serial-rig helpers: Teensy port detection, the `E` identity handshake, `send()`, the `ACC,<id>,x,y,z` parser, and `collect_samples()` — the one accelerometer collector |
| `acceleration_metrics.py` | the vibration-intensity metrics, the counts → m/s² conversion, the metric naming/registry, and the lossless raw three-axis sample store |

This file documents `acceleration_metrics.py`, because every
accelerometer experiment now computes, stores and plots its intensities
through it. Read it once here rather than four times in the experiment
READMEs.

---

## 1. The two intensity metrics

Every measurement window is scored with **both** metrics. Which one a
chart is drawn with is a **plot-time** choice, not a measurement-time
one — the run stores both numbers *and* the raw samples they came from.

### 1.1 Legacy magnitude RMS — `legacy_magnitude_rms`

The project's original formula, unchanged, kept so historical results
stay directly comparable:

```
magnitude_i = sqrt(x_i² + y_i² + z_i²)
legacy_magnitude_rms = sqrt( mean( (magnitude_i − baseline_magnitude)² ) )
```

`baseline_magnitude` is the mean `|a|` of a separate quiet (motor-off)
window, re-measured during the run — it is essentially gravity
(≈ 1000 counts at ±2 g, 1 count = 1 mg).

**Known weakness.** `|a|` is only *second-order* sensitive to vibration
perpendicular to gravity: `sqrt(g² + v²) ≈ g + v²/2g`, so a 40-count
horizontal vibration moves the magnitude by well under a count while the
same vibration along gravity moves it by 40. The metric therefore mixes
the rig's *mounting orientation* into every intensity it reports. (This
is the same blind spot the motor→ACC delay experiment documented years
ago as the reason its detector never used `|a|`.)

### 1.2 Demeaned three-axis vector RMS — `vector_rms` (recommended, default)

Computed per window, with no external baseline:

```
mean_x = mean(x)   mean_y = mean(y)   mean_z = mean(z)

vector_rms = sqrt( mean( (x_i − mean_x)² + (y_i − mean_y)² + (z_i − mean_z)² ) )
```

Equivalently, from the per-axis RMS values:

```
rms_x = sqrt(mean((x_i − mean_x)²))      (likewise rms_y, rms_z)
vector_rms = sqrt(rms_x² + rms_y² + rms_z²)
```

Subtracting each axis's own mean removes **gravity, the sensor's static
bias and the mounting pose** in one step, so the number is the AC energy
of the vibration in any direction. It is the recommended metric and the
**default** for new runs and new plots.

### 1.3 Choosing between them

| | Legacy magnitude RMS | Demeaned 3-axis vector RMS |
|---|---|---|
| needs a separate baseline window | **yes** | no |
| rejects gravity / static bias | partly (via the baseline) | **fully** |
| sensitive to vibration ⟂ gravity | **no** (second order) | **yes** |
| unchanged if the rig is remounted in another pose | no | **yes** |
| available for pre-2026-07 runs | **yes** | no (needs per-axis samples) |
| default for new runs | no | **yes** |

Use the **vector RMS** unless you are explicitly reproducing or
comparing against a historical number, in which case use the **legacy**
metric on both sides of the comparison.

The two can rank cells differently — that is the point, not a bug. The
run log prints where the *other* metric peaks, so a divergence is
visible without re-plotting.

### 1.4 Units and naming

```
value_ms2 = value_counts × ms2_per_count      (ms2_per_count = 0.00980665)
```

Conversion happens exactly once, in
`acceleration_metrics.counts_to_ms2()`. **Every stored field name ends
in `_counts` or `_ms2`**; the bare name `rms` is not used anywhere any
more. Chart axis and colour-bar labels are generated from the metric
name, so a saved figure always says which metric it shows:

```
Legacy magnitude RMS acceleration (m/s²)
Demeaned 3-axis vector RMS acceleration (m/s²)
```

---

## 2. Public interface

```python
from validation_experiments.acceleration_metrics import (
    compute_legacy_magnitude_rms,   # (samples, baseline_magnitude) -> counts
    compute_demeaned_vector_rms,    # (samples) -> counts
    compute_axis_statistics,        # (samples) -> per-axis means + RMS
    compute_acceleration_metrics,   # (samples, baseline) -> both metrics + axes
    compute_baseline_magnitude,     # (quiet samples) -> mean |a|
    counts_to_ms2,                  # the single unit conversion
    get_metric_value,               # (result_row, metric_name, unit) -> float|None
    save_raw_acceleration_samples,  # write the raw NPZ
    load_raw_acceleration_samples,  # read it back
    recompute_window_metrics,       # raw NPZ -> both metrics per window
    RawSampleRecorder,              # what the experiments record into
)
```

`samples` is a sequence of `(x, y, z)` or `(t, x, y, z)` tuples (the form
`rig.collect_samples()` returns) or an `(N, 3)` / `(N, 4)` array — all
accepted interchangeably.

`get_metric_value()` returns **`None`**, never a guess, when a row does
not carry the requested metric. It reads the pre-refactor column names
(`rms_delta`, `rms_delta_counts`, `rms_ms2`, `baseline_mag`, …) for the
legacy metric only: **a stored scalar magnitude RMS cannot be turned
back into a vector RMS**, and nothing in the codebase pretends
otherwise.

Unit tests: `test-script/test_acceleration_metrics.py` (run from `main/`
with `python test-script/test_acceleration_metrics.py`).

---

## 3. Summary-CSV columns

Each experiment's summary CSV keeps its own identity columns
(`freq_hz`/`amp`, `frequency_hz`/`sweep_pass`, `trial_id`/`status`/…)
and then carries this **identical block**, in this order:

| Column | Meaning |
|---|---|
| `n_samples` | samples in the measurement window |
| `mean_x_counts`, `mean_y_counts`, `mean_z_counts` | per-axis means (the static component the vector RMS removes) |
| `rms_x_counts`, `rms_y_counts`, `rms_z_counts` | per-axis demeaned RMS |
| `legacy_magnitude_rms_counts` | metric 1, raw counts |
| `legacy_magnitude_rms_ms2` | metric 1, m/s² |
| `vector_rms_counts` | metric 2, raw counts |
| `vector_rms_ms2` | metric 2, m/s² |
| `baseline_magnitude_counts` | quiet-window mean `|a|` metric 1 subtracted |
| `peak_magnitude_delta_counts` | largest single-sample `|a|` deviation (legacy secondary metric) |

`vector_rms_counts² = rms_x_counts² + rms_y_counts² + rms_z_counts²`, so
the axis breakdown and the headline metric are consistent by
construction.

---

## 4. Raw three-axis data

Every run writes `<summary_csv_stem>.raw_acc.npz` next to its CSV — a
**compressed NPZ in long format, one row per accelerometer sample**:

| Array | Meaning |
|---|---|
| `window_id` | unique id of the measurement window |
| `cell_id` | sweep cell / trial the window belongs to (−1 = none) |
| `trial_id` | repetition within the cell (−1 when unused) |
| `baseline_window_id` | which baseline window this window's legacy baseline came from (−1 = none) |
| `sweep_pass` | `""`, `"coarse"`, `"fine"` (stored as codes + a label table) |
| `phase` | `"baseline"` or `"vibration"` (same encoding) |
| `commanded_freq_hz`, `commanded_amp` | what the rig was told to do |
| `sensor_id` | LIS3DH sensor id in the `ACC` stream |
| `timestamp_s` | **Unix epoch seconds** (host arrival time) |
| `x`, `y`, `z` | raw LIS3DH counts, `int32` — lossless |
| `meta_json` | run id, experiment, `ms2_per_count`, `metric_version`, column list, plus the experiment's own parameters |

Baseline windows are stored too, and each vibration window records the
baseline window it used, so the **legacy metric can be reproduced with
the very baseline the live run subtracted** — not an approximation of it.

`recompute_window_metrics(raw)` turns the file straight back into both
metrics per window. On a real run this reproduces the summary CSV
exactly (verified to 0 counts of difference in the end-to-end check).

**Size.** Roughly 30 kB per 5 000 samples. A Coarse spectrogram run is a
few MB; a Fine, long-Vibrate-time 2-D sweep can reach tens of MB. The
sample count and file are printed at the end of every run.

---

## 5. Metadata block

Each run's `.meta.json` carries, alongside its own `parameters`:

```jsonc
"metrics": {
  "metric_version": 2,
  "available_metrics": ["vector_rms", "legacy_magnitude_rms"],
  "selected_plot_metric": "vector_rms",
  "default_plot_metric": "vector_rms",
  "raw_acceleration_file": "spectrogram_<ts>.raw_acc.npz",
  "raw_data_format": "acceleration_raw_npz_v1",
  "ms2_per_count": 0.00980665,
  "metric_definitions": { "...": "the formulas, in words" }
},
"files": { "...": "...", "raw_acceleration": "spectrogram_<ts>.raw_acc.npz" },
"result": { "metric": "vector_rms", "...": "always stated for the selected metric" }
```

`metric_version` is `1` for pre-refactor runs (single `rms_delta*`
column, no raw file) and `2` from this refactor onward.

---

## 5.5 Reading the chart full size

The chart inside each experiment window is a **thumbnail** — a
four-panel latency summary or a spectrogram is unreadable at that size.
**Click the chart** (or press **Enlarge Chart**) to open it at its own
resolution in a separate viewer:

| Control | Action |
|---|---|
| **Fit to window** / `Ctrl+0` | scale to the window (never past 100 %) |
| **100 %** / `Ctrl+1` | the PNG's own resolution |
| **+** / **−**, `Ctrl+±`, `Ctrl+scroll` | zoom; scrollbars pan once the chart is larger than the window |
| `Esc` | close |

**One viewer is shared by all four experiment windows** — clicking a
chart in another window swaps the image rather than opening a second
viewer. It tracks the panel it was opened from, so finishing a run or
switching the Plot metric refreshes it in place (keeping your zoom), and
it closes automatically with the last validation window.

---

## 6. Picking the metric in the GUI

Launcher → **9. Validation Experiments** → any of the three intensity
windows has a **Plot metric** dropdown:

* **Demeaned 3-axis vector RMS (Recommended)** — the default
* **Legacy magnitude RMS**

Both metrics are measured every run, so switching the dropdown re-plots
the displayed run immediately — **no new hardware run**. The choice
drives the chart, the peak / resonance / recommended cue amp, the
in-cell annotations, the colour-bar or y-axis label, the run log's
strongest/peak lines, the `result` block of the meta and the GUI status
line.

**Metric-specific thresholds switch with it.** The two metrics are
different rulers — the legacy formula is only second-order sensitive to
vibration perpendicular to gravity, so on the same drive it reads
≈ 0.5 m/s² where the vector RMS reads ≈ 2.7 m/s². Any absolute
threshold therefore belongs to *one* metric. The amplitude sweep's
target cue band is defined per metric in `CUE_TARGETS` /
`cue_target(metric)` (see
[`lra_resonance_intensity_calibration/README.md`](lra_resonance_intensity_calibration/README.md)
§ 2.5); a metric with no calibrated band is reported as
`Target band not calibrated` and gets no recommendation rather than
borrowing the other's numbers.

**Old runs.** A run saved before raw three-axis samples were kept offers
the legacy metric only; the vector option is **disabled** with a
tooltip and the status line explains why, rather than the value being
faked. Starting a new run re-arms the dropdown with your own choice
(default: vector RMS), so previewing an old run never silently changes
what the next run measures.

The **Motor → ACC Delay** window has no such dropdown: its figure is a
latency plot, and its onset detector is deliberately untouched — see
§ 7.

---

## 7. What was NOT unified, and why

The motor→ACC delay experiment shares the raw-sample store, the shared
computation helpers and both offline intensity metrics per trial, but
its **onset/settling detector keeps its own statistic**: the per-sample
per-axis deviation from the per-trial baseline means, feeding a CUSUM
change-point test for the onset and a short centred moving RMS of the
same quantity — the vibration envelope — for the settling time.

The reason is that the two answer different questions. An RMS is an
average over a whole window and cannot *date* an event; onset detection
needs a per-sample quantity that responds within one sample. The three
are consistent by construction — the demeaned vector RMS is exactly the
RMS of that per-axis deviation over a window, i.e. the detector is the
per-sample form, the settling envelope the sliding-window form and the
metric the whole-window form — so nothing is gained by swapping one in,
while every historical latency number would change.

The per-trial intensity numbers that experiment records are **offline
only** and are never fed back into detection. They span the whole drive
window — quiet pre-onset samples, ring-up and steady state — so they
are *not* steady-state intensities comparable with a sweep cell; that
experiment's `steady_state_envelope_counts` is the settled level, and
the meta says so in its `drive_window_intensity` block.
