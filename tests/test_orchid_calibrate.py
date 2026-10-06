"""Calibration wrapper tests: fake bus only, no serial devices."""
import contextlib
from dataclasses import dataclass
import io
import json
from pathlib import Path
import tempfile
import unittest

import orchid_calibrate as cal
import orchid_key as key


@dataclass
class Calibration:
    id: int
    drive_mode: int = 0
    homing_offset: int = 0
    range_min: int = 1000
    range_max: int = 3000


class Bus:
    def __init__(self):
        self.is_connected = False
        self.flags = dict.fromkeys(key.MOTORS, 0)
        self.hardware = {m: Calibration(i) for i, m in enumerate(key.MOTORS, 1)}
        self.writes = []
        self.small_range = False
        self.bad_readback = False
        self.disconnected = None

    def connect(self):
        self.is_connected = True

    def disconnect(self, disable_torque=True):
        self.disconnected = disable_torque
        self.is_connected = False

    def sync_read(self, register, motors=None, **kwargs):
        if register == "Torque_Enable":
            return dict(self.flags)
        value = {"Present_Voltage": 120, "Operating_Mode": 0, "Lock": 1, "Present_Position": 2047}[register]
        return dict.fromkeys(motors or key.MOTORS, value)

    def read_calibration(self):
        if self.bad_readback:
            self.bad_readback = False
            return {}
        return dict(self.hardware)

    def write_calibration(self, value):
        self.hardware = dict(value)

    def write(self, register, motor, value, **kwargs):
        if register == "Torque_Enable":
            assert value == 0
        assert register != "Goal_Position"
        self.writes.append((register, motor, value))

    def set_half_turn_homings(self):
        self.hardware = {m: Calibration(i, homing_offset=100) for i, m in enumerate(key.MOTORS, 1)}
        return dict.fromkeys(key.MOTORS, 100)

    def record_ranges_of_motion(self, motors, display_values=True):
        assert not display_values
        self.sync_read("Present_Position", motors, normalize=False)
        return dict.fromkeys(motors, 2000 if self.small_range else 900), dict.fromkeys(motors, 2010 if self.small_range else 3200)


class Robot:
    def __init__(self, path):
        self.calibration_fpath = path
        self.bus = Bus()
        self.calibration = dict(self.bus.hardware)
        self.fail_after_homing = False

    def connect(self):
        raise AssertionError("robot.connect would configure/enable torque")

    def _save_calibration(self):
        raise AssertionError("unguarded save")

    def calibrate(self):
        offsets = self.bus.set_half_turn_homings()
        if self.fail_after_homing:
            raise KeyboardInterrupt()
        motors = [m for m in key.MOTORS if m != "wrist_roll"]
        mins, maxes = self.bus.record_ranges_of_motion(motors)
        mins["wrist_roll"], maxes["wrist_roll"] = 0, 4095
        self.calibration = {m: Calibration(i, homing_offset=offsets[m], range_min=mins[m], range_max=maxes[m])
                            for i, m in enumerate(key.MOTORS, 1)}
        self.bus.write_calibration(self.calibration)
        self._save_calibration()


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.path = self.folder / "calibration.json"
        self.original = '{"original": true}\n'
        self.path.write_text(self.original)
        self.robot = Robot(self.path)
        self.run = cal.CalibrationRun(self.robot, self.folder / "backup", sleep=lambda _: None)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_calibrates_all_six_verifies_and_saves_without_robot_connect(self):
        self.run.run()
        saved = json.loads(self.path.read_text())
        self.assertEqual(set(saved), set(key.MOTORS))
        self.assertEqual(saved["wrist_roll"]["range_max"], 4095)
        self.assertEqual(self.run.stage, "complete")
        self.assertEqual((self.run.folder / "previous-calibration.json").read_text(), self.original)
        self.assertFalse(self.robot.bus.disconnected)
        self.assertEqual(len(self.robot.bus.writes), 6)  # Restore EEPROM locks only.

    def test_motor_on_refuses_without_disabling_it(self):
        self.robot.bus.flags["elbow_flex"] = 1
        with self.assertRaises(key.SafetyError):
            self.run.run()
        self.assertFalse(self.robot.bus.writes)
        self.assertEqual(self.path.read_text(), self.original)

    def test_stop_after_homing_restores_previous_hardware_and_preserves_file(self):
        old = self.robot.bus.read_calibration()
        self.robot.fail_after_homing = True
        with self.assertRaises(KeyboardInterrupt):
            self.run.run()
        self.assertEqual(self.robot.bus.read_calibration(), old)
        self.assertEqual(self.path.read_text(), self.original)
        self.assertFalse(self.robot.bus.disconnected)

    def test_small_ranges_never_saved(self):
        old = self.robot.bus.read_calibration()
        self.robot.bus.small_range = True
        with self.assertRaisesRegex(key.SafetyError, "usable range"):
            self.run.run()
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(self.robot.bus.read_calibration(), old)

    def test_readback_mismatch_never_saved(self):
        original = self.robot.bus.write_calibration
        count = 0
        def corrupt_readback(value):
            nonlocal count
            count += 1
            original(value)
            if count == 1:
                self.robot.bus.bad_readback = True
        self.robot.bus.write_calibration = corrupt_readback
        with self.assertRaisesRegex(key.SafetyError, "readback failed"):
            self.run.run()
        self.assertEqual(self.path.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
