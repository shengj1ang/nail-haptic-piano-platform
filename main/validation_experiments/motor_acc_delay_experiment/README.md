# Motor → Accelerometer Delay

Measures the **end-to-end latency between the host issuing a motor-on
command and the vibration physically starting**, for both actuator
types on the rig (LRA and ERM). This is the electromechanical part of
the haptic-cue latency chain, and the validation behind treating the
haptic command timestamp as the cue-onset time in the main user
study's reaction-time logging.

## 1. What is measured

```
host t_cmd ── "S mask amp" ──USB──> firmware ──PWM──> motor starts moving
                                                          │ mechanical coupling
host detects Δ|a| > threshold <──USB── "ACC,id,x,y,z" <── LIS3DH glued to the motor
```

Two latency metrics are reported per trial, both timed against the
host clock at the `S` command and refined by **linear interpolation**
between the last sub-threshold and first supra-threshold sample (the
standard sub-sample onset estimate):

- **Motion onset** (`onset_ms`) — the first instant the signal
  departs from baseline noise, estimated by **CUSUM change-point
  detection** (Page, 1954): the cumulative sum
  `S_i = max(0, S_{i−1} + (d_i − μ − kσ))` (slack k = 1 σ of the
  baseline deviations) alarms when it exceeds h = 10 σ, and the onset
  is dated at the sample where the alarmed excursion *started*. Unlike
  a fixed-amplitude criterion — which admits an ERM's impulsive start
  instantly but only admits an LRA's exponential ring-up once it has
  grown past the level, biasing the comparison — a change-point
  statistic dates both to the first sustained departure from noise,
  whatever the amplitude-growth shape. This is the command-to-first-
  motion latency: serial write + firmware dispatch + motor start-up +
  mechanical transmission + sensor sampling + return trip.
- **Detection-level crossing** (`delay_ms`) — the response reaching
  3 × the baseline-noise p95. For a resonantly driven LRA this
  includes the mechanical ring-up, so `delay_ms − onset_ms` is the
  rise time from first motion to robustly detectable amplitude.

Both are conservative upper bounds on their respective physical
instants (each includes the stream's transport back to the host).

Detection is purely accelerometer-based (the motor drive is open-loop
and reports nothing back), which is why the actuator and the LIS3DH
must be glued together so they move as one unit.

## 2. Mounting: suspend the rig, don't clamp it

**This test requires the motor+sensor pair to hang freely in the air**
(e.g. from its own wires). Clamping it to the desk sinks the vibration
energy into the desk and, at the calibrated cue level (~0.5 m/s²), the
residual acceleration is too small and too unrepeatable for reliable
onset detection — desk-mounted runs mostly time out or detect late.
(The LRA sweeps keep their desk-mounted protocol: they only compare
conditions against each other on one fixed mounting, which tolerates
the damping; this test needs an absolute, clean onset.)

- **Starting orientation is deliberately unconstrained** — hang it
  however it ends up. The detector measures per-axis deviation from a
  per-trial baseline, so where gravity points is irrelevant.
- **Every trial gates on stillness first**: the stream is watched in
  0.5 s windows until the in-window p95 deviation drops to
  ≤ 60 counts (`STILL_MAX_DEV`), then the baseline is taken and the
  motor fires. The previous trial's buzz leaves the rig swinging; the
  gate absorbs that automatically (60 s timeout, then it asks you to
  steady the rig). The limit sits above the measured noise floor of a
  genuinely still hanging rig (~30–45 counts p95 from wire-borne
  micro-vibration and sensor noise) — real swinging reads well over
  100. If the gate plateaus just above the limit for ~5 s it logs the
  observed floor and suggests raising `STILL_MAX_DEV`.

## 3. Method

Per trial:

0. **Settle** — wait until the suspended rig hangs still (see the
   stillness gate above).
1. **Baseline** — 1 s of the quiet ACC stream; per-axis means
   (b̄x, b̄y, b̄z) plus the mean `|a|` (≈1 g ≈ 1000 counts at ±2 g,
   1 count = 1 mg — the magnitude baseline is gravity). The baseline
   noise sets the trial's statistics: the **detection level**
   `max(40, 3 × p95(baseline deviations))` counts, and the deviation
   noise mean/SD that parameterise the CUSUM onset detector.
2. **Drive** — record `t_cmd`, send `S (1<<motor) 64`; motor stays on
   200 ms.
3. **Detect** — every sample's **per-axis deviation**
   `√((x−b̄x)² + (y−b̄y)² + (z−b̄z)²)` feeds the CUSUM onset statistic
   and the detection-level test; after the detection-level crossing a
   further 25 samples are recorded so the trace captures the full
   ring-up. The detection-level instant is linearly interpolated; the
   onset is the start sample of the CUSUM's alarmed excursion. 3 s
   timeout.
4. **Rest** — motor off (`X`), 1 s pause.

### Why per-axis + adaptive (a fix over the original detector)

The original implementation thresholded `| |a| − baseline |` at a fixed
150 counts, which failed on a properly desk-mounted rig for two
compounding reasons:

- `|a|` is only **second-order sensitive to vibration perpendicular to
  gravity** — `√(g² + v²) ≈ g + v²/2g`, so a 150-count horizontal
  vibration moves the magnitude by only ~11 counts. Suspended rigs
  vibrate in all directions with large amplitude (hence "it only works
  in the air"); clamped rigs vibrate small and directional, right in
  the blind spot.
- The calibrated cue level (amp 64 at 224 Hz resonance) was
  deliberately tuned to **~0.5 m/s² ≈ 50 counts RMS** — permanently
  below a 150-count threshold, so the calibrated on-desk configuration
  could never trigger at all.

The per-axis deviation is first-order in every direction, and the
adaptive threshold sits just above the measured noise floor instead of
at an arbitrary absolute level. Per-trial thresholds are recorded in
the CSV (`threshold` column) and shown on the figure.

10 trials per run; mean/SD/min/max over successful detections, for
both metrics. With firmware ≥ v2.9.0 the LIS3DH runs at **1.344 kHz**
(0.74 ms sample period) and the stream at `A START 1`, giving ~6
samples per 224 Hz vibration cycle; combined with the interpolated
crossings this removes most of the 2 ms grid quantisation and the
phase-luck error that earlier runs showed (a 224 Hz signal sampled at
~2 ms had only ~2 samples per cycle, so a sample landing near the
oscillation's zero-crossing pushed detection to the next peak).

Actuator selection: **LRA → motor port 11, ERM → motor port 10** (the
rig's two test channels). The type is a recorded label; the port is
what is actually driven, and both are parameters.

### ERM drive frequency (why the ERM "stopped working" after v2.7.0)

Since firmware v2.7.0 every motor pin boots at **224 Hz PWM** — the
LRA's resonance. That default is *wrong for an ERM*: a DC motor
chopped at 224 Hz (amp 64 = 1.1 ms on / 3.3 ms off) cannot overcome
stiction and never spins. ERMs need kHz-range PWM so the drive acts as
smooth DC — the ~4.5 kHz Teensy default of firmware ≤ v2.4 is why the
historical ERM runs worked. The experiment therefore sets the port's
PWM frequency per actuator (`ACTUATOR_PWM_HZ`: LRA 224 Hz, ERM
5 kHz) before the trials, records it in the meta (`pwm_freq_hz`), and
restores the boot default afterwards; the GUI's Test Buzz applies the
same frequency. This also applies to any future tool that drives the
ERM channel.

## 4. Historical results (firmware v2.1.0, runs of ≈2026-04-17)

| Actuator | Mean | SD | Range | n |
|---|---|---|---|---|
| **LRA** (port 11) | **6.18 ms** | 1.58 | 3.76–7.80 ms | 10/10 |
| **ERM** (port 10) | **8.58 ms** | 1.93 | 5.76–11.75 ms | 10/10 |

**The historical direction (LRA faster) was an artifact** of the
off-resonance drive of that firmware era and did not survive
re-measurement — see the current results below. The one conclusion
that stands: **both actuators are far below perceptual/reaction-time
scales** (hundreds of ms), so logging the haptic command time as cue
onset introduces a negligible, near-constant offset.

The historical timestamps are approximate (git commit date,
`"timestamp_approximate": true` in their meta) and their per-sample
traces were not preserved — only summary rows, console logs and the
original figures.

### Why current-firmware delays come out HIGHER, not lower

Re-measurement on v2.8.0 gives *larger* delays than the v2.1.0
numbers, despite the later serial-latency optimisation. This is real
physics, not a regression:

- **Resonant ring-up.** v2.1.0 had no `F` command; the LRA was driven
  at the old non-resonant PWM default, which acts like a step kick —
  an immediate broadband transient the detector catches within a few
  ms. Current firmware drives the LRA *at* its 224 Hz resonance,
  where a high-Q spring–mass builds amplitude over several ~4.5 ms
  cycles before crossing any threshold. The extra milliseconds are
  the actuator genuinely taking longer to reach detectable (and
  perceivable) amplitude.
- **Calibrated, lower drive.** amp 64 at resonance targets the
  comfortable ~0.5 m/s² cue level, a far gentler drive than the old
  configuration — slower ring-up still.

The new numbers are therefore the *more representative* ones: they
measure the latency of the actual cue the study delivers, ring-up
included, while the old numbers measured an off-resonance kick that no
longer exists. Compare new runs against each other (LRA vs ERM, both
on current firmware, suspended), not against the v2.1.0 table above.

### Current results (firmware v2.9.0, suspended rig, CUSUM onset)

| Actuator | Motion onset | Detection-level crossing | n |
|---|---|---|---|
| **ERM** (5 kHz PWM) | **1.37 ± 0.87 ms** | 1.39 ms | 10/10 |
| **LRA** (224 Hz resonant) | **5.88 ± 2.85 ms** | 6.26 ms | 10/10 |

Under representative drive conditions the **ERM's mechanical onset is
~4.5 ms faster than the LRA's** — the direction is *reversed* from the
v2.1.0 table. This is expected physics, not a measurement bias (the
CUSUM onset dates both actuators to their first departure from noise):
an ERM produces an immediate torque impulse at power-on, while a
resonantly driven LRA at the calibrated gentle amplitude needs its
first ring-up cycles before any signal clears the sensor noise floor.

Interpretation for the study: **onset latency was never a selection
criterion for the actuator** — the LRA was chosen for perceptual
clarity, comfort and nail-mounted wearability (see
`archived/human_reaction_experiment`), and this measurement's
role is to bound
the systematic error of using the haptic command timestamp as cue
onset. That bound is < 10 ms for either actuator — well over an order
of magnitude below reaction-time scales — and near-constant within
condition, so it shifts no within-subject comparison.

## 5. Running it

```bash
python motor_acc_delay.py            # CLI, defaults: LRA on port 11
```

or launcher → "9. Validation Experiments" → **Motor → ACC Delay
(LRA/ERM)**: actuator dropdown (selecting an actuator resets the
motor port, PWM frequency and drive amp to that actuator's defaults —
LRA → port 11 / 224 Hz / amp 64, ERM → port 10 / 5 kHz / amp 64; each
can still be overridden afterwards), motor/ACC overrides, progress bar
per trial, Test Buzz (sent at the selected PWM frequency), embedded
Accelerometer Live View, latest-run preview and Load-Chart-from-CSV —
the same shell as the LRA sweeps.

**Drive amp is exposed for exploration, not for the headline number:**
amp 64 is the calibrated ~0.5 m/s² cue the study delivers, so only
amp-64 runs bound the study's timestamp error. A harder drive rings
the LRA up faster and reads lower — a real effect, but of a cue the
study never plays. Non-default-amp runs are flagged in the run log,
the status line, and carry their amp in the meta/plot title.

## 6. Outputs

`main/data/validation_experiments/motor_acc_delay_experiment/`,
`<ts>` = Unix epoch seconds at save time:

| File | Content |
|---|---|
| `delay_trials_<ts>.csv` | per-trial rows: trial_id, status, onset_ms, delay_ms, baseline_mag, peak_delta, onset_threshold, threshold |
| `delay_samples_<ts>.csv` | per-sample traces: trial_id, rel_time_s, delta (new runs only) |
| `delay_summary_<ts>.png` | two panels: delay per trial + mean line; Δ-traces with threshold and detection marks |
| `delay_trials_<ts>.meta.json` | parameters, firmware identity, linked files, result stats |
| `delay_log_<ts>.txt` | (historical runs only) original console logs |

## 7. Caveats

- Resolution ≈ the 2 ms stream interval; sub-millisecond differences
  are not resolvable with this method.
- The threshold crossing lags the true mechanical onset slightly
  (ring-up must clear the noise floor), so values are conservative
  upper bounds — fine for the "is it negligible?" question this
  experiment answers.
- The historical v2.1.0 numbers were measured with the old fixed
  |a|-threshold detector; their large peak_deltas (150–850) mean the
  detector change should not materially shift those delays, but exact
  old-vs-new comparisons carry that caveat.
- One serial connection serves both the motor command and the ACC
  stream (same Teensy); the original two-handle implementation was
  merged into one when this was integrated.
