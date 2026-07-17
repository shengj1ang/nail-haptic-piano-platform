"""One-off (idempotent) migration: every stored time in data/ becomes an
absolute wall-clock epoch timestamp (time.time() float).

Ran July 2026 alongside the code change that made all recorders stamp
absolute times (see app/midi.py, app/music_recording.py). What it does:

- */meta.json, TrialStructure.json: created_at / started_at /
  completed_at ISO-8601 strings -> epoch floats.
- data/**/raw/notes.json and */results.json: recorder-relative event
  times -> absolute, by adding the recording's MIDI clock start
  (sync.json's midi_start_time, or midi_raw.json's abs_time - rel_time
  when sync.json is missing).
- data/**/raw/midi_raw.json: the redundant rel_time field is dropped
  (abs_time was always recorded).

Recordings with neither sync.json nor a usable midi_raw.json keep their
relative times (there is no clock reference to convert with); the
analysis code treats any time < 1e6 as legacy video-relative, so those
still work.

Safe to re-run: every conversion checks the current format first.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
EPOCH_MIN = 1e6  # anything below this is a relative time, not an epoch stamp


def iso_to_epoch(value):
    if isinstance(value, (int, float)):
        return value, False
    return datetime.fromisoformat(value).timestamp(), True


def save(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def migrate_created_at(path, keys=("created_at",)):
    obj = json.load(open(path))
    changed = False
    for key in keys:
        if key in obj and obj[key]:
            obj[key], did = iso_to_epoch(obj[key])
            changed |= did
    if changed:
        save(path, obj)
    return changed


def midi_clock_start(raw_dir):
    """The recording's MIDI clock start, for shifting relative event times."""
    sync_path = raw_dir / "sync.json"
    if sync_path.exists():
        return json.load(open(sync_path))["midi_start_time"]
    midi_path = raw_dir / "midi_raw.json"
    if midi_path.exists():
        events = json.load(open(midi_path))
        for e in events:
            if "abs_time" in e and "rel_time" in e:
                return e["abs_time"] - e["rel_time"]
    return None


def main():
    stats = {"iso": 0, "shifted": 0, "rel_dropped": 0, "no_clock": []}

    # created_at in quiz/song/sequence metas
    for meta in list(DATA.glob("quiz/*/meta.json")) + list(DATA.glob("music/*/meta.json")) + list(
        DATA.glob("sequence/*/meta.json")
    ):
        stats["iso"] += migrate_created_at(meta)

    # TrialStructure.json: created_at plus per-trial started/completed
    for ts_path in DATA.glob("MainUserStudy/*/TrialStructure.json"):
        obj = json.load(open(ts_path))
        changed = False
        if obj.get("created_at"):
            obj["created_at"], did = iso_to_epoch(obj["created_at"])
            changed |= did
        for trial in obj.get("trials", []):
            for key in ("started_at", "completed_at"):
                if trial.get(key):
                    trial[key], did = iso_to_epoch(trial[key])
                    changed |= did
        if changed:
            save(ts_path, obj)
            stats["iso"] += 1

    # Every recording's raw/ (quizzes and songs share the layout)
    for raw_dir in sorted(DATA.glob("*/*/raw")):
        start = midi_clock_start(raw_dir)

        notes_path = raw_dir / "notes.json"
        if notes_path.exists():
            notes = json.load(open(notes_path))
            if notes and notes[0]["time"] < EPOCH_MIN:
                if start is None:
                    stats["no_clock"].append(str(raw_dir.parent.relative_to(DATA)))
                else:
                    for n in notes:
                        n["time"] += start
                    save(notes_path, notes)
                    stats["shifted"] += 1

        midi_path = raw_dir / "midi_raw.json"
        if midi_path.exists():
            events = json.load(open(midi_path))
            if any("rel_time" in e for e in events):
                for e in events:
                    e.pop("rel_time", None)
                save(midi_path, events)
                stats["rel_dropped"] += 1

        # quiz results live next to raw/
        results_path = raw_dir.parent / "results.json"
        if results_path.exists():
            results = json.load(open(results_path))
            needs = any(
                (r.get("cue_onset_time") or 0) < EPOCH_MIN and r.get("cue_onset_time") is not None
                for r in results
            )
            if needs and start is not None:
                for r in results:
                    for key in ("cue_onset_time", "keypress_time"):
                        if r.get(key) is not None and r[key] < EPOCH_MIN:
                            r[key] += start
                save(results_path, results)
                stats["shifted"] += 1
            elif needs:
                stats["no_clock"].append(str(results_path.parent.relative_to(DATA)))

    print(f"ISO -> epoch: {stats['iso']} files")
    print(f"relative -> absolute event logs: {stats['shifted']} files")
    print(f"rel_time dropped from midi_raw: {stats['rel_dropped']} files")
    if stats["no_clock"]:
        uniq = sorted(set(stats["no_clock"]))
        print(f"left relative (no clock reference): {len(uniq)}")
        for name in uniq:
            print(f"  - {name}")


if __name__ == "__main__":
    sys.exit(main())
