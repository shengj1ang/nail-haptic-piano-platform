"""Unit tests for the shared haptic actuator configuration
(common/haptic_config.py) and for the places that consume it: app.config,
the Initial Setup window, the validation-experiment windows and the
experiment modules' defaults.

Covers, in order: an old config with no haptic block at all, a partial
block, per-actuator defaults, saving/reloading, illegal values (rejected
or safely clamped, never sent to hardware), other config keys surviving a
haptic save, the Initial Setup controls, the validation windows' defaults
and prose following the config, manual experiment values NOT being
overwritten by it, historical runs still using their own meta, and Test
Buzz driving the configured actuator.

Every test runs against a TEMPORARY config.json - the project's own
config file is never read or written.

Run from main/:  python test-script/test_haptic_config.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import haptic_config as hc  # noqa: E402

# The GUI tests need a Qt platform that works without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def write_config(path, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


class _TempConfigCase(unittest.TestCase):
    """Points common.haptic_config at a throwaway config.json for the
    duration of one test, so nothing here can touch the real one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="haptic_config_test_")
        self.config_path = Path(self._tmp.name) / "config.json"
        self._real_path = hc.DEFAULT_CONFIG_PATH
        hc.DEFAULT_CONFIG_PATH = self.config_path
        hc.invalidate_cache()

    def tearDown(self) -> None:
        hc.DEFAULT_CONFIG_PATH = self._real_path
        hc.invalidate_cache()
        self._tmp.cleanup()


# =========================================================================
# 1-2. Missing / partial haptic block
# =========================================================================

class TestBackwardCompatibility(_TempConfigCase):

    def test_no_haptic_key_at_all_uses_builtin_defaults(self):
        """An old config.json predating the haptic block must load, not
        raise, and must produce the documented built-in defaults."""
        write_config(self.config_path,
                     {"camera": {"index": 0}, "visual_cue_style": "hand"})
        config = hc.get_haptic_config(reload=True)
        self.assertEqual(config.using, "lra")
        self.assertEqual(config.lra.default_frequency, 224)
        self.assertEqual(config.lra.default_amp, 64)
        self.assertEqual(config.erm.default_frequency, 1000)
        self.assertEqual(config.erm.default_amp, 80)
        self.assertEqual(hc.last_warnings(), [])

    def test_missing_config_file_is_not_an_error(self):
        config = hc.get_haptic_config(reload=True)  # file never created
        self.assertEqual(config.using, hc.DEFAULT_USING)
        self.assertEqual(config.active.default_frequency, 224)

    def test_partial_block_fills_only_the_missing_fields(self):
        """Present fields are kept exactly; absent ones fall back."""
        write_config(self.config_path, {
            "haptic": {"using": "erm", "erm": {"default_amp": 90}},
        })
        config = hc.get_haptic_config(reload=True)
        self.assertEqual(config.using, "erm")
        self.assertEqual(config.erm.default_amp, 90)          # kept
        self.assertEqual(config.erm.default_frequency, 1000)  # filled in
        self.assertEqual(config.lra.default_frequency, 224)   # whole block
        self.assertEqual(config.lra.default_amp, 64)

    def test_haptic_block_of_the_wrong_type_falls_back(self):
        write_config(self.config_path, {"haptic": "lra"})
        config = hc.get_haptic_config(reload=True)
        self.assertEqual(config.using, hc.DEFAULT_USING)
        self.assertTrue(hc.last_warnings())

    def test_actuator_block_of_the_wrong_type_falls_back(self):
        write_config(self.config_path, {"haptic": {"lra": [224, 64]}})
        config = hc.get_haptic_config(reload=True)
        self.assertEqual(config.lra.default_frequency, 224)
        self.assertTrue(hc.last_warnings())


# =========================================================================
# 3-4. `using` selects which defaults are the active ones
# =========================================================================

class TestActiveDefaults(_TempConfigCase):

    def test_using_lra_returns_the_lra_defaults(self):
        write_config(self.config_path, {"haptic": {
            "using": "lra",
            "lra": {"default_frequency": 224, "default_amp": 64},
            "erm": {"default_frequency": 1000, "default_amp": 80}}})
        hc.invalidate_cache()
        self.assertEqual(hc.get_active_haptic_type(), "lra")
        active = hc.get_active_haptic_defaults()
        self.assertEqual((active.default_frequency, active.default_amp),
                         (224, 64))
        self.assertEqual(hc.get_default_frequency(), 224)
        self.assertEqual(hc.get_default_amp(), 64)

    def test_using_erm_returns_the_erm_defaults(self):
        write_config(self.config_path, {"haptic": {
            "using": "erm",
            "lra": {"default_frequency": 224, "default_amp": 64},
            "erm": {"default_frequency": 1000, "default_amp": 80}}})
        hc.invalidate_cache()
        self.assertEqual(hc.get_active_haptic_type(), "erm")
        active = hc.get_active_haptic_defaults()
        self.assertEqual((active.default_frequency, active.default_amp),
                         (1000, 80))

    def test_the_inactive_actuator_is_still_reachable(self):
        """Switching `using` must not hide the other actuator's defaults -
        the LRA experiments read them explicitly."""
        hc.update_haptic_config(using="erm")
        lra = hc.get_actuator_defaults("lra")
        self.assertEqual((lra.default_frequency, lra.default_amp), (224, 64))

    def test_actuator_type_is_case_insensitive(self):
        hc.update_haptic_config(using="ERM")
        self.assertEqual(hc.get_active_haptic_type(), "erm")
        self.assertEqual(hc.get_actuator_defaults("LRA").default_frequency, 224)

    def test_summary_and_description_follow_the_config(self):
        hc.update_haptic_config(using="erm")
        self.assertEqual(hc.summary_line(), "ERM @ 1000 Hz, amp 80")
        self.assertIn("Current haptic actuator: ERM", hc.describe_active())
        self.assertIn("Default drive: 1000 Hz, amp 80", hc.describe_active())


# =========================================================================
# 5. Save + reload round trip
# =========================================================================

class TestSaveAndReload(_TempConfigCase):

    def test_changed_values_survive_a_reload(self):
        hc.update_haptic_config(using="erm", lra_frequency=235, lra_amp=70,
                                erm_frequency=2500, erm_amp=120)
        hc.invalidate_cache()
        config = hc.get_haptic_config(reload=True)
        self.assertEqual(config.using, "erm")
        self.assertEqual((config.lra.default_frequency, config.lra.default_amp),
                         (235, 70))
        self.assertEqual((config.erm.default_frequency, config.erm.default_amp),
                         (2500, 120))

    def test_partial_update_leaves_the_other_fields_alone(self):
        hc.update_haptic_config(lra_frequency=240, erm_amp=99)
        hc.update_haptic_config(using="erm")          # only `using`
        config = hc.get_haptic_config(reload=True)
        self.assertEqual(config.lra.default_frequency, 240)
        self.assertEqual(config.erm.default_amp, 99)
        self.assertEqual(config.using, "erm")

    def test_saved_json_matches_the_documented_schema(self):
        hc.update_haptic_config(using="lra")
        with open(self.config_path, encoding="utf-8") as f:
            block = json.load(f)["haptic"]
        self.assertEqual(set(block), {"using", "lra", "erm"})
        self.assertEqual(set(block["lra"]),
                         {"default_frequency", "default_amp"})

    def test_written_file_is_always_valid_json(self):
        """The atomic write must never leave a partial file behind."""
        for freq in (100, 224, 350):
            hc.update_haptic_config(lra_frequency=freq)
            with open(self.config_path, encoding="utf-8") as f:
                json.load(f)  # raises if the file was truncated


# =========================================================================
# 6-7. Illegal values: rejected, or clamped - never sent to hardware
# =========================================================================

class TestValidation(_TempConfigCase):

    def test_illegal_actuator_type_in_the_file_falls_back_safely(self):
        write_config(self.config_path, {"haptic": {"using": "piezo"}})
        config = hc.get_haptic_config(reload=True)
        self.assertEqual(config.using, hc.DEFAULT_USING)
        self.assertTrue(any("using" in w for w in hc.last_warnings()))

    def test_illegal_actuator_type_from_the_gui_is_rejected(self):
        with self.assertRaises(ValueError):
            hc.update_haptic_config(using="piezo")

    def test_out_of_range_stored_values_are_clamped_into_range(self):
        write_config(self.config_path, {"haptic": {
            "lra": {"default_frequency": 999999, "default_amp": 900},
            "erm": {"default_frequency": 1, "default_amp": -5}}})
        config = hc.get_haptic_config(reload=True)
        lra_min, lra_max = hc.frequency_range("lra")
        erm_min, erm_max = hc.frequency_range("erm")
        self.assertEqual(config.lra.default_frequency, lra_max)
        self.assertEqual(config.lra.default_amp, hc.AMP_MAX)
        self.assertEqual(config.erm.default_frequency, erm_min)
        self.assertEqual(config.erm.default_amp, hc.AMP_MIN)
        self.assertEqual(len(hc.last_warnings()), 4)  # all four reported

    def test_non_numeric_values_fall_back_to_the_builtin_default(self):
        write_config(self.config_path, {"haptic": {
            "lra": {"default_frequency": "fast", "default_amp": None}}})
        config = hc.get_haptic_config(reload=True)
        self.assertEqual(config.lra.default_frequency, 224)
        self.assertEqual(config.lra.default_amp, 64)
        self.assertEqual(len(hc.last_warnings()), 2)

    def test_nothing_out_of_range_can_reach_the_hardware(self):
        """Whatever the file says, every value handed out is inside the
        firmware's own 'F'/'S' limits (motor_driver.cpp)."""
        write_config(self.config_path, {"haptic": {
            "using": "erm",
            "lra": {"default_frequency": -40, "default_amp": 4000},
            "erm": {"default_frequency": 10 ** 9, "default_amp": 999}}})
        hc.invalidate_cache()
        for actuator in hc.ACTUATOR_TYPES:
            defaults = hc.get_actuator_defaults(actuator)
            low, high = hc.frequency_range(actuator)
            self.assertGreaterEqual(defaults.default_frequency, low)
            self.assertLessEqual(defaults.default_frequency, high)
            # Whatever the per-actuator band is, it must sit inside what
            # the firmware's F command accepts.
            self.assertGreaterEqual(defaults.default_frequency,
                                    hc.FIRMWARE_FREQ_MIN_HZ)
            self.assertLessEqual(defaults.default_frequency,
                                 hc.FIRMWARE_FREQ_MAX_HZ)
            self.assertGreaterEqual(defaults.default_amp, hc.AMP_MIN)
            self.assertLessEqual(defaults.default_amp, hc.AMP_MAX)

    def test_out_of_range_values_from_the_gui_are_rejected_not_clamped(self):
        """A number the USER typed must never turn into a different one."""
        with self.assertRaises(ValueError):
            hc.update_haptic_config(lra_amp=300)
        with self.assertRaises(ValueError):
            hc.update_haptic_config(erm_frequency=hc.FIRMWARE_FREQ_MAX_HZ + 1)
        with self.assertRaises(ValueError):
            hc.update_haptic_config(lra_frequency=hc.FIRMWARE_FREQ_MIN_HZ - 1)

    def test_a_rejected_update_changes_nothing_on_disk(self):
        hc.update_haptic_config(lra_amp=70)
        with self.assertRaises(ValueError):
            hc.update_haptic_config(lra_amp=999)
        self.assertEqual(hc.get_haptic_config(reload=True).lra.default_amp, 70)

    def test_ranges_are_derived_from_the_firmware_limits(self):
        for actuator in hc.ACTUATOR_TYPES:
            low, high = hc.frequency_range(actuator)
            self.assertEqual(low, hc.FIRMWARE_FREQ_MIN_HZ)
            self.assertLessEqual(high, hc.FIRMWARE_FREQ_MAX_HZ)
        self.assertEqual(hc.amp_range(), (0, 255))


# =========================================================================
# 8. Saving haptic config preserves every other key
# =========================================================================

class TestOtherConfigKeysPreserved(_TempConfigCase):

    OTHER = {
        "camera": {"index": 2, "width": 1280},
        "midi": {"port_name": "SE25 MIDI1"},
        "seeds": {"sequence_generator": 1799551287},
        "active_keyboard_profile": "white-city-lab-20260717",
        "visual_cue_style": "hand",
        "some_future_key": {"kept": True},
    }

    def test_haptic_save_keeps_every_unrelated_key(self):
        write_config(self.config_path, dict(self.OTHER))
        hc.update_haptic_config(using="erm", erm_amp=77)
        with open(self.config_path, encoding="utf-8") as f:
            data = json.load(f)
        for key, value in self.OTHER.items():
            self.assertEqual(data[key], value, f"{key} was lost or changed")
        self.assertEqual(data["haptic"]["using"], "erm")

    def test_app_config_save_keeps_unknown_keys_and_the_haptic_block(self):
        from app.config import Config
        write_config(self.config_path, dict(self.OTHER))
        hc.update_haptic_config(using="erm", erm_amp=77)
        config = Config.load(self.config_path)
        self.assertEqual(config.haptic.using, "erm")
        config.save(self.config_path)
        with open(self.config_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["some_future_key"], {"kept": True})
        self.assertEqual(data["haptic"]["erm"]["default_amp"], 77)
        self.assertEqual(data["active_keyboard_profile"],
                         "white-city-lab-20260717")

    def test_app_config_load_survives_a_config_without_haptic(self):
        from app.config import Config
        write_config(self.config_path, dict(self.OTHER))
        config = Config.load(self.config_path)
        self.assertEqual(config.haptic.using, "lra")
        self.assertEqual(config.haptic.lra.default_amp, 64)


# =========================================================================
# 9. The Initial Setup window reads and saves the config
# =========================================================================

class TestInitialSetupWindow(_TempConfigCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from app.gui.haptic_config_window import HapticConfigWindow
        window = HapticConfigWindow(None)
        self.addCleanup(window.close)
        return window

    def test_controls_open_on_the_stored_values(self):
        hc.update_haptic_config(using="erm", lra_frequency=230, lra_amp=70,
                                erm_frequency=1800, erm_amp=44)
        window = self._window()
        self.assertTrue(window._radios["erm"].isChecked())
        self.assertEqual(window._boxes["lra"].values(), (230, 70))
        self.assertEqual(window._boxes["erm"].values(), (1800, 44))

    def test_spin_ranges_match_the_config_validation_ranges(self):
        window = self._window()
        for actuator in hc.ACTUATOR_TYPES:
            box = window._boxes[actuator]
            low, high = hc.frequency_range(actuator)
            self.assertEqual((box.freq_spin.minimum(), box.freq_spin.maximum()),
                             (low, high))
            self.assertEqual((box.amp_spin.minimum(), box.amp_spin.maximum()),
                             hc.amp_range())

    def test_editing_a_control_saves_immediately(self):
        window = self._window()
        window._boxes["lra"].freq_spin.setValue(240)
        window._save_timer.stop()   # fire the debounced write now
        window._save()
        self.assertEqual(hc.get_haptic_config(reload=True).lra.default_frequency,
                         240)

    def test_switching_actuator_saves_immediately(self):
        window = self._window()
        window._radios["erm"].setChecked(True)
        self.assertEqual(hc.get_active_haptic_type(), "erm")
        self.assertIn("ERM", window.summary.text())

    def test_a_pending_edit_is_written_when_the_window_closes(self):
        window = self._window()
        window._boxes["erm"].amp_spin.setValue(111)   # debounce still pending
        window.close()
        self.assertEqual(hc.get_haptic_config(reload=True).erm.default_amp, 111)

    def test_load_time_corrections_are_shown_not_hidden(self):
        write_config(self.config_path,
                     {"haptic": {"lra": {"default_amp": 900}}})
        hc.invalidate_cache()
        window = self._window()
        self.assertTrue(window.warnings.isVisible()
                        or bool(window.warnings.text()))
        self.assertEqual(window._boxes["lra"].amp_spin.value(), hc.AMP_MAX)


# =========================================================================
# 10-11, 13. The validation windows follow the config, but not over the user
# =========================================================================

class TestValidationWindows(_TempConfigCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, cls):
        window = cls(None)
        self.addCleanup(window.close)
        return window

    def test_delay_window_opens_on_the_configured_actuator(self):
        from app.gui.validation_experiment_window import MotorAccDelayWindow
        hc.update_haptic_config(using="erm", erm_frequency=1500, erm_amp=80)
        window = self._window(MotorAccDelayWindow)
        self.assertEqual(window.actuator_combo.currentText(), "ERM")
        self.assertEqual(window.freq_spin.value(), 1500)
        self.assertEqual(window.amp_spin.value(), 80)
        self.assertEqual(window.motor_spin.value(),
                         hc.get_actuator_motor_port("erm"))

    def test_delay_window_description_states_the_configured_drive(self):
        from app.gui.validation_experiment_window import MotorAccDelayWindow
        hc.update_haptic_config(using="erm", erm_frequency=1500, erm_amp=80)
        window = self._window(MotorAccDelayWindow)
        text = window._description_text() + window._haptic_info_text()
        self.assertIn("1500", text)
        self.assertIn("80", text)
        self.assertNotIn("224", text)

    def test_amplitude_window_quotes_the_configured_lra_values(self):
        from app.gui.validation_experiment_window import AmplitudeSweepWindow
        hc.update_haptic_config(lra_frequency=232, lra_amp=68)
        window = self._window(AmplitudeSweepWindow)
        self.assertEqual(window.freq_spin.value(), 232)
        self.assertIn("232", window._description_text())
        self.assertIn("68", window._description_text())

    def test_frequency_sweep_window_still_sweeps_its_whole_range(self):
        """Point 6: the resonance sweep must not collapse onto the
        configured default frequency."""
        from validation_experiments.lra_resonance_intensity_calibration \
            import lra_frequency_sweep
        hc.update_haptic_config(lra_frequency=300)
        self.assertEqual(lra_frequency_sweep.COARSE_START_HZ, 100)
        self.assertEqual(lra_frequency_sweep.COARSE_STOP_HZ, 350)
        self.assertLess(lra_frequency_sweep.COARSE_STEP_HZ, 350)

    def test_amplitude_sweep_still_sweeps_its_own_amp_points(self):
        from validation_experiments.lra_resonance_intensity_calibration \
            import lra_amplitude_sweep
        hc.update_haptic_config(lra_amp=64)
        self.assertGreater(len(lra_amplitude_sweep.AMP_VALUES), 10)
        self.assertNotEqual(lra_amplitude_sweep.AMP_VALUES, [64])

    def test_a_user_edited_value_is_not_overwritten_at_start(self):
        """Point 11: after the operator types a drive, the config must not
        be re-applied when the run is launched."""
        from app.gui.validation_experiment_window import MotorAccDelayWindow
        hc.update_haptic_config(using="lra", lra_frequency=224, lra_amp=64)
        window = self._window(MotorAccDelayWindow)
        window.amp_spin.setValue(100)
        window.freq_spin.setValue(900)
        kwargs = window._extra_run_kwargs()
        self.assertEqual(kwargs["amp"], 100)
        self.assertEqual(kwargs["pwm_freq_hz"], 900)
        # ... even if the config changes between the edit and the launch.
        hc.update_haptic_config(lra_amp=30)
        self.assertEqual(window._extra_run_kwargs()["amp"], 100)

    def test_test_buzz_uses_the_configured_actuator(self):
        """Point 13: the buzz drive is the configured one, and it is the
        frequency actually pushed to the port before the pulse."""
        from app.gui.validation_experiment_window import (
            MotorAccDelayWindow, SpectrogramWindow)
        hc.update_haptic_config(using="erm", erm_frequency=1200, erm_amp=55)
        for cls in (SpectrogramWindow, MotorAccDelayWindow):
            window = self._window(cls)
            freq, amp, label = window._buzz_defaults()
            self.assertEqual((freq, amp, label), (1200, 55, "ERM"), cls.__name__)
            self.assertIn(f"F {window.motor_spin.value()} 1200",
                          window._pre_buzz_cmds(window.motor_spin.value()))

    def test_spectrogram_type_change_preserves_the_selected_motor_port(self):
        """Actuator type changes ranges and buzz defaults, never wiring."""
        from app.gui.validation_experiment_window import SpectrogramWindow
        window = self._window(SpectrogramWindow)
        self.assertEqual(window.motor_spin.value(), 0)

        erm_index = window.type_combo.findData("ERM")
        self.assertGreaterEqual(erm_index, 0)
        window.type_combo.setCurrentIndex(erm_index)

        self.assertEqual(window.motor_spin.value(), 0)
        self.assertEqual(window.freq_min_spin.value(), 50)
        self.assertEqual(window.freq_max_spin.value(), 5000)

        lra_index = window.type_combo.findData("LRA")
        window.type_combo.setCurrentIndex(lra_index)
        self.assertEqual(window.motor_spin.value(), 0)
        self.assertEqual(window.freq_max_spin.value(), 350)

    def test_lra_experiments_buzz_with_the_lra_config(self):
        """An LRA experiment stays on the LRA's configured drive even when
        the rig is set to use the ERM - an LRA buzzed at an ERM carrier
        would not move, which is useless as a wiring check."""
        from app.gui.validation_experiment_window import (
            AmplitudeSweepWindow, FrequencySweepWindow)
        hc.update_haptic_config(using="erm", lra_frequency=226, lra_amp=66)
        for cls in (FrequencySweepWindow, AmplitudeSweepWindow):
            window = self._window(cls)
            self.assertEqual(window._buzz_defaults(), (226, 66, "LRA"),
                             cls.__name__)


# =========================================================================
# 12. Historical runs keep their own parameters
# =========================================================================

class TestHistoricalRuns(_TempConfigCase):

    def test_delay_meta_parameters_win_over_the_current_config(self):
        """Re-rendering a saved run must describe THAT run."""
        from validation_experiments.motor_acc_delay_experiment \
            import motor_acc_delay
        hc.update_haptic_config(using="erm", erm_amp=200, erm_frequency=3000)
        params = {"amp": 64, "pwm_freq_hz": 224, "actuator_type": "LRA"}
        self.assertEqual(params.get("amp", motor_acc_delay.HISTORICAL_AMP), 64)
        # A run saved before the field existed falls back to the fixed
        # historical value, never to today's configured default.
        self.assertEqual(motor_acc_delay.HISTORICAL_AMP, 64)
        self.assertNotEqual(motor_acc_delay.HISTORICAL_AMP,
                            hc.get_default_amp())

    def test_spectrogram_render_fallbacks_are_fixed_not_configured(self):
        from validation_experiments.actuator_spectrogram import (
            actuator_spectrogram)
        hc.update_haptic_config(using="lra")
        self.assertEqual(actuator_spectrogram.HISTORICAL_MOTOR_TYPE, "ERM")
        self.assertEqual(actuator_spectrogram.HISTORICAL_MOTOR_INDEX, 10)

    def test_amplitude_sweep_render_fallback_is_fixed(self):
        from validation_experiments.lra_resonance_intensity_calibration \
            import lra_amplitude_sweep
        hc.update_haptic_config(lra_frequency=300)
        self.assertEqual(lra_amplitude_sweep.HISTORICAL_FREQ_HZ, 224)


# =========================================================================
# Motor port stays a wiring fact, and the firmware boot default stays fixed
# =========================================================================

class TestPortsAndFirmwareConstants(_TempConfigCase):

    def test_motor_ports_are_not_stored_in_the_config_file(self):
        hc.update_haptic_config(using="erm")
        with open(self.config_path, encoding="utf-8") as f:
            block = json.load(f)["haptic"]
        self.assertNotIn("motor_port", json.dumps(block))
        self.assertEqual(set(block), {"using", "lra", "erm"})

    def test_the_one_port_mapping_is_shared(self):
        from validation_experiments.motor_acc_delay_experiment \
            import motor_acc_delay
        self.assertEqual(motor_acc_delay.ACTUATOR_MOTORS["LRA"],
                         hc.get_actuator_motor_port("lra"))
        self.assertEqual(motor_acc_delay.ACTUATOR_MOTORS["ERM"],
                         hc.get_actuator_motor_port("erm"))

    def test_firmware_boot_frequency_is_independent_of_the_config(self):
        """The restore-on-exit frequency must keep matching the firmware,
        whatever the LRA default is set to."""
        hc.update_haptic_config(lra_frequency=300)
        self.assertEqual(hc.FIRMWARE_BOOT_PWM_HZ, 224)
        from validation_experiments.lra_resonance_intensity_calibration \
            import lra_frequency_sweep
        self.assertEqual(lra_frequency_sweep.DEFAULT_PWM_FREQ, 224)

    def test_snapshot_records_the_whole_config_plus_ports(self):
        hc.update_haptic_config(using="erm", erm_amp=40)
        snapshot = hc.config_snapshot()
        self.assertEqual(snapshot["using"], "erm")
        self.assertEqual(snapshot["erm"]["default_amp"], 40)
        self.assertEqual(snapshot["motor_ports"]["erm"],
                         hc.get_actuator_motor_port("erm"))

    def test_value_source_labels_defaults_and_overrides(self):
        self.assertEqual(hc.value_source(64, 64), "config_default")
        self.assertEqual(hc.value_source(100, 64), "manual")


# =========================================================================
# Change notification (open windows refresh)
# =========================================================================

class TestNotification(_TempConfigCase):

    def test_subscribers_are_called_on_save(self):
        seen = []
        callback = hc.subscribe(lambda config: seen.append(config.using))
        self.addCleanup(hc.unsubscribe, callback)
        hc.update_haptic_config(using="erm")
        self.assertEqual(seen, ["erm"])

    def test_a_failing_subscriber_does_not_break_the_save(self):
        def boom(config):
            raise RuntimeError("widget already destroyed")
        self.addCleanup(hc.unsubscribe, hc.subscribe(boom))
        hc.update_haptic_config(using="erm")
        self.assertEqual(hc.get_haptic_config(reload=True).using, "erm")

    def test_a_new_read_sees_a_change_made_outside_this_process(self):
        hc.get_haptic_config(reload=True)
        write_config(self.config_path,
                     {"haptic": {"using": "erm",
                                 "erm": {"default_amp": 33}}})
        # No explicit invalidation: the mtime/size stamp must catch it.
        self.assertEqual(hc.get_haptic_config().using, "erm")
        self.assertEqual(hc.get_default_amp(), 33)


# =========================================================================
# The haptic quiz cue uses the configured drive
# =========================================================================

class TestHapticCue(_TempConfigCase):

    def test_cue_helpers_follow_the_config(self):
        from app import haptic_cue
        hc.update_haptic_config(using="erm", erm_frequency=1100, erm_amp=45)
        self.assertEqual(haptic_cue.cue_amplitude(), 45)
        self.assertEqual(haptic_cue.cue_frequency(), 1100)

    def test_cue_sends_the_configured_amp_and_frequency(self):
        """The cue must tune the port as well as set the amp: the firmware
        boots every pin at the LRA's resonance, where an ERM never starts."""
        from app import haptic_cue

        class FakeController:
            def __init__(self):
                self.sent = []

            def connect(self):
                pass

            def stop_all(self):
                self.sent.append("X")

            def send(self, cmd):
                self.sent.append(cmd)

            def close(self):
                pass

        hc.update_haptic_config(using="erm", erm_frequency=1100, erm_amp=45)
        fake = FakeController()
        original = haptic_cue.VibratorController
        haptic_cue.VibratorController = lambda: fake
        try:
            cue = haptic_cue.HapticCueOutput()
            cue.show_target(60, "R2")
        finally:
            haptic_cue.VibratorController = original

        motor = haptic_cue.FINGER_TO_MOTOR["R2"]
        self.assertIn(f"F {motor} 1100", fake.sent)
        self.assertIn(f"S {1 << motor} 45", fake.sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
