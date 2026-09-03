"""
Reusable, GUI-free MIDI note -> audio tone playback.

Mirrors note_led_map.NoteLEDMapper's note-keyed press/release interface, so
any future teaching module can wire up sound the same way it wires up LEDs:
play_key(note) on press, stop_key(note) on release - no separate key_id, no
per-teaching-module reimplementation of tone generation.

Frequency comes straight from the MIDI note number via the standard
equal-tempered formula (note 69 = A4 = 440Hz) - no calibration/profile
needed, unlike the LED side, since audio pitch is just physics, not a fact
about a specific piece of hardware.

A note's *timbre* (its tone color) is additive synthesis - a stack of sine
harmonics above the fundamental, each with its own weight, shaped by an
attack/decay/sustain/release envelope - rather than the single bare sine
wave this used to be. TIMBRES below has a few presets (sine/piano/electric
piano); NoteAudioPlayer.set_timbre() switches which one new key-presses
use, so a GUI can offer a dropdown without knowing anything about
synthesis. Uses sounddevice (already a project dependency, see
test-script/measure_latency.py) for continuous tones, mixing multiple
simultaneous notes together.
"""

import threading
from dataclasses import dataclass
from typing import List

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 44100
_A4_NOTE = 69
_A4_FREQ = 440.0


def note_to_frequency(note: int) -> float:
    """Standard equal-tempered MIDI note -> frequency in Hz (note 69 = A4 = 440Hz)."""
    return _A4_FREQ * 2 ** ((note - _A4_NOTE) / 12)


@dataclass
class Timbre:
    """One tone color: a set of harmonic weights (index 0 = fundamental, 1
    = 2nd harmonic, ...) plus an attack/decay/sustain/release envelope.
    decay_to is the sustain level (as a fraction of full amplitude) the
    note settles to while still held - real struck instruments (like a
    piano) decay noticeably even without releasing the key; decay_to=1.0
    means it just holds at full volume instead (an organ/sine-like tone)."""

    name: str
    harmonics: List[float]
    attack_ms: float = 5.0
    decay_to: float = 1.0
    decay_ms: float = 1.0
    release_ms: float = 8.0


TIMBRES = {
    "sine": Timbre(name="Sine", harmonics=[1.0], attack_ms=8.0, decay_to=1.0, decay_ms=1.0, release_ms=8.0),
    "piano": Timbre(
        name="Piano",
        harmonics=[1.0, 0.55, 0.3, 0.15, 0.1, 0.05],
        attack_ms=3.0,
        decay_to=0.25,
        decay_ms=600.0,
        release_ms=120.0,
    ),
    "electric_piano": Timbre(
        name="Electric Piano",
        harmonics=[1.0, 0.15, 0.5, 0.05, 0.25],
        attack_ms=5.0,
        decay_to=0.5,
        decay_ms=350.0,
        release_ms=200.0,
    ),
    # No harmonics at all -> _Voice.render() always produces a zero wave,
    # regardless of its (otherwise normal) attack/decay/sustain/release
    # envelope - silent audio feedback, without any special-casing needed
    # elsewhere. Every timbre dropdown in the codebase just loops over
    # TIMBRES.items(), so this shows up as a normal "Mute" choice
    # everywhere for free. Useful for the auditory-control condition
    # (the final report's Methods chapter, §"Design and Procedure":
    # "keyboard sound and all other audio output were muted") where MIDI
    # sound must be disabled but events are still logged.
    "mute": Timbre(name="Mute", harmonics=[]),
}
DEFAULT_TIMBRE = "piano"


class _Voice:
    """One currently-sounding note: a stack of harmonics shaped by its
    timbre's attack/decay/sustain/release envelope. Envelope stage
    transitions are evaluated once per audio callback block rather than
    per-sample - at typical block sizes (a few/tens of ms) that's an
    inaudible approximation, and it keeps this cheap enough for several
    simultaneous notes in a plain Python callback."""

    def __init__(self, frequency: float, timbre: Timbre, sample_rate: int):
        self.frequency = frequency
        self.timbre = timbre
        self.phase_samples = 0
        self.releasing = False
        self.stage = "attack"  # attack -> decay -> sustain -> release -> done
        self.stage_pos = 0  # samples elapsed within the current stage
        self.level = 0.0  # amplitude as of the end of the last block (release start point)
        self.release_start_level = 0.0
        self._harmonic_sum = sum(timbre.harmonics) or 1.0
        self._attack_n = max(1, round(sample_rate * timbre.attack_ms / 1000))
        self._decay_n = max(1, round(sample_rate * timbre.decay_ms / 1000))

    def render(self, frames: int, sample_rate: int) -> tuple[np.ndarray, bool]:
        t = (np.arange(frames) + self.phase_samples) / sample_rate
        wave = np.zeros(frames, dtype=np.float32)
        for harmonic, weight in enumerate(self.timbre.harmonics, start=1):
            if weight:
                wave += weight * np.sin(2.0 * np.pi * self.frequency * harmonic * t).astype(np.float32)
        wave /= self._harmonic_sum
        self.phase_samples += frames

        if self.releasing and self.stage != "release":
            self.stage = "release"
            self.stage_pos = 0
            self.release_start_level = self.level

        elapsed_s = (self.stage_pos + np.arange(frames)) / sample_rate

        if self.stage == "attack":
            env = np.minimum(elapsed_s / max(self.timbre.attack_ms / 1000.0, 1e-6), 1.0)
            self.stage_pos += frames
            if self.stage_pos >= self._attack_n:
                self.stage, self.stage_pos = "decay", 0
        elif self.stage == "decay":
            decay_to = self.timbre.decay_to
            tau = max(self.timbre.decay_ms / 1000.0 / 3.0, 1e-6)
            env = decay_to + (1.0 - decay_to) * np.exp(-elapsed_s / tau)
            self.stage_pos += frames
            if self.stage_pos >= self._decay_n:
                self.stage, self.stage_pos = "sustain", 0
        elif self.stage == "sustain":
            env = np.full(frames, self.timbre.decay_to, dtype=np.float32)
        elif self.stage == "release":
            release_s = max(self.timbre.release_ms / 1000.0, 1e-6)
            env = self.release_start_level * np.maximum(1.0 - elapsed_s / release_s, 0.0)
            self.stage_pos += frames
        else:
            env = np.zeros(frames, dtype=np.float32)

        self.level = float(env[-1]) if frames > 0 else self.level
        done = self.releasing and self.level <= 1e-4
        return wave * env.astype(np.float32), done


class NoteAudioPlayer:
    """Plays a continuous tone for each MIDI note currently held down (like
    a real piano key), mixing multiple simultaneous notes. Not tied to any
    specific keyboard/profile - any MIDI note number works."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, volume: float = 0.2, timbre: str = DEFAULT_TIMBRE):
        self.sample_rate = sample_rate
        self.volume = volume
        self.timbre_name = timbre if timbre in TIMBRES else DEFAULT_TIMBRE
        self._voices: dict[int, _Voice] = {}
        # play_key/stop_key run on the GUI/MIDI thread while _callback runs
        # on sounddevice's audio thread; without this lock the callback's
        # iteration races the dict mutations ("dictionary changed size
        # during iteration" under fast repeated key presses).
        self._voices_lock = threading.Lock()
        self._stream = sd.OutputStream(
            samplerate=sample_rate, channels=1, dtype="float32", callback=self._callback
        )
        self._stream.start()

    def set_timbre(self, name: str) -> None:
        """Which TIMBRES entry new key-presses use from now on - notes
        already sounding keep whatever timbre they started with, so
        switching mid-chord doesn't warp an already-playing note."""
        if name not in TIMBRES:
            raise ValueError(f"unknown timbre {name!r} - choices: {sorted(TIMBRES)}")
        self.timbre_name = name

    def play_key(self, note: int) -> bool:
        """Start (or re-trigger) the tone for MIDI `note`."""
        with self._voices_lock:
            voice = self._voices.get(note)
            if voice is None:
                self._voices[note] = _Voice(note_to_frequency(note), TIMBRES[self.timbre_name], self.sample_rate)
            else:
                voice.releasing = False
        return True

    def stop_key(self, note: int) -> bool:
        """Fade out the tone for MIDI `note`. Returns False if it wasn't playing."""
        with self._voices_lock:
            voice = self._voices.get(note)
            if voice is None:
                return False
            voice.releasing = True
        return True

    def stop_all(self) -> None:
        with self._voices_lock:
            for voice in self._voices.values():
                voice.releasing = True

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()

    def _callback(self, outdata, frames, time_info, status):
        buffer = np.zeros(frames, dtype=np.float32)
        with self._voices_lock:
            finished = []
            for note, voice in self._voices.items():
                chunk, done = voice.render(frames, self.sample_rate)
                buffer += chunk * self.volume
                if done:
                    finished.append(note)
            for note in finished:
                del self._voices[note]

        np.clip(buffer, -1.0, 1.0, out=buffer)
        outdata[:, 0] = buffer

    def __enter__(self) -> "NoteAudioPlayer":
        return self

    def __exit__(self, *args) -> None:
        self.close()


if __name__ == "__main__":
    import time

    with NoteAudioPlayer() as player:
        # Demo: play middle C (MIDI note 60) with each timbre for 1.5s.
        note = 60
        for name in TIMBRES:
            player.set_timbre(name)
            print(f"Playing note {note} ({note_to_frequency(note):.1f} Hz) with timbre {TIMBRES[name].name!r}")
            player.play_key(note)
            time.sleep(1.5)
            player.stop_key(note)
            time.sleep(0.5)  # let the release fade finish before the next timbre
