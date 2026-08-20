"""The project's single source of truth for haptic actuator defaults.

Everything that drives a vibration motor - the quiz's haptic cue, the
manual bench windows, the Test Buzz of every validation experiment and
the initial control values of the LRA/ERM experiments - asks THIS module
what actuator is in use and how it should be driven by default, instead
of repeating "224 Hz / amp 64" in a dozen files.

The values live in the project's `config.json` under a "haptic" key:

    "haptic": {
      "using": "lra",
      "lra": {"default_frequency": 224, "default_amp": 64},
      "erm": {"default_frequency": 1000, "default_amp": 80}
    }

`using` names the actuator the platform currently drives; each actuator
carries its OWN default frequency/amp, so switching `using` switches the
whole default drive in one step. The two frequencies do not mean the
same thing: an LRA's frequency is a mechanical drive/resonance frequency
(it only vibrates properly near it), while an ERM's is a PWM carrier -
the rotor's mechanical vibration frequency follows the motor's speed and
has nothing to do with it (see README).

COMPATIBILITY: an older config.json with no "haptic" key, or with only
some of its fields, is not an error - every missing field falls back to
the built-in default and the file is left alone until something actually
saves. Values outside the valid range are clamped (or, when they are not
numbers at all, replaced by the built-in default) and the correction is
reported through `last_warnings()` and the `logging` module, so a bad
config can never reach the hardware and can never turn into a silently
different one.

WHAT THIS IS NOT: it holds DEFAULTS, not experiment semantics. A sweep
still sweeps its own frequency/amp points, and a value the user typed in
an experiment window always wins over the config default. Motor PORT is
a wiring fact, not a default - it stays in ACTUATOR_MOTOR_PORTS here and
is deliberately not part of config.json.

This module is deliberately dependency-free (stdlib only) and lives in
common/ so the app package, the standalone entry-point scripts and the
validation_experiments scripts can all import it cheaply.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: The project config file this module reads and writes (main/config.json).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

#: Actuator type ids, as stored in config.json (lower case).
LRA = "lra"
ERM = "erm"
ACTUATOR_TYPES: Tuple[str, ...] = (LRA, ERM)

#: Human-facing labels for the GUI (config stores the lower-case ids).
ACTUATOR_LABELS: Dict[str, str] = {LRA: "LRA", ERM: "ERM"}

# --- hardware limits (teensy_driver/motor_driver.cpp) --------------------
# amp is the PWM duty byte of the firmware's 'P'/'S' commands
# (handlePulse() rejects amp outside 0-255; 'S' writes it straight to
# analogWrite).
AMP_MIN, AMP_MAX = 0, 255
# The 'F' command's own limits: FREQ_MIN / FREQ_MAX in motor_driver.cpp.
# A frequency outside them is DROPPED by the firmware, so nothing here
# may ever hand one to the rig.
FIRMWARE_FREQ_MIN_HZ, FIRMWARE_FREQ_MAX_HZ = 50, 20000
#: Every motor pin boots at this PWM frequency (DEFAULT_PWM_FREQ in
#: motor_driver.cpp, the LRA's measured resonance). It is a FIRMWARE
#: fact, not a preference: the experiments restore it when they finish,
#: so it must keep matching the firmware even if the LRA default changes.
FIRMWARE_BOOT_PWM_HZ = 224

# --- per-actuator frequency limits --------------------------------------
# Both start at the firmware minimum. The upper bounds come from the
# project's existing constants, not from taste:
#   LRA - the drive must sit at the mechanical resonance; the platform's
#         own "LRA band" is the motor bench's high-precision slider
#         (test_haptic_single_motor.FREQ_MAX_HIGH = 1000 Hz), and the
#         resonance sweep only covers 100-350 Hz.
#   ERM - a PWM carrier: it must be high enough to act as smooth DC
#         (the project drives ERMs at 1-5 kHz), and the firmware's
#         20 kHz ceiling is the only hard limit.
FREQUENCY_LIMITS: Dict[str, Tuple[int, int]] = {
    LRA: (FIRMWARE_FREQ_MIN_HZ, 1000),
    ERM: (FIRMWARE_FREQ_MIN_HZ, FIRMWARE_FREQ_MAX_HZ),
}
#: Advisory band per actuator - outside it the value is still legal and
#: still sent, the GUI just says it is unusual (an off-resonance LRA is
#: weak; a sub-kHz ERM may not start at all).
TYPICAL_FREQUENCY_HZ: Dict[str, Tuple[int, int]] = {
    LRA: (100, 350),
    ERM: (1000, 5000),
}

# --- built-in defaults ---------------------------------------------------
#: LRA: the measured resonance and the calibrated ~0.5 m/s2 cue level
#: (validation_experiments/lra_resonance_intensity_calibration/README.md).
#: ERM: the measured all-round 1 kHz carrier at a reliably effective cue amp.
BUILTIN_DEFAULTS: Dict[str, Tuple[int, int]] = {
    LRA: (224, 64),
    ERM: (1000, 80),
}
DEFAULT_USING = LRA

#: Wiring convention of the rig's two actuator test channels (see the
#: Wiring Guide window and the teensy_driver README). A PORT is a fact
#: about how the board is wired, so it is NOT a config default and NOT
#: part of config.json - it is centralised here only so the windows and
#: experiments agree on one mapping. The per-experiment motor-port
#: controls still override it.
ACTUATOR_MOTOR_PORTS: Dict[str, int] = {LRA: 11, ERM: 10}

#: The config.json key this module owns.
CONFIG_KEY = "haptic"


# =========================================================================
# Data model
# =========================================================================

@dataclass
class ActuatorDefaults:
    """One actuator's default drive settings."""

    default_frequency: int
    default_amp: int

    def to_dict(self) -> dict:
        return {"default_frequency": int(self.default_frequency),
                "default_amp": int(self.default_amp)}


@dataclass
class HapticConfig:
    """The whole "haptic" config block: which actuator is in use plus
    each actuator's own defaults."""

    using: str = DEFAULT_USING
    lra: ActuatorDefaults = None  # type: ignore[assignment]
    erm: ActuatorDefaults = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.lra is None:
            self.lra = ActuatorDefaults(*BUILTIN_DEFAULTS[LRA])
        if self.erm is None:
            self.erm = ActuatorDefaults(*BUILTIN_DEFAULTS[ERM])

    def for_type(self, actuator_type: str) -> ActuatorDefaults:
        """This config's defaults for `actuator_type` ("lra"/"LRA"/...)."""
        name = normalise_actuator_type(actuator_type)
        if name is None:
            raise ValueError(
                f"unknown actuator type {actuator_type!r} - "
                f"expected one of {', '.join(ACTUATOR_TYPES)}")
        return self.lra if name == LRA else self.erm

    @property
    def active(self) -> ActuatorDefaults:
        """The defaults of the actuator named by `using`."""
        return self.for_type(self.using)

    def to_dict(self) -> dict:
        return {"using": self.using,
                LRA: self.lra.to_dict(),
                ERM: self.erm.to_dict()}


def default_haptic_config() -> HapticConfig:
    """A fresh config holding nothing but the built-in defaults."""
    return HapticConfig(
        using=DEFAULT_USING,
        lra=ActuatorDefaults(*BUILTIN_DEFAULTS[LRA]),
        erm=ActuatorDefaults(*BUILTIN_DEFAULTS[ERM]),
    )


# =========================================================================
# Validation
# =========================================================================

def normalise_actuator_type(value) -> Optional[str]:
    """"LRA"/"lra"/" Lra " -> "lra"; anything else -> None."""
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    return name if name in ACTUATOR_TYPES else None


def actuator_label(actuator_type: str) -> str:
    """Display name ("LRA"/"ERM") for an actuator id."""
    name = normalise_actuator_type(actuator_type)
    return ACTUATOR_LABELS.get(name or "", str(actuator_type))


def frequency_range(actuator_type: str) -> Tuple[int, int]:
    """(min, max) drive frequency in Hz allowed for this actuator. The
    GUI spin boxes and the config validation use the SAME numbers."""
    name = normalise_actuator_type(actuator_type) or DEFAULT_USING
    return FREQUENCY_LIMITS[name]


def amp_range() -> Tuple[int, int]:
    """(min, max) amp - the firmware's PWM duty byte, same for both."""
    return AMP_MIN, AMP_MAX


def is_frequency_valid(value, actuator_type: str) -> bool:
    low, high = frequency_range(actuator_type)
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and low <= value <= high


def is_amp_valid(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and AMP_MIN <= value <= AMP_MAX


def clamp_frequency(value, actuator_type: str) -> int:
    """`value` forced into this actuator's legal range, so a bad number
    can never be handed to the 'F' command (which would drop it)."""
    low, high = frequency_range(actuator_type)
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return BUILTIN_DEFAULTS[normalise_actuator_type(actuator_type)
                                or DEFAULT_USING][0]
    return max(low, min(high, number))


def clamp_amp(value) -> int:
    """`value` forced into the firmware's 0-255 amp range."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return BUILTIN_DEFAULTS[DEFAULT_USING][1]
    return max(AMP_MIN, min(AMP_MAX, number))


def _coerce(value, *, kind: str, actuator_type: str, fallback: int,
            warnings: List[str]) -> int:
    """One frequency/amp field: keep it if it is a valid number in range,
    clamp it if it is a number out of range, fall back to the built-in
    default if it is not a number at all. Every correction is recorded."""
    is_number = (isinstance(value, (int, float))
                 and not isinstance(value, bool))
    label = actuator_label(actuator_type)
    if not is_number:
        warnings.append(
            f"haptic.{actuator_type}.default_{kind}: {value!r} is not a "
            f"number - using the built-in {label} default {fallback}")
        return fallback
    if kind == "frequency":
        low, high = frequency_range(actuator_type)
        unit = " Hz"
    else:
        low, high = amp_range()
        unit = ""
    number = int(round(float(value)))
    if number < low or number > high:
        clamped = max(low, min(high, number))
        warnings.append(
            f"haptic.{actuator_type}.default_{kind}: {number}{unit} is "
            f"outside the allowed {low}-{high}{unit} range for a {label} "
            f"- clamped to {clamped}{unit}")
        return clamped
    return number


def validate_haptic_config(raw) -> Tuple[HapticConfig, List[str]]:
    """Turn whatever sits under config.json's "haptic" key into a valid
    HapticConfig, plus the list of corrections that had to be made.

    Never raises: a missing key, a partial block, a wrong type or an
    out-of-range number all resolve to a usable config (that is what
    keeps an old config.json from breaking startup). Use
    `update_haptic_config` for the strict, user-facing path."""
    warnings: List[str] = []
    cfg = default_haptic_config()

    if raw is None:
        return cfg, warnings
    if not isinstance(raw, dict):
        warnings.append(
            f"haptic: expected an object, got {type(raw).__name__} - using "
            "the built-in defaults")
        return cfg, warnings

    if "using" in raw:
        using = normalise_actuator_type(raw.get("using"))
        if using is None:
            warnings.append(
                f"haptic.using: {raw.get('using')!r} is not one of "
                f"{'/'.join(ACTUATOR_TYPES)} - falling back to "
                f"'{DEFAULT_USING}'")
        else:
            cfg.using = using

    for name in ACTUATOR_TYPES:
        block = raw.get(name)
        defaults = cfg.for_type(name)
        if block is None:
            continue
        if not isinstance(block, dict):
            warnings.append(
                f"haptic.{name}: expected an object, got "
                f"{type(block).__name__} - using the built-in "
                f"{actuator_label(name)} defaults")
            continue
        if "default_frequency" in block:
            defaults.default_frequency = _coerce(
                block.get("default_frequency"), kind="frequency",
                actuator_type=name, fallback=BUILTIN_DEFAULTS[name][0],
                warnings=warnings)
        if "default_amp" in block:
            defaults.default_amp = _coerce(
                block.get("default_amp"), kind="amp", actuator_type=name,
                fallback=BUILTIN_DEFAULTS[name][1], warnings=warnings)

    return cfg, warnings


# =========================================================================
# Loading / saving
# =========================================================================

_cache: Optional[HapticConfig] = None
_cache_key: Optional[tuple] = None
_last_warnings: List[str] = []


def read_config_file(path: Optional[Path] = None) -> dict:
    """The whole config.json as a dict ({} when it is missing or
    unreadable) - the base every save merges into, so no unrelated
    setting is ever lost."""
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read %s (%s) - treating it as empty", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def atomic_write_json(path, payload: dict) -> None:
    """Write `payload` to `path` via a temp file in the same directory
    plus os.replace(), so a crash or a full disk mid-write can never
    leave a half-written (unparseable) config.json behind."""
    path = Path(path)
    directory = path.parent if str(path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".config-",
                                    suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _cache_stamp(path: Path) -> tuple:
    try:
        stat = os.stat(path)
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (str(path), None, None)


def get_haptic_config(path: Optional[Path] = None,
                      reload: bool = False) -> HapticConfig:
    """The current haptic config, read from config.json.

    Cached, but the cache is keyed on the file's mtime/size, so a window
    that saves (or an edit made outside the app) is picked up on the next
    call - a newly opened window always sees the latest values."""
    global _cache, _cache_key, _last_warnings
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    stamp = _cache_stamp(path)
    if not reload and _cache is not None and _cache_key == stamp:
        return _cache
    cfg, warnings = validate_haptic_config(read_config_file(path).get(CONFIG_KEY))
    for message in warnings:
        log.warning("config.json: %s", message)
    _cache, _cache_key, _last_warnings = cfg, stamp, warnings
    return cfg


def last_warnings() -> List[str]:
    """Corrections applied to the most recently loaded config (empty when
    it was clean) - shown by the Initial Setup window."""
    return list(_last_warnings)


def invalidate_cache() -> None:
    """Drop the cached config; the next read goes back to the file."""
    global _cache, _cache_key
    _cache, _cache_key = None, None


def save_haptic_config(cfg: HapticConfig, path: Optional[Path] = None) -> HapticConfig:
    """Write `cfg` into config.json's "haptic" key and notify listeners.

    Read-modify-write: every OTHER key in the file is preserved exactly
    as it was, and the write itself is atomic."""
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    validated, warnings = validate_haptic_config(cfg.to_dict())
    for message in warnings:
        log.warning("haptic config corrected before saving: %s", message)
    data = read_config_file(path)
    data[CONFIG_KEY] = validated.to_dict()
    atomic_write_json(path, data)
    invalidate_cache()
    notify_listeners(validated)
    return validated


def update_haptic_config(using: Optional[str] = None,
                         lra_frequency: Optional[int] = None,
                         lra_amp: Optional[int] = None,
                         erm_frequency: Optional[int] = None,
                         erm_amp: Optional[int] = None,
                         path: Optional[Path] = None) -> HapticConfig:
    """Change some haptic settings and save immediately.

    Only the arguments that are not None are touched, so a GUI control
    can save its own field without restating the others. STRICT: an
    invalid actuator type or an out-of-range number raises ValueError
    rather than being quietly clamped - a value a user typed must never
    turn into a different one behind their back (the lenient clamping
    path is for reading an old/edited file)."""
    cfg = get_haptic_config(path, reload=True)
    new = HapticConfig(
        using=cfg.using,
        lra=ActuatorDefaults(cfg.lra.default_frequency, cfg.lra.default_amp),
        erm=ActuatorDefaults(cfg.erm.default_frequency, cfg.erm.default_amp),
    )

    if using is not None:
        name = normalise_actuator_type(using)
        if name is None:
            raise ValueError(
                f"unknown actuator type {using!r} - expected one of "
                f"{', '.join(ACTUATOR_TYPES)}")
        new.using = name

    for actuator, freq, amp in ((LRA, lra_frequency, lra_amp),
                                (ERM, erm_frequency, erm_amp)):
        target = new.for_type(actuator)
        if freq is not None:
            if not is_frequency_valid(freq, actuator):
                low, high = frequency_range(actuator)
                raise ValueError(
                    f"{actuator_label(actuator)} default frequency {freq!r} "
                    f"is outside the allowed {low}-{high} Hz range")
            target.default_frequency = int(freq)
        if amp is not None:
            if not is_amp_valid(amp):
                raise ValueError(
                    f"{actuator_label(actuator)} default amp {amp!r} is "
                    f"outside the allowed {AMP_MIN}-{AMP_MAX} range")
            target.default_amp = int(amp)

    return save_haptic_config(new, path)


# =========================================================================
# Convenience accessors (what the rest of the project actually calls)
# =========================================================================

def get_active_haptic_type(path: Optional[Path] = None) -> str:
    """The actuator id currently in use ("lra"/"erm")."""
    return get_haptic_config(path).using


def get_actuator_defaults(actuator_type: str,
                          path: Optional[Path] = None) -> ActuatorDefaults:
    """One actuator's configured default frequency/amp."""
    return get_haptic_config(path).for_type(actuator_type)


def get_active_haptic_defaults(path: Optional[Path] = None) -> ActuatorDefaults:
    """The default frequency/amp of the actuator named by `using`."""
    return get_haptic_config(path).active


def get_default_frequency(actuator_type: Optional[str] = None,
                          path: Optional[Path] = None) -> int:
    """Default drive frequency (Hz) - of `actuator_type`, or of the
    actuator in use when it is None."""
    cfg = get_haptic_config(path)
    defaults = cfg.active if actuator_type is None else cfg.for_type(actuator_type)
    return defaults.default_frequency


def get_default_amp(actuator_type: Optional[str] = None,
                    path: Optional[Path] = None) -> int:
    """Default drive amp - of `actuator_type`, or of the actuator in use
    when it is None."""
    cfg = get_haptic_config(path)
    defaults = cfg.active if actuator_type is None else cfg.for_type(actuator_type)
    return defaults.default_amp


def get_actuator_motor_port(actuator_type: Optional[str] = None) -> int:
    """The motor port this actuator is wired to by convention. A WIRING
    fact (not a config default): every window that uses it still lets the
    operator override the port."""
    cfg_type = (normalise_actuator_type(actuator_type)
                if actuator_type is not None else get_active_haptic_type())
    return ACTUATOR_MOTOR_PORTS[cfg_type or DEFAULT_USING]


def frequency_note(actuator_type: str, frequency: int) -> str:
    """"" when `frequency` sits in the actuator's typical band, else a
    short sentence saying why the value is unusual. Legal-but-unusual is
    allowed on purpose - the rig is a research rig."""
    name = normalise_actuator_type(actuator_type) or DEFAULT_USING
    low, high = TYPICAL_FREQUENCY_HZ[name]
    if low <= frequency <= high:
        return ""
    if name == LRA:
        return (f"{frequency} Hz is outside the LRA's usual "
                f"{low}-{high} Hz band - an LRA driven off its mechanical "
                "resonance vibrates weakly. Re-measure with the LRA "
                "Frequency Sweep before adopting it.")
    return (f"{frequency} Hz is outside the usual {low}-{high} Hz ERM "
            "carrier band - below ~1 kHz the chopped drive stops looking "
            "like smooth DC and the rotor may not start at all.")


def describe_active(path: Optional[Path] = None) -> str:
    """Two-line summary for status bars and window headers:

        Current haptic actuator: ERM
        Default drive: 1000 Hz, amp 80
    """
    cfg = get_haptic_config(path)
    defaults = cfg.active
    return (f"Current haptic actuator: {actuator_label(cfg.using)}\n"
            f"Default drive: {defaults.default_frequency} Hz, "
            f"amp {defaults.default_amp}")


def summary_line(path: Optional[Path] = None) -> str:
    """One-line form of describe_active(), for tooltips and log lines."""
    cfg = get_haptic_config(path)
    defaults = cfg.active
    return (f"{actuator_label(cfg.using)} @ {defaults.default_frequency} Hz, "
            f"amp {defaults.default_amp}")


def config_snapshot(path: Optional[Path] = None) -> dict:
    """The whole haptic config as it stands right now, for an experiment
    to record in its meta.json alongside the values it actually sent."""
    cfg = get_haptic_config(path)
    snapshot = cfg.to_dict()
    snapshot["motor_ports"] = dict(ACTUATOR_MOTOR_PORTS)
    return snapshot


def value_source(value, default_value) -> str:
    """"config_default" when a run used the configured default for a
    parameter, "manual" when the operator typed something else - recorded
    in the experiment meta so a run can be read back correctly."""
    return "config_default" if value == default_value else "manual"


# =========================================================================
# Change notification (so open windows can refresh)
# =========================================================================

_listeners: List[Callable[[HapticConfig], None]] = []


def subscribe(callback: Callable[[HapticConfig], None]) -> Callable:
    """Call `callback(config)` whenever the haptic config is saved.

    Windows subscribe on open and unsubscribe on close, so editing the
    defaults in Initial Setup refreshes the windows that are already
    open; a window opened later simply reads the new file."""
    if callback not in _listeners:
        _listeners.append(callback)
    return callback


def unsubscribe(callback: Callable[[HapticConfig], None]) -> None:
    if callback in _listeners:
        _listeners.remove(callback)


def notify_listeners(cfg: HapticConfig) -> None:
    """Fire every subscriber. A listener that raises (e.g. a Qt widget
    already destroyed) must not break the save that triggered it."""
    for callback in list(_listeners):
        try:
            callback(cfg)
        except Exception:
            log.exception("haptic config listener failed")
