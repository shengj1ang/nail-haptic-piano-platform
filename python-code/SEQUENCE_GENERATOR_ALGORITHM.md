# Experiment Sequence Generator — Algorithm Reference

This documents exactly what `app/sequence_generator.py` does, as implemented, so it can be used to
update `final_report_2026/method/method.tex` ("Sequence Design and Difficulty Levels" and related
sections). Where the implementation has diverged from the thesis text as originally written, that is
called out explicitly under **Deviations from the original thesis text**.

Code entry points: `app/sequence_generator.py` (all logic, GUI-free), `app/gui/sequence_generator_window.py`
(the "Experiment Sequence Generator" tool), `app/gui/sequence_metrics_window.py` (the read-only
"Sequence/Music Metrics" viewer, which recomputes the same numbers for already-saved songs).

## 1. What gets generated

A **sequence** is an ordered list of *actions* `a_t = (h_t, f_t, m_t)`:

- `h_t` — hand, `L` or `R`
- `f_t` — finger, `1` (thumb) .. `5` (little finger)
- `m_t` — the actual MIDI note number this action targets

Sequence length is configurable (`n_actions`, default 12, allowed range 12–16 — `DEFAULT_N_ACTIONS`,
`MIN_N_ACTIONS`, `MAX_N_ACTIONS`), matching the thesis's 12-action stimuli with the 12–16 generator
range kept available.

Every sequence belongs to one of three difficulty levels — `alpha`, `beta`, `gamma` (displayed as
α/β/γ) — each with its own finger-transition grammar and acceptance thresholds (Section 3).

## 2. Keyboard range: scalable, not a fixed formula

The original thesis text fixed key index 1 = C4 (MIDI 60) and used `m_t = 59 + k_t`, which only holds
for one specific physical keyboard/transpose setting. The generator instead derives everything from
the *currently active calibration profile's own* `midi_mapping.json`:

- `valid_notes_for_profile(profile)` — every MIDI note that appears anywhere in that profile's
  `midi_mapping.json`, i.e. every note this specific physical keyboard can actually send.
- `profile_note_range(profile)` = `(min(valid_notes), max(valid_notes))` — the bounds shown to the
  user as `START_NOTE`/`END_NOTE`.
- `white_notes_for_profile(profile)` — the same set filtered to natural (white-key) notes only.

There is no abstract "key index" at all any more — an action's position *is* its real MIDI note
number. This means `LEVEL_MAX_KEY_JUMP` (below) is measured directly in semitones, not in the
thesis's chromatic key-index units, and the whole generator automatically works for any keyboard
size/range as long as it has a completed calibration + MIDI mapping.

**Tonal restriction (unchanged from the thesis):** only white-key (natural) notes are ever used —
no black keys, no chords. This is now a fact about a MIDI note's pitch class
(`note % 12 in {1, 3, 6, 8, 10}` = black), not something read from the profile's calibration, so it
doesn't depend on `keyboard_template.json` at all, only `midi_mapping.json`.

## 3. Difficulty grammar per level

### 3.1 Finger-transition legality matrices

`FINGER_MATRICES[level][f_prev - 1][f_curr - 1]` — `1` = allowed, `0` = disallowed, unchanged from
the thesis:

```
F_alpha =                    F_beta =                     F_gamma =
[1 1 0 0 0]                  [1 1 1 0 0]                  [1 1 1 1 1]
[1 1 1 0 0]                  [1 1 1 1 0]                  [1 1 1 1 1]
[0 1 1 1 0]                  [1 1 1 1 1]                  [1 1 1 1 1]
[0 0 1 1 1]                  [0 1 1 1 1]                  [1 1 1 1 1]
[0 0 0 1 1]                  [0 0 1 1 1]                  [1 1 1 1 1]
```

### 3.2 Per-transition thresholds

| Level | Max finger jump | Max key jump (semitones) | Hand-switch probability target | H_norm target |
|---|---|---|---|---|
| alpha | 1 | 2 | 0.00 (exactly, no switching) | 0.15–0.30 |
| beta | 2 | 7 | 0.00 (exactly, no switching) | 0.50–0.65 |
| gamma | 4 | unbounded (segment-limited only) | 0.40–0.55 | 0.80–0.95 |

`LEVEL_MAX_KEY_JUMP` is a **deviation from the thesis's 2/4 chromatic-key-index values** (see
Section 8): 2 semitones covers one adjacent white key (occasionally 1, at the E–F/B–C boundary), so
alpha = 2 still means "adjacent white key only"; beta = 7 covers roughly "up to a fifth away."

Only `gamma` ever allows a hand switch. Alpha and beta run entirely on one hand for the whole
sequence (chosen at random per sequence).

## 4. Hand regions — spatial/biomechanical plausibility, scalable to any keyboard

This is new relative to the original thesis text and exists specifically so cross-hand transitions
in `gamma` land somewhere a hand could plausibly reach, rather than an arbitrary switch teleporting a
hand to the opposite end of the keyboard — and so the whole scheme still works on a keyboard of any
size, since everything is a *proportion* of the profile's own range, never an absolute note number.

Given the profile's `(k_min, k_max)` range and span `= k_max − k_min`:

```
overlap_width      = HAND_OVERLAP_RATIO * span                 # 0.15  (~10–20%, per method.tex guidance)
non_overlap_width   = span − overlap_width
left_exclusive      = HAND_REGION_ALPHA * non_overlap_width     # 0.33
right_exclusive     = non_overlap_width − left_exclusive

L region = [k_min,               k_min + left_exclusive + overlap_width]
R region = [k_max − right_exclusive − overlap_width,  k_max]
```

- `HAND_REGION_ALPHA = 0.33` is the **left hand's share of the non-overlapping territory** — the
  default gives the left hand the lower third and the right hand the remaining two-thirds
  (treble/melody register), matching how a pianist's hands are typically distributed rather than an
  exact 50/50 split.
- `HAND_OVERLAP_RATIO = 0.15` is the width of the shared middle band, as a fraction of the full
  range, that *either* hand may use — this is what makes a hand switch near the middle plausible.
- The two regions always union to cover the entire `[k_min, k_max]` range exactly (no note is
  reachable by neither hand), and overlap by exactly `overlap_width` around the middle by
  construction (this is provable algebraically from the formula above, not just empirically).

A hand may only ever be assigned notes from its own region. For `alpha`/`beta` this simply restricts
the single chosen hand's available notes for the whole sequence; for `gamma`, each hand switch's
destination note is drawn from the *new* hand's region.

## 5. Biomechanical cost function

```
M_t = |f_t − f_{t-1}|                     (finger-jump penalty)
    + 0.5 * |m_t − m_{t-1}|               (key/note-distance penalty, in semitones)
    + 2 * I(h_t ≠ h_{t-1})                (hand-shift penalty)
```

This combines all three plausibility factors the thesis asks for (key-distance, finger-jump penalty,
hand-shift penalty) into one scalar, unchanged in form from the original design. `mean_motor_cost`
is its average over all `n_actions − 1` transitions in a sequence.

Implausible transitions are rejected in two places:

1. **Per-transition, at construction time** — the finger-legality matrix, `max_finger_jump`, and
   `max_key_jump` thresholds above are hard constraints; an illegal candidate is never produced in
   the first place.
2. **Per-sequence, after construction** — a candidate sequence is discarded (and generation retried)
   unless its aggregate `H_norm` and hand-switch probability land inside the level's target ranges
   (Section 3.2). There is no separate "reject the whole sequence for cost" step — cost is combined
   into `H_norm`'s transition classes (Section 6) and reported as a descriptive statistic, not used
   as an accept/reject threshold on its own.

## 6. Entropy (H_norm)

Each transition `a_{t-1} → a_t` is classified into one of a small number of discrete transition
classes:

```
class = ( I(hand switch),  |f_t − f_{t-1}|,  bin(|m_t − m_{t-1}|) )

bin(d):  0 if d == 0
         1 if d <= 2
         2 if d <= 7
         3 otherwise
```

`H_norm = −Σ p(c) log2 p(c) / log2(T)`, where `T = n_actions − 1` is the number of transitions and
`p(c)` is the empirical frequency of each class within the sequence — normalized Shannon entropy,
unchanged in form from the thesis. `bin(·)`'s edges (0/2/7) mirror `LEVEL_MAX_KEY_JUMP`'s alpha/beta
thresholds; the thesis text does not specify exact bin edges, so this is a documented choice rather
than a literal transcription.

## 7. Sequence construction algorithm

For one candidate sequence (`_build_one_sequence`):

1. Split the level's usable note pool into `hand_notes["L"]` / `hand_notes["R"]` via the hand
   regions (Section 4).
2. If `gamma`, sample a **target hand-switch count** once for the whole sequence: draw a fraction
   uniformly from the level's hand-switch range (0.40–0.55) and multiply by `n_actions − 1`, rounded
   to the nearest integer. Alpha/beta implicitly target exactly 0.
3. Pick a starting hand (only from hands that have any usable notes), a random starting finger
   (1–5), and a random starting note from that hand's pool.
4. For each subsequent action, enumerate every `(finger, hand, note)` triple that is simultaneously:
   - finger-legal per the level's matrix and within `max_finger_jump` of the previous finger;
   - hand-switch-budget-consistent (a switch is only offered while switches remain in the target
     count; a non-switch is only offered while there is still enough room left in the sequence to
     spend the remaining owed switches);
   - drawn from the (possibly new, if switching) hand's own region-filtered note pool, and within
     `max_key_jump` semitones of the previous note (if the level bounds it at all).
   One candidate is picked uniformly at random from this legal set. If the set is empty, the whole
   sequence attempt fails (returns `None`) and generation retries from scratch.
5. Reject the sequence if any 3-action chunk `(hand, finger, note)` repeats verbatim elsewhere in it
   (`has_repeated_trigram`) — prevents obviously-looped stimuli.
6. Accept only if the finished sequence's `H_norm` and hand-switch probability land inside the
   level's target ranges (`_within_level_targets`); otherwise retry.

`generate_single()` wraps this in a retry loop (default up to 500 attempts) for one sequence.

## 8. Matched groups (replacing the thesis's fixed "X/Y/Z" triples)

The thesis's original design fixed exactly 3 sequences per level (X, Y, Z), matched pairwise within
tolerance. The generator now supports a **configurable group size per level** (`count`, default 9,
range 1–50 — `DEFAULT_FAMILY_COUNT`, `MIN_FAMILY_COUNT`, `MAX_FAMILY_COUNT`), since a fixed-3 pool
was a thesis-specific choice, not an algorithmic requirement.

Matching tolerances (unchanged from the thesis): two sequences are considered "matched" if all three
differ by no more than:

| Statistic | Tolerance |
|---|---|
| H_norm | ± 0.05 |
| Hand-switch probability | ± 0.05 |
| Mean motor cost | ± 0.25 |

**Algorithm** (`generate_matched_family`):

1. Generate a pool of individually-accepted candidate sequences (deduplicated) — pool size defaults
   to `max(count × 30, 40)`, generation capped at `max(4000, pool_size × 20)` raw attempts. The ×30
   multiplier was tuned empirically against `gamma` (the hardest level to match, since its key jumps
   are the least constrained) so that `count = 9` succeeds reliably (~10/10 in repeated testing).
2. Build a compatibility graph over the pool: an edge between two candidates exists iff they satisfy
   all three tolerances above pairwise.
3. Find the largest clique (mutually-compatible group) up to size `count`, via a greedy heuristic —
   expand from each node via its highest-mutual-degree common neighbour, keep the best clique found
   across all starting nodes (`_largest_matched_group`). Exact maximum-clique search is NP-hard, so
   this is a standard approximate heuristic rather than an exhaustive search — exhaustive search
   over every combination of `count` candidates was the original (thesis-era) approach and only
   stays computationally feasible for `count ≤ ~3`.
4. If the clique found is smaller than `count`, raise an error (reported to the user; the tool still
   shows whichever difficulty levels *did* succeed rather than failing the whole batch — see
   `generate_all_matched_families`).

`generate_all_matched_families()` simply runs the above once per level (alpha, beta, gamma) and
collects successes/failures separately, since a full stimulus set always needs all three levels.

## 9. Naming

Each sequence in a level's group is numbered `1..count` (not `X/Y/Z` letters). The default name
shown in the generator's table is:

```
<level symbol>-<id>              e.g. α-1, α-2, ..., γ-9      (no batch name set)
<batch name>-<level symbol>-<id> e.g. Pilot01-α-1             (batch name set)
```

The batch name is an optional free-text field in the GUI; when blank, naming falls back to the
plain `<level>-<id>` form. Names are still editable per-row before saving.

## 10. Output format and file layout

A saved sequence is written to `data/sequence/<name>/{meta.json, fingering.json}` —
**the identical layout** `app.music_recording` produces from a real teacher recording under
`data/music/<name>/`, just under a separate top-level folder so the two are easy to tell apart. This
is why a generated sequence needs no special handling anywhere else: `music_playback.py`,
`student_quiz.py`, and `student_quiz_haptic.py` all load a "song" via `app.song_library`, which lists
both folders and labels each entry `"music/<name>"` or `"sequence/<name>"` — neither tool knows or
cares which one produced any given entry.

- `fingering.json` entries are spaced `INTER_NOTE_INTERVAL_S = DEFAULT_NOTE_DURATION_S (0.4s) + 0.75s
  = 1.15s` apart — the fixed 750 ms inter-trial blank interval from the thesis's "Trial Structure",
  plus a nominal note-hold length — purely so `music_playback.py`'s demo transport has a plausible
  schedule to step through; `student_quiz.py` never reads this field (it waits for a real MIDI
  response instead).
- `key_id` per note is resolved via the active profile's `midi_mapping.json`
  (`MidiMapping.key_for_note`).
- `meta.json`'s `difficulty` field is `1`/`2`/`3` for `alpha`/`beta`/`gamma` (`LEVEL_DIFFICULTY`) —
  the same field a real recording's `meta.json` uses, just assigned by the generator instead of a
  teacher.

## 11. Recomputing metrics for already-saved songs

`app/gui/sequence_metrics_window.py` ("Sequence/Music Metrics") recomputes `H_norm`, mean motor
cost, and hand-switch probability for **any already-saved song**, real recording or generated
sequence alike, using the exact same functions:

- `sequence_from_fingering(entries)` turns a saved `fingering.json` back into the same
  `Action`-based sequence type used during generation (skipping notes with no resolved finger — only
  possible on a real recording where the camera pipeline couldn't confidently match one).
- `evaluate_song(name, data_dir)` = `compute_stats(sequence_from_fingering(load_fingering(...)))`.

Because this reuses `compute_stats()` verbatim, a number shown in the metrics viewer means exactly
the same thing as the identically-named number shown while generating — they are not two
implementations of the same idea, they are the same code path.

## 12. Deviations from the original thesis text — summary for the rewrite

| Thesis text (as originally written) | Current implementation |
|---|---|
| Key index 1 = C4 fixed; `m_t = 59 + k_t` | Range derived per-profile from `midi_mapping.json`; no abstract key index, actions carry real MIDI notes directly |
| `LEVEL_MAX_KEY_JUMP` in chromatic key-index units (2 / 4) | Same *numbers* repurposed as semitone units (2 / 7 for alpha/beta; gamma unbounded) — not equivalent, needs a fresh table in the thesis |
| No explicit hand-region concept | Proportional left/right hand regions (`HAND_REGION_ALPHA = 0.33`, `HAND_OVERLAP_RATIO = 0.15`), scalable to any keyboard size, constraining which notes a hand may use and keeping `gamma` cross-hand switches spatially plausible |
| Fixed 3 sequences per level (X, Y, Z) | Configurable group size per level (`count`, default 9), found via a greedy clique search rather than exhaustive triple-checking |
| No batch/naming scheme beyond "L\<level\>-X/Y/Z" | `<level symbol>-<id>` or `<batch name>-<level symbol>-<id>`, `id` numeric 1..count |
| — | A read-only metrics viewer that recomputes the same statistics for saved songs (recorded or generated) using identical code, for direct before/after or across-condition comparison |
