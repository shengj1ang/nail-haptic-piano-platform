"""Self-checks for the melody generator.

Runs either way::

    python -m melody_generator.test_melody_generator      # no pytest needed
    pytest melody_generator/test_melody_generator.py

Nothing here touches the study application or writes outside a temp folder.
"""

import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from .config import (
    KEYBOARD_MAX_MIDI,
    KEYBOARD_MIN_MIDI,
    GeneratorConfig,
    default_phrase_plan,
)
from .export import export_sequence, sequence_to_dict
from .generator import generate_sequence
from .layouts import LAYOUTS
from .midi_writer import read_midi_file
from .theory import KEYS, note_name
from .timing import sounding_beats
from .validation import validate

WHITE = {0, 2, 4, 5, 7, 9, 11}

#: (layout, key) pairs that must all produce valid melodies.
COMBINATIONS = [
    ("middle_c", "c_major"),
    ("middle_c", "a_minor"),
    ("right_c_position", "c_major"),
    ("left_c_position", "c_major"),
    ("octave_echo", "c_major"),
]


def _config(layout="middle_c", key="c_major", **melody):
    base = GeneratorConfig()
    return base.with_overrides(
        melody=replace(base.melody, layout=layout, key=key, **melody)
    )


def test_default_is_fifteen_notes_on_white_keys_in_range():
    sequence = generate_sequence(1)
    assert len(sequence.notes) == 15
    for note in sequence.notes:
        assert KEYBOARD_MIN_MIDI <= note.midi_note <= KEYBOARD_MAX_MIDI
        assert note.midi_note % 12 in WHITE
        assert note.note_name == note_name(note.midi_note)


def test_same_seed_reproduces_the_same_melody():
    first = sequence_to_dict(generate_sequence(2026))
    second = sequence_to_dict(generate_sequence(2026))
    first.pop("created_at_epoch_s")
    second.pop("created_at_epoch_s")
    assert first == second
    assert sequence_to_dict(generate_sequence(2027))["events"] != first["events"]


def test_one_note_sounds_at_a_time():
    for seed in range(1, 16):
        sequence = generate_sequence(seed)
        for first, second in zip(sequence.notes, sequence.notes[1:]):
            assert first.note_off_time_sec <= second.note_on_time_sec + 1e-9
            assert first.note_on_time_sec < second.note_on_time_sec
        for note in sequence.notes:
            assert note.note_off_time_sec > note.note_on_time_sec


def test_gate_times_match_the_documented_articulation():
    cfg = GeneratorConfig()
    assert sounding_beats(1.0, cfg.timing) == 0.75
    assert sounding_beats(2.0, cfg.timing) == 1.75
    assert sounding_beats(3.0, cfg.timing) == 2.75
    ratio = replace(cfg.timing, gate_mode="ratio")
    assert sounding_beats(1.0, ratio) == 0.75
    assert abs(sounding_beats(3.0, ratio) - 2.25) < 1e-9

    sequence = generate_sequence(5)
    for note in sequence.notes:
        held = note.note_off_time_sec - note.note_on_time_sec
        assert abs(held - (note.duration_beats - 0.25)) < 1e-6


def test_every_key_keeps_one_finger_and_the_finger_can_reach_it():
    for layout_name, key_name in COMBINATIONS:
        sequence = generate_sequence(3, _config(layout_name, key_name))
        layout = LAYOUTS[layout_name]
        reachable = {(slot.midi, slot.label) for slot in layout.slots}
        seen = {}
        for note in sequence.notes:
            assert (note.midi_note, note.finger) in reachable
            assert note.finger[0] == note.hand
            assert seen.setdefault(note.midi_note, note.finger) == note.finger


def test_rhythm_has_held_notes_rests_and_a_whole_beat_grid():
    for seed in range(1, 16):
        sequence = generate_sequence(seed)
        durations = [note.duration_beats for note in sequence.notes]
        assert any(d >= 2 for d in durations), "no 2- or 3-beat held note"
        assert len(set(durations)) >= 2, "rhythm is monotonous"
        assert 1 <= len(sequence.rests) <= 3
        for note in sequence.notes:
            assert note.onset_beat == round(note.onset_beat)
            assert note.duration_beats in (1, 2, 3)
        for rest in sequence.rests:
            assert rest.duration_beats == 1
            assert 0 < rest.onset_beat < sequence.total_beats


def test_every_combination_validates_over_many_seeds():
    for layout_name, key_name in COMBINATIONS:
        cfg = _config(layout_name, key_name)
        for seed in range(1, 11):
            sequence = generate_sequence(seed, cfg)
            assert sequence.validation.ok, sequence.validation.issues
            assert sequence.scores.musicality >= cfg.scoring.min_musicality
            assert sequence.scores.difficulty <= cfg.scoring.max_difficulty


def test_two_hand_layouts_use_both_hands():
    for layout_name in ("middle_c", "octave_echo"):
        sequence = generate_sequence(4, _config(layout_name))
        assert {note.hand for note in sequence.notes} == {"L", "R"}


def test_melody_ends_on_the_tonic():
    for layout_name, key_name in COMBINATIONS:
        sequence = generate_sequence(6, _config(layout_name, key_name))
        assert sequence.notes[-1].midi_note % 12 == KEYS[key_name].tonic_pc


def test_broken_candidates_are_rejected():
    """Mutate a good sequence and confirm the rules catch each fault."""
    sequence = generate_sequence(11)
    cfg = sequence.config

    def codes_for(notes):
        report = validate(
            notes,
            sequence.rests,
            sequence.rhythm,
            sequence.line,
            _space(sequence),
            sequence.layout,
            cfg,
        )
        return report.codes()

    out_of_range = list(sequence.notes)
    out_of_range[0] = replace(out_of_range[0], midi_note=95, note_name="B6")
    assert "range_out_of_bounds" in codes_for(tuple(out_of_range))

    black_key = list(sequence.notes)
    black_key[0] = replace(black_key[0], midi_note=61, note_name="C#4")
    assert "non_white_key" in codes_for(tuple(black_key))

    wrong_finger = list(sequence.notes)
    wrong_finger[0] = replace(wrong_finger[0], finger="L5", hand="L")
    assert "fingering_impossible" in codes_for(tuple(wrong_finger))

    overlapping = list(sequence.notes)
    overlapping[0] = replace(overlapping[0], note_off_time_sec=99.0)
    assert "overlapping_notes" in codes_for(tuple(overlapping))

    all_short = tuple(
        replace(note, duration_beats=1.0) for note in sequence.notes
    )
    assert "no_long_note" in codes_for(all_short)


def _space(sequence):
    from .melody import build_pitch_space

    return build_pitch_space(sequence.layout, sequence.key)


def test_midi_file_round_trips():
    sequence = generate_sequence(8)
    out = Path(tempfile.mkdtemp(prefix="melodygen_"))
    try:
        paths = export_sequence(sequence, out)
        parsed = read_midi_file(paths["midi"])
        assert abs(parsed["bpm"] - sequence.config.timing.bpm) < 0.01
        assert len(parsed["notes"]) == len(sequence.notes)
        for written, note in zip(parsed["notes"], sequence.notes):
            assert written["midi_note"] == note.midi_note
            assert written["velocity"] == note.velocity
            assert abs(written["on_beat"] - note.onset_beat) < 1e-6
            assert abs(
                written["off_beat"] - (note.onset_beat + note.sounding_beats)
            ) < 1e-6
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_exports_are_written_and_complete():
    sequence = generate_sequence(9)
    out = Path(tempfile.mkdtemp(prefix="melodygen_"))
    try:
        paths = export_sequence(sequence, out)
        for kind in ("midi", "json", "csv", "summary"):
            assert paths[kind].exists() and paths[kind].stat().st_size > 0

        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert len(data["events"]) == 15
        assert len(data["rests"]) == len(sequence.rests)
        required = {
            "event_index",
            "hand",
            "finger",
            "midi_note",
            "note_name",
            "onset_beat",
            "duration_beats",
            "note_on_time_sec",
            "note_off_time_sec",
            "velocity",
        }
        assert required <= set(data["events"][0])
        assert {"onset_beat", "duration_beats", "start_time_sec", "end_time_sec"} <= set(
            data["rests"][0]
        )
        assert data["validation"]["ok"] is True

        rows = paths["csv"].read_text(encoding="utf-8").strip().splitlines()
        assert len(rows) == 1 + len(sequence.notes) + len(sequence.rests)
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_other_lengths_and_tempi_still_work():
    cfg = _config(note_count=12)
    sequence = generate_sequence(3, cfg)
    assert len(sequence.notes) == 12

    base = GeneratorConfig()
    faster = base.with_overrides(timing=replace(base.timing, bpm=90.0))
    sequence = generate_sequence(3, faster)
    assert abs(sequence.beat_seconds - 60.0 / 90.0) < 1e-9
    first = sequence.notes[0]
    assert abs(first.note_on_time_sec - first.onset_beat * (60.0 / 90.0)) < 1e-6


def test_phrase_plan_splits_sensibly():
    for count in range(6, 25):
        plan = default_phrase_plan(count)
        assert sum(plan) == count
        assert min(plan) >= 3
        assert len(plan) >= 2


def test_cli_writes_files():
    from .cli import main

    out = Path(tempfile.mkdtemp(prefix="melodygen_cli_"))
    try:
        code = main(["--seed", "21", "--count", "2", "--out", str(out), "--quiet"])
        assert code == 0
        assert sorted(p.name for p in out.glob("*.mid")) == [
            "melody_seed21.mid",
            "melody_seed22.mid",
        ]
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _run_all() -> int:
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for name, test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print(f"FAIL {name}: {error}")
        except Exception as error:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"ERROR {name}: {type(error).__name__}: {error}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
