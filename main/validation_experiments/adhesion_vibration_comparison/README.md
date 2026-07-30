# Adhesion Vibration Comparison (LRA)

How much of the LRA's vibration reaches the accelerometer depends on
what holds the two together. This experiment measures that for the three
mounting adhesives the project uses, and compares them:

1. **Blu Tack**
2. **Double-sided tape**
3. **Cosmetic adhesive** — the cosmetic eyelash adhesive, single-use

Launcher → **9. Validation Experiments → Adhesion Vibration Comparison
(LRA)**, or standalone from `main/`:

```
python validation_experiments/adhesion_vibration_comparison/adhesion_vibration.py --method "Blu Tack" --trials 5 --notes "thin layer"
python validation_experiments/adhesion_vibration_comparison/adhesion_comparison.py     # newest run of each method
```

---

## 1. One run = one adhesive

A run measures the adhesive that is physically mounted **right now** —
the GUI's *Adhesion method* selector offers exactly the three methods
above and nothing else. Comparing methods is a separate, offline step
(**Analyse adhesion methods**) over three saved runs, because the three
measurements are separated by a physical re-mount and must be recorded
as three independent runs.

### The drive is fixed, and always the LRA config

Whatever `haptic.using` currently says, this experiment reads
`haptic.lra.default_frequency` and `haptic.lra.default_amp`
(`config.json`, via `common/haptic_config.py`) — an adhesive comparison
is only meaningful when all three runs are driven identically, so the
drive is shown **read-only** in the GUI rather than offered as a
control. Each run's meta records the delivered values, the full config
snapshot, and whether they matched the config
(`frequency_source`/`amp_source`).

### Per-trial sequence (identical for every trial and every adhesive)

1. motor off
2. quiet baseline window (1 s)
3. `S` motor-on command — timestamped (`command_time_s`)
4. continuous LRA drive for the *Vibration duration* (0.5–10 s, default 2 s)
5. every raw X/Y/Z sample of the window recorded losslessly
6. `X` motor off
7. *Rest duration* (default 1 s), then the next trial

Controls: *Trials* (1–20, default 5), *Vibration duration*, *Rest
duration*, free-text *Notes* (adhesive thickness/layers, curing time,
installation order, anomalies — stored in the CSV and meta), plus the
motor-port and ACC-sensor pickers every validation window has.

---

## 2. What is measured

| region | figures |
|---|---|
| whole drive window | the shared intensity block (`acceleration_metrics.METRIC_CSV_COLUMNS`): demeaned 3-axis vector RMS + legacy magnitude RMS + per-axis stats |
| rise | onset latency, 10–90 % rise time, settling time — from the **Motor → ACC Delay experiment's detector** (`motor_acc_delay.detect_onset_and_settling`), not a re-implementation |
| steady state | steady vector RMS, absolute peak, robust peak (p99), per-axis peak-to-peak, amplitude spectrum, amplitude at the configured drive frequency, harmonics 2–5, THD |

**Steady state** = detected stable time → motor-off. A trial that never
stabilises is stored as `not_settled` with an **empty** settling time
(never an invented one); its steady-state numbers come from the window
tail instead, flagged `steady_region_source = window_tail_fallback`, and
are **excluded** from steady-state statistics and comparisons.

**Two peaks on purpose.** `peak_abs` is the largest single steady-state
sample — one knock sets it. `peak_robust_p99` is the 99th percentile of
the same series, which one outlier cannot move. Both are saved and both
are plotted; no conclusion rests on a single anomalous sample.

**Spectrum.** Per-axis demeaned, Hann-windowed rFFT, combined in
quadrature over the axes (the frequency-domain twin of the demeaned
vector RMS). Tone amplitudes are estimated from the main-lobe energy /
ENBW, so they do not depend on where the tone falls between bin centres.
`THD = sqrt(Σ_{k≥2} A_k²) / A_1` over harmonics 2–5 of the configured
drive frequency. All of this lives in the **shared**
`acceleration_metrics` module (`compute_vibration_spectrum`,
`harmonic_amplitudes`, `total_harmonic_distortion`).

---

## 3. Per-run outputs

Under `data/validation_experiments/adhesion_vibration_comparison/`
(Unix-epoch stamp `<ts>`, method id in the name):

```
adhesion_blu_tack_<ts>.csv           per-trial summary rows
adhesion_blu_tack_<ts>.raw_acc.npz   lossless raw three-axis samples (shared NPZ format)
adhesion_blu_tack_<ts>.png           run figure (waveform, zoom, envelope, spectrum, per-trial panels)
adhesion_blu_tack_<ts>.meta.json     parameters, config snapshot, analysis constants, result, per-trial rows
```

The CSV carries, per trial: method, trial id, status, drive
frequency/amp, motor port, sensor id, sampling rate/interval, baseline
and vibration sample counts, the three instants (command/onset/stable,
epoch seconds), the derived intervals (onset latency, rise time,
settling time, stable latency — kept separate, never summed), the steady
region (source/start/end), all steady amplitude figures (counts and
m/s²), spectrum figures (dominant frequency, amplitude at the drive
frequency, harmonics, THD), notes, and the shared whole-window intensity
block. Re-opening a run rebuilds the figure and statistics from these
files alone.

---

## 4. Analyse adhesion methods (grouped comparison)

The window's **Analyse adhesion methods** button opens a table of every
saved run (time, method, trials, frequency, amp, port, duration, notes).
Tick **any number of runs per method**, covering at least two methods.
The analysis refuses — naming each parameter and each run's value —
unless:

* at least two runs, covering at least two **different** methods,
* identical LRA frequency, amp, motor port, sensor id,
* identical ACC stream interval and vibration duration.

A method with no run is simply left out; it is never inferred. All three
methods are *not* required, so an adhesive that could not be mounted at
all does not block the comparison of the others.

Options: the **reference method** for the relative figures (default Blu
Tack; only a method present in the selection can be chosen) and the
waveform **alignment** (motor-on command, or each trial's own onset).

### Several runs per method, and how they are averaged

The same adhesive applied twice is **not** the same mount: layer
thickness, contact area and how hard it was pressed all move the
transmitted vibration, and that spread can rival the difference between
two adhesives. So record several runs of a method and select them all.

**A run is the averaging unit.** A method's mean is the mean of its
*run* means, not the mean of all its trials — the trials inside one run
are repeated measurements of a single mount, so pooling them would
weight a 5-trial run above a 3-trial one and would understate the real
spread (pseudo-replication). Two standard deviations are therefore
reported and never conflated:

| field | meaning |
|---|---|
| `sd_between_runs` | spread of the run means — **mount repeatability**, i.e. how reproducibly this adhesive can be applied |
| `sd_within_run` | pooled spread of trials around their own run's mean — **measurement repeatability** of one mount |

The headline `sd` / `cv_percent` use the between-run spread when a
method has at least two runs, and the within-run one otherwise;
`sd_basis` records which, so a single-run method is never read as if its
mount repeatability had been measured. The figures show both: each run's
mean as a ◆ marker, each trial as a faint dot, and the error bar as the
headline SD. The text report's "SD over" column says `mounts` or
`trials` per row.

### Outputs

```
adhesion_comparison_<ts>_waveform.png   aligned mean envelopes ± SD, rise zoom, steady waveform shape, timing bars
adhesion_comparison_<ts>_spectrum.png   mean spectra, fundamental/dominant, harmonics, THD
adhesion_comparison_<ts>_metrics.png    strength, peaks, per-metric CV, relative transfer in dB
adhesion_comparison_<ts>.csv            tidy rows at three levels — scope = trial | run | summary —
                                        so every method mean can be checked against the mount means
                                        behind it, and every mount mean against its trials
adhesion_comparison_<ts>.meta.json      inputs (every run, grouped by method), drive, analysis
                                        parameters, alignment, averaging + SD definitions, steady
                                        definition, reference method, versions, result summary
```

### Relative vibration transfer — not transmissibility

For each steady-state amplitude series (vector RMS, robust peak,
absolute peak, peak-to-peak, fundamental amplitude), both sides being
means over mounts:

```
ratio     = mean(method) / mean(reference)
change %  = 100 · (ratio − 1)
dB        = 20 · log10(ratio)
```

These are **relative vibration transfer / relative attenuation** figures
between three mounted configurations. They are deliberately *not* called
transmissibility: no reference accelerometer measures the actuator's
input side, so no input/output transfer function exists in this setup.

---

## 5. Experiment validity

The comparison is only as good as the mounting protocol (shown in the
GUI, stored in every meta under `validity.protocol`):

* the same physical LRA for all three methods;
* the same accelerometer, motor port, mounting position, orientation
  and surface;
* as equal an adhesive area and thickness as possible;
* a consistent curing time for the cosmetic adhesive;
* several trials per run **and several runs per method** — trials show
  how steady one mount is, runs show how reproducible the mounting is,
  and only the second answers "is this adhesive better than that one";
* stricter runs vary the **order** the methods are tested in, so
  temperature, LRA self-heating or battery level cannot bias one method
  systematically.

Record installation order, curing time, adhesive thickness/layers and
ambient notes in the *Notes* field — they are saved with the run.

---

## 6. Tests

`test-script/test_adhesion_vibration.py` (run from `main/`): method
whitelisting, the fixed `haptic.lra` read (even with `using = "erm"`),
one-method-per-run outputs, raw-NPZ save/reload consistency, numerical
correctness of RMS/peaks/fundamental/harmonics/THD on synthetic signals,
`not_settled` handling, waveform alignment, every rejection rule of the
three-run selection, the relative-transfer formulas, the comparison's
file outputs, and the GUI window/dialog.
