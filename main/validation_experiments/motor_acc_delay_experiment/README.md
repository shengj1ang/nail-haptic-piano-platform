# Motor → Accelerometer Delay and Settling

Measures, per trial, **how long the actuator takes to start moving and
how long it then takes to reach a steady vibration** after the host
issues a motor-on command, for both actuator types on the rig (LRA and
ERM). This is the electromechanical part of the haptic-cue latency
chain, and the validation behind treating the haptic command timestamp
as the cue-onset time in the main user study's reaction-time logging.

## 1. What is measured

```
host t_cmd ── "S mask amp" ──USB──> firmware ──PWM──> motor starts moving
                                                          │ mechanical coupling
host records x,y,z  <──USB── "ACC,id,x,y,z" @1 kHz <── LIS3DH glued to the motor
                                     (for the whole Vibration duration)
```

### The three instants, and the two latencies derived from them

Every trial records **three times**, all stored as Unix epoch seconds:

| Column | Meaning |
|---|---|
| `command_time_s` | the host clock at the `S` motor-on write |
| `onset_time_s` | the first sustained departure from the rest noise |
| `stable_time_s` | the first instant the envelope enters the steady band **and holds it** |

and derives from them:

```
onset_latency_from_command  = onset_time  − command_time
settling_time_from_onset    = stable_time − onset_time
stable_latency_from_command = stable_time − command_time
```

**Onset latency and settling time are separate metrics** and are never
merged, summed or averaged into a single "delay"; the summary reports
mean/median/SD/range for each independently. Because "settling time"
is ambiguous about which zero it is measured from, **both forms are
stored**: `settling_time_from_onset_ms` and
`stable_latency_from_command_ms`.

- **Vibration onset latency** (`onset_ms`, also written as
  `onset_latency_from_command_ms`) — the command-to-first-motion
  latency: serial write + firmware dispatch + motor start-up +
  mechanical transmission + sensor sampling + return trip. It marks
  the point where the signal *leaves the rest noise and starts rising*
  — not where it peaks and not where it stabilises.
- **Settling time** (`settling_time_from_onset_ms`) — the rise: how
  long the vibration takes from that onset to a steady, sustained
  amplitude.

A third, historical metric is still reported alongside them:

- **Detection-level crossing** (`delay_ms`) — the response reaching
  3 × the baseline-noise p95, refined by **linear interpolation**
  between the last sub-threshold and the first supra-threshold sample.
  Unchanged, so every number in § 4 stays comparable. It is neither
  the onset nor the settling time.

All are conservative upper bounds on their respective physical
instants (each includes the stream's transport back to the host).

Detection is purely accelerometer-based (the motor drive is open-loop
and reports nothing back), which is why the actuator and the LIS3DH
must be glued together so they move as one unit.

### Vibration duration

The motor is driven **continuously for the whole `Vibration duration`**
(GUI input / `vib_duration_s` argument) and **every raw X/Y/Z sample of
that window is stored** in the run's `.raw_acc.npz`:

| | |
|---|---|
| default | **2.0 s** |
| range | 0.5 – 10.0 s |
| step | 0.5 s |

The window ends at motor-off, so the steady-state estimate never
includes the ring-down. Nothing in the detector assumes 2 s: every
detection window (envelope, steady reference, hold) is a duration in
seconds or a fraction of the vibration duration, and the test suite
exercises 0.5, 1, 2, 5 and 10 s. Longer windows demand more evidence
before a trial counts as settled, and make the run proportionally
longer (the GUI shows the estimate live). A slow ERM spin-up needs the
room; only shorten it for a fast-settling actuator.

### Per-trial status

| `status` | Meaning |
|---|---|
| `ok` | onset **and** stable state found |
| `no_onset` | no sustained departure from the rest noise |
| `not_settled` | onset found, but the vibration never stabilised inside the drive window |
| `insufficient_data` | the window was empty or too short to date both events |

A `not_settled` trial **keeps its onset latency**, stores an **empty**
settling time and stable time, and is counted separately in the
summary. No stable time is ever invented.

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
   `max(40, 3 × p95(baseline deviations))` counts, the deviation noise
   mean/SD that parameterise the CUSUM onset detector, and the **p95 of
   the baseline envelope**, which is what the steady-state sanity
   guards are measured against.
2. **Drive** — record `t_cmd` (host clock and Unix epoch), send
   `S (1<<motor) 64`; the motor stays on for the whole Vibration
   duration.
3. **Record** — the acquisition loop only parses and stores samples, so
   nothing competes with the 1.344 kHz stream. Every sample of the
   drive window is kept: raw X/Y/Z into the `.raw_acc.npz`, and the
   **per-axis deviation** `√((x−b̄x)² + (y−b̄y)² + (z−b̄z)²)` into the
   detector series.
4. **Detect (offline)** — the algorithm below runs on the finished
   window, so it can be replayed on stored or synthetic data.
5. **Rest** — motor off (`X`), 1 s pause.

### The detection algorithm

Everything is derived from the per-sample per-axis deviation `d_i` and
its **envelope** — a *centred* moving RMS of `d_i` over
`ENVELOPE_WINDOW_S`. That envelope is exactly the shared demeaned
three-axis vector RMS evaluated over a short sliding window, so the
detector, the envelope and the project's intensity metric are the same
statistic at three time scales and can never disagree. The window is
centred (not trailing) so the envelope carries no systematic lag that
would inflate every settling time, and at the two ends it is shifted
inwards rather than truncated, so no edge effect fakes a slow start or
a late settle.

**A. Vibration onset** — CUSUM change-point detection (Page, 1954):
`S_i = max(0, S_{i−1} + (d_i − μ − kσ))` (slack k = 1 σ of the baseline
deviations) alarms when it exceeds h = 10 σ, and the onset is dated at
the sample where the alarmed excursion *started*. A fixed-amplitude
criterion would admit an ERM's impulsive start instantly but only admit
an LRA's exponential ring-up once it had grown past the level, biasing
the comparison; a change-point statistic dates both to the first
sustained departure from noise, whatever the amplitude-growth shape.

**Spike guard.** An alarm is only accepted when the deviation stays
above the slack level for at least `ONSET_SUSTAIN_FRACTION` of the
following `ONSET_SUSTAIN_S`; otherwise the accumulator is reset and the
search resumes past it. A lone mechanical knock alarms but is followed
by pure rest noise (which clears the slack level only ~16–20 % of the
time), so it fails and is stepped over. A genuine start — however slow
— has already raised the mean above the slack level by the time it
alarms, so it passes. The test is applied at the **alarm** sample, not
at the dated onset, precisely so a slow ERM ramp is not required to be
large at its own onset.

**B. Steady state and settling** —

1. the **steady level** is the *median* envelope over the last
   `STEADY_REF_FRACTION` of the drive window, never earlier than the
   onset (median, so a late glitch cannot move it; and the window ends
   at motor-off, so no ring-down is included);
2. it must clear `max(MIN_STEADY_ENVELOPE_COUNTS, STEADY_MIN_SNR ×
   baseline envelope p95)`, otherwise the vibration never established
   and the trial is `not_settled`;
3. the **steady band** is that level `× (1 ± STEADY_TOL_FRACTION)`;
4. the **stable time** is the first sample, at or after the onset, from
   which the envelope stays inside the band for a whole **hold window**
   — `STEADY_HOLD_INSIDE_FRACTION` of its samples inside, so one
   envelope wobble does not restart the search, but a transient spike
   into the band cannot qualify either.

The hold window scales with the vibration duration between a floor and
a cap: `clamp(STEADY_HOLD_FRACTION × duration, STEADY_HOLD_MIN_S,
STEADY_HOLD_MAX_S)` — 50 ms at 0.5 s, 200 ms at 2 s, 500 ms at 10 s.

### Default detection parameters

All of these live together in one block in `motor_acc_delay.py`
(“Onset / settling detection”) and are written into every run's
`.meta.json` and, for the ones that can vary per trial, into the CSV.

| Constant | Default | Role |
|---|---|---|
| `CUSUM_SLACK_SIGMA` | 1.0 σ | per-sample slack above the noise mean |
| `CUSUM_ALARM_SIGMA` | 10.0 σ | accumulated evidence needed to alarm |
| `ONSET_SUSTAIN_S` | 30 ms | spike-guard persistence window |
| `ONSET_SUSTAIN_FRACTION` | 0.40 | of that window must stay above the slack |
| `ENVELOPE_WINDOW_S` | 20 ms | centred moving-RMS envelope (~4.5 cycles at 224 Hz) |
| `STEADY_REF_FRACTION` | 0.30 | tail of the drive window defining the steady level |
| `STEADY_REF_MIN_S` | 50 ms | shorter reference ⇒ `not_settled` |
| `STEADY_TOL_FRACTION` | 0.20 | ± band around the steady level |
| `STEADY_HOLD_FRACTION` | 0.10 | hold = this × duration… |
| `STEADY_HOLD_MIN_S` / `MAX_S` | 50 ms / 500 ms | …clamped to this range |
| `STEADY_HOLD_INSIDE_FRACTION` | 0.95 | of the hold window must be in band |
| `STEADY_MIN_SNR` | 1.5 × | steady level vs baseline envelope p95 |
| `MIN_STEADY_ENVELOPE_COUNTS` | 20 | absolute floor for "a vibration happened" |
| `MIN_DETECTION_SAMPLES` | 64 | fewer ⇒ `insufficient_data` |

The tolerance, the smoothing window and the minimum hold are the three
knobs to touch if a real actuator reads `not_settled` too often; widen
the tolerance first.

### Why it works for both actuator types

- **LRA** — fast, near-fixed-frequency resonant ring-up. The CUSUM
  dates the onset at the first departure from noise rather than waiting
  for the ring-up to clear a level, and the 20 ms envelope averages the
  224 Hz carrier away while still resolving a few-millisecond rise.
- **ERM** — the rotor spins up gradually and its frequency climbs with
  it. The change-point onset is unaffected by how slowly the amplitude
  grows, the spike guard is anchored at the alarm rather than at the
  onset so it does not penalise a slow ramp, and the envelope is
  amplitude-based, so a sweeping drive frequency does not disturb the
  steady-state test.

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
the CSV (`threshold_counts` column) and shown on the figure.

### Why the detector was NOT replaced by the shared intensity metrics

This experiment shares the other accelerometer experiments' raw-sample
store, their two offline vibration-intensity metrics and the shared
computation helpers (see [`../README.md`](../README.md)), but its
**detector keeps its own statistic** — the per-sample per-axis
deviation above, feeding the CUSUM test and the settling envelope.

The reason is that the two answer different questions: an RMS is an
average over a whole window and cannot *date* an event, while onset
detection needs a per-sample quantity that responds within one sample.
They are consistent by construction — the recommended **demeaned 3-axis
vector RMS is exactly the RMS of this per-axis deviation over a window**,
i.e. the detector is the per-sample form, the settling envelope the
sliding-window form and the metric the whole-window form — so
substituting one for the other would gain nothing while changing every
historical latency number. Only the storage and the offline statistics
are unified.

The per-trial intensity columns the CSV carries
(`vector_rms_ms2`, `legacy_magnitude_rms_ms2`, the per-axis means/RMS)
are therefore **offline only** and never feed back into detection. They
also cover the whole recorded drive window — which starts at the
motor-on command and so includes the quiet pre-onset samples and the
ring-up as well as the steady state — so they are **not** steady-state
intensities comparable with a sweep cell; the settling detector's
`steady_state_envelope_counts` / `_ms2` is the settled level. The meta
says so in its `drive_window_intensity` block.
Consequently there is no "Plot metric" dropdown in this window: its
figure is a latency plot, not an intensity plot.

10 trials per run. The summary reports **mean, median, SD and range
separately** for the onset latency and for the settling time (and for
the stable latency from command and the historical crossing), together
with the number of successful detections and the counts of `no_onset`
and `not_settled`. With firmware ≥ v2.9.0 the LIS3DH runs at **1.344 kHz**
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

## 4. Results

> **Note.** The run files behind both tables below have been cleared for
> re-measurement under the shared raw-sample pipeline. The numbers are
> kept as the record of what was found; the CUSUM onset detector that
> produced them is unchanged (the added spike guard only *rejects*
> transients, which none of these runs' onsets were), so the onset
> column of a re-run is directly comparable. **There are no settling
> numbers yet**: those runs drove the motor for 200 ms, which is too
> short for a steady-state estimate — the settling column is filled in
> by the first re-measurement at the 2 s default.

### Historical results (firmware v2.1.0, runs of ≈2026-04-17)

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

```bash
python -c "from validation_experiments.motor_acc_delay_experiment import motor_acc_delay as m; m.run_experiment(actuator_type='ERM', motor_index=10, vib_duration_s=5.0)"
```

or launcher → "9. Validation Experiments" → **Motor → ACC Delay
(LRA/ERM)**. The controls sit on three short rows so the window fits a
laptop screen:

| Row | Controls |
|---|---|
| 1 | Motor port · ACC sensor id · Actuator · Test Buzz |
| 2 | Drive amp · PWM freq · **Vibration duration** |
| 3 | Stillness limit, then the live run-time estimate |

Selecting an actuator resets the motor port, PWM frequency and drive
amp to that actuator's defaults (LRA → port 11 / 224 Hz / amp 64,
ERM → port 10 / 5 kHz / amp 64); each can still be overridden
afterwards. **Vibration duration** (0.5–10 s, default 2 s, 0.5 s steps)
sets how long the motor runs per trial and how much data the settling
detector gets; changing it updates the run-time estimate immediately.
Every control carries a tooltip. The rest of the shell is the same as
the LRA sweeps: progress bar per trial, Test Buzz (sent at the selected
PWM frequency), embedded Accelerometer Live View, latest-run preview
and Load-Chart-from-CSV. The four-panel figure is dense, so **click the
chart** (or "Enlarge Chart") to open it full size in the shared zoomable
viewer — see [`../README.md`](../README.md) § 5.5.

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
| `delay_trials_<ts>.csv` | one row per trial — see the column list below |
| `delay_samples_<ts>.csv` | per-sample **detector** traces: `trial_id`, `rel_time_s`, `delta`, `envelope` |
| `delay_trials_<ts>.raw_acc.npz` | **the raw three-axis samples** of every baseline and drive window — the complete X/Y/Z series of each whole vibration period, lossless `int32` counts with Unix-epoch timestamps, so a trial can be re-analysed offline (format: [`../README.md`](../README.md) § 4) |
| `delay_summary_<ts>.png` | four panels — see § 6.1 |
| `delay_trials_<ts>.meta.json` | parameters (including `vib_duration_s` and every detection constant), a `time_definitions` block spelling out the three instants and the three latencies, firmware identity, linked files, result stats, the shared `metrics` block, and `drive_window_intensity` (offline only) |
| `delay_log_<ts>.txt` | (historical runs only) original console logs |

`delay_trials_<ts>.csv` columns, in order:

| Group | Columns |
|---|---|
| identity | `trial_id`, `status`, `actuator_type` |
| the three instants (Unix epoch s) | `command_time_s`, `onset_time_s`, `stable_time_s` |
| the latencies | `onset_ms` (= `onset_latency_from_command_ms`), `settling_time_from_onset_ms`, `stable_latency_from_command_ms` |
| steady state | `steady_state_envelope_counts` / `_ms2`, `steady_band_low_counts`, `steady_band_high_counts`, `env_noise_p95_counts` |
| historical crossing | `delay_ms`, `peak_axis_delta_counts`, `onset_threshold_counts`, `threshold_counts` |
| detection parameters | `vib_duration_s`, `envelope_window_s`, `steady_tolerance_frac`, `steady_hold_s`, `onset_sustain_s`, `sample_rate_hz` |
| shared offline intensity block | `n_samples`, `mean_*_counts`, `rms_*_counts`, `legacy_magnitude_rms_counts/_ms2`, `vector_rms_counts/_ms2`, `baseline_magnitude_counts`, `peak_magnitude_delta_counts` |

`onset_ms` and `onset_latency_from_command_ms` are the same number:
the first is the name every historical run and loader already uses, the
second spells out its reference instant the way the two settling
columns do. Undetected quantities are **empty cells**, never zeros.

### 6.1 The figure

1. **Latency from command per trial** — onset latency and stable
   latency, plus the historical crossing, with their mean lines
   (symlog axis, because the two live on different scales).
2. **Settling time from onset per trial** — on its own, with mean and
   median lines and a count of the trials that never settled.
3. **Steady-state level per trial** — the median envelope each
   settling time was judged against, its ±20 % band as a bar, and the
   mean rest-noise envelope for reference.
4. **The envelope traces** — every trial thin, the median-onset trial
   thick and annotated: the motor command at t = 0, the vibration
   onset (▼), the stable state (●), the steady level and its band
   shaded, and a double arrow across the rise labelled with the
   settling time.

Pre-refactor CSVs used the column names `baseline_mag`, `peak_delta`,
`threshold` and `onset_threshold` and carried no intensity block; the
onset/crossing-era CSVs carry no settling columns and used the status
`timeout`. Both still load (the old names are recognised, missing
settling values come back as `None`) and still re-render; a
`delay_samples` file without an `envelope` column has its envelope
recomputed on load.

## 7. Caveats

- Resolution ≈ the stream interval (0.74 ms sample period at the
  1.344 kHz ODR); sub-millisecond differences are not resolvable with
  this method.
- The threshold crossing lags the true mechanical onset slightly
  (ring-up must clear the noise floor), so values are conservative
  upper bounds — fine for the "is it negligible?" question this
  experiment answers.
- The settling time inherits the envelope's time resolution: a
  20 ms centred moving RMS cannot resolve a settling faster than
  ~10 ms, and a settling time near zero means "already steady within
  one envelope window", not "instantaneous".
- The steady level is a *median over the tail of the drive window*, so
  an actuator that is still drifting at motor-off will read
  `not_settled` rather than settle late — which is the intended
  behaviour, but it means a too-short Vibration duration produces
  `not_settled` trials for a slow ERM. Lengthen the window before
  widening the tolerance.
- `peak_axis_delta_counts` and the offline intensity block now cover
  the **whole** drive window rather than the first few samples after
  the crossing, so those columns are not directly comparable with the
  onset/crossing-era runs (the latency columns are).
- The historical v2.1.0 numbers were measured with the old fixed
  |a|-threshold detector; their large peak_deltas (150–850) mean the
  detector change should not materially shift those delays, but exact
  old-vs-new comparisons carry that caveat.
- One serial connection serves both the motor command and the ACC
  stream (same Teensy); the original two-handle implementation was
  merged into one when this was integrated.

## 8. Tests

The detector is a pure function over a per-sample deviation series, so
it is tested against synthetic signals with a **known** onset and a
**known** settling time — no hardware, no saved run:

```bash
python test-script/test_motor_acc_delay_detection.py   # run from main/
```

Covered: a clean rising edge with a flat plateau; a realistic noisy
resonant start; a lone spike (must not be dated as the onset, either
before a real start or on its own); onset detected but never settled
(a burst, and a runaway amplitude); no onset at all; an LRA-style fast
start; an ERM-style slow spin-up with a climbing drive frequency; every
vibration duration from 0.5 s to 10 s; and the plumbing around all of
it — the three timestamps, both settling forms, empty (never zeroed)
cells for undetected quantities, the status counts, the CSV round trip,
the figure, and loading pre-settling and pre-refactor CSVs.

`python test-script/test_acceleration_metrics.py` covers this
experiment's half of the shared metric layer (the CSV round trip keeps
both RMS metrics, and the detector statistic really is the per-sample
form of the demeaned vector RMS).
