# Main User Study Design

This document defines the planned main human experiment for the final project.
It has two purposes:

1. Provide a stable experimental protocol for the final report.
2. Give Claude Code a concrete implementation target for the experiment software.

The current project should not treat old experiments under `individual_project_2026/experiments/`
as the final protocol. Those earlier experiments are useful as pilot and validation work,
but the main user study is defined here.

## Research Aim

The study evaluates whether nail-mounted finger-specific vibrotactile cues can support
early-stage piano-like sensorimotor learning.

The experiment is not designed to evaluate expressive piano performance, rhythm reproduction,
or note duration control. It is a controlled cue-response task using short melody-like keyboard
sequences.

Participants are asked to press the cued key with the indicated finger as accurately and
promptly as possible. The keyboard may produce sound as natural auditory feedback, but
rhythmic accuracy and key release duration are not scored.

## Research Questions

### RQ1: Does explicit finger guidance improve complete action performance?

This compares the baseline condition against the two finger-guidance conditions.

- A: backlight only, no finger cue.
- B: backlight plus visual finger cue.
- C: backlight plus haptic finger cue.

The key question is whether finger guidance improves the ability to press the correct key
with the intended finger.

### RQ2: When finger information is provided, how does haptic guidance compare with visual guidance?

This is the main comparison:

- B: visual finger information.
- C: vibrotactile finger information.

Both conditions provide the same information: which key to press and which finger to use.
Only the finger-cue modality changes.

This is cleaner than prior work such as Coscia and Al Borno, where the visual condition
showed target keys but did not provide explicit finger-use information, while the vibrotactile
condition conveyed finger sequence information.

## Experimental Conditions

The target key is always shown using the keyboard backlight. This controls for music-reading
ability and ensures that all participants know which key should be pressed.

| Condition | Name | Key cue | Finger cue | Purpose |
| --- | --- | --- | --- | --- |
| A | Backlight only | Keyboard backlight | None | Baseline without explicit finger guidance |
| B | Visual finger cue | Keyboard backlight | Screen cue, e.g. `R2` or highlighted hand/finger | Visual finger-guidance comparison |
| C | Haptic finger cue | Keyboard backlight | Nail-mounted vibration on target finger | Main haptic condition |

Important implementation rule:

- In A, do not show the target finger anywhere.
- In B, show the target finger visually but do not vibrate.
- In C, vibrate the target finger but do not show the target finger visually.
- In all conditions, the target key backlight is present.

## Experimental Design

Use a within-subject design. Every participant experiences all three guidance conditions.

The order of A/B/C must be counterbalanced across participants using the six possible
condition orders:

| Assignment group | Condition order |
| --- | --- |
| 1 | A B C |
| 2 | A C B |
| 3 | B A C |
| 4 | B C A |
| 5 | C A B |
| 6 | C B A |

Repeat these six rows for participants 7-12, 13-18, and so on.

Condition order must be saved in the data log for every participant.

## Sequence Sets

Participants should not choose their own songs. The experiment uses a fixed pool of
controlled melody-like sequences. This avoids confounds caused by different participants
or different conditions receiving easier or more familiar music.

Prepare nine sequences:

- Level 1: `L1-A`, `L1-B`, `L1-C`
- Level 2: `L2-A`, `L2-B`, `L2-C`
- Level 3: `L3-A`, `L3-B`, `L3-C`

The letters A/B/C here are sequence-set labels, not experimental conditions. To avoid
confusion in code, consider naming the sequence sets `X`, `Y`, and `Z`:

- `L1-X`, `L1-Y`, `L1-Z`
- `L2-X`, `L2-Y`, `L2-Z`
- `L3-X`, `L3-Y`, `L3-Z`

The sequence assignment is rotated across participants so that each condition is paired
with each sequence set across the study.

Example rotation:

| Participant group | A uses | B uses | C uses |
| --- | --- | --- | --- |
| 1 | X | Y | Z |
| 2 | Y | Z | X |
| 3 | Z | X | Y |
| 4 | X | Z | Y |
| 5 | Y | X | Z |
| 6 | Z | Y | X |

For each participant, the same sequence set applies across all difficulty levels within
a condition. For example, if participant 1 uses set Y for condition B, then condition B
uses `L1-Y`, `L2-Y`, and `L3-Y`.

## Repetition Structure

Within each condition and level, repeat the same sequence three times. This allows
learning progression to be measured from trial 1 to trial 3 on the same material.

Example for one condition:

- Level 1 sequence: 3 repeated trials.
- Level 2 sequence: 3 repeated trials.
- Level 3 sequence: 3 repeated trials.

Recommended full design:

- A/B/C conditions.
- 3 difficulty levels.
- 3 repeated trials per level.
- 27 trials per participant.

If session duration becomes too long, A can be reduced to 1-2 repeats per level because
A is primarily a baseline. However, the full 27-trial design is statistically cleaner.

## Task Difficulty Definition

Difficulty should not be defined mainly by note count. A short sequence can be difficult
if it requires large jumps, black-key targeting, or frequent hand switching; a longer
sequence can still be easy if it stays in a fixed hand position.

The study defines difficulty using spatial-motor descriptors commonly used in piano
performance difficulty literature:

- Note count.
- Pitch range.
- Mean inter-note interval.
- Maximum inter-note interval.
- Hand switch rate.
- Finger jump rate.
- Direction change rate.
- Black-key count or black-key proportion.
- Position shift count.
- Chord count.

The selected sequences should be short enough to avoid fatigue but long enough to feel
like a meaningful learning trial. Recommended length: 24-32 notes per sequence.

### Level 1: Single-Hand Fixed Position

Purpose: basic key-finger mapping with minimal spatial complexity.

Suggested constraints:

- One hand only.
- Fixed five-finger position.
- White keys only.
- No hand switch.
- No hand position shift.
- No chords.
- Mostly stepwise movement and small skips.
- Similar note count across `L1-X`, `L1-Y`, and `L1-Z`.

Suggested descriptor targets:

| Descriptor | Target |
| --- | --- |
| Note count | 24-28 |
| Hands used | 1 |
| Hand switch rate | 0 |
| Black-key proportion | 0 |
| Chord count | 0 |
| Position shift count | 0 |
| Pitch range | One five-finger position |
| Mean interval | Low |
| Maximum interval | Low to moderate |

### Level 2: Two-Hand Alternation, White Keys

Purpose: add hand-selection demand while keeping pitch targets visually simple.

Suggested constraints:

- Both hands are used.
- White keys only.
- No simultaneous notes.
- Each hand mostly stays in a local fixed position.
- Alternation between hands occurs, but not on every note.
- No chords.

Suggested descriptor targets:

| Descriptor | Target |
| --- | --- |
| Note count | 24-28 |
| Hands used | 2 |
| Hand switch rate | Moderate |
| Black-key proportion | 0 |
| Chord count | 0 |
| Position shift count | 0 or minimal |
| Pitch range | Larger than Level 1 but constrained by the small keyboard |
| Mean interval | Moderate |
| Maximum interval | Moderate |

### Level 3: Two-Hand Alternation With Black Keys

Purpose: increase spatial and fingering complexity without requiring a larger keyboard.

Since the available keyboard is limited in size, Level 3 should not rely only on larger
pitch range. Instead, it should introduce black-key targets and more complex switching.

Suggested constraints:

- Both hands are used.
- Includes some black keys.
- More frequent hand switching than Level 2.
- More direction changes and non-adjacent finger transitions.
- No chords for the main study unless the camera/finger-detection pipeline is ready for simultaneous notes.
- Hand position shifts should be controlled and explicitly logged if used.

Suggested descriptor targets:

| Descriptor | Target |
| --- | --- |
| Note count | 24-32 |
| Hands used | 2 |
| Hand switch rate | High |
| Black-key proportion | Low to moderate, e.g. 15-30 percent |
| Chord count | 0 for main study |
| Position shift count | 0 to small, but controlled |
| Pitch range | Similar to or slightly larger than Level 2 |
| Mean interval | Moderate to high |
| Maximum interval | Higher than Level 2 where feasible |
| Direction change rate | Higher than Level 2 |

## Sequence Selection Procedure

Recommended workflow:

1. Prepare more candidate sequences than needed, e.g. 5-6 candidates per level.
2. Compute the difficulty descriptors for each candidate.
3. Select three sequences per level whose descriptors are closely matched within that level.
4. Ensure the three levels show increasing spatial-motor difficulty.
5. Save both the sequence content and descriptor table.

The final report can state that nine controlled melody-like sequences were selected
using quantitative difficulty descriptors. This is more defensible than saying that
sequences were selected subjectively.

## Sequence File Format

Use a structured JSON or CSV format. JSON is preferred because it can store per-note
metadata and sequence-level descriptors clearly.

Example:

```json
{
  "sequence_id": "L2-X",
  "level": 2,
  "set_id": "X",
  "display_name": "Level 2 sequence X",
  "description": "Two-hand alternating white-key sequence",
  "descriptors": {
    "note_count": 26,
    "pitch_range_semitones": 12,
    "mean_interval_semitones": 3.1,
    "max_interval_semitones": 7,
    "hand_switch_rate": 0.42,
    "finger_jump_rate": 0.31,
    "direction_change_rate": 0.48,
    "black_key_proportion": 0.0,
    "position_shift_count": 0,
    "chord_count": 0
  },
  "events": [
    {
      "index": 0,
      "note": 60,
      "note_name": "C4",
      "hand": "R",
      "finger": "R1",
      "is_black_key": false
    }
  ]
}
```

## Trial Flow

Each note event follows this flow:

1. Show target key using keyboard backlight.
2. Depending on condition, show or activate the finger cue.
3. Record cue onset time.
4. Participant presses one key.
5. Record the first MIDI `note_on` after cue onset.
6. Clear the active cues.
7. Wait for key release if possible.
8. Add a short fixed gap before the next cue, e.g. 300-700 ms.

The system should not require a fixed note duration. Participants should be instructed:

> Press the cued key once using the indicated finger. You do not need to hold the key
> for a specific duration. Release the key naturally before the next cue.

The first valid keypress is the response used for note accuracy and cue-to-keypress time.

## Haptic Cue Design

The main study should use one fixed haptic cue design. Do not compare "one buzz vs two
buzzes" as part of the main experiment. That would create a separate tactile vocabulary
experiment and dilute the main research question.

The haptic cue should encode finger identity through spatial location:

- The target finger actuator vibrates.
- No pulse pattern is used to encode additional information.
- The cue starts at the same logical cue onset as the backlight and visual cue.

### Required Software Option

The experiment software must expose haptic cue settings in the GUI.

The GUI should allow the experimenter to choose:

- Continuous vibration until response.
- Fixed-duration vibration.

Recommended controls:

| Control | Type | Default | Notes |
| --- | --- | --- | --- |
| `haptic_mode` | two-option selector | `continuous_until_response` | Options: `continuous_until_response` or `fixed_duration` |
| `fixed_duration_ms` | number input/text box | 500 | Enabled only when `fixed_duration` is selected |
| `timeout_s` | number input | 5 | Maximum response window |

Do not expose repeated-pulse controls or amplitude controls in the main study GUI.
Use a stable internal default amplitude, such as the value already used in the
working haptic cue implementation, and log that value automatically.

Default for the main experiment:

- `haptic_mode = continuous_until_response`
- internal default amplitude = 80, or the empirically stable value used by the hardware
- timeout = 5 s

Rationale:

- Continuous haptic cue is directly comparable to a visual finger cue that remains visible
  until response.
- It avoids adding pulse-pattern interpretation as another learning demand.
- It is robust for novice participants.

If comfort becomes an issue during pilot testing, switch the default to:

- `haptic_mode = fixed_duration`
- `fixed_duration_ms = 500`

Do not change this setting mid-study unless the change is explicitly recorded and the
affected data are treated as pilot data.

## LRA vs ERM Decision

Do not include actuator type as a factor in the main study.

Existing local pilot/validation data already compare LRA and ERM:

- Location: `individual_project_2026/experiments/human_reaction_experiment/analysis_output/analysis_report.md`
- Participants: 5
- Trials: 150 total
- LRA reaction time: 276.98 +/- 44.92 ms
- ERM reaction time: 281.65 +/- 49.92 ms
- Accuracy: 100 percent for both
- Preference: LRA preferred by 5/5 participants

Use this as technical/perceptual validation to justify selecting LRA for the main study.
The main study should focus on guidance conditions A/B/C, not actuator comparison.

## Software Requirements For Claude Code

The existing quiz system already has much of the required infrastructure:

- MIDI logging.
- Keyboard backlight cueing.
- Visual finger cue.
- Haptic finger cue.
- Video/camera recording.
- Offline finger matching and analysis.

The main user study software should be built by extending the existing quiz pipeline,
not by creating an unrelated tool from scratch.

### Required Entry Point

Create a clear entry point for the main study, for example:

- `main_user_study.py`
- or a launcher option named `Main User Study`.

### Setup Screen

The GUI must allow the experimenter to enter or select:

- Participant ID, e.g. `P01`.
- Session date/time, auto-filled.
- MIDI input port.
- Keyboard/camera profile.
- LED connection.
- Guidance condition order, auto-generated from participant ID but editable if needed.
- Sequence assignment, auto-generated from participant ID but editable if needed.
- Number of repeats per condition/level.
- Timeout duration.
- Haptic cue settings.

### Run Screen

The experimenter should see:

- Current participant ID.
- Current condition A/B/C.
- Current difficulty level.
- Current sequence ID.
- Current repeat number.
- Current note index.
- Start/pause/cancel controls.
- Hardware connection status.

The participant should see only the appropriate cue information:

- A: no finger information.
- B: visual finger information.
- C: no visual finger information; haptic cue only.

### Automatic Scheduling

The software should automatically generate the trial schedule:

```text
participant_id -> condition_order -> sequence_assignment -> levels -> repeats -> notes
```

For example:

```text
P01:
  condition_order = A, B, C
  A uses set X
  B uses set Y
  C uses set Z
```

Then the schedule expands to:

```text
A / L1-X / repeat 1
A / L1-X / repeat 2
A / L1-X / repeat 3
A / L2-X / repeat 1
...
C / L3-Z / repeat 3
```

Recommended order within each condition:

- Level 1, then Level 2, then Level 3.

This matches the oral presentation plan and avoids surprising novice participants with
the hardest material first. Since condition order is counterbalanced, the main order
confound is already controlled.

### Data Logging

The software must log both trial-level and note-level data.

At minimum, save:

- `participant_id`
- `condition_order`
- `condition`
- `sequence_set_id`
- `sequence_id`
- `level`
- `repeat_index`
- `note_index`
- `target_note`
- `target_note_name`
- `target_finger`
- `target_hand`
- `target_is_black_key`
- `cue_onset_time`
- `led_on_time`
- `visual_cue_on_time`
- `haptic_command_time`
- `haptic_mode`
- `haptic_fixed_duration_ms`, if fixed-duration mode is selected
- `haptic_amplitude`, logged from the internal default value
- `actual_note`
- `actual_note_name`
- `keypress_time`
- `release_time`, if available
- `timing_error_s`
- `note_correct`
- `actual_finger`, after offline analysis
- `finger_correct`, after offline analysis
- `correct_action`, after offline analysis
- `timed_out`
- `false_start_count`

`correct_action` should be true only when:

```text
note_correct == true AND finger_correct == true
```

### Data Folder Structure

Recommended structure:

```text
data/main_user_study/
  participants/
    P01/
      schedule.json
      session_meta.json
      raw/
        midi_raw.json
        performance.mp4
        sync.json
      trials.csv
      note_events.csv
      results.json
      analysis_summary.json
  sequences/
    L1-X.json
    L1-Y.json
    L1-Z.json
    L2-X.json
    L2-Y.json
    L2-Z.json
    L3-X.json
    L3-Y.json
    L3-Z.json
    sequence_descriptors.csv
```

### Analysis Output

The analysis should compute, per participant, condition, level, and repeat:

- Note accuracy.
- Finger accuracy.
- Correct action rate.
- Mean cue-to-keypress time for correct notes.
- Mean cue-to-keypress time for correct actions.
- Timeout/miss rate.

It should also support export of a combined CSV for statistical analysis.

Recommended statistical model for the final report:

- Fixed effects: condition, level, repeat.
- Random effect: participant.
- Main planned comparison: B vs C.
- Baseline comparison: A vs B/C.

If the sample size is small, report descriptive summaries and paired comparisons in
addition to any mixed-model analysis.

## Subjective Questionnaire

After each condition, ask short 5-point Likert questions:

- The finger cue was clear.
- The task felt easy to follow.
- The feedback was comfortable.
- I felt confident using the guidance.
- I would prefer this guidance method for learning.

For A, adapt wording to avoid asking about a finger cue that does not exist:

- The guidance was clear.
- The task felt easy to follow.
- I felt confident during the task.

At the end, ask preference:

- Which guidance method did you prefer: A, B, or C?
- Why?

## Participant Instructions

Suggested wording:

> In this experiment, the lit key shows which key to press. In some conditions,
> you will also receive information about which finger to use, either visually
> or through vibration on that finger.
>
> For each cue, press the lit key once using the indicated finger if a finger cue
> is provided. Press as accurately and promptly as you can. You do not need to
> hold the key for a specific duration. Release it naturally and wait for the next cue.
>
> The keyboard may produce sound, but you are not being tested on rhythm or note duration.

For condition A:

> In this condition, only the target key is shown. Use whichever finger feels natural.

## Report-Writing Notes

Important claims to make:

- This is a controlled cue-response sensorimotor learning task, not a full musical performance test.
- Note accuracy measures task correctness.
- Finger accuracy measures motor strategy and intended finger use.
- Correct action rate combines both requirements.
- Cue-to-keypress time measures response efficiency.
- Rhythm and note duration are intentionally excluded to isolate key-finger mapping.

Suggested method wording:

> Task difficulty was operationalised using quantitative spatial-motor descriptors,
> including hand usage, pitch range, inter-note interval size, hand-switch rate,
> finger-jump rate, direction-change rate, and black-key proportion, while keeping
> sequence length approximately constant across levels.

Suggested haptic wording:

> Vibrotactile cues encoded finger identity through spatial location. In the main
> configuration, the actuator on the target finger remained active during the response
> window and was cleared after the first keypress or timeout. No additional pulse
> pattern was used, so that the tactile channel conveyed only the target finger.

## References To Use

See `reference/experiment_design_literature.md` for a working literature index.

Key references:

- Coscia and Al Borno, 2025, "Vibrotactile versus Visual Stimulation in Learning the Piano"
- Lee et al., 2025, "Hapticus"
- Yuksel et al., 2016, "Learn Piano with BACh"
- Nakamura and Yoshii, 2018, "Statistical Piano Reduction Controlling Performance Difficulty"
