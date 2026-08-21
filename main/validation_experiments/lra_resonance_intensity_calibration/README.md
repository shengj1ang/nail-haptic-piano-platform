# LRA Resonance and Intensity Calibration

Two linked calibration experiments that determine the **optimal drive
configuration of the LRA** used by the haptic-piano system:

1. **Frequency sweep** (`lra_frequency_sweep.py`) — find the mounted
   resonant frequency f₀, the frequency at which the LRA vibrates the
   strongest.
2. **Amplitude sweep** (`lra_amplitude_sweep.py`) — at f₀, find the
   **recommended cue amp**: the PWM amplitude (`amp`) whose measured
   acceleration is *closest to the target "clearly perceptible, not
   annoying" intensity*. Note what this is **not** — it is not the amp
   that produces the strongest vibration (that is just the top of the
   sweep range) and not a physically optimal operating point of the
   actuator. It is the amp nearest a chosen target intensity, and the
   target is defined **per metric** (§ 2.5).

Outcome adopted by the project: **f₀ = 224 Hz** (firmware boot default
since v2.7.0) and **`amp = 64`** (default intensity in all Python
code). Every later experiment that delivers haptic cues rests on these
two constants, which is why the calibration is documented in full
below.

---

## 1. Introduction

An LRA (linear resonant actuator) is a spring–mass resonator: it only
produces strong vibration when driven with an AC signal at its resonant
frequency f₀, and its response falls off steeply within a few Hz of the
peak (high Q). Two facts make a one-off calibration necessary:

- **The datasheet f₀ is only nominal.** The resonance of the *mounted*
  actuator shifts by several Hz with the attachment method, the mass it
  drives, and temperature, so f₀ must be measured on the assembled rig.
- **Drive strength is nonlinear in the PWM duty.** With this rig's
  unipolar (single-transistor) drive on a 10 V rail, the LRA responds
  to the AC fundamental of the PWM square wave, whose amplitude is
  proportional to `sin(π·amp/255)` — it peaks at `amp = 128` and falls
  back to zero at 255. `amp` values are therefore only meaningful in
  the monotonic 0–128 range, and the mapping from `amp` to perceived
  strength has to be measured, not assumed.

The calibration answers two questions: *at what frequency should the
LRA be driven* (Experiment 1), and *at what amplitude, given that
frequency, is the cue clearly perceptible without being unpleasantly
strong* (Experiment 2). Getting both right matters for every later
user study: an off-resonance drive wastes most of the actuator's output
and confounds intensity comparisons, and an over-strong default was in
fact reported as subjectively uncomfortable during piloting.

## 2. Method

### 2.1 Apparatus

| Item | Value |
|------|------|
| Actuator | Coin LRA, wired to motor **port 11**, stuck to the desk with Blu-Tack (adhesive putty) |
| Sensor | LIS3DH accelerometer, sensor 0 (CS pin 36), coupled to the LRA through the same Blu-Tack mount; ±2 g high-resolution mode (1 count = 1 mg) |
| Drive | Teensy 4.1 PWM through the motor driver board, 10 V motor rail; `haptic-piano` firmware **v2.6.0+** (`F` frequency command, `ACC,id,x,y,z` stream) |
| Sampling | `A START 3` (3 ms interval; the LIS3DH ODR is 400 Hz) |

The rig sits untouched on the desk for the whole run — hand-holding it
adds motion noise to both the baseline and the vibration windows.

### 2.2 Experiment 1 — frequency sweep

Amplitude fixed at `amp = 128` (the maximum AC fundamental, so the
resonance peak has the best signal-to-noise ratio). Two passes:

1. **Coarse**: 100 → 350 Hz in 5 Hz steps.
2. **Fine**: coarse peak ±10 Hz in 1 Hz steps.

Each frequency step runs: set frequency (`F 0 <freq>`) → record a
0.30 s quiet baseline → motor on (`S 1 128`) → 0.15 s settling (LRA
ring-up) → 0.40 s measurement → motor off → 0.15 s rest. The baseline
is re-measured at every step so slow drift cannot bias the curve.
Total duration ≈ 1.5 min.

### 2.3 Experiment 2 — amplitude sweep

Frequency fixed at the Experiment 1 result (224 Hz); `amp` stepped
4 → 128 in steps of 4 with the same per-step protocol (~35 s total).

### 2.4 Measures

Every measurement window is scored with **both** shared
vibration-intensity metrics (defined in full in
[`../README.md`](../README.md) § 1, implemented once in
`../acceleration_metrics.py`):

| Metric | Formula (raw LIS3DH counts) | Role |
|---|---|---|
| `vector_rms` — **demeaned 3-axis vector RMS** | `√(mean((x−x̄)² + (y−ȳ)² + (z−z̄)²))`, equivalently `√(rms_x² + rms_y² + rms_z²)` | **the default.** Each axis is demeaned over the window, so gravity, the sensor's static bias and the mounting pose drop out |
| `legacy_magnitude_rms` — **legacy magnitude RMS** | `√(mean((\|a\|ᵢ − baseline_magnitude)²))` | the original formula (§ 3's historical numbers are in these units), kept for like-for-like comparison with earlier runs |

`baseline_magnitude` is the mean `|a|` of the quiet window re-measured
at every step. Both metrics are converted to absolute units **once**,
via the sensor's high-resolution sensitivity (1 count = 1 mg):
`a[m/s²] = counts × 0.00980665`; every stored field name carries a
`_counts` or `_ms2` suffix. `peak_magnitude_delta_counts` (largest
single-sample `|a|` deviation) is logged as a legacy secondary metric,
and the per-axis means and RMS values are stored too.

Which metric is **plotted** — and therefore which frequency is reported
as f₀ and which amp is the recommended cue amp — is chosen with the
**Plot metric** dropdown (default: vector RMS). It is a plot-time
choice: both metrics and the full raw three-axis samples are saved, so a
run can be re-analysed under either without re-running the hardware. The
legacy metric is only second-order sensitive to vibration perpendicular
to gravity, so on a rig whose LRA does not push along gravity the two
can give different answers; the run log prints the other metric's peak
so the difference is visible.

**The two metrics are different rulers, so each has its OWN target
band** — see § 2.5. Switching the Plot metric switches the curve, the
shaded band, the target value, the recommended cue amp, the in-band
list, the legend, the title, the meta `result` block and the GUI status
line *together*.

### 2.5 Target cue intensity — one band **per metric**

The amplitude sweep does not look for the strongest vibration; it looks
for the amp closest to a **target cue intensity**. That target is a
number in m/s², so it only means anything alongside the metric that
measures it — and **the two metrics are different rulers**. On this rig
the same drive reads ≈ 0.5 m/s² under the legacy magnitude RMS and
≈ 2.7 m/s² under the demeaned vector RMS, because the legacy formula is
only second-order sensitive to vibration perpendicular to gravity while
the vector RMS captures the full AC energy.

> **This used to be a bug.** Both metrics shared the legacy
> 0.4–0.6 m/s² band, so switching the Plot metric moved the recommended
> amp from ≈ 52 to ≈ 4. That looked like a change in the actuator's
> operating point but was only a change of ruler. Each metric now has
> its own band and both select amp ≈ 52 on the same data.

| Metric | Target band | Aim point | Calibration |
|---|---|---|---|
| Legacy magnitude RMS | **0.4–0.6 m/s²** | 0.5 m/s² | `historical` — the perception-based band the project adopted `amp = 64` from |
| Demeaned 3-axis vector RMS | **2.4–3.0 m/s²** | 2.7 m/s² | `provisional` — estimated from the vector RMS this rig measured at amp ≈ 48–56 (2.55–2.94 m/s²) |

The vector band is **provisional**: it is a rescaling of the historical
band onto this metric's scale — the same physical drive range, measured
with the better ruler — *not* an independent perceptual calibration.
Expect it to be re-measured; every run's meta records the status so a
saved result always states how firm its target was.

All of these live in **one place**, `CUE_TARGETS` /
`cue_target(metric)` in `lra_amplitude_sweep.py`. Nothing else — not the
plot, not the recommendation, not the GUI — may hard-code a band.

**Uncalibrated metrics.** `cue_target()` returns an *uncalibrated*
target for any metric with no entry. Such a metric gets its curve
plotted and nothing else: the plot and the meta say
`Target band not calibrated`, no cue amp is recommended and the in-band
list is empty. Another metric's thresholds are never substituted.

#### Where the numbers come from

No standard prescribes a haptic-cue intensity, so the target band is
perception-based, with the relevant standards used to scope the
problem:

- **ISO 2631-1 (whole-body vibration)** is *not applicable*: its
  frequency weightings stop at 80 Hz, below the 224 Hz drive. Its
  comfort descriptor scale (< 0.315 m/s² "not uncomfortable") is used
  only as a conservative order-of-magnitude cross-check.
- **ISO 5349-1 (hand-transmitted vibration, 8–1000 Hz)** *covers* the
  frequency range but regulates occupational *exposure*: the daily
  action value is A(8) = 2.5 m/s² (Wh-weighted, 8 h). Short haptic
  cues at < 1 m/s² unweighted are orders of magnitude below it —
  safety is therefore not the binding constraint, comfort and
  detectability are.
- **Psychophysics** sets the actual target: fingertip vibrotactile
  detection thresholds near 200–250 Hz (the Pacinian corpuscle
  sensitivity peak) are ~0.1–0.4 m/s² RMS, and a clearly perceptible
  but non-annoying cue is conventionally placed just above threshold.

Target band adopted for the **legacy magnitude RMS**:
**0.4–0.6 m/s² RMS**, aiming at its centre (0.5 m/s²). The vector-RMS
band above is that same drive range re-expressed on the vector scale.

### 2.6 Running the experiments

```bash
python lra_frequency_sweep.py    # ~1.5 min, prints f0
python lra_amplitude_sweep.py    # ~35 s, prints the recommended cue amp
```

Alternatively, open them from `launcher.py`, section "9. Validation
Experiments" - the GUI windows (`app/gui/validation_experiment_window.py`)
are thin Start/Stop + progress-bar + log wrappers around the same
`run_experiment()` functions and write identical output files. They also
preview the latest saved response curve from `output/` on open, can
re-render the chart from any saved sweep CSV ("Load Chart from CSV...",
rendered to a temp file so `output/` is never modified), and expose the
motor-port / ACC-sensor-id choice (defaults 0/0, matching the wiring
described above).

Both scripts auto-detect the rig (USB VID:PID `16C0:0483`, confirmed by
the `E` → `E haptic-piano ...` handshake), and on exit — including
Ctrl+C — stop all motors, restore the boot-default frequency and stop
the accelerometer stream. Outputs are timestamped into
`main/data/validation_experiments/lra_resonance_intensity_calibration/`
(referred to as `<data>/` below):

| File | Content |
|------|------|
| `sweep_<ts>.csv` / `frequency_response_<ts>.png` | Experiment 1 data and plot |
| `amp_sweep_<ts>.csv` / `amplitude_response_<ts>.png` | Experiment 2 data and plot |
| `sweep_<ts>.raw_acc.npz` / `amp_sweep_<ts>.raw_acc.npz` | **the raw three-axis samples of every baseline and measurement window** — lossless `int32` counts with Unix-epoch timestamps, keyed by step/pass/commanded frequency/amp. Both metrics and both plots can be regenerated from this file alone (format: [`../README.md`](../README.md) § 4) |
| `sweep_<ts>.meta.json` / `amp_sweep_<ts>.meta.json` | Per-run record: full parameter set (motor/sensor/amp or freq + timing constants), firmware identity, linked csv/png/raw filenames, the `metrics` block (`metric_version`, `available_metrics`, `selected_plot_metric`, `raw_acceleration_file`, `raw_data_format`, `ms2_per_count`) and the headline result **for the selected metric** |

Both CSVs carry the same shared metric block: `n_samples`,
`mean_x_counts`/`mean_y_counts`/`mean_z_counts`,
`rms_x_counts`/`rms_y_counts`/`rms_z_counts`,
`legacy_magnitude_rms_counts`/`_ms2`, `vector_rms_counts`/`_ms2`,
`baseline_magnitude_counts`, `peak_magnitude_delta_counts`.

`<ts>` is the Unix epoch timestamp in seconds at save time (the
project-wide timestamp convention), so runs never overwrite each other
and sort chronologically by filename. "Load Chart from CSV" in the GUI
reads the sibling meta to re-render an old run with the parameters it
was actually measured at.

**Old CSVs.** Runs written before this refactor carry only the
pre-refactor columns (`rms_delta` / `rms_delta_counts` / `rms_ms2`,
`peak_delta`, `baseline_mag`) and no raw file. They still load and plot,
under the **legacy** metric, whose old column names are recognised
automatically; the vector-RMS option is disabled for them, since it
cannot be derived from a stored scalar magnitude RMS.

## 3. Results (runs of 2026-07-15)

> **Note.** The numbers in this section were measured with the **legacy
> magnitude RMS** (the only metric that existed then) and their data
> files have since been cleared for re-measurement under the two-metric
> pipeline. They are kept as the record of how f₀ = 224 Hz and amp = 64
> were arrived at. When the calibration is re-run, compare new *legacy*
> values against these, and use the vector RMS for the new headline.

### 3.1 Experiment 1 — resonant frequency

Data `<data>/sweep_1784125356.csv`, plot
`<data>/frequency_response_1784125356.png` (both cleared).

- **f₀ = 224 Hz** (fine-pass peak: legacy magnitude RMS = 111.1 counts,
  peak magnitude delta = 298 counts).
- The resonance region spans ~221–231 Hz (legacy RMS ≈ 103–111 counts),
  with the sharp high-side cliff typical of a high-Q resonator
  (231 → 232 Hz drops from 105 to 67).
- At the previous 300 Hz default the same drive produced ≈ 40 counts:
  retuning to 224 Hz yields **~2.8× more vibration** from the same
  voltage.

### 3.2 Experiment 2 — amplitude at 224 Hz

Data `<data>/amp_sweep_1784126365.csv`, plot
`<data>/amplitude_response_1784126365.png` (both cleared).

- **Recommended cue `amp = 64`** → 0.49 m/s² legacy RMS, the centre of
  the legacy target band — i.e. the amp closest to the target
  *intensity*, not the amp that vibrates hardest. In-band alternatives:
  `amp = 60` (0.40) and `amp = 68` (0.57).
- The interim default `amp = 75` measures ≈ 0.65 m/s² — above the
  band, consistent with pilot feedback that it felt too strong.
- Below `amp ≈ 56` readings flatten at ~0.28–0.38 m/s²: this is the
  rig's measurement noise floor (baseline |a| jitter), not real
  vibration; absolute values below ~0.4 m/s² are not trustworthy.
- From `amp ≈ 60` upward the curve tracks the `sin(π·amp/255)` model
  well; above `amp ≈ 88` it plateaus at 0.72–0.84 m/s² (LRA stroke
  saturation) rather than following the model to its 128 peak.

## 4. Discussion

### 4.1 Adopted configuration

| Setting | Value | Where it lives |
|------|------|------|
| LRA drive frequency | **224 Hz** | `config.json` → `haptic.lra.default_frequency` (also the firmware boot default `DEFAULT_PWM_FREQ` since v2.7.0); overridden at runtime with `F <port> <freq>` |
| Default cue intensity | **`amp = 64`** (0.49 m/s² RMS at 224 Hz) | `config.json` → `haptic.lra.default_amp` |

Both values measured here are **the project defaults stored in
`config.json`'s `haptic` block** and read through
`common/haptic_config.py` — the quiz cue, the bench windows, Test Buzz
and the experiment controls all take them from there, so adopting a
newly measured f₀ or cue amp means editing them once in the launcher's
Initial Setup → Haptic Actuator Defaults (see main/README.md, "Haptic
actuator configuration"). The 224 Hz **firmware boot default** is a
separate constant: the sweeps restore it on the port when they finish,
so it tracks the firmware, not the config.

### 4.2 Interpretation

- The measured amplitude curve validates the `sin(π·amp/255)` drive
  model in the usable region and reveals stroke saturation above
  `amp ≈ 88` — driving harder than ~90 gains nothing, which bounds the
  useful intensity range to roughly 60–90.
- The division of labour confirmed by these data: **frequency is a
  calibration constant** (set once to f₀, never used as an intensity
  knob — the resonance is only ~10 Hz wide and detuning changes the
  perceived pitch as well as the strength), while **intensity is
  controlled by `amp`** within the monotonic, unsaturated 0–90 region.

### 4.3 Limitations

- f₀ is **mount-specific**: 224 Hz holds for this Blu-Tack desk mount.
  Re-run Experiment 1 (and ideally Experiment 2) whenever the LRA is
  remounted, e.g. onto the finger rig — expect a shift of a few Hz.
- Absolute intensities below ~0.4 m/s² are floor-limited on this rig;
  a stiffer mount or averaging longer windows would be needed to
  resolve them.
- Single actuator, single session: unit-to-unit spread and temperature
  drift are not characterised (both are typically a few Hz for coin
  LRAs — within the measured 221–231 Hz plateau).
- The 0.4–0.6 m/s² legacy band (and the 2.4–3.0 m/s² vector band
  rescaled from it) is a design target from perception literature,
  not a standard's requirement; per-user preference can still be
  exposed as a setting (60/64/68 are all in-band).

### 4.4 Troubleshooting a re-run

- A flat frequency curve with no peak usually means the accelerometer
  is not mechanically coupled to the LRA (check the Blu-Tack) or the
  motor is not on port 11 (`MOTOR_INDEX`).
- A peak at the edge of the sweep range: widen `COARSE_START_HZ` /
  `COARSE_STOP_HZ`.
- `amp = 128` on the 10 V rail is a deliberate short-burst overdrive
  (0.4 s bursts are fine; haptic driver ICs overdrive on purpose for
  fast ring-up) — avoid holding the LRA at high `amp` continuously.

## Appendix: configuration constants

All knobs are constants at the top of each script:

| Constant | Default | Meaning |
|------|------|------|
| `MOTOR_INDEX` | 11 | motor port driving the LRA |
| `AMP` (Exp 1) | 128 | sweep drive amplitude (max AC fundamental) |
| `FREQ_HZ` (Exp 2) | 224 | fixed drive frequency for the amplitude sweep |
| `AMP_VALUES` (Exp 2) | 4–128 step 4 | amplitude steps |
| `CUE_TARGETS` (Exp 2) | legacy 0.4–0.6 / 0.5 (historical); vector 2.4–3.0 / 2.7 (provisional) | the target cue band and aim point **per metric** — the single definition, read via `cue_target(metric)` |
| `COARSE_START_HZ` / `COARSE_STOP_HZ` / `COARSE_STEP_HZ` (Exp 1) | 100 / 350 / 5 | coarse pass range and step |
| `FINE_SPAN_HZ` / `FINE_STEP_HZ` (Exp 1) | 10 / 1 | fine pass around the coarse peak |
| `BASELINE_S` / `SETTLE_S` / `MEASURE_S` / `REST_S` | 0.30 / 0.15 / 0.40 / 0.15 | per-step timing |
| `ACC_INTERVAL_MS` | 3 | stream interval (LIS3DH ODR is 400 Hz, keep ≥ 3) |

`MS2_PER_COUNT` (0.00980665 — LIS3DH HR ±2 g, 1 count = 1 mg), the two
metric formulas and the raw-sample format now live in
`../acceleration_metrics.py`, shared with every other accelerometer
experiment; see [`../README.md`](../README.md).
