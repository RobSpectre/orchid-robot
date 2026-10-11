"""Core motion safety (entries, plans, the Controller and the Arm adapter) with fake motors only. Never opens a serial or MIDI device."""

import contextlib
import io
import json
import unittest
from unittest.mock import Mock

from orchid_demo import motion


def pose(offset=0):
    return {m: 2000 + (offset if m == "wrist_flex" else 0) for m in motion.MOTORS}


def entry():
    return {
        "path": [pose(), pose(12), pose(24), pose(36)],
        "touch_index": 2,
        "complete": True,
        "padded_tip": True,
        "calibration_sha256": "a" * 64,
        "lerobot_version": "test",
        "fixture_note": "synthetic test fixture",
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class FakeArm:
    def __init__(self, current=None):
        self.current = dict(current or pose())
        self.commands = []
        self.enabled = False
        self.jammed = False
        self.fail_read = False

    def read(self):
        if self.fail_read:
            raise ConnectionError("serial lost")
        return dict(self.current)

    def read_raw(self):
        return self.read()

    def limit_issues(self, current):
        return []

    def require_torque(self, enabled):
        motion.require(self.enabled == enabled, "unexpected torque state")

    def send(self, target):
        self.commands.append(dict(target))
        if not self.jammed:
            self.current = dict(target)

    def arm_at_current(self, current):
        self.require_torque(False)
        self.send(current)
        self.enabled = True


class PlanTests(unittest.TestCase):
    def test_partial_press_uses_only_taught_contact_path(self):
        original = entry()
        result = motion.plan(original, 0.25)
        self.assertEqual(result, [pose(), pose(12), pose(24), pose(27)])
        self.assertEqual(original, entry())
        self.assertEqual(motion.plan(original, 1), original["path"])

    def test_hover_only_is_available_before_contact_teaching(self):
        data = {**entry(), "path": [pose()], "complete": False, "touch_index": None, "padded_tip": False}
        self.assertEqual(motion.plan(data, hover_only=True), [pose()])
        with self.assertRaisesRegex(motion.SafetyError, "Teach hover"):
            motion.plan(data)

    def test_rejects_unknown_key_and_invalid_paths(self):
        variants = [None]
        for mutate in (
            lambda d: d["path"][1].update(wrist_flex=2100),
            lambda d: d["path"][1].update(gripper=2100),
            lambda d: d["path"][1].update(wrist_flex=float("nan")),
            lambda d: d["path"][1].pop("gripper"),
            lambda d: d.update(touch_index=True),
            lambda d: d.update(padded_tip=False),
            lambda d: d.update(path=[pose(12 * i) for i in range(12)]),
            lambda d: d.update(path=[pose(12 * i) for i in range(7)], touch_index=1),
        ):
            data = entry()
            mutate(data)
            variants.append(data)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(motion.SafetyError):
                motion.plan(data)

    def test_rejects_invalid_fractions(self):
        for fraction in (0, -1, 1.1, float("nan"), float("inf")):
            with self.subTest(fraction=fraction), self.assertRaises(motion.SafetyError):
                motion.plan(entry(), fraction)

    def test_trajectory_stays_between_endpoints_and_respects_duration(self):
        samples = list(motion.segment(pose(), pose(24)))
        self.assertEqual(samples[-1], pose(24))
        self.assertGreaterEqual(len(samples) * motion.PERIOD, 1.875 * 24 / motion.SPEED)
        values = [p["wrist_flex"] for p in samples]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(2000 <= v <= 2024 for v in values))
        self.assertTrue(all(p["gripper"] == 2000 for p in samples))
        # Quantization permits a one-tick rounding difference from the rate bound.
        self.assertLessEqual(max(b - a for a, b in zip(values, values[1:])), motion.SPEED * motion.PERIOD + 1)

    def controller(self, arm=None, midi=None):
        clock = FakeClock()
        log = io.StringIO()
        arm = arm or FakeArm()
        controller = motion.Controller(arm, motion.plan(entry()), log, midi, clock, clock.sleep)
        return controller, arm, clock, log

    def run_quietly(self, controller):
        with contextlib.redirect_stdout(io.StringIO()):
            controller.run(0.2)

    def test_one_complete_press_reverses_to_hover_with_torque_on(self):
        c, arm, _, log = self.controller()
        self.run_quietly(c)
        self.assertEqual(arm.current, pose())
        self.assertTrue(arm.enabled)
        self.assertEqual(max(p["wrist_flex"] for p in arm.commands), 2036)
        self.assertTrue(all(p["gripper"] == 2000 for p in arm.commands))
        events = [json.loads(line) for line in log.getvalue().splitlines()]
        self.assertEqual(events[-1]["stage"], "complete")
        self.assertFalse(events[-1]["midi_verified"])
        down = [e["target"]["wrist_flex"] for e in events if e["stage"] == "down"]
        up = [e["target"]["wrist_flex"] for e in events if e["stage"] == "up"]
        self.assertEqual(down, sorted(down))
        self.assertEqual(up, sorted(up, reverse=True))

    def test_wrong_start_rejects_before_any_motor_write(self):
        c, arm, _, _ = self.controller(FakeArm(pose(9)))
        with self.assertRaisesRegex(motion.SafetyError, "hover"):
            c.run(0.2)
        self.assertFalse(arm.enabled)
        self.assertEqual(arm.commands, [])

    def test_gripper_mismatch_rejects_before_arming(self):
        current = {**pose(), "gripper": 2010}
        c, arm, _, _ = self.controller(FakeArm(current))
        with self.assertRaises(motion.SafetyError):
            c.run(0.2)
        self.assertEqual(arm.commands, [])

    def test_start_correction_is_interpolated(self):
        c, arm, _, _ = self.controller(FakeArm(pose(-6)))
        self.run_quietly(c)
        self.assertLessEqual(max(motion.distance(a, b) for a, b in zip(arm.commands, arm.commands[1:])), 2)

    def test_jammed_joint_aborts_without_finishing_press(self):
        arm = FakeArm()
        arm.jammed = True
        c, arm, _, _ = self.controller(arm)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(motion.SafetyError):
            c.run(0.2)
        self.assertLess(max(p["wrist_flex"] for p in arm.commands), 2036)
        before = len(arm.commands)
        with contextlib.redirect_stderr(io.StringIO()):
            c.stop()
        self.assertEqual(len(arm.commands), before + 1)
        self.assertEqual(arm.commands[-1], pose())
        self.assertTrue(arm.enabled)

    def test_stale_read_sends_no_target(self):
        c, arm, clock, _ = self.controller()
        arm.enabled = True
        original = arm.read
        def delayed():
            clock.sleep(0.3)
            return original()
        arm.read = delayed
        with self.assertRaisesRegex(motion.SafetyError, "Stale"):
            c.tick(pose(), "test")
        self.assertEqual(arm.commands, [])

    def test_loop_stall_does_not_skip_forward(self):
        c, arm, clock, _ = self.controller()
        arm.enabled = True
        c.tick(pose(), "test")
        count = len(arm.commands)
        clock.sleep(1)
        with self.assertRaisesRegex(motion.SafetyError, "stalled"):
            c.tick(pose(6), "test")
        self.assertEqual(len(arm.commands), count)

    def test_serial_loss_cannot_trigger_blind_retraction(self):
        c, arm, _, _ = self.controller()
        c.enabled = arm.enabled = True
        arm.fail_read = True
        with contextlib.redirect_stdout(io.StringIO()) as output:
            c.stop()
        self.assertEqual(arm.commands, [])
        self.assertIn("hardware power stop", output.getvalue())

    def test_total_duration_is_bounded(self):
        c, arm, clock, _ = self.controller()
        arm.enabled = True
        c.started = 0
        clock.sleep(motion.MAX_RUN_TIME + 1)
        with self.assertRaisesRegex(motion.SafetyError, "time limit"):
            c.tick(pose(), "test")
        self.assertEqual(arm.commands, [])

    def test_invalid_hold_rejects_before_arming(self):
        for hold in (-1, 0.6, float("nan")):
            c, arm, _, _ = self.controller()
            with self.assertRaises(motion.SafetyError):
                c.run(hold)
            self.assertEqual(arm.commands, [])

    def test_overlong_path_rejects_before_arming(self):
        c, arm, _, _ = self.controller()
        c.path = [pose(), pose(12)] + [pose(13), pose(12)] * 20 + [pose(24)]
        with self.assertRaisesRegex(motion.SafetyError, "too long"):
            c.run(0.2)
        self.assertEqual(arm.commands, [])

    def test_missing_midi_is_reported_after_release_without_retry(self):
        midi = Mock()
        midi.poll.return_value = []
        midi.verify.side_effect = motion.SafetyError("missing note")
        c, arm, _, _ = self.controller(midi=midi)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(motion.SafetyError, "missing note"):
            c.run(0.2)
        self.assertEqual(arm.current, pose())
        midi.verify.assert_called_once()


class AdapterTests(unittest.TestCase):
    def arm(self):
        arm = motion.Arm.__new__(motion.Arm)
        arm.bus = Mock()
        arm.calibration = {m: {"range_min": 1000, "range_max": 3000} for m in motion.MOTORS}
        return arm

    def test_raw_io_and_joint_limit_rejection(self):
        arm = self.arm()
        arm.bus.sync_read.return_value = pose()
        self.assertEqual(arm.read(), pose())
        arm.bus.sync_read.assert_called_with("Present_Position", normalize=False, num_retry=0)
        arm.send(pose())
        arm.bus.sync_write.assert_called_with("Goal_Position", pose(), normalize=False, num_retry=0)
        arm.bus.sync_write.reset_mock()
        with self.assertRaises(motion.SafetyError):
            arm.send({**pose(), "wrist_flex": 1000})
        arm.bus.sync_write.assert_not_called()

    def test_seed_targets_before_torque_enable(self):
        arm = self.arm()
        def read(register, **kwargs):
            if register == "Torque_Enable":
                return dict.fromkeys(motion.MOTORS, int(arm.bus.enable_torque.called))
            return pose()
        arm.bus.sync_read.side_effect = read
        arm.arm_at_current(pose())
        names = [c[0] for c in arm.bus.mock_calls]
        self.assertLess(names.index("sync_write"), names.index("enable_torque"))

    def test_seed_readback_failure_never_enables_torque(self):
        arm = self.arm()
        arm.bus.sync_read.side_effect = lambda name, **kwargs: dict.fromkeys(motion.MOTORS, 0) if name == "Torque_Enable" else pose(1)
        with self.assertRaisesRegex(motion.SafetyError, "seeded"):
            arm.arm_at_current(pose())
        arm.bus.enable_torque.assert_not_called()

    def test_gripper_movement_during_arming_never_enables_torque(self):
        arm = self.arm()
        def read(register, **kwargs):
            return {"Torque_Enable": dict.fromkeys(motion.MOTORS, 0), "Goal_Position": pose(), "Present_Position": {**pose(), "gripper": 2010}}[register]
        arm.bus.sync_read.side_effect = read
        with self.assertRaisesRegex(motion.SafetyError, "moved"):
            arm.arm_at_current(pose())
        arm.bus.enable_torque.assert_not_called()

    def test_close_never_releases_torque(self):
        arm = self.arm()
        arm.bus.is_connected = True
        arm.close()
        arm.bus.disconnect.assert_called_once_with(disable_torque=False)

    def test_release_connection_does_not_require_pose_or_calibration_match(self):
        arm = self.arm()
        arm.bus.is_calibrated = False
        arm.bus.sync_read.return_value = dict.fromkeys(motion.MOTORS, 120)
        arm.open(release_only=True)
        arm.bus.sync_write.assert_not_called()
        arm.bus.sync_read.assert_called_once_with("Present_Voltage", normalize=False)

    def test_release_writes_only_torque_flags_and_verifies_all_six(self):
        arm = self.arm()
        arm.bus.sync_read.return_value = dict.fromkeys(motion.MOTORS, 0)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            arm.release()
        self.assertEqual(arm.bus.write.call_count, 6)
        for motor in motion.MOTORS:
            arm.bus.write.assert_any_call("Torque_Enable", motor, 0, normalize=False, num_retry=0)
        arm.bus.disable_torque.assert_not_called()
        arm.bus.sync_write.assert_not_called()
        self.assertEqual(output.getvalue().count("Torque_Enable=0 (OFF)"), 6)

    def test_incomplete_or_nonzero_torque_readback_never_passes_as_off(self):
        arm = self.arm()
        for values in ({}, {m: 0 for m in motion.MOTORS[:-1]},
                       {**dict.fromkeys(motion.MOTORS, 0), "wrist_flex": 1},
                       {**dict.fromkeys(motion.MOTORS, 0), "wrist_flex": 2}):
            arm.bus.sync_read.return_value = values
            with self.subTest(values=values), self.assertRaises(motion.SafetyError):
                arm.require_torque(False)

    def test_teaching_connection_allows_resting_pose_but_playback_still_rejects_it(self):
        arm = self.arm()
        parked = {**pose(), "shoulder_lift": 1002}
        arm.bus.is_calibrated = True
        arm.bus.sync_read.side_effect = lambda register, **kwargs: {
            "Present_Voltage": dict.fromkeys(motion.MOTORS, 120),
            "Operating_Mode": dict.fromkeys(motion.MOTORS, 0),
            "Present_Position": parked,
        }[register]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            arm.open(teaching=True)
        self.assertIn("shoulder_lift: measured 1002 ticks; permitted 1016..2984", output.getvalue())
        with self.assertRaisesRegex(motion.SafetyError, "shoulder_lift"):
            arm.open()
        with self.assertRaises(motion.SafetyError):
            arm.send(parked)
        arm.bus.write.assert_not_called()
        arm.bus.sync_write.assert_not_called()
        arm.bus.enable_torque.assert_not_called()

    def test_teaching_start_does_not_bypass_calibration_mismatch(self):
        arm = self.arm()
        arm.bus.is_calibrated = False
        arm.bus.sync_read.return_value = dict.fromkeys(motion.MOTORS, 120)
        with self.assertRaisesRegex(motion.SafetyError, "calibration differs"):
            arm.open(teaching=True)
        arm.bus.write.assert_not_called()

    def test_limit_error_reports_measured_value_and_allowed_range(self):
        arm = self.arm()
        with self.assertRaisesRegex(motion.SafetyError, r"shoulder_lift: measured 1002 ticks; permitted 1016\.\.2984"):
            arm.check_pose({**pose(), "shoulder_lift": 1002})


class CaptureTests(unittest.TestCase):
    def test_intermediate_samples_preserve_observed_bends(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        bend = {**pose(12), "elbow_flex": 2012}
        motion.append_capture(data, bend)
        motion.append_capture(data, pose(24))
        self.assertEqual(data["path"], [pose(), bend, pose(24)])

    def test_too_fast_motion_is_rejected_without_inventing_samples(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        with self.assertRaisesRegex(motion.SafetyError, "too far apart"):
            motion.append_capture(data, pose(30))
        self.assertEqual(data["path"], [pose()])

    def test_saved_point_decimation_does_not_create_a_false_gap(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        motion.append_capture(data, pose(11))  # Observed, but under the save threshold.
        self.assertEqual(data["path"], [pose()])
        motion.append_capture(data, pose(25), previous_observation=pose(11))
        self.assertEqual(data["path"], [pose(), pose(11), pose(25)])
        motion.validate_entry(data)

    def test_actual_read_gap_remains_rejected_with_a_pending_point(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        motion.append_capture(data, pose(11))
        with self.assertRaisesRegex(motion.SafetyError, r"wrist_flex moved 29 ticks \(2011 -> 2040\)"):
            motion.append_capture(data, pose(40), previous_observation=pose(11))
        self.assertEqual(data["path"], [pose()])

    def test_gripper_drift_rejects_even_below_capture_movement_threshold(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        with self.assertRaisesRegex(motion.SafetyError, "Gripper opening"):
            motion.append_capture(data, {**pose(), "gripper": 2004})
        self.assertEqual(data["path"], [pose()])
