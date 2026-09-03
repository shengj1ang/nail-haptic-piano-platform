# Experiment Sequence Generator — Algorithm Reference

This documents exactly what `app/sequence_generator.py` does, as implemented. It is kept in
lockstep with the [final report](../shengjiang_final_report.pdf), specifically
§ "Controlled Stimulus Sequence Generator" and Appendix §§ "Difficulty Components"
through "Family Matching Tolerances". As of 2026-07-09 the code and the
report describe the same design, including the two deliberate deviations that are documented
in both places (H_hand demoted to a diagnostic; P_pred estimated translation-invariantly).

Code entry points: `app/sequence_generator.py` (all generation logic, GUI-free),
`app/stimulus_validation.py` (difficulty validation, GUI-free),
`app/gui/sequence_generator_window.py` (the "Experiment Sequence Generator" tool),
`app/gui/sequence_metrics_window.py` (the read-only "Sequence/Music Metrics" viewer),
`app/gui/stimulus_validation_dialog.py` (the validation report/box-plot window).

## 0. Positioning (design principle for all future changes)

This is **not** a *Piano Fingering Generator*. It is a **Controlled Finger Assignment
Generator** — equivalently, a **Controlled Finger-specific Motor Sequence Generator**.

Its purpose is to produce **controlled, reproducible, quantifiable finger-specific motor
sequences** as experimental stimuli — not to simulate how a piano teacher would assign
fingering to a piece.

Practical consequence: when weighing a change, the question is never "would a pianist finger it
this way?" but "does this keep the stimuli controlled (constraint-driven), reproducible (seeded),
and quantifiable (measured by D = (C_m, C_s, C_c))?". Ergonomic rules (finger-run limits, the
cross-region plausibility checks, the same-key/same-finger rule) exist only to keep stimuli
*physically performable and free of confounds*, not to make them musically idiomatic.

(Deliberately not written into the thesis — this is an internal design principle.)

## 1. What gets generated

A **sequence** is an ordered list of exactly **T = 30 single-key cue events** (`SEQUENCE_LENGTH`),
each a complete action `a_t = (h_t, f_t, m_t)`:

- `h_t` — hand, `L` or `R`
- `f_t` — finger, `1` (thumb) .. `5` (little finger)
- `m_t` — the actual MIDI note number this action targets

One hand, one finger, one key, one required keypress per event — no chords or two-key events.
Every sequence must use **both hands across its 30 events** (bimanual at the sequence level,
single-key at the event level). Difficulty levels are `alpha`, `beta`, `gamma` (α/β/γ).

## 2. Keyboard range: profile-derived, optionally narrowed

Everything derives from the active calibration profile's own `midi_mapping.json`:

- `valid_notes_for_profile(profile)` — every MIDI note the calibrated keyboard can send.
- `profile_note_range(profile)` = `(min, max)` — the `START_NOTE`/`END_NOTE` bounds.
- White keys only: `note % 12 in {0, 2, 4, 5, 7, 9, 11}` (an experimental control choice).

If the experimenter narrows the range, `k_min`, `k_max`, span `S = k_max − k_min`, and midpoint
`k_mid = k_min + S/2` all follow the **selected** range, so hand regions and every span-normalised
constraint describe the keyboard area actually in use. The planned pilot profile is MIDI 48–72
(C3–C5, S = 24, k_mid = 60), giving a 15-note white-key pool.

## 3. Hand regions

Per level, `R_L(r) = [k_min, k_mid + rS/2]`, `R_R(r) = [k_mid − rS/2, k_max]` with overlap ratio
`LEVEL_OVERLAP_RATIO`: α `r = 0` (strict split, no crossing), β `r = 0.15` (small shared middle
band), γ `r = 0.30` (plus limited cross-region movement, Section 6).

## 4. Difficulty representation D = (C_m, C_s, C_c)

No scalar difficulty score exists. `compute_stats(actions, k_min, k_max)` returns a
`SequenceStats` with 15 scalar components (`COMPONENTS` lists them with group + validation role):

**C_m — motor movement cost** (semitones, on the calibrated profile):

| Component | Definition | Validation role |
|---|---|---|
| `d_seq_mean` | mean \|m_t − m_{t−1}\| across consecutive cue events | increases with level |
| `d_m_mean` | mean same-hand key displacement (consecutive active actions of one hand, pooled) | increases |
| `d_m_p95` | 95th percentile same-hand key displacement (linear interpolation) | increases |
| `d_f_mean` | mean same-hand finger-transition distance | increases |
| `r_l`, `r_r` | per-hand used note range (max − min) | increase |

**C_s — sequence complexity:**

| Component | Definition | Validation role |
|---|---|---|
| `h_norm` | transition-class entropy / log2 K | increases |
| `v_trans` | distinct transition classes / (T−1) | increases |
| `p_pred` | share of the dominant complete relative move (see below) | **decreases** |

Transition class: `c_t = (I(hand switch), b(d_seq), φ)` where `φ = b(|Δfinger|)` on a same-hand
transition and a dedicated "switch" symbol otherwise; bins `b(d)`: 0 / (0,2] / (2,5] / >5.
`K = 16` (same-hand: 4 spatial × 3 reachable finger bins = 12; switch: 4 × 1 = 4) — level-independent.

`p_pred` estimates P(a_{t+1} | a_t) over **complete actions**. A 30-event sequence cannot support a
raw action→action count table (singleton contexts would make the most varied sequences score as
maximally predictable — this was measured and was anti-monotone), so the conditional is
parameterised translation-invariantly by the complete relative move
`δ = (I(hand switch), Δfinger, Δnote)` pooled over the 29 transitions; `p_pred = max_δ p̂(δ)`.
Measured medians on 48–72: α ≈ 0.14–0.21 > β ≈ 0.10 > γ ≈ 0.07 (correctly falling).

**C_c — bimanual coordination:**

| Component | Definition | Validation role |
|---|---|---|
| `a_h` | hand alternation frequency (switches / (T−1)) | increases |
| `h_hand` | 4-class hand-transition entropy / log2 4 | **diagnostic only** |
| `b_h` | 1 − \|n_L − n_R\| / T | matching constraint (checked vs level floor) |
| `o_lr` | overlap of the hands' used note ranges (see below) | increases |
| `x_f`, `x_e` | cross-region frequency / extent vs k_mid | validated separately |

`o_lr` counts interval sizes as **semitone positions inclusive of both endpoints**, so two hands
meeting only at k_mid give a small non-zero overlap — this is what makes β's `X_f = 0` +
`O_LR > 0` combination satisfiable (both hands must touch the midpoint note).

`h_hand` is **not** a level constraint or matching tolerance: given each level's A_h lower bound,
B_h floor, and the 60% share rule, its achievable range is pinned to ≈[0.81, 1.0] for α/β (the
original table bounds ≤0.60/≤0.80 were unsatisfiable), and it peaks at A_h = 0.5 (inside γ's
range) so it cannot order the levels. It is computed, displayed, and logged as a diagnostic.

## 5. Level acceptance constraints

`LEVEL_CONSTRAINTS` (inclusive ranges; `*_s` = normalised by S; `STRICT_LOWER` marks strict `<`
lower bounds, e.g. β's `0.08 < d̄m/S`):

| Constraint | α | β | γ |
|---|---|---|---|
| `d_seq_mean_s` | ≤ 0.12 | (0.08, 0.22] | (0.15, 0.38] |
| `d_m_mean_s` | ≤ 0.10 | (0.08, 0.20] | (0.15, 0.35] |
| `d_m_p95_s` | ≤ 0.16 | ≤ 0.30 | ≤ 0.45 |
| `d_f_mean` | ≤ 1.0 | ≤ 2.0 | ≤ 3.0 |
| `r_h_s` (max of both hands) | ≤ 0.30 | ≤ 0.45 | ≤ 0.65 |
| `h_norm` | [0.15, 0.35] | [0.40, 0.65] | [0.65, 0.90] |
| `a_h` | [0.15, 0.35] | [0.30, 0.55] | [0.50, 0.80] |
| `b_h` | ≥ 0.65 | ≥ 0.70 | ≥ 0.75 |
| `o_lr` | = 0 | (0, 0.15] | [0.10, 0.30] |
| `x_f` | = 0 | = 0 | (0, 0.20] |
| `x_e_s` | = 0 | = 0 | ≤ 0.20 |

Structural rejection rules (`structural_violations`, all levels): both hands must appear; no
repeated identical three-event chunk; no hand with more than 60% of events (note: at T = 30 this
implies B_h ≥ 0.8, so the table's B_h floors are not binding in practice); no finger used more
than 3 consecutive times by the same hand; a hand that presses the same key on consecutive
occurrences (consecutive within that hand's own action subsequence) must use the same finger
for both.

## 6. Candidate construction (`_build_one_sequence`)

1. **Hand labels first** — `_sample_hand_labels` rejection-samples a length-30 L/R sequence
   satisfying the level's A_h range, B_h floor, and the 60% rule. Fixing the labels up front makes
   every same-hand pair count known, enabling exact running budgets.
2. **Note/finger walk** — for each event, enumerate legal `(note, finger)` candidates:
   - note within the hand's region; running budgets against the level's upper bounds for
     `d_m` (plus a per-step cap at the `d_m_p95` bound), `d_f`, `d_seq`, and per-hand range;
   - γ cross-region ergonomics: cross candidates limited by the `x_e_s` bound, ≤ 2 consecutive
     cross events, jumps into/out of cross-region ≤ 0.45 S, and in crossed adjacent opposite-hand
     pairs the crossing hand must use a thumb-side finger (1–3);
   - O_LR = 0 levels: prune candidates that would make the two hands' used ranges intersect;
   - finger run rule (≤ 3 consecutive same finger per hand);
   - same-key/same-finger rule: if the note equals the hand's previous note, only the same
     finger is legal.
   Dead end (no candidates) → discard and restart.
3. **Proposal steering** — these shape only the *proposal* distribution; acceptance stays strictly
   constraint-driven, so they cannot admit an out-of-range sequence. Without them, several ranges
   (notably α's H_norm ≤ 0.35 ≈ 2–3 dominant classes over 29 transitions) are reached with
   negligible probability by uniform sampling:
   - `_CLASS_PERSISTENCE` (α 0.99 / β 0.80 / γ 0.0): steer back to the dominant transition class;
   - `_MOVE_PERSISTENCE` (α 0.5 / β 0.2 / γ 0.0): steer to the dominant exact complete move
     (this is what separates the levels on `p_pred`);
   - `_SWITCH_LOCALITY` (α, β): anchor each hand's first note and switch-adjacent notes to the
     midpoint / previous note, so switches don't burn the d_seq budget;
   - `_FINGER_JUMP_WINDOW` (α {0,1} / β {1,2} / γ {2–4}, applied with p = 0.6): separates the
     levels on `d_f_mean`;
   - "must eventually happen" steering: γ needs ≥ 1 cross event; O_LR > 0 levels need the hands'
     ranges to meet.
4. **Acceptance** — `structural_violations` empty and `within_level_constraints` true.

Generating all three 9-sequence families on the 48–72 profile takes ~2 s total.

## 7. Matched families (`generate_matched_family`)

Pool of individually valid, deduplicated candidates (default `max(count × 25, 40)`), then a
compatibility graph under the pairwise tolerances (`FAMILY_TOLERANCES`, matching the thesis table):

| Statistic | Tolerance |
|---|---|
| `h_norm` | ± 0.05 |
| `d_m_mean_s` | ± 0.04 |
| `a_h` | ± 0.08 |
| `b_h` | ± 0.10 |
| `o_lr` | ± 0.05 |

A greedy clique heuristic finds a mutually compatible group of `count` (default 9, range 1–50).
On failure the error asks the experimenter to widen the note range, relax the tolerances, rerun,
or request a smaller pool. `generate_all_matched_families` runs all three levels off one shared
RNG and reports per-level successes/failures separately.

## 8. Difficulty validation (`app/stimulus_validation.py`)

`validate_level_pools({level: [(actions, stats), ...]})` implements the report's validation; it
runs automatically in the generator window after every generation (before pools are locked) and
from the metrics viewer's "Validate Stimulus Set" button (same functions, saved sequences):

- per component: median, IQR, full range per level;
- monotonic = level medians follow the intended direction **and** < 10% of adjacent-level pairwise
  comparisons (all α×β plus all β×γ) violate it; `p_pred` checked falling;
- `b_h` checked per-sequence against the level floor (not for a trend); `h_hand` reported only;
- cross-region separately: `x_f = x_e = 0` for every α/β sequence, γ within its table limits;
- structural rules re-checked per sequence;
- conclusion: valid / partially valid / invalid / insufficient data.

On the 48–72 profile, 4 of 5 random seeds produced fully "valid" pools; marginal failures (e.g.
R_L at 11.1% violations) are what the validation step exists to catch — regenerate.

## 9. Seeded, reproducible generation

The GUI has a **Seed** field: an integer seeds `random.Random(seed)`; blank draws a fresh seed and
writes it back into the field, so every run is reproducible after the fact. Reproduction requires
the **same profile + note range + Count + seed + software version** — the seed only fixes the
random stream; the settings decide how the stream is consumed (e.g. Count changes the pool size,
which shifts every later draw, including the other levels').

## 10. Output format and provenance

Saved to `data/sequence/<name>/{meta.json, fingering.json}` — the identical layout
`app.music_recording` produces for real recordings under `data/music/<name>/`, so
`music_playback.py`, `student_quiz.py`, and `student_quiz_haptic.py` load generated sequences via
`app.song_library` with no special-casing. Events are spaced `INTER_NOTE_INTERVAL_S = 1.15 s`
(0.4 s nominal hold + the 750 ms inter-cue blank from the thesis's Trial Structure).

`meta.json` extras on generated sequences (absent/None on real recordings; old files still load):
`start_note`, `end_note` (the effective generation bounds — used to recompute span-normalised
components identically later), `generation_seed`, `generation_count` (reproduction provenance).
`difficulty` stays 1/2/3 for α/β/γ.

Default names: `<level symbol>-<id>` (α-1 … γ-9), or `<batch>-<level symbol>-<id>` with a batch
name; editable per row before saving. The generator window shows the five matching statistics
plus `H_hand (diag)`.

## 11. Metrics viewer

`app/gui/sequence_metrics_window.py` recomputes **all 15 components** for every saved song (music
and sequence alike) with the exact same `compute_stats` code path. Note bounds resolve in order:
saved `meta.json` bounds → active profile's range → the sequence's own min/max. Unresolved-finger
notes (possible only on real recordings) are excluded; the Resolved column shows how many notes
went into the numbers.
