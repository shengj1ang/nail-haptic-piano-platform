"""The `remote_guidance` block of the platform's config.json.

Strictly additive. The existing top-level `camera`, `midi` and
`active_keyboard_profile` keys keep their meaning for every non-remote
tool and are never read as remote settings, never rewritten by a remote
save, and never reinterpreted. A config.json written before this module
existed loads with defaults, so nothing breaks by being older.

Why the remote roles get their own device settings
--------------------------------------------------
Student and teacher are two machines - or at least two processes with two
cameras and two keyboards. Pointing both at the shared `camera`/`midi`
keys would mean one role's setup silently redefines the other's, and the
ordinary tools' settings on top of that. So each role carries its own
camera, MIDI port and keyboard profile here, and `role_config()` hands a
tool a *copy* of Config with those substituted in - the shared cfg
objects are never mutated.

Serial devices
--------------
The student has two of them: the keyboard's LED strip and the haptic rig.
common.serial_utils.auto_detect_port scores candidate ports and, with two
similar boards attached, can confidently pick the wrong one - and for the
LED strip vs the vibration controller "wrong" means cueing the wrong
modality entirely. Blank ports use auto-detection; explicit config-only
overrides are available for a two-board rig and are validated to be
different.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.config import CameraConfig, Config, MidiConfig
from common.haptic_config import DEFAULT_CONFIG_PATH, atomic_write_json, read_config_file

from .protocol import GUIDANCE_BOTH, GUIDANCE_MODES

CONFIG_KEY = "remote_guidance"

# Bumped only when this block's *meaning* changes in a way a loader has to
# know about. A stored value newer than SCHEMA_VERSION is loaded on a
# best-effort basis and reported through load_warnings.
SCHEMA_VERSION = 1

ROLE_STUDENT = "student"
ROLE_TEACHER = "teacher"
ROLES = (ROLE_STUDENT, ROLE_TEACHER)


class RemoteConfigError(ValueError):
    """A setting that would send a cue to the wrong device or produce a
    meaningless session - raised instead of guessing."""


@dataclass
class NetworkConfig:
    # One URL for both HTTP and WebSocket: ws:// is derived from http://,
    # wss:// from https://, so there is no way to have them disagree.
    server_url: str = "http://127.0.0.1:18765"
    # Kept for the credential-free parts of a session (room id, last
    # username). Passwords and tokens are never stored in config.json.
    username: str = ""
    room_id: str = ""
    join_code: str = ""
    verify_tls: bool = True
    heartbeat_interval_s: float = 10.0
    reconnect_initial_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0
    # How many undelivered cues may wait behind the current one before the
    # student starts refusing them. Bounded on purpose: silently dropping
    # or silently overwriting a teacher's cue would both be worse than
    # saying the student is behind.
    guidance_queue_size: int = 16

    @property
    def http_base(self) -> str:
        return self.server_url.rstrip("/")

    @property
    def ws_base(self) -> str:
        base = self.http_base
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base

    def room_ws_url(self, room_id: str) -> str:
        return f"{self.ws_base}/ws/v1/rooms/{room_id}"

    @property
    def api_base(self) -> str:
        return f"{self.http_base}/api/v1"


@dataclass
class SerialDeviceConfig:
    """An explicit serial port. None means "fall back to auto-detection",
    which is fine when only one board is attached and risky when two are -
    see the module docstring."""

    port: Optional[str] = None


@dataclass
class StudentConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    midi: MidiConfig = field(default_factory=MidiConfig)
    keyboard_profile: str = "default"
    led: SerialDeviceConfig = field(default_factory=SerialDeviceConfig)
    haptic: SerialDeviceConfig = field(default_factory=SerialDeviceConfig)
    default_guidance_mode: str = GUIDANCE_BOTH
    record_video: bool = True
    # Kept identical in meaning to the local quiz's timeout.
    default_timeout_s: float = 5.0


@dataclass
class TeacherConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    midi: MidiConfig = field(default_factory=MidiConfig)
    keyboard_profile: str = "default"
    # Multi-finger matching for simultaneous note-ons (chords), through
    # the existing app.finger_matching.match_notes_to_fingers.
    chord_detection: bool = True
    # Record a live lesson to data/music/<session name>/, in the Song
    # Recording Wizard's layout. The default for the checkbox on the
    # session page, which is where it is chosen per session - same
    # arrangement as the student's record_video.
    record_video: bool = True
    # How long "Start teaching" waits before both ends start recording.
    # The student has its camera and MediaPipe already running by then -
    # this is the moment to sit still and let both video writers open on
    # the same agreed instant rather than whenever each machine got round
    # to it.
    lesson_lead_s: float = 3.0


@dataclass
class LocalServerConfig:
    """How the launcher's Relay Server Control Panel starts a relay on
    this machine. Not used when pointing at a remote server."""

    enabled: bool = True
    host: str = "127.0.0.1"
    # Must match server.config.ServerConfig.port, which is what the
    # launcher actually starts: a mismatch here points the clients at a
    # port nothing is listening on. In the registered-port range on
    # purpose - 8765 is a common default and clashed with other services.
    port: int = 18765
    use_gui: bool = True


@dataclass
class RemoteGuidanceConfig:
    schema_version: int = SCHEMA_VERSION
    network: NetworkConfig = field(default_factory=NetworkConfig)
    student: StudentConfig = field(default_factory=StudentConfig)
    teacher: TeacherConfig = field(default_factory=TeacherConfig)
    local_server: LocalServerConfig = field(default_factory=LocalServerConfig)

    # ------------------------------------------------------------------
    # loading / saving
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RemoteGuidanceConfig":
        """Tolerant by design: every missing key falls back to its
        default, and an unknown key is ignored rather than raising, so a
        config written by a newer version still loads."""
        data = data or {}
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            network=_build(NetworkConfig, data.get("network")),
            student=StudentConfig(
                camera=_build(CameraConfig, (data.get("student") or {}).get("camera")),
                midi=_build(MidiConfig, (data.get("student") or {}).get("midi")),
                keyboard_profile=(data.get("student") or {}).get("keyboard_profile", "default"),
                led=_build(SerialDeviceConfig, (data.get("student") or {}).get("led")),
                haptic=_build(SerialDeviceConfig, (data.get("student") or {}).get("haptic")),
                default_guidance_mode=(data.get("student") or {}).get("default_guidance_mode", GUIDANCE_BOTH),
                record_video=bool((data.get("student") or {}).get("record_video", True)),
                default_timeout_s=float((data.get("student") or {}).get("default_timeout_s", 5.0)),
            ),
            teacher=TeacherConfig(
                camera=_build(CameraConfig, (data.get("teacher") or {}).get("camera")),
                midi=_build(MidiConfig, (data.get("teacher") or {}).get("midi")),
                keyboard_profile=(data.get("teacher") or {}).get("keyboard_profile", "default"),
                chord_detection=bool((data.get("teacher") or {}).get("chord_detection", True)),
                record_video=bool((data.get("teacher") or {}).get("record_video", True)),
            ),
            local_server=_build(LocalServerConfig, data.get("local_server")),
        )

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "RemoteGuidanceConfig":
        """A config.json with no remote_guidance block - i.e. every file
        written before this module existed - yields defaults. Nothing is
        written back on load."""
        return cls.from_dict(read_config_file(path).get(CONFIG_KEY))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "network": asdict(self.network),
            "student": {
                "camera": asdict(self.student.camera),
                "midi": asdict(self.student.midi),
                "keyboard_profile": self.student.keyboard_profile,
                "led": asdict(self.student.led),
                "haptic": asdict(self.student.haptic),
                "default_guidance_mode": self.student.default_guidance_mode,
                "record_video": self.student.record_video,
                "default_timeout_s": self.student.default_timeout_s,
            },
            "teacher": {
                "camera": asdict(self.teacher.camera),
                "midi": asdict(self.teacher.midi),
                "keyboard_profile": self.teacher.keyboard_profile,
                "chord_detection": self.teacher.chord_detection,
                "record_video": self.teacher.record_video,
            },
            "local_server": asdict(self.local_server),
        }

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        """Writes exactly one key.

        The rest of config.json - including the top-level camera, midi and
        active_keyboard_profile the ordinary tools depend on - is read
        back and re-written untouched, so saving remote settings can never
        move another tool's camera or repoint its MIDI port. Atomic, same
        as every other save in this codebase."""
        payload = read_config_file(path)
        payload[CONFIG_KEY] = self.to_dict()
        atomic_write_json(path, payload)

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    def validate(self) -> List[str]:
        """Everything wrong with this configuration, as human-readable
        lines. Empty means usable. Kept separate from raising so a
        settings window can show all the problems at once."""
        problems: List[str] = []

        if not self.network.server_url.startswith(("http://", "https://")):
            problems.append(
                f"network.server_url must start with http:// or https:// (got {self.network.server_url!r})"
            )
        if self.student.default_guidance_mode not in GUIDANCE_MODES:
            problems.append(
                f"student.default_guidance_mode must be one of {list(GUIDANCE_MODES)} "
                f"(got {self.student.default_guidance_mode!r})"
            )
        problems.extend(self.serial_port_problems())
        if self.network.guidance_queue_size < 1:
            problems.append("network.guidance_queue_size must be at least 1")
        if self.schema_version > SCHEMA_VERSION:
            problems.append(
                f"remote_guidance.schema_version is {self.schema_version} but this build understands "
                f"{SCHEMA_VERSION} - some settings may be ignored"
            )
        return problems

    def serial_port_problems(self) -> List[str]:
        """The LED strip and the haptic rig are two different boards. If
        they are configured on the same port, one of the two cues would
        be driven onto the wrong device - refuse rather than send motor
        commands to an LED controller."""
        led = (self.student.led.port or "").strip()
        haptic = (self.student.haptic.port or "").strip()
        if led and haptic and led == haptic:
            return [
                f"student.led.port and student.haptic.port are both {led!r}. The keyboard backlight and the "
                "vibration rig are separate serial devices - set each to its own port (they are usually two "
                "different /dev/tty.usbmodem* or COM* entries)."
            ]
        return []

    def require_valid(self) -> None:
        problems = self.validate()
        if problems:
            raise RemoteConfigError("\n".join(problems))


def _build(cls, data: Optional[Dict[str, Any]]):
    """Construct a dataclass from stored data, ignoring keys it does not
    model (forward compatibility) and filling in what is absent."""
    if not data:
        return cls()
    known = set(getattr(cls, "__dataclass_fields__", {}))
    return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Per-role Config views
# ---------------------------------------------------------------------------


def role_config(cfg: Config, remote: RemoteGuidanceConfig, role: str) -> Config:
    """A Config for one remote role, so existing tools (Camera,
    HandTracker consumers, MIDI listeners, LED mapper) can be reused
    unchanged.

    Returns a deep copy with camera/midi/active_keyboard_profile replaced
    by that role's settings. The caller's `cfg` - shared with every
    ordinary tool in the process - is never mutated, so opening a remote
    window cannot move the local quiz's camera."""
    if role not in ROLES:
        raise RemoteConfigError(f"unknown role {role!r}, expected one of {list(ROLES)}")

    role_settings = remote.student if role == ROLE_STUDENT else remote.teacher
    view = copy.deepcopy(cfg)
    view.camera = replace(role_settings.camera)
    view.midi = replace(role_settings.midi)
    view.active_keyboard_profile = role_settings.keyboard_profile
    return view


def student_config(cfg: Config, remote: RemoteGuidanceConfig) -> Config:
    return role_config(cfg, remote, ROLE_STUDENT)


def teacher_config(cfg: Config, remote: RemoteGuidanceConfig) -> Config:
    return role_config(cfg, remote, ROLE_TEACHER)


CameraIndex = Union[int, str]
