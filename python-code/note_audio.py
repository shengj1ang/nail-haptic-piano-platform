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

Uses sounddevice (already a project dependency, see test-script/measure_latency.py)
for a continuous tone for as long as a note is held, mixing multiple
simultaneous notes together, with a short linear fade on press/release to
avoid audible clicks.
"""

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 44100
_A4_NOTE = 69
_A4_FREQ = 440.0


def note_to_frequency(note: int) -> float:
    """Standard equal-tempered MIDI note -> frequency in Hz (note 69 = A4 = 440Hz)."""
    return _A4_FREQ * 2 ** ((note - _A4_NOTE) / 12)


class _Voice:
    """One currently-sounding note: a sine wave with a linear attack/release
    envelope so starting/stopping it doesn't click."""

    def __init__(self, frequency: float, fade_samples: int):
        self.frequency = frequency
        self.fade_samples = max(1, fade_samples)
        self.phase_samples = 0
        self.amplitude = 0.0
        self.releasing = False

    def render(self, frames: int, sample_rate: int) -> tuple[np.ndarray, bool]:
        t = (np.arange(frames) + self.phase_samples) / sample_rate
        wave = np.sin(2.0 * np.pi * self.frequency * t).astype(np.float32)
        self.phase_samples += frames

        target = 0.0 if self.releasing else 1.0
        step = 1.0 / self.fade_samples
        ramp = np.arange(1, frames + 1) * step
        if target > self.amplitude:
            envelope = np.minimum(target, self.amplitude + ramp)
        else:
            envelope = np.maximum(target, self.amplitude - ramp)
        self.amplitude = float(envelope[-1]) if frames > 0 else self.amplitude

        done = self.releasing and self.amplitude <= 0.0
        return wave * envelope, done


class NoteAudioPlayer:
    """Plays a continuous tone for each MIDI note currently held down (like
    a real piano key), mixing multiple simultaneous notes. Not tied to any
    specific keyboard/profile - any MIDI note number works."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, volume: float = 0.2, fade_ms: float = 8.0):
        self.sample_rate = sample_rate
        self.volume = volume
        self._fade_samples = int(sample_rate * fade_ms / 1000)
        self._voices: dict[int, _Voice] = {}
        self._stream = sd.OutputStream(
            samplerate=sample_rate, channels=1, dtype="float32", callback=self._callback
        )
        self._stream.start()

    def play_key(self, note: int) -> bool:
        """Start (or re-trigger) the tone for MIDI `note`."""
        voice = self._voices.get(note)
        if voice is None:
            self._voices[note] = _Voice(note_to_frequency(note), self._fade_samples)
        else:
            voice.releasing = False
        return True

    def stop_key(self, note: int) -> bool:
        """Fade out the tone for MIDI `note`. Returns False if it wasn't playing."""
        voice = self._voices.get(note)
        if voice is None:
            return False
        voice.releasing = True
        return True

    def stop_all(self) -> None:
        for voice in self._voices.values():
            voice.releasing = True

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()

    def _callback(self, outdata, frames, time_info, status):
        buffer = np.zeros(frames, dtype=np.float32)
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
        # Demo: play middle C (MIDI note 60) for 1 second, then release it.
        note = 60
        print(f"Playing note {note} ({note_to_frequency(note):.1f} Hz)")
        player.play_key(note)
        time.sleep(1.0)
        player.stop_key(note)
        time.sleep(0.5)  # let the release fade finish before closing the stream
