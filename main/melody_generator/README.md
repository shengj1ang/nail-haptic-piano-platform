# melody_generator

A standalone generator of short, simple, fixed-fingering practice melodies for
the piano haptic follow-up experiment.

Each run produces one melody as four files: a playable `.mid`, a complete
`.json`, a `.csv` for eyeballing, and a `.txt` summary.

## Isolation - this cannot affect any earlier experiment

The rhythm experiment is a separate study, and nothing here is allowed to
disturb data or settings the main haptic user study already depends on. That is
structural, not a convention:

* **No shared code.** It does not import, call or share anything with
  `app/sequence_generator.py` (the controlled bimanual pilot-study stimulus
  generator) or with any other `app/` module. It has **no third-party
  dependencies** either - the Standard MIDI File writer is pure standard
  library, so the only requirement is Python 3.9+.
* **No shared data.** It writes only where you point `--out` (the launcher
  window defaults to `data/rhythm_experiment/`, a folder no other tool reads
  or writes). It never writes `config.json`, never touches a keyboard profile,
  and never writes `data/sequence/`, `data/music/`, `data/quiz/` or
  `data/MainUserStudy/`.
* **No shared stimulus pool.** Its output is not registered with
  `app/song_library.py`, so a rhythm melody can never appear in the song
  pickers used by `music_playback.py`, `student_quiz.py` or
  `student_quiz_haptic.py`.
* **No dependence on the rig's calibration.** The note range comes from the
  chosen five-finger hand position (always inside MIDI 48-72, white keys
  only), not from the active calibrated profile.

## Reading a melody back

`load.py` is the other half of `export.py`, and the only reader of the format:

```python
from melody_generator import list_melodies, load_melody

for path in list_melodies("data/rhythm_experiment"):
    melody = load_melody(path)
    print(melody.summary_line(), melody.midi_agrees)
```

The `.json` is authoritative - it carries the fingering, which a MIDI file
cannot - so a loaded melody is built from it, and the sibling `.mid` is parsed
as a cross-check (`midi_agrees`) that the two still describe the same
performance. A `.mid` with no `.json` beside it loads and plays as well, with
no fingering.

## In the launcher

`launcher.py` section **11. Rhythm Experiment** -> "Rhythm Melody Generator
(15-note)" opens `app/gui/rhythm_melody_window.py`, a GUI over this package:
the same parameters on the left, and on the right the Song Playback preview -
an 88-key piano, ten finger dots and a Play/Pause/Stop/seek transport - so a
melody can be watched and heard before its files are written. The only things
that window borrows from the rest of the project are display and playback
parts that write nothing: `note_audio.py`'s tone synthesiser and
`test_virtual_piano_led.py`'s `PianoKey`/`KeyFeedback`, the latter given an
empty LED table so no strip is touched. The second button, "Playback
Rhythm Melody" (`app/gui/rhythm_melody_player_window.py`), is a read-only
browser over a folder of generated melodies, using `load.py` and the same
preview panel. Everything below
works identically from the command line, with or without the launcher.

---

## Quick start

```bash
cd individual_project_2026/main
python -m melody_generator --seed 42 --out ./melody_out
```

That writes `melody_out/melody_seed42.{mid,json,csv,txt}` and prints the
summary. Open the `.mid` in any player to hear it.

Other common runs:

```bash
python -m melody_generator --seed 100 --count 10 --out ./melody_out --quiet
python -m melody_generator --seed 7 --key a_minor --min-notes-per-hand 5
python -m melody_generator --seed 7 --layout right_c_position --dry-run
python -m melody_generator --list-layouts
python -m melody_generator --help
```

`--dry-run` prints without writing. The same `--seed` always reproduces the
same melody, byte for byte.

---

## What it generates, and why

The brief was a melody that sounds like a *tune* - simple, natural, easy to
learn - not a legal-but-random note sequence. Four ideas do that work.

### 1. Motif and phrase structure, not note-by-note sampling

15 notes are laid out as four phrases (`4 + 4 + 4 + 3` by default) with
musical roles:

| phrase | role | how it is built |
|---|---|---|
| 1 | statement `A` | a motif grown under the melodic rules, ending on an open tone |
| 2 | answer `A'` | `A` repeated, transposed by a scale step, re-tailed or mirrored |
| 3 | contrast `B` | a new idea, biased to the other hand's register |
| 4 | cadence `A''` | `A`'s opening, then a step onto the tonic |

Repetition is what makes 15 notes memorable, and it is also what makes the
melody learnable in a fixed number of practice repetitions.

### 2. Melodic rules in scale steps

Pitch is generated in *scale-step space*, so `+1` is always the next white key.
Interval weights favour steps and thirds; a fourth is the widest interval
allowed; after any leap the melody usually steps back the other way
(leap-then-step, the standard voice-leading rule); two leaps never occur back
to back; a phrase stays inside a five-key window but must cover at least a
third. The seam between phrases obeys the same rules as the inside of one.

### 3. Fingering is a static map, not a search

Each hand keeps one five-white-key position for the whole melody. A key's
finger follows from which block it falls in, so **the same key is always played
by the same finger, in every repetition** - no thumb-under, no hand shift, no
finger substitution. Awkward fingering is not filtered out afterwards; it
cannot be generated.

```
middle_c (default) - both thumbs meet on C4, nine contiguous white keys:

   L5  L4  L3  L2  L1 | R1  R2  R3  R4  R5
   F3  G3  A3  B3  C4 | C4  D4  E4  F4  G4
   53  55  57  59  60 | 60  62  64  65  67
```

| layout | keys | notes |
|---|---|---|
| `middle_c` *(default)* | F3-G4, both hands | no gap, no thumb-under; supports C major and A minor |
| `right_c_position` | C4-G4, `R1-R5` | the simplest option |
| `left_c_position` | C3-G3, `L5-L1` | |
| `octave_echo` | C3-G3 + C4-G4 | both hands hold the same shape an octave apart; a repeated phrase becomes an octave echo in the other hand |

C4 is reachable by either thumb in `middle_c`; the hand is chosen **once per
melody** from its neighbours, so "one key, one finger" still holds.

Fingers are not forced: a melody uses whichever fingers it needs. On a two-hand
layout each hand must play at least `--min-notes-per-hand` notes (default 1);
raise it for a more evenly bimanual sequence, or set `0` to allow a one-handed
result.

### 4. Rhythm: one rule, no counting

Every onset is on a whole beat. **Every phrase ends on a held note, and some
phrase endings are followed by a one-beat rest.** Nothing else varies - no
eighth notes, no syncopation, no ties. Parallel phrases therefore share a
rhythm, reinforcing the repetition in the pitches. The closing note is three
beats. Optionally (25% of the time) one interior note is held for two beats.

### Gate time (articulation)

A key is released before the next is pressed, so exactly one note sounds at a
time. The default `fixed_gap` mode releases every note a constant **0.25 beat**
before its slot ends:

| written | sounds for | silence |
|---|---|---|
| 1 beat | 0.75 beat | 0.25 beat |
| 2 beats | 1.75 beats | 0.25 beat |
| 3 beats | 2.75 beats | 0.25 beat |

A constant *gap* rather than a constant *ratio* is what keeps a held note
sounding held - scaling a 3-beat note by 0.75 would clip 0.75 s off it and it
would read as a shortened note. `--gate-mode ratio --gate-ratio 0.75` gives the
proportional behaviour instead. At the default 60 BPM one beat is one second.

---

## Output files

`<name>.mid` - format 0, 480 ticks per beat, tempo meta set from `--bpm`, one
note-on/note-off pair per event at the real gate times. Playable anywhere.
(A player reports the file's length up to the last note-off, i.e. one release
gap shorter than `total_seconds` in the JSON, which counts the closing note's
full slot.)

`<name>.json` - the complete record: melody, fingering, every event, every
rest, the scores, the validation report, the generation statistics and the full
config that produced it. Each event carries exactly the requested fields:

```json
{
  "event_index": 0, "hand": "L", "finger": "L4",
  "midi_note": 55, "note_name": "G3",
  "onset_beat": 0.0, "duration_beats": 1.0,
  "note_on_time_sec": 0.0, "note_off_time_sec": 0.75,
  "velocity": 80,
  "phrase_index": 0, "scale_degree": 5, "is_phrase_start": true,
  "sounding_beats": 0.75, "slot_end_beat": 1.0, "slot_end_time_sec": 1.0
}
```

Rests are recorded explicitly, with their start time and length in beats:

```json
{ "rest_index": 0, "onset_beat": 5.0, "duration_beats": 1.0,
  "start_time_sec": 5.0, "end_time_sec": 6.0, "after_event_index": 3 }
```

`<name>.csv` - one row per event, notes and rests together in time order, with
a `kind` column. `<name>.txt` - a printable summary with the phrase table, a
beat-by-beat timeline and the event list.

---

## Automatic checks

A candidate is discarded unless it passes every rule. Each rule reports a
stable code, and the counts of what was rejected end up in the JSON under
`generation.rejection_counts`.

| group | codes |
|---|---|
| playability | `range_out_of_bounds` (outside MIDI 48-72), `non_white_key`, `unplayable_key`, `note_count`, `overlapping_notes`, `non_monotonic_time`, `non_positive_duration` |
| fingering | `fingering_missing`, `fingering_impossible`, `fingering_hand_mismatch`, `fingering_inconsistent` (one key, two fingers), `fingering_hand_crossing`, `finger_repetition_excessive`, `hand_switch_excessive`, `alternation_excessive`, `too_few_distinct_keys`, `unused_hand` |
| melodic motion | `leap_too_large`, `consecutive_leaps`, `leap_ratio_high`, `range_span_excessive`, `phrase_span_excessive`, `repeated_note_excessive` |
| rhythm | `no_long_note`, `too_few_long_notes`, `rhythm_monotonous`, `rhythm_too_complex`, `rhythm_illegal_duration`, `rhythm_off_grid`, `rhythm_adjacent_rests`, `rhythm_leading_rest`, `rhythm_trailing_rest`, `rhythm_illegal_rest` |
| structure | `no_phrase_structure` (no motif ever repeats and no two phrases share a shape), `pitch_entropy_high` (pitch use is near-uniform, i.e. it reads as random digits), `weak_cadence` (does not end on the tonic, or the tonic is not approached by step) |

### Scores

Two small scores rank the survivors; both are a plain weighted mean of a
handful of features in `[0, 1]`, and every feature is reported in the JSON.

* **musicality** (higher is better): singable motion, motif repetition,
  cadence, contour shape, tonal focus, note variety, phrasing.
* **difficulty** (lower is better): mean and widest interval, hand changes,
  number of distinct fingers, rhythmic load, range.

The generator builds candidates until it has a pool of 40 (or runs out of
attempts), ranks them by `musicality - 0.3 x difficulty`, and lets the seed
choose between the **best five**. Typical output scores 0.89-0.98 musicality
and 0.22-0.47 difficulty.

### Why the best five and not simply the best

Because taking only the top-scoring candidate made different seeds return the
same tune. A 40-candidate pool holds about 39 *different* melodies, and an
argmax keeps one of them: whichever shape was both easy to sample and
high-scoring won over and over. Measured across 300 seeds, one melody came up
in **9.3%** of runs and the top five in 20% - so asking for a new seed and
getting the tune you already had was ordinary, not bad luck.

Every candidate in the pool has already passed validation, `max_difficulty`
and `min_musicality`. They are all melodies this generator calls acceptable,
and picking the highest score among them was ranking by the third decimal
place. Choosing between the best five instead:

| | argmax | best five |
|---|---|---|
| distinct melodies from 300 seeds | 158 | **197** |
| most common melody | 9.3% | **3.3%** |
| mean musicality | 0.954 | 0.948 |

The choice is drawn from the same seeded stream the candidates came from, so
**the same seed still gives exactly the same tune**. Which of the five was
taken is recorded in the melody's JSON as `generation.selected_rank` and
`generation.selected_from`, so a melody says how it was chosen and not only
what it is. The number itself is `generator.SELECTION_POOL`.

---

## From Python

```python
from pathlib import Path
from dataclasses import replace
from melody_generator import GeneratorConfig, generate_sequence, generate_and_export

sequence = generate_sequence(42)
print(sequence.notes[0].finger, sequence.notes[0].note_name)

base = GeneratorConfig()
cfg = base.with_overrides(
    melody=replace(base.melody, key="a_minor", note_count=15),
    timing=replace(base.timing, bpm=60.0, release_gap_beats=0.25),
)
sequence, paths = generate_and_export(42, Path("melody_out"), cfg=cfg)
```

## Module map

| file | responsibility |
|---|---|
| `config.py` | every tunable parameter, split by concern |
| `theory.py` | note names, white-key scales, keys, scale degrees |
| `layouts.py` | hand positions and their finger maps |
| `melody.py` | motif growth, phrase transformations, cadence |
| `fingering.py` | melodic index to (MIDI note, hand, finger) |
| `rhythm.py` | beat grid, held notes, rests |
| `timing.py` | beats to seconds, gate time, event assembly |
| `validation.py` | the rejection rules |
| `features.py` | shared measurements used by rules and scores |
| `scoring.py` | musicality and difficulty |
| `midi_writer.py` | Standard MIDI File writer and reader (stdlib only) |
| `export.py` | `.mid` / `.json` / `.csv` / `.txt` |
| `generator.py` | candidate loop and the finished `MelodySequence` |
| `cli.py` | `python -m melody_generator` |

## Tests

```bash
python -m melody_generator.test_melody_generator
```

or `pytest melody_generator/test_melody_generator.py`. The suite checks
reproducibility, the 15-note/white-key/48-72 constraints, that only one note
sounds at a time, the gate times, the static fingering map, the rhythm rules,
that broken candidates are actually rejected, that the `.mid` round-trips, and
that all five layout/key combinations validate across many seeds.
