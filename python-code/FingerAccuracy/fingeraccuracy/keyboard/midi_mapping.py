"""Maps our key ids (from keyboard_template.json) to physical MIDI note
numbers. Saved as midi_mapping.json alongside keyboard_template.json /
keyboard_key_map.png in the same profile folder - a profile then carries
both "where are the keys" and "which MIDI note does each one send".
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

MIDI_MAPPING_FILENAME = "midi_mapping.json"

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(note: int) -> str:
    return f"{_NOTE_NAMES[note % 12]}{note // 12 - 1}"


@dataclass
class MidiMapping:
    port_name: Optional[str] = None
    key_to_note: Dict[int, int] = field(default_factory=dict)  # key_id -> MIDI note number

    def note_for_key(self, key_id: int) -> Optional[int]:
        return self.key_to_note.get(key_id)

    def key_for_note(self, note: int) -> Optional[int]:
        for key_id, mapped_note in self.key_to_note.items():
            if mapped_note == note:
                return key_id
        return None

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "port_name": self.port_name,
            "mapping": [
                {"key_id": key_id, "note": note, "note_name": note_name(note)}
                for key_id, note in sorted(self.key_to_note.items())
            ],
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "MidiMapping":
        with open(path) as f:
            data = json.load(f)

        key_to_note = {int(item["key_id"]): int(item["note"]) for item in data["mapping"]}
        return cls(port_name=data.get("port_name"), key_to_note=key_to_note)
