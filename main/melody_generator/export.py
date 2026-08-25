"""Output files: a playable .mid, a complete .json, a .csv and a text summary.

The four files are written together and share a stem, so a sequence is always
one name: ``melody_seed42.mid`` / ``.json`` / ``.csv`` / ``.txt``.

* ``.mid``   note-on/note-off with the real gate times - play it to hear it.
* ``.json``  everything the experiment needs, including the config and the
             validation report, so a sequence is fully traceable.
* ``.csv``   one row per event, notes and rests, for eyeballing in a spreadsheet.
* ``.txt``   a printable summary with a beat-by-beat timeline.
"""

import csv
import json
from pathlib import Path
from typing import Dict, List

from .midi_writer import MidiNote, write_midi_file
from .theory import note_name
from .timing import sounding_beats

#: The columns the experiment asked for, plus the context needed to check a
#: sequence by hand. Rests appear as their own rows, marked in ``kind``.
CSV_COLUMNS = (
    "kind",
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
    "sounding_beats",
    "slot_end_time_sec",
    "phrase_index",
    "scale_degree",
    "is_phrase_start",
)


def _articulation_table(sequence) -> Dict[str, float]:
    cfg = sequence.config
    return {
        str(beats): round(sounding_beats(float(beats), cfg.timing), 6)
        for beats in cfg.rhythm.note_beats
    }


def sequence_to_dict(sequence) -> Dict[str, object]:
    cfg = sequence.config
    phrase_notes: List[Dict[str, object]] = []
    cursor = 0
    for phrase in sequence.line.phrases:
        span = range(cursor, cursor + len(phrase.indices))
        phrase_notes.append(
            {
                "phrase_index": phrase.position,
                "role": phrase.role,
                "transformation": phrase.op,
                "hand": (
                    sequence.fingering.phrase_hands[phrase.position]
                    if sequence.layout.mode == "echo"
                    else None
                ),
                "event_indices": list(span),
                "notes": [sequence.notes[i].note_name for i in span],
                "fingers": [sequence.notes[i].finger for i in span],
                "scale_degrees": [sequence.notes[i].scale_degree for i in span],
            }
        )
        cursor += len(phrase.indices)

    return {
        "format": "melody_generator/1",
        "name": sequence.name,
        "seed": sequence.seed,
        "created_at_epoch_s": sequence.created_at_epoch_s,
        "music": {
            "key": sequence.key.name,
            "key_display": sequence.key.display,
            "layout": sequence.layout.name,
            "layout_description": sequence.layout.description,
            "layout_mode": sequence.layout.mode,
            "bpm": cfg.timing.bpm,
            "beat_seconds": round(sequence.beat_seconds, 6),
            "note_count": len(sequence.notes),
            "rest_count": len(sequence.rests),
            "total_beats": round(sequence.total_beats, 6),
            "total_seconds": round(sequence.total_seconds, 6),
            "phrase_plan": list(sequence.phrase_plan),
            "midi_range": [
                min(n.midi_note for n in sequence.notes),
                max(n.midi_note for n in sequence.notes),
            ],
        },
        "articulation": {
            "gate_mode": cfg.timing.gate_mode,
            "release_gap_beats": cfg.timing.release_gap_beats,
            "gate_ratio": cfg.timing.gate_ratio,
            "sounding_beats_by_duration": _articulation_table(sequence),
        },
        "fingering": {
            "key_to_finger": {
                str(midi): label
                for midi, label in sorted(sequence.fingering.key_to_finger.items())
            },
            "key_names": {
                str(midi): note_name(midi)
                for midi in sorted(sequence.fingering.key_to_finger)
            },
            "fingers_used": list(sequence.fingers_used()),
            # only meaningful when a whole phrase belongs to one hand
            "phrase_hands": (
                list(sequence.fingering.phrase_hands)
                if sequence.layout.mode == "echo"
                else None
            ),
            "shared_key_decisions": {
                str(midi): hand
                for midi, hand in sequence.fingering.shared_key_decisions.items()
            },
        },
        "phrases": phrase_notes,
        "events": [note.to_dict() for note in sequence.notes],
        "rests": [rest.to_dict() for rest in sequence.rests],
        "scores": sequence.scores.to_dict(),
        "validation": sequence.validation.to_dict(),
        "generation": sequence.stats.to_dict(),
        "config": cfg.to_dict(),
    }


def write_json(path: Path, sequence) -> Path:
    path = Path(path)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(sequence_to_dict(sequence), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def write_csv(path: Path, sequence) -> Path:
    path = Path(path)
    rows: List[Dict[str, object]] = []
    for note in sequence.notes:
        rows.append(
            {
                "kind": "note",
                "event_index": note.event_index,
                "hand": note.hand,
                "finger": note.finger,
                "midi_note": note.midi_note,
                "note_name": note.note_name,
                "onset_beat": note.onset_beat,
                "duration_beats": note.duration_beats,
                "note_on_time_sec": f"{note.note_on_time_sec:.3f}",
                "note_off_time_sec": f"{note.note_off_time_sec:.3f}",
                "velocity": note.velocity,
                "sounding_beats": note.sounding_beats,
                "slot_end_time_sec": f"{note.slot_end_time_sec:.3f}",
                "phrase_index": note.phrase_index,
                "scale_degree": note.scale_degree,
                "is_phrase_start": int(note.is_phrase_start),
            }
        )
    for rest in sequence.rests:
        rows.append(
            {
                "kind": "rest",
                "event_index": "",
                "hand": "",
                "finger": "",
                "midi_note": "",
                "note_name": "",
                "onset_beat": rest.onset_beat,
                "duration_beats": rest.duration_beats,
                "note_on_time_sec": "",
                "note_off_time_sec": "",
                "velocity": "",
                "sounding_beats": "",
                "slot_end_time_sec": f"{rest.end_time_sec:.3f}",
                "phrase_index": "",
                "scale_degree": "",
                "is_phrase_start": "",
            }
        )
    rows.sort(key=lambda row: (float(row["onset_beat"]), row["kind"] == "rest"))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _timeline_rows(sequence, beats_per_row: int = 12) -> List[str]:
    """A cell per beat: note name on its onset, '~' held, '.' rest."""
    total = int(round(sequence.total_beats))
    cells: List[str] = []
    for beat in range(total):
        cell = "."
        for note in sequence.notes:
            start = int(round(note.onset_beat))
            end = start + int(round(note.duration_beats))
            if start == beat:
                cell = f"{note.note_name}/{note.finger}"
                break
            if start < beat < end:
                cell = "~"
                break
        cells.append(cell)

    lines: List[str] = []
    for start in range(0, total, beats_per_row):
        chunk = cells[start : start + beats_per_row]
        header = " ".join(f"{start + i:>7d}" for i in range(len(chunk)))
        body = " ".join(f"{cell:>7s}" for cell in chunk)
        lines.append(f"  beat {header}")
        lines.append(f"       {body}")
        lines.append("")
    return lines


def summary_text(sequence) -> str:
    cfg = sequence.config
    articulation = _articulation_table(sequence)
    lines: List[str] = []
    lines.append(f"{sequence.name}")
    lines.append("=" * len(sequence.name))
    lines.append("")
    lines.append(f"seed            {sequence.seed}")
    lines.append(f"key             {sequence.key.display}")
    lines.append(f"hand position   {sequence.layout.name} ({sequence.layout.mode} mode)")
    lines.append(f"                {sequence.layout.description}")
    lines.append(
        f"tempo           {cfg.timing.bpm:g} BPM "
        f"(1 beat = {sequence.beat_seconds:.3f} s)"
    )
    lines.append(
        f"length          {len(sequence.notes)} notes, {len(sequence.rests)} rests, "
        f"{sequence.total_beats:g} beats = {sequence.total_seconds:.3f} s"
    )
    lines.append(
        "articulation    "
        + f"{cfg.timing.gate_mode}, "
        + ", ".join(
            f"{beats}-beat note sounds {held:g}" for beats, held in articulation.items()
        )
    )
    lines.append(
        f"fingers used    {' '.join(sequence.fingers_used())}"
    )
    lines.append(
        "fingering map   "
        + "  ".join(
            f"{note_name(midi)}={label}"
            for midi, label in sorted(sequence.fingering.key_to_finger.items())
        )
    )
    lines.append(
        f"scores          musicality {sequence.scores.musicality:.3f}, "
        f"difficulty {sequence.scores.difficulty:.3f}"
    )
    status = "PASS" if sequence.validation.ok else "FAIL"
    lines.append(
        f"validation      {status} ({len(sequence.validation.issues)} issues)"
    )
    for issue in sequence.validation.issues:
        lines.append(f"                {issue}")
    lines.append(
        f"candidates      {sequence.stats.accepted} accepted of "
        f"{sequence.stats.attempts} attempts"
    )
    lines.append("")

    lines.append("phrases")
    cursor = 0
    for phrase in sequence.line.phrases:
        span = range(cursor, cursor + len(phrase.indices))
        notes = " ".join(f"{sequence.notes[i].note_name}" for i in span)
        fingers = " ".join(sequence.notes[i].finger for i in span)
        hand = (
            f" [{sequence.fingering.phrase_hands[phrase.position]}]"
            if sequence.layout.mode == "echo"
            else ""
        )
        lines.append(
            f"  {phrase.position}  {phrase.role:<10s} {phrase.op:<12s}{hand}"
        )
        lines.append(f"       notes   {notes}")
        lines.append(f"       fingers {fingers}")
        cursor += len(phrase.indices)
    lines.append("")

    lines.append("timeline")
    lines.extend(_timeline_rows(sequence))

    lines.append("events")
    header = (
        f"  {'#':>2s} {'hand':>4s} {'fin':>4s} {'note':>5s} {'midi':>5s} "
        f"{'onset_b':>8s} {'dur_b':>6s} {'on_s':>8s} {'off_s':>8s} {'vel':>4s}"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for note in sequence.notes:
        lines.append(
            f"  {note.event_index:>2d} {note.hand:>4s} {note.finger:>4s} "
            f"{note.note_name:>5s} {note.midi_note:>5d} "
            f"{note.onset_beat:>8.2f} {note.duration_beats:>6.2f} "
            f"{note.note_on_time_sec:>8.3f} {note.note_off_time_sec:>8.3f} "
            f"{note.velocity:>4d}"
        )
    if sequence.rests:
        lines.append("")
        lines.append("rests")
        lines.append(
            f"  {'#':>2s} {'onset_b':>8s} {'dur_b':>6s} {'start_s':>8s} {'end_s':>8s}"
            f"  after event"
        )
        for rest in sequence.rests:
            lines.append(
                f"  {rest.rest_index:>2d} {rest.onset_beat:>8.2f} "
                f"{rest.duration_beats:>6.2f} {rest.start_time_sec:>8.3f} "
                f"{rest.end_time_sec:>8.3f}  {rest.after_event_index}"
            )
    lines.append("")
    return "\n".join(lines)


def write_summary(path: Path, sequence) -> Path:
    path = Path(path)
    path.write_text(summary_text(sequence), encoding="utf-8")
    return path


def write_midi(path: Path, sequence, split_hand_channels: bool = False) -> Path:
    notes = [
        MidiNote(
            midi_note=note.midi_note,
            velocity=note.velocity,
            on_beat=note.onset_beat,
            off_beat=note.onset_beat + note.sounding_beats,
            channel=(1 if split_hand_channels and note.hand == "L" else 0),
        )
        for note in sequence.notes
    ]
    return write_midi_file(
        Path(path),
        notes,
        bpm=sequence.config.timing.bpm,
        track_name=sequence.name,
    )


def export_sequence(
    sequence, out_dir: Path, split_hand_channels: bool = False
) -> Dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / sequence.name
    return {
        "midi": write_midi(
            stem.with_suffix(".mid"), sequence, split_hand_channels
        ),
        "json": write_json(stem.with_suffix(".json"), sequence),
        "csv": write_csv(stem.with_suffix(".csv"), sequence),
        "summary": write_summary(stem.with_suffix(".txt"), sequence),
    }
