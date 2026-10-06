"""Exercise chat capture, powered handover, and command freshness with fake motors."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import orchid_key as key
import orchid_session as session


def pose(offset=0):
    return {m: 2200 + (offset if m == "wrist_flex" else 0) for m in key.MOTORS}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Arm:
    signature = "a" * 64
    version = "fake"

    def __init__(self):
        self.current = pose(36)
        self.enabled = False
        self.commands = []
        self.releases = 0
        self.enable_calls = 0
        self.jammed = False
        self.fail_read = False
        self.partial_enable = False

    def torque_status(self):
        return dict.fromkeys(key.MOTORS, int(self.enabled))

    def require_torque(self, enabled):
        key.require(self.enabled == enabled, "unexpected torque")
        return self.torque_status()

    def read_raw(self):
        if self.fail_read:
            raise ConnectionError("serial lost")
        return dict(self.current)

    def check_pose(self, current):
        key.pose_valid(current)
        key.require(all(2100 <= p <= 2300 for p in current.values()), "outside margin")

    def read(self):
        value = self.read_raw()
        self.check_pose(value)
        return value

    def send(self, target):
        self.check_pose(target)
        self.commands.append(dict(target))
        if not self.jammed:
            self.current = dict(target)

    def arm_at_current(self, current):
        self.require_torque(False)
        self.send(current)
        self.enabled = True
        self.enable_calls += 1
        if self.partial_enable:
            raise ConnectionError("partial enable failure")

    def release(self):
        self.enabled = False
        self.releases += 1


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name in ("commands", "responses", "drafts", "logs"):
            (self.folder / name).mkdir()
        self.map_path = self.folder / "keys.json"
        self.map_path.write_text(json.dumps(key.empty_store()))
        self.arm, self.clock = Arm(), Clock()
        self.log = io.StringIO()
        self.s = session.Session(self.arm, self.folder, self.map_path, "pad", self.log,
                                 clock=self.clock, sleep=self.clock.sleep, wall=self.clock)
        self.s.publish()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def command(self, action, supported=False, selected=None):
        return {"id": "b" * 32, "session_id": self.s.session_id,
                "created": self.clock(), "revision": self.s.revision,
                "action": action, "supported": supported, "key": selected}

    def send(self, action, supported=False, selected=None):
        command = self.command(action, supported, selected)
        self.s.validate_command(command)
        self.s.dispatch(command)

    def steady(self):
        for _ in range(16):
            self.s.observe()

    def move(self, offset):
        start = self.arm.current["wrist_flex"] - 2200
        step = 3 if offset > start else -3
        for position in range(start, offset, step):
            self.arm.current = pose(position)
            self.s.observe()
        self.arm.current = pose(offset)
        self.steady()

    def capture(self):
        self.steady()
        self.send("pressed")
        self.move(24)
        self.send("touch")
        self.move(0)
        self.send("clear")

    def test_capture_reverses_observed_lift_and_holds_without_stroke(self):
        self.capture()
        self.assertEqual(self.s.phase, "holding")
        self.assertEqual(self.s.draft["path"][0], pose())
        self.assertEqual(self.s.draft["path"][-1], pose(36))
        self.assertEqual(self.s.draft["path"][self.s.draft["touch_index"]], pose(24))
        raw = [json.loads(line)["position"] for line in self.log.getvalue().splitlines()
               if json.loads(line).get("event") == "encoder"]
        self.assertTrue(all(p in raw for p in self.s.draft["path"]))
        self.assertEqual(set(p["wrist_flex"] for p in self.arm.commands), {2200})
        self.assertEqual(self.arm.enable_calls, 1)
        self.assertIsNone(key.load_store(self.map_path)["keys"]["C"])
        self.assertEqual(len(list((self.folder / "drafts").glob("*.json"))), 1)

    def test_one_trial_then_pass_saves_only_selected_key(self):
        self.capture()
        self.send("test")
        self.assertEqual(self.s.phase, "result")
        self.assertEqual(self.arm.current, pose())
        self.assertEqual(max(p["wrist_flex"] for p in self.arm.commands), 2236)
        self.assertEqual(self.arm.enable_calls, 1)
        self.assertTrue(self.arm.enabled)
        self.assertIsNone(key.load_store(self.map_path)["keys"]["C"])
        self.send("pass")
        stored = key.load_store(self.map_path)
        self.assertEqual(stored["keys"]["C"]["verification"]["successful_trials"], 1)
        self.assertEqual(sum(p is not None for p in stored["keys"].values()), 1)
        self.assertEqual(len(stored["keys"]), 12)
        self.assertEqual(len(list(self.folder.glob("keys.json.*.bak"))), 1)
        with self.assertRaises(key.SafetyError):
            self.send("pass")

    def test_next_needs_success_and_explicit_support_then_selects_csharp(self):
        self.capture()
        with self.assertRaises(key.SafetyError):
            self.send("next", supported=True)
        self.send("test")
        self.send("pass")
        with self.assertRaises(key.SafetyError):
            self.send("next")
        self.assertTrue(self.arm.enabled)
        self.send("next", supported=True)
        self.assertFalse(self.arm.enabled)
        self.assertEqual(self.s.selected, "C#")
        self.assertEqual(self.s.phase, "ready")
        self.assertEqual(self.arm.releases, 1)

    def test_repeat_trial_while_holding_does_not_reenable_or_realign(self):
        self.capture()
        self.send("test")
        self.send("pass")
        # A long conversation is allowed while feedback monitoring keeps running.
        for _ in range(1100):
            self.s.observe()
        self.send("test")
        self.send("pass")
        self.assertEqual(self.arm.enable_calls, 1)
        self.assertEqual(self.s.completed_trials, 2)

    def test_unstable_marker_rejected_without_losing_session(self):
        for i in range(16):
            self.arm.current = pose(i)
            self.s.observe()
        with self.assertRaisesRegex(key.SafetyError, "Still moving"):
            self.send("pressed")
        self.assertEqual(self.s.phase, "ready")
        self.steady()
        self.send("pressed")
        self.assertEqual(self.s.phase, "pressed")
        self.assertFalse(self.arm.commands)

    def test_capture_gap_preserves_map_and_retry_works_without_reconnect(self):
        before = self.map_path.read_bytes()
        self.steady()
        self.send("pressed")
        self.arm.current = pose(0)  # genuine 36-tick jump
        self.s.observe()
        self.assertEqual(self.s.phase, "capture_error")
        self.assertFalse(self.arm.commands)
        self.assertEqual(self.map_path.read_bytes(), before)
        self.send("retry", supported=True)
        self.assertEqual(self.s.phase, "ready")
        self.arm.current = pose(36)
        self.capture()
        self.assertEqual(self.s.phase, "holding")

    def test_lost_recording_interval_never_bridged(self):
        self.steady()
        self.send("pressed")
        self.clock.sleep(1)
        self.s.observe()
        self.assertEqual(self.s.phase, "capture_error")
        self.assertFalse(self.arm.commands)

    def test_gripper_drift_and_joint_limit_error_block_capture(self):
        for motor, value in (("gripper", 2205), ("wrist_flex", 2301)):
            self.send("retry", supported=True)
            self.arm.current = pose(36)
            self.steady()
            self.send("pressed")
            self.arm.current[motor] = value
            self.s.observe()
            self.assertEqual(self.s.phase, "capture_error")
        self.assertFalse(self.arm.commands)

    def test_stale_session_revision_and_expired_commands_rejected(self):
        for mutation in ({"session_id": "old"}, {"revision": -1},
                         {"created": self.clock() - 11}, {"created": self.clock() + 1}):
            command = {**self.command("pressed"), **mutation}
            with self.assertRaises(key.SafetyError):
                self.s.validate_command(command)
        self.assertFalse(self.arm.commands)

    def test_stale_duplicate_test_is_consumed_without_second_stroke(self):
        self.capture()
        command = self.command("test")
        session.atomic_json(self.folder / "commands" / "a.json", command)
        session.atomic_json(self.folder / "commands" / "b.json", command)
        self.s.process_commands()
        self.assertEqual(self.s.test_number, 1)
        self.assertFalse(self.s.last_ack["ok"])
        self.assertFalse(list((self.folder / "commands").glob("*.json")))

    def test_fail_leaves_existing_map_and_draft_intact(self):
        before = self.map_path.read_bytes()
        self.capture()
        self.send("test")
        self.send("fail")
        self.assertEqual(self.s.phase, "failed")
        self.assertEqual(self.map_path.read_bytes(), before)
        self.assertTrue(self.s.draft)
        self.assertTrue(self.arm.enabled)

    def test_cannot_accept_before_trial_or_overwrite_external_map_changes(self):
        self.capture()
        with self.assertRaises(key.SafetyError):
            self.send("pass")
        self.send("test")
        external = key.load_store(self.map_path)
        external["external_revision"] = 1
        self.map_path.write_text(json.dumps(external))
        with self.assertRaisesRegex(key.SafetyError, "changed outside"):
            self.send("pass")
        self.assertEqual(key.load_store(self.map_path), external)

    def test_stop_during_trial_holds_and_does_not_release_or_retract(self):
        self.capture()
        original_tick = self.s.controller.tick

        def stop_during_down(target, stage):
            if stage == "down":
                session.atomic_json(self.folder / "stop.json", self.command("stop"))
            return original_tick(target, stage)

        self.s.controller.tick = stop_during_down
        with self.assertRaisesRegex(key.SafetyError, "Chat stop"):
            self.send("test")
        self.assertEqual(self.s.phase, "fault")
        self.assertTrue(self.arm.enabled)
        self.assertEqual(self.arm.releases, 0)
        self.assertIsNone(key.load_store(self.map_path)["keys"]["C"])

    def test_partial_enable_enters_fault_without_drop(self):
        self.steady()
        self.send("pressed")
        self.move(24)
        self.send("touch")
        self.move(0)
        self.arm.partial_enable = True
        with self.assertRaises(ConnectionError):
            self.send("clear")
        self.assertEqual(self.s.phase, "fault")
        self.assertEqual(self.arm.releases, 0)
        self.assertTrue(self.arm.enabled)

    def test_jam_faults_without_extra_press_or_automatic_release(self):
        self.capture()
        self.arm.jammed = True
        with self.assertRaises(key.SafetyError):
            self.send("test")
        self.assertEqual(self.s.phase, "fault")
        self.assertEqual(self.arm.releases, 0)
        self.send("release", supported=True)
        self.assertEqual(self.s.phase, "ready")
        self.assertFalse(self.arm.enabled)

    def test_stale_bridge_refuses_to_queue_and_cli_never_constructs_arm(self):
        with patch.object(session.time, "time", self.clock), patch.object(key, "Arm", side_effect=AssertionError("hardware touched")):
            session.main(["--session", str(self.folder), "status"])
            session.main(["--session", str(self.folder), "send", "pressed", "--wait", "0"])
            self.clock.sleep(3)
            with self.assertRaisesRegex(key.SafetyError, "fresh live"):
                session.send_command(self.folder, "pressed")

    def test_every_key_can_be_captured_without_cross_key_commands(self):
        for name in key.KEYS:
            self.assertEqual(self.s.selected, name)
            self.arm.current = pose(36)
            start = len(self.arm.commands)
            self.capture()
            self.send("test")
            self.send("pass")
            self.send("next", supported=True)
            self.assertTrue(all(2200 <= p["wrist_flex"] <= 2236 for p in self.arm.commands[start:]))
        self.assertEqual(self.s.phase, "finished")
        self.assertFalse(self.arm.enabled)
        saved = key.load_store(self.map_path)
        self.assertTrue(all(saved["keys"][name]["verification"]["successful_trials"] == 1 for name in key.KEYS))


if __name__ == "__main__":
    unittest.main()
