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

Each box is simply the broadband RMS intensity the drive produced
(baseline-subtracted `√(mean((|a| − baseline)²)) × 0.00981`, m/s²) — the
same intensity metric the LRA/ERM amplitude sweeps use. There is no
frequency analysis of the signal: the y-axis is the frequency the motor
is *driven* at, and each box is one scalar intensity measurement. Cells
are drawn as discrete filled boxes, so the picture reads as the grid of
measured intensities the boxes were "filled" with.

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
- **Annotate** — print each cell's value inside its box, as either the raw
  **RMS value** (m/s²) or a **Normalized** 0–1 value (the cell's position
  between the map's min and max). The text is white or black, chosen per
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
| `spectrogram_<ts>.npz` | the intensity grid for re-rendering: `freqs` (n_freq,), `amps` (n_amp,), `intensity` (n_freq × n_amp, RMS m/s², NaN where a cell was dropped) |
| `spectrogram_<ts>.png` | the amp × drive-frequency intensity map (darker = stronger) |
| `spectrogram_<ts>.csv` | one row per (freq, amp) cell: freq_hz, amp, rms_delta_counts, rms_ms2, peak_delta_counts, baseline_mag, n_samples |
| `spectrogram_<ts>.meta.json` | full parameter set (motor type, amp/frequency ranges, precision, freq/amp steps, Vibrate time, annotate mode, timing), firmware identity, linked files, and the run result (strongest-vibration freq / amp / RMS) |

"Load Chart from CSV" re-renders the map from any saved run — the heatmap
is redrawn from the run's sibling `.npz`, with labels from its
`.meta.json`.

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
| `ANNOTATE_MODES` | off / rms / normalized | print each cell's value: none, raw RMS, or 0–1 normalised |
| `COLORMAP` | `magma_r` | pale (low) → near-black (high): darker = stronger |
| `BASELINE_S` / `SETTLE_S` / `REST_S` | 0.30 / 0.30 / 0.15 | other per-step timing (baseline re-measured once per frequency row) |
| `MS2_PER_COUNT` | 0.00981 | LIS3DH HR ±2 g: 1 count = 1 mg |
