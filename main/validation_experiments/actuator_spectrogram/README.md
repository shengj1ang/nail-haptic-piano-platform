# Actuator Spectrogram (ERM / LRA)

A **2-D drive-frequency × amp vibration-intensity map** for either
actuator on the rig. It is a nested parameter sweep — for every PWM drive
frequency and every amp it drives the motor at that combination and fills
a grid box with the vibration intensity it measures there:

```
for freq in freq_min .. freq_max:   # PWM drive frequency (F command)
    for amp in amp_min .. amp_max:   # PWM duty
        drive at (freq, amp) for the Vibrate time
        measure the accelerometer RMS intensity
        grid[freq][amp] = that intensity
```

| axis | meaning |
|------|---------|
| **x** | amp (PWM duty) |
| **y** | **drive frequency** — the value set with the `F` command |
| **colour** | measured **RMS acceleration** at that (amp, frequency) cell — **darker = stronger** |

Each box is simply the broadband RMS intensity the drive produced, in
m/s². There is no frequency analysis of the signal: the y-axis is the
frequency the motor is *driven* at, and each box is one scalar intensity
measurement. Cells are drawn as discrete filled boxes, so the picture
reads as the grid of measured intensities the boxes were "filled" with.

## Which intensity metric?

Every cell is measured with **both** shared vibration-intensity metrics
(the same ones the LRA sweeps use — full definitions in
[`../README.md`](../README.md)):

- **Demeaned 3-axis vector RMS** — `√(mean(Σₐₓᵢₛ(a−mean_axis)²))`, the
  **default**: gravity, sensor bias and mounting pose all drop out.
- **Legacy magnitude RMS** — `√(mean((|a| − baseline)²))`, the original
  project formula, kept for comparison with historical runs.

The **Plot metric** dropdown chooses which one colours the map — and
therefore which cell is reported as the strongest. Because the run also
saves every cell's full three-axis sample series, switching the dropdown
re-colours a saved run instantly, with no new hardware sweep. The two
metrics can legitimately peak at *different* cells (the legacy metric
barely sees vibration perpendicular to gravity); the run log prints
where the other metric peaks so a divergence is never silent.

You pick:

- **Motor port** (0–11) — which port the actuator under test is wired to.
- **Actuator type** — **ERM** or **LRA**. Selecting it **seeds the amp and
  frequency ranges with that type's defaults** (ERM freq 0–1000 Hz, LRA
  freq 0–350 Hz; both amp 0–255) and points the port at its usual wiring
  (ERM → 10, LRA → 11).
- **Amp range** and **Frequency range** — adjustable min/max for each
  axis, seeded from the type. (The sweep can't drive below 50 Hz, the
  firmware `F`-command minimum, so the frequency axis starts there even if
  you set a lower min.)
- **Scan precision** — Coarse / Medium / Fine — steps **both** axes
  (freq step 100 / 50 / 25 Hz, amp step 32 / 16 / 8).
- **Vibrate time** — how long (seconds) each (freq, amp) cell is driven
  continuously before its intensity is measured. Default **2 s**, range
  0.5–30 s.
- **Plot metric** — **Demeaned 3-axis vector RMS (Recommended)** or
  **Legacy magnitude RMS**. Both are always measured; this chooses the
  one that colours the map, defines the reported peak, feeds the cell
  annotations and names the colour bar. Changing it re-renders the
  displayed run (and updates that run's PNG and meta `result`) without
  re-running the hardware.
- **Annotate** — print each cell's value inside its box, as either the
  selected metric's raw **RMS value** (m/s²) or a **Normalized** 0–1
  value (the cell's position between the map's min and max). The text is
  white or black, chosen per
  cell from that cell's colour luminance so it always contrasts (readable
  on both dark and light cells); edge cells are aligned inward so nothing
  clips off the plot. Best with Coarse precision — a dense grid gets
  crowded. The choice is saved in the run's meta, so "Load Chart from CSV"
  reproduces it.

## Run length (it is a full 2-D sweep)

Cell count = frequencies × amps, so the run is long and finer precision
multiplies it fast. At the default 2 s Vibrate time:

| precision | ERM (0–1000 Hz) | LRA (0–350 Hz) |
|-----------|-----------------|----------------|
| Coarse | ~220 s | ~90 s |
| Medium | ~15 min | ~6 min |
| Fine | ~50 min+ | ~15 min |

The exact estimate is printed when you Start. Use **Coarse** precision and
a short Vibrate time for a quick look; go finer only for the region you
care about.

## Interpretation differs by type

- **LRA** — the drive frequency *is* its vibration frequency, so the map
  is its **resonance × amplitude response surface**: a bright horizontal
  band around its ~224 Hz resonance that darkens with amp.
- **ERM** — the swept frequency is the **PWM carrier** (the ERM vibrates
  at its rotor speed, not the carrier). An ERM needs a kHz-range carrier
  to act as smooth DC, so it is weak across most of the sub-kHz axis and
  only firms up toward the top — the map shows how carrier × amp affects
  intensity.

## Running it

Launcher → **9. Validation Experiments** → **Actuator Spectrogram
(ERM/LRA)**: the standard Start/Stop + progress bar + log + plot-preview
shell, with the motor-port / type / precision / Vibrate pickers, a Test
Buzz, and an "Open Accelerometer Live View" button. Selecting a type also
points the port at that type's usual wiring (ERM → 10, LRA → 11); override
if needed.

**Test Buzz uses the LRA's best config (224 Hz, amp 64) for *both* actuator
types** — a single, consistent known-good wiring check, not the swept
drive. (An ERM will not spin at 224 Hz; the buzz is a wiring/where-is-it
check.)

Or standalone:

```bash
python actuator_spectrogram.py    # ERM, Coarse precision, 2 s, ~220 s
```

The script auto-detects the rig and, on exit — including Ctrl+C — stops
all motors, restores the boot-default PWM frequency and stops the ACC
stream.

## Physical setup

The accelerometer must be **glued/taped to the motor under test** so the
two move as one unit, and the pair fixed to a rigid desk. A loose sensor
or free-floating rig invalidates every measurement.

## Outputs

Timestamped (Unix epoch seconds) into
`main/data/validation_experiments/actuator_spectrogram/`, the same
convention as every other validation experiment:

| File | Content |
|------|---------|
| `spectrogram_<ts>.raw_acc.npz` | **the raw three-axis samples of every cell** (and of every row baseline), long format, lossless `int32` counts with Unix-epoch timestamps — the file both metrics and every chart can be regenerated from. Format documented in [`../README.md`](../README.md) § 4 |
| `spectrogram_<ts>.npz` | the intensity grid for re-rendering: `freqs` (n_freq,), `amps` (n_amp,), `intensity_vector_rms_ms2` and `intensity_legacy_magnitude_rms_ms2` (each n_freq × n_amp, NaN where a cell was dropped), plus `intensity` = the legacy grid, which is exactly what that key meant in pre-refactor files |
| `spectrogram_<ts>.png` | the amp × drive-frequency intensity map (darker = stronger); the colour bar names the metric it shows |
| `spectrogram_<ts>.csv` | one row per (freq, amp) cell: `freq_hz`, `amp`, then the shared metric block (`n_samples`, `mean_*_counts`, `rms_*_counts`, `legacy_magnitude_rms_counts/_ms2`, `vector_rms_counts/_ms2`, `baseline_magnitude_counts`, `peak_magnitude_delta_counts`) |
| `spectrogram_<ts>.meta.json` | full parameter set (motor type, amp/frequency ranges, precision, freq/amp steps, Vibrate time, annotate mode, timing), firmware identity, linked files, the `metrics` block (`metric_version`, `available_metrics`, `selected_plot_metric`, `raw_acceleration_file`, `raw_data_format`, `ms2_per_count`), and the run result **for the selected metric** |

"Load Chart from CSV" re-renders the map from any saved run — the heatmap
is rebuilt from the run's CSV (which carries both metrics), with labels
from its `.meta.json`.

### Old runs

A `spectrogram_*.csv` written before this refactor has only the single
`rms_delta_counts` / `rms_ms2` columns and no raw file. Such a run still
loads and plots — under the **legacy** metric, whose old column names are
read automatically. The vector-RMS option is **disabled** for it, because
a stored scalar magnitude RMS genuinely cannot be turned back into a
vector RMS; the window says so instead of inventing a number. Its
sibling `.npz` (which contains only `intensity`) likewise still loads as
the legacy grid.

## Configuration constants

At the top of `actuator_spectrogram.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `MOTOR_INDEX` | 10 | default motor port (ERM channel; LRA is 11) |
| `TYPE_CONFIG` | ERM amp 0–255 / freq 0–1000 · LRA amp 0–255 / freq 0–350 | per-type **default** amp & frequency ranges (all adjustable per run) |
| `AMP_MIN` / `AMP_MAX` | 0 / 255 | absolute bounds the amp-range controls allow |
| `FREQ_DRIVE_MIN` / `FREQ_MAX_LIMIT` | 50 / 20000 | firmware `F`-command min/max (Hz); the sweep starts at the min |
| `PRECISION_STEPS` | Coarse/Medium/Fine → freq 100/50/25 Hz, amp 32/16/8 | scan precision → (freq step, amp step) |
| `MEASURE_S` (Vibrate) | 2.00 | default per-cell continuous drive/measure window; set per run (`MEASURE_S_MIN`/`MEASURE_S_MAX` = 0.5–30 s) |
| `ANNOTATE_MODES` | off / rms / normalized | print each cell's value: none, the selected metric's raw m/s², or 0–1 normalised |
| `COLORMAP` | `magma_r` | pale (low) → near-black (high): darker = stronger |
| `BASELINE_S` / `SETTLE_S` / `REST_S` | 0.30 / 0.30 / 0.15 | other per-step timing (baseline re-measured once per frequency row) |

`MS2_PER_COUNT` (0.00980665 — LIS3DH HR ±2 g, 1 count = 1 mg) and the
metric names/formulas live in `../acceleration_metrics.py`, shared with
every other accelerometer experiment.
