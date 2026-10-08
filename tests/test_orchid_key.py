"""Safety/control tests use fake motors only. Never open a serial or MIDI device."""

import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import orchid_key as key


def pose(offset=0):
    return {m: 2000 + (offset if m == "wrist_flex" else 0) for m in key.MOTORS}


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
        key.require(self.enabled == enabled, "unexpected torque state")

    def send(self, target):
        self.commands.append(dict(target))
        if not self.jammed:
            self.current = dict(target)

    def arm_at_current(self, current):
        self.require_torque(False)
        self.send(current)
        self.enabled = True


class PlanTests(unittest.TestCase):
    def test_twelve_empty_slots(self):
        store = key.empty_store()
        self.assertEqual(tuple(store["keys"]), key.KEYS)
        self.assertTrue(all(p is None for p in store["keys"].values()))

    def test_partial_press_uses_only_taught_contact_path(self):
        original = entry()
        result = key.plan(original, 0.25)
        self.assertEqual(result, [pose(), pose(12), pose(24), pose(27)])
        self.assertEqual(original, entry())
        self.assertEqual(key.plan(original, 1), original["path"])

    def test_hover_only_is_available_before_contact_teaching(self):
        data = {**entry(), "path": [pose()], "complete": False, "touch_index": None, "padded_tip": False}
        self.assertEqual(key.plan(data, hover_only=True), [pose()])
        with self.assertRaisesRegex(key.SafetyError, "Teach hover"):
            key.plan(data)

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
            with self.subTest(data=data), self.assertRaises(key.SafetyError):
                key.plan(data)

    def test_rejects_invalid_fractions(self):
        for fraction in (0, -1, 1.1, float("nan"), float("inf")):
            with self.subTest(fraction=fraction), self.assertRaises(key.SafetyError):
                key.plan(entry(), fraction)

    def test_trajectory_stays_between_endpoints_and_respects_duration(self):
        samples = list(key.segment(pose(), pose(24)))
        self.assertEqual(samples[-1], pose(24))
        self.assertGreaterEqual(len(samples) * key.PERIOD, 1.875 * 24 / key.SPEED)
        values = [p["wrist_flex"] for p in samples]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(2000 <= v <= 2024 for v in values))
        self.assertTrue(all(p["gripper"] == 2000 for p in samples))
        # Quantization permits a one-tick rounding difference from the rate bound.
        self.assertLessEqual(max(b - a for a, b in zip(values, values[1:])), key.SPEED * key.PERIOD + 1)

    def test_reteach_preserves_other_keys_and_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keys.json"
            store = key.empty_store()
            store["keys"]["D"] = entry()
            path.write_text(json.dumps(store))
            original = path.read_bytes()
            updated = key.load_store(path)
            updated["keys"]["C"] = entry()
            with contextlib.redirect_stdout(io.StringIO()):
                key.save_store(path, updated)
            self.assertEqual(key.load_store(path)["keys"]["D"], store["keys"]["D"])
            backups = list(Path(folder).glob("*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_preview_never_constructs_hardware_or_midi(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keys.json"
            store = key.empty_store()
            store["keys"]["C"] = entry()
            path.write_text(json.dumps(store))
            with patch.object(key, "Arm", side_effect=AssertionError("hardware touched")), patch.object(key, "MidiMonitor", side_effect=AssertionError("MIDI touched")), contextlib.redirect_stdout(io.StringIO()):
                key.main(["play", "--file", str(path)])

    def test_init_never_overwrites_existing_map(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keys.json"
            path.write_text("preserve me")
            with self.assertRaises(FileExistsError):
                key.main(["init", "--file", str(path)])
            self.assertEqual(path.read_text(), "preserve me")


class ControllerTests(unittest.TestCase):
    def controller(self, arm=None, midi=None):
        clock = FakeClock()
        log = io.StringIO()
        arm = arm or FakeArm()
        controller = key.Controller(arm, key.plan(entry()), log, midi, clock, clock.sleep)
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
        with self.assertRaisesRegex(key.SafetyError, "hover"):
            c.run(0.2)
        self.assertFalse(arm.enabled)
        self.assertEqual(arm.commands, [])

    def test_gripper_mismatch_rejects_before_arming(self):
        current = {**pose(), "gripper": 2010}
        c, arm, _, _ = self.controller(FakeArm(current))
        with self.assertRaises(key.SafetyError):
            c.run(0.2)
        self.assertEqual(arm.commands, [])

    def test_start_correction_is_interpolated(self):
        c, arm, _, _ = self.controller(FakeArm(pose(-6)))
        self.run_quietly(c)
        self.assertLessEqual(max(key.distance(a, b) for a, b in zip(arm.commands, arm.commands[1:])), 2)

    def test_jammed_joint_aborts_without_finishing_press(self):
        arm = FakeArm()
        arm.jammed = True
        c, arm, _, _ = self.controller(arm)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(key.SafetyError):
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
        with self.assertRaisesRegex(key.SafetyError, "Stale"):
            c.tick(pose(), "test")
        self.assertEqual(arm.commands, [])

    def test_loop_stall_does_not_skip_forward(self):
        c, arm, clock, _ = self.controller()
        arm.enabled = True
        c.tick(pose(), "test")
        count = len(arm.commands)
        clock.sleep(1)
        with self.assertRaisesRegex(key.SafetyError, "stalled"):
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
        clock.sleep(key.MAX_RUN_TIME + 1)
        with self.assertRaisesRegex(key.SafetyError, "time limit"):
            c.tick(pose(), "test")
        self.assertEqual(arm.commands, [])

    def test_invalid_hold_rejects_before_arming(self):
        for hold in (-1, 0.6, float("nan")):
            c, arm, _, _ = self.controller()
            with self.assertRaises(key.SafetyError):
                c.run(hold)
            self.assertEqual(arm.commands, [])

    def test_overlong_path_rejects_before_arming(self):
        c, arm, _, _ = self.controller()
        c.path = [pose(), pose(12)] + [pose(13), pose(12)] * 20 + [pose(24)]
        with self.assertRaisesRegex(key.SafetyError, "too long"):
            c.run(0.2)
        self.assertEqual(arm.commands, [])

    def test_missing_midi_is_reported_after_release_without_retry(self):
        midi = Mock()
        midi.poll.return_value = []
        midi.verify.side_effect = key.SafetyError("missing note")
        c, arm, _, _ = self.controller(midi=midi)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(key.SafetyError, "missing note"):
            c.run(0.2)
        self.assertEqual(arm.current, pose())
        midi.verify.assert_called_once()


class AdapterTests(unittest.TestCase):
    def arm(self):
        arm = key.Arm.__new__(key.Arm)
        arm.bus = Mock()
        arm.calibration = {m: {"range_min": 1000, "range_max": 3000} for m in key.MOTORS}
        return arm

    def test_raw_io_and_joint_limit_rejection(self):
        arm = self.arm()
        arm.bus.sync_read.return_value = pose()
        self.assertEqual(arm.read(), pose())
        arm.bus.sync_read.assert_called_with("Present_Position", normalize=False, num_retry=0)
        arm.send(pose())
        arm.bus.sync_write.assert_called_with("Goal_Position", pose(), normalize=False, num_retry=0)
        arm.bus.sync_write.reset_mock()
        with self.assertRaises(key.SafetyError):
            arm.send({**pose(), "wrist_flex": 1000})
        arm.bus.sync_write.assert_not_called()

    def test_seed_targets_before_torque_enable(self):
        arm = self.arm()
        def read(register, **kwargs):
            if register == "Torque_Enable":
                return dict.fromkeys(key.MOTORS, int(arm.bus.enable_torque.called))
            return pose()
        arm.bus.sync_read.side_effect = read
        arm.arm_at_current(pose())
        names = [c[0] for c in arm.bus.mock_calls]
        self.assertLess(names.index("sync_write"), names.index("enable_torque"))

    def test_seed_readback_failure_never_enables_torque(self):
        arm = self.arm()
        arm.bus.sync_read.side_effect = lambda name, **kwargs: dict.fromkeys(key.MOTORS, 0) if name == "Torque_Enable" else pose(1)
        with self.assertRaisesRegex(key.SafetyError, "seeded"):
            arm.arm_at_current(pose())
        arm.bus.enable_torque.assert_not_called()

    def test_gripper_movement_during_arming_never_enables_torque(self):
        arm = self.arm()
        def read(register, **kwargs):
            return {"Torque_Enable": dict.fromkeys(key.MOTORS, 0), "Goal_Position": pose(), "Present_Position": {**pose(), "gripper": 2010}}[register]
        arm.bus.sync_read.side_effect = read
        with self.assertRaisesRegex(key.SafetyError, "moved"):
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
        arm.bus.sync_read.return_value = dict.fromkeys(key.MOTORS, 120)
        arm.open(release_only=True)
        arm.bus.sync_write.assert_not_called()
        arm.bus.sync_read.assert_called_once_with("Present_Voltage", normalize=False)

    def test_release_writes_only_torque_flags_and_verifies_all_six(self):
        arm = self.arm()
        arm.bus.sync_read.return_value = dict.fromkeys(key.MOTORS, 0)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            arm.release()
        self.assertEqual(arm.bus.write.call_count, 6)
        for motor in key.MOTORS:
            arm.bus.write.assert_any_call("Torque_Enable", motor, 0, normalize=False, num_retry=0)
        arm.bus.disable_torque.assert_not_called()
        arm.bus.sync_write.assert_not_called()
        self.assertEqual(output.getvalue().count("Torque_Enable=0 (OFF)"), 6)

    def test_incomplete_or_nonzero_torque_readback_never_passes_as_off(self):
        arm = self.arm()
        for values in ({}, {m: 0 for m in key.MOTORS[:-1]},
                       {**dict.fromkeys(key.MOTORS, 0), "wrist_flex": 1},
                       {**dict.fromkeys(key.MOTORS, 0), "wrist_flex": 2}):
            arm.bus.sync_read.return_value = values
            with self.subTest(values=values), self.assertRaises(key.SafetyError):
                arm.require_torque(False)

    def test_read_only_status_does_not_change_any_motor_state(self):
        arm = Mock()
        arm.torque_status.return_value = dict.fromkeys(key.MOTORS, 0)
        arm.read_raw.return_value = pose()
        arm.limit_issues.return_value = []
        with patch.object(key, "Arm", return_value=arm), contextlib.redirect_stdout(io.StringIO()):
            key.main(["status", "--port", "fake", "--calibration", "fake.json"])
        arm.open.assert_called_once_with(release_only=True)
        arm.release.assert_not_called()
        arm.arm_at_current.assert_not_called()
        arm.send.assert_not_called()
        arm.close.assert_called_once()

    def test_teaching_connection_allows_resting_pose_but_playback_still_rejects_it(self):
        arm = self.arm()
        parked = {**pose(), "shoulder_lift": 1002}
        arm.bus.is_calibrated = True
        arm.bus.sync_read.side_effect = lambda register, **kwargs: {
            "Present_Voltage": dict.fromkeys(key.MOTORS, 120),
            "Operating_Mode": dict.fromkeys(key.MOTORS, 0),
            "Present_Position": parked,
        }[register]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            arm.open(teaching=True)
        self.assertIn("shoulder_lift: measured 1002 ticks; permitted 1016..2984", output.getvalue())
        with self.assertRaisesRegex(key.SafetyError, "shoulder_lift"):
            arm.open()
        with self.assertRaises(key.SafetyError):
            arm.send(parked)
        arm.bus.write.assert_not_called()
        arm.bus.sync_write.assert_not_called()
        arm.bus.enable_torque.assert_not_called()

    def test_teaching_start_does_not_bypass_calibration_mismatch(self):
        arm = self.arm()
        arm.bus.is_calibrated = False
        arm.bus.sync_read.return_value = dict.fromkeys(key.MOTORS, 120)
        with self.assertRaisesRegex(key.SafetyError, "calibration differs"):
            arm.open(teaching=True)
        arm.bus.write.assert_not_called()

    def test_limit_error_reports_measured_value_and_allowed_range(self):
        arm = self.arm()
        with self.assertRaisesRegex(key.SafetyError, r"shoulder_lift: measured 1002 ticks; permitted 1016\.\.2984"):
            arm.check_pose({**pose(), "shoulder_lift": 1002})

    def test_calibration_mismatch_rejects_before_opening_bus(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keys.json"
            store = key.empty_store()
            store["keys"]["C"] = entry()
            path.write_text(json.dumps(store))
            arm = Mock(signature="different", version="test")
            with patch.object(key, "Arm", return_value=arm), patch.object(key.sys.stdin, "isatty", return_value=True), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(key.SafetyError, "calibration changed"):
                key.main(["play", "--file", str(path), "--execute", "--port", "fake", "--calibration", "fake.json"])
            arm.open.assert_not_called()


class MidiTests(unittest.TestCase):
    def test_exact_note_pair_required(self):
        monitor = key.MidiMonitor.__new__(key.MidiMonitor)
        monitor.note = 60
        correct = [{"type": "note_on", "note": 60}, {"type": "note_off", "note": 60}]
        monitor.events = correct
        monitor.verify()
        for events in ([], correct[:1], correct * 2, list(reversed(correct)), [{"type": "note_on", "note": 61}]):
            monitor.events = events
            with self.subTest(events=events), self.assertRaises(key.SafetyError):
                monitor.verify()


class TimedArm(FakeArm):
    signature = "a" * 64
    version = "test"

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.enabled = True
        self.released_at = None

    def release(self):
        self.enabled = False
        self.released_at = self.clock()

    def read(self):
        # Five-second support countdown, then three seconds per stage.
        now = self.clock()
        # Wait beyond each CAPTURED cue before beginning the next hand motion.
        if now < 8.5:
            return pose()
        if now < 11.5:
            return pose(round(min(12, (now - 8.5) * 12)))
        return pose(12 + round(min(12, (now - 11.5) * 12)))


class HandsFreeTests(unittest.TestCase):
    def teach(self, arm, clock, answer="save", **options):
        cues = []
        with patch.object(key, "require_human") as prompt, patch.object(key.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value=answer) as inputs, contextlib.redirect_stdout(io.StringIO()):
            result = key.teach_hands_free(
                arm, options.get("existing"), options.get("contact_ready", True), "test fixture", 5, 3,
                clock, clock.sleep, cues.append,
            )
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.args[1], "ready")
        inputs.assert_called_once()  # Only final review; never per-pose input.
        return result, cues

    def test_complete_hands_free_capture_without_any_goal_or_torque_enable(self):
        clock = FakeClock()
        arm = TimedArm(clock)
        result, cues = self.teach(arm, clock)
        key.validate_entry(result, complete=True)
        self.assertEqual(result["path"][0], pose())
        self.assertEqual(result["path"][result["touch_index"]], pose(12))
        self.assertEqual(result["path"][-1], pose(24))
        self.assertGreaterEqual(arm.released_at, 5)
        self.assertEqual(arm.commands, [])
        self.assertFalse(arm.enabled)
        self.assertEqual(sum(c.startswith("CAPTURED:") for c in cues), 3)

    def test_final_discard_preserves_existing_entry(self):
        clock = FakeClock()
        arm = TimedArm(clock)
        existing = entry()
        result, _ = self.teach(arm, clock, answer="discard", existing=existing)
        self.assertIs(result, existing)
        self.assertEqual(existing, entry())

    def test_hover_only_still_needs_no_per_pose_typing(self):
        clock = FakeClock()
        arm = TimedArm(clock)
        result, cues = self.teach(arm, clock, contact_ready=False)
        self.assertFalse(result["complete"])
        self.assertEqual(result["path"], [pose()])
        self.assertEqual(sum(c.startswith("CAPTURED:") for c in cues), 1)

    def test_intermediate_samples_preserve_observed_bends(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        bend = {**pose(12), "elbow_flex": 2012}
        key.append_capture(data, bend)
        key.append_capture(data, pose(24))
        self.assertEqual(data["path"], [pose(), bend, pose(24)])

    def test_too_fast_motion_is_rejected_without_inventing_samples(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        with self.assertRaisesRegex(key.SafetyError, "too far apart"):
            key.append_capture(data, pose(30))
        self.assertEqual(data["path"], [pose()])

    def test_saved_point_decimation_does_not_create_a_false_gap(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        key.append_capture(data, pose(11))  # Observed, but under the save threshold.
        self.assertEqual(data["path"], [pose()])
        key.append_capture(data, pose(25), previous_observation=pose(11))
        self.assertEqual(data["path"], [pose(), pose(11), pose(25)])
        key.validate_entry(data)

    def test_actual_read_gap_remains_rejected_with_a_pending_point(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        key.append_capture(data, pose(11))
        with self.assertRaisesRegex(key.SafetyError, r"wrist_flex moved 29 ticks \(2011 -> 2040\)"):
            key.append_capture(data, pose(40), previous_observation=pose(11))
        self.assertEqual(data["path"], [pose()])

    def test_timed_capture_preserves_skipped_observation_for_bridge(self):
        clock = FakeClock()
        arm = FakeArm()
        reads = iter([pose(11), pose(25)])
        arm.read = lambda: next(reads, pose(25))
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        with contextlib.redirect_stdout(io.StringIO()):
            key.timed_capture(arm, "test", 3, data, clock, clock.sleep, lambda text: None)
        self.assertEqual(data["path"], [pose(), pose(11), pose(25)])

    def test_gripper_drift_rejects_even_below_capture_movement_threshold(self):
        data = {**entry(), "path": [pose()], "touch_index": None, "complete": False}
        with self.assertRaisesRegex(key.SafetyError, "Gripper opening"):
            key.append_capture(data, {**pose(), "gripper": 2004})
        self.assertEqual(data["path"], [pose()])

    def test_unstable_endpoint_times_out_without_advancing_phase(self):
        clock = FakeClock()
        arm = FakeArm()
        arm.read = lambda: pose(4 * (math.floor(clock() * 10) % 2))
        cues = []
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(key.SafetyError, "stable endpoint"):
            key.timed_capture(arm, "test", 3, clock=clock, sleep=clock.sleep, cue=cues.append)
        self.assertFalse(any(c.startswith("CAPTURED") for c in cues))
        self.assertEqual(arm.commands, [])

    def test_torque_reenabled_during_capture_aborts(self):
        clock = FakeClock()
        arm = FakeArm()
        def read():
            if clock() > 0.5:
                arm.enabled = True
            return pose()
        arm.read = read
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(key.SafetyError, "torque state"):
            key.timed_capture(arm, "test", 3, clock=clock, sleep=clock.sleep, cue=lambda text: None)
        self.assertEqual(arm.commands, [])

    def test_slow_serial_read_cannot_be_saved_as_stable(self):
        clock = FakeClock()
        arm = FakeArm()
        def read():
            clock.sleep(0.3)
            return pose()
        arm.read = read
        with self.assertRaisesRegex(key.SafetyError, "Stale feedback"):
            key.timed_capture(arm, "test", 3, clock=clock, sleep=clock.sleep, cue=lambda text: None)

    def test_bad_countdown_rejected_before_torque_release(self):
        arm = Mock()
        for setup, stage in ((0, 8), (10, 0), (float("nan"), 8), (10, float("inf"))):
            with self.subTest(setup=setup, stage=stage), self.assertRaises(key.SafetyError):
                key.teach_hands_free(arm, None, True, "fixture", setup, stage)
        arm.release.assert_not_called()


class TeachingRecoveryTests(unittest.TestCase):
    def arm(self):
        arm = FakeArm()
        arm.calibration = {m: {"range_min": 1000, "range_max": 3000} for m in key.MOTORS}
        arm.limit_issues = lambda current: key.Arm.limit_issues(arm, current)
        return arm

    def test_manual_repositioning_waits_for_in_range_pose_without_writing(self):
        clock = FakeClock()
        arm = self.arm()
        poses = iter([{**pose(), "shoulder_lift": 1002}, {**pose(), "shoulder_lift": 1015}, pose()])
        arm.read = lambda: next(poses)
        cues = []
        with contextlib.redirect_stdout(io.StringIO()) as output:
            key.wait_for_teaching_pose(arm, clock, clock.sleep, cues.append)
        self.assertEqual(len(cues), 2)
        self.assertIn("Capture is paused", cues[0])
        self.assertIn("measured 1002", output.getvalue())
        self.assertGreaterEqual(clock(), 0.2)
        self.assertEqual(arm.commands, [])
        self.assertFalse(arm.enabled)

    def test_repositioning_never_runs_with_torque_on(self):
        clock = FakeClock()
        arm = self.arm()
        arm.enabled = True
        with self.assertRaisesRegex(key.SafetyError, "torque state"):
            key.wait_for_teaching_pose(arm, clock, clock.sleep, lambda text: None)
        self.assertEqual(arm.commands, [])

    def test_bad_position_cannot_be_accepted_after_timeout(self):
        clock = FakeClock()
        arm = self.arm()
        arm.current["shoulder_lift"] = 1002
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(key.SafetyError, "no poses saved"):
            key.wait_for_teaching_pose(arm, clock, clock.sleep, lambda text: None)
        self.assertEqual(arm.commands, [])

    def test_stale_repositioning_feedback_is_rejected(self):
        clock = FakeClock()
        arm = self.arm()
        def read():
            clock.sleep(0.3)
            return pose()
        arm.read = read
        with self.assertRaisesRegex(key.SafetyError, "Stale feedback"):
            key.wait_for_teaching_pose(arm, clock, clock.sleep, lambda text: None)


class HandsFreeStartTests(unittest.TestCase):
    def test_setup_and_alignment_send_no_motor_commands(self):
        clock = FakeClock()
        arm = FakeArm()
        with contextlib.redirect_stdout(io.StringIO()):
            key.prepare_hands_free_start(arm, pose(), 5, clock, clock.sleep)
        self.assertAlmostEqual(clock(), 6, places=6)
        self.assertFalse(arm.enabled)
        self.assertEqual(arm.commands, [])

    def test_hover_only_handoff_holds_without_a_downstroke(self):
        clock = FakeClock()
        arm = FakeArm()
        with contextlib.redirect_stdout(io.StringIO()):
            key.prepare_hands_free_start(arm, pose(), 5, clock, clock.sleep)
            controller = key.Controller(arm, [pose()], io.StringIO(), clock=clock, sleep=clock.sleep)
            controller.run(0.2)
        self.assertTrue(arm.enabled)
        self.assertTrue(arm.commands)
        self.assertTrue(all(command == pose() for command in arm.commands))

    def test_wrong_pose_times_out_without_enabling(self):
        clock = FakeClock()
        arm = FakeArm(pose(20))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(key.SafetyError, "alignment timed out"):
            key.prepare_hands_free_start(arm, pose(), 5, clock, clock.sleep)
        self.assertFalse(arm.enabled)
        self.assertEqual(arm.commands, [])

    def test_unexpected_torque_during_setup_aborts(self):
        clock = FakeClock()
        arm = FakeArm()
        arm.enabled = True
        with self.assertRaisesRegex(key.SafetyError, "torque state"):
            key.prepare_hands_free_start(arm, pose(), 5, clock, clock.sleep)
        self.assertEqual(arm.commands, [])

    def test_invalid_delay_rejected_before_reading_hardware(self):
        arm = Mock()
        for seconds in (0, float("nan"), 61):
            with self.subTest(seconds=seconds), self.assertRaises(key.SafetyError):
                key.prepare_hands_free_start(arm, pose(), seconds)
        arm.require_torque.assert_not_called()


if __name__ == "__main__":
    unittest.main()
