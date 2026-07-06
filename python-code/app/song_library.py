"""Combines the two places a "song" (a data/<...>/<name>/{meta.json,
fingering.json} folder, see app.music_recording) can come from:

  - a real recording, under data/music/ (music_recording_wizard.py)
  - a generated experimental sequence, under data/sequence/
    (experiment_sequence_wizard.py, see app.sequence_generator)

into one combined picker list, so tools that step through a song's
fingering - music_playback.py, student_quiz.py, student_quiz_haptic.py -
can offer both without caring which produced any given entry. Both
folders use the identical file layout, so nothing here reads or writes
song data itself; it only tells the caller which directory a given name
actually lives under.

Entries are labelled "music/<name>" or "sequence/<name>" so a user can
tell the two apart at a glance in a dropdown.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .music_recording import MUSIC_DATA_DIR, list_songs
from .sequence_generator import SEQUENCE_DATA_DIR

MUSIC_LABEL_PREFIX = "music"
SEQUENCE_LABEL_PREFIX = "sequence"


@dataclass(frozen=True)
class SongEntry:
    label: str  # e.g. "music/Fur Elise" or "sequence/Lα-X" - what a picker displays
    name: str  # the bare song name, e.g. "Fur Elise" - what song_dir()/load_quiz_targets() etc. take
    data_dir: Path  # which data dir this entry's name lives under


def list_song_entries() -> List[SongEntry]:
    entries = [
        SongEntry(label=f"{MUSIC_LABEL_PREFIX}/{name}", name=name, data_dir=MUSIC_DATA_DIR)
        for name in list_songs(MUSIC_DATA_DIR)
    ]
    entries += [
        SongEntry(label=f"{SEQUENCE_LABEL_PREFIX}/{name}", name=name, data_dir=SEQUENCE_DATA_DIR)
        for name in list_songs(SEQUENCE_DATA_DIR)
    ]
    return entries


def find_song_entry(label: str) -> Optional[SongEntry]:
    for entry in list_song_entries():
        if entry.label == label:
            return entry
    return None
