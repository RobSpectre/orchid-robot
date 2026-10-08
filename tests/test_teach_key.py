"""Offline checks for teach_key.py using the real LeRobot SO101 driver classes over a fake bus.

Run with the hardware environment (LeRobot installed):
    ~/.virtualenvs/replay-lw/bin/python -m unittest tests.test_teach_key -v
No serial port is opened. Skipped where LeRobot is not installed.
"""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HAVE_LEROBOT = importlib.util.find_spec("lerobot") is not None
MOTORS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def calibration_json(offset=0):
    cal = {n: {"id": i, "drive_mode": 0, "homing_offset": offset, "range_min": 1000, "range_max": 3000}
           for i, n in enumerate(MOTORS, 1)}
    cal["wrist_roll"].update(range_min=0, range_max=4095)
    return cal


class FakeSerial:
    """pyserial stand-in: setting `exclusive` takes a flock, which fails if another program holds one."""

    def __init__(self):
        self.locked_elsewhere, self._exclusive = False, None

    @property
    def exclusive(self):
        return self._exclusive

    @exclusive.setter
    def exclusive(self, value):
        if value and self.locked_elsewhere:
            raise OSError("Could not exclusively lock port")
        self._exclusive = value


class FakeBus:
    """Feetech bus stand-in: real LeRobot normalization, simple servo physics, an event log."""

    def __init__(self, real, eeprom, *, start=2000, rate=1800, clock=None):
        from lerobot.motors import MotorCalibration

        self.real, self.motors, self.port = real, real.motors, real.port
        self.calibration = real.calibration
        self.eeprom = {n: MotorCalibration(**v) for n, v in eeprom.items()}
        self.raw = dict.fromkeys(MOTORS, start)
        self.goal = dict(self.raw)
        self.torque = dict.fromkeys(MOTORS, 0)
        self.rate, self.floor = rate, {}  # rate: ticks/s the servo can move
        self.clock, self.moved_at = clock, 0.0
        self.port_handler = type("PortHandler", (), {"ser": FakeSerial()})()
        self.is_connected = False
        self.log = []

    # normalization goes through LeRobot's real implementation
    def _normalize(self, ids_values):
        return self.real._normalize(ids_values)

    def _unnormalize(self, ids_values):
        return self.real._unnormalize(ids_values)

    def _norm(self, raw):
        out = self.real._normalize({self.motors[n].id: v for n, v in raw.items()})
        return {n: out[self.motors[n].id] for n in raw}

    def _unnorm(self, values):
        out = self.real._unnormalize({self.motors[n].id: v for n, v in values.items()})
        return {n: out[self.motors[n].id] for n in values}

    def connect(self, handshake=True):
        assert not self.is_connected
        self.is_connected = True
        self.log.append(("connect",))

    def disconnect(self, disable_torque=True):
        if disable_torque:
            self.torque = dict.fromkeys(MOTORS, 0)
        self.is_connected = False
        self.log.append(("disconnect", disable_torque))

    @property
    def is_calibrated(self):
        return self.eeprom == self.calibration

    def read_calibration(self):
        return dict(self.eeprom)

    def _physics(self):
        """Servos keep moving between bus calls, so integrate over the fake clock's elapsed time."""
        now = self.clock() if self.clock else self.moved_at + 1 / 30
        limit = int(self.rate * (now - self.moved_at))
        self.moved_at = now
        for n in MOTORS:
            if self.torque[n]:
                step = max(-limit, min(limit, self.goal[n] - self.raw[n]))
                self.raw[n] = max(self.floor.get(n, -10**9), self.raw[n] + step)

    def sync_read(self, name, motors=None, *, normalize=True, num_retry=0):
        assert self.is_connected
        self._physics()
        if name == "Present_Position":
            return self._norm(self.raw) if normalize else dict(self.raw)
        if name == "Torque_Enable":
            return dict(self.torque)
        raise AssertionError(name)

    def sync_write(self, name, values, *, normalize=True, num_retry=0):
        assert self.is_connected and name == "Goal_Position"
        self._physics()
        raw = self._unnorm(values) if normalize else dict(values)
        self.goal.update(raw)
        self.log.append(("goal", dict(raw), dict(self.torque)))

    def write(self, name, motor, value, *, normalize=True, num_retry=0):
        assert self.is_connected
        if name == "Torque_Enable":
            self.torque[motor] = value
            if value:  # STS servos drive to Goal_Position once torque is on
                self.log.append(("torque_on", motor, self.goal[motor], self.raw[motor]))
        self.log.append(("write", name, motor, value))

    def configure_motors(self, *args, **kwargs):
        self.log.append(("configure_motors",))

    def disable_torque(self, motors=None, num_retry=0):
        for n in MOTORS:
            self.write("Torque_Enable", n, 0)

    def enable_torque(self, motors=None, num_retry=0):
        for n in MOTORS:
            self.write("Torque_Enable", n, 1)

    @contextlib.contextmanager
    def torque_disabled(self, motors=None):
        self.disable_torque()
        try:
            yield
        finally:
            self.enable_torque()

    def writes(self):
        return [e for e in self.log if e[0] in ("write", "goal")]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 1e-4)


class ScriptedInbox:
    """poll() releases lines once the fake clock passes their time; ask() pops answers in order."""

    def __init__(self, clock, timed=(), answers=()):
        self.clock, self.timed, self.answers, self.asked = clock, list(timed), list(answers), []

    def poll(self):
        due = [line for at, line in self.timed if at <= self.clock()]
        self.timed = [(at, line) for at, line in self.timed if at > self.clock()]
        return due

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0)


@unittest.skipUnless(HAVE_LEROBOT, "LeRobot is not installed in this environment")
class TeachKeyTest(unittest.TestCase):
    def setUp(self):
        import teach_key as tk
        from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
        from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig

        self.tk = tk
        self.tmp = Path(tempfile.mkdtemp())
        for sub in ("robots", "teleoperators"):
            (self.tmp / sub).mkdir()
        (self.tmp / "robots" / "so101_follower.json").write_text(json.dumps(calibration_json()))
        (self.tmp / "teleoperators" / "so101_leader.json").write_text(json.dumps(calibration_json()))
        self.follower = SO101Follower(SO101FollowerConfig(
            port="/dev/not-opened-f", id="so101_follower", calibration_dir=self.tmp / "robots",
            disable_torque_on_disconnect=False))
        self.leader = SO101Leader(SO101LeaderConfig(
            port="/dev/not-opened-l", id="so101_leader", calibration_dir=self.tmp / "teleoperators"))
        for device in (self.follower, self.leader):
            real = device.bus
            real.connect = lambda *a, **k: self.fail("a test tried to open a serial port")
            device.bus = FakeBus(real, calibration_json())
        self.fb, self.lb = self.follower.bus, self.leader.bus
        self.clock = Clock()
        self.fb.clock = self.clock
        self.out = io.StringIO()
        patcher = contextlib.redirect_stdout(self.out)
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)

    def deg(self, ticks):
        return ticks * 360 / 4095

    # --- connection ---------------------------------------------------------------------------

    def test_connect_seeds_goal_at_measured_pose_before_torque_enable(self):
        self.fb.raw = {n: 2000 + 37 * i for i, n in enumerate(MOTORS)}
        self.fb.goal = dict.fromkeys(MOTORS, 3000)  # stale target from another program
        self.tk.connect_follower("x", ScriptedInbox(self.clock), robot=self.follower)
        torque_on = [e for e in self.fb.log if e[0] == "torque_on"]
        self.assertEqual(len(torque_on), 6)
        for _, motor, goal, raw in torque_on:
            self.assertLessEqual(abs(goal - raw), 1, motor)  # no jump when torque engages
        self.assertTrue(all(self.fb.torque.values()))
        # LeRobot's own configure() still ran: gains and gripper protection
        names = {(e[1], e[2]) for e in self.fb.log if e[0] == "write"}
        for n in MOTORS:
            self.assertIn(("P_Coefficient", n), names)
        self.assertIn(("Protection_Current", "gripper"), names)

    def test_connect_refuses_calibration_mismatch_without_writing(self):
        self.fb.eeprom["elbow_flex"].homing_offset = 99
        with self.assertRaises(SystemExit) as caught:
            self.tk.connect_follower("x", ScriptedInbox(self.clock), robot=self.follower)
        self.assertIn("sync-calibration", str(caught.exception))
        self.assertEqual(self.fb.writes(), [])
        self.assertEqual(self.fb.log[-1], ("disconnect", False))

    def test_a_second_program_cannot_share_the_arm(self):
        self.fb.port_handler.ser.locked_elsewhere = True  # e.g. the web app holds the port
        with self.assertRaises(SystemExit) as caught:
            self.tk.connect_follower("x", ScriptedInbox(self.clock), robot=self.follower)
        self.assertIn("already in use", str(caught.exception))
        self.assertEqual(self.fb.writes(), [])
        self.assertEqual(self.fb.log[-1], ("disconnect", False))
        self.lb.port_handler.ser.locked_elsewhere = True
        with self.assertRaises(SystemExit):
            self.tk.connect_leader("x", teleop=self.leader)
        self.assertEqual(self.lb.writes(), [])

    def test_ports_are_locked_after_connecting(self):
        self.tk.connect_follower("x", ScriptedInbox(self.clock), robot=self.follower)
        self.tk.connect_leader("x", teleop=self.leader)
        self.assertTrue(self.fb.port_handler.ser.exclusive and self.lb.port_handler.ser.exclusive)

    def test_connect_asks_for_support_when_torque_already_on(self):
        self.fb.torque = dict.fromkeys(MOTORS, 1)
        inbox = ScriptedInbox(self.clock, answers=["q"])
        with self.assertRaises(SystemExit):
            self.tk.connect_follower("x", inbox, robot=self.follower)
        self.assertEqual(len(inbox.asked), 1)
        self.assertEqual(self.fb.writes(), [])
        self.assertTrue(all(self.fb.torque.values()))  # left holding, not dropped

    def test_leader_connect_never_prompts_and_refuses_mismatch(self):
        self.lb.eeprom["gripper"].range_max = 2999
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            with self.assertRaises(SystemExit):
                self.tk.connect_leader("x", teleop=self.leader)
        self.assertFalse(self.lb.is_connected)

    # --- teaching -----------------------------------------------------------------------------

    def connected(self):
        self.tk.connect_follower("x", ScriptedInbox(self.clock), robot=self.follower)
        self.tk.connect_leader("x", teleop=self.leader)
        self.fb.log.clear()

    def goals(self):
        return [e[1] for e in self.fb.log if e[0] == "goal"]

    def test_teleop_ramps_to_leader_then_follows_and_records(self):
        self.connected()
        self.lb.raw["elbow_flex"] = 2500  # leader ~44 deg away from the follower
        saved = []

        def leader_motion():
            t = self.clock()
            if 3.0 <= t < 5.0:  # "press" while recording
                self.lb.raw["wrist_flex"] = 2000 + int(100 * (t - 3.0))

        inbox = ScriptedInbox(self.clock, timed=[(3.0, ""), (5.0, ""), (6.0, "q")])
        real_poll = inbox.poll
        inbox.poll = lambda: (leader_motion(), real_poll())[1]
        self.tk.teleop(self.follower, self.leader, inbox, on_recording=saved.append,
                       clock=self.clock, sleep=self.clock.sleep)
        goals = self.goals()
        step_limit = 4095 / 360 * self.tk.RAMP_SPEED / self.tk.FPS + 1  # ticks per step, plus rounding
        ramp = [g["elbow_flex"] for g in goals if g["elbow_flex"] < 2500]
        self.assertGreater(len(ramp), 30)  # took over a second, not one jump
        previous = 2000
        for value in ramp:
            self.assertLessEqual(abs(value - previous), step_limit)
            previous = value
        self.assertEqual(goals[-1]["elbow_flex"], 2500)
        self.assertEqual(len(saved), 1)
        frames = saved[0]
        self.assertAlmostEqual(frames[-1]["t"], 2.0, delta=0.1)
        self.assertGreater(frames[-1]["goal"]["wrist_flex"], frames[0]["goal"]["wrist_flex"] + 10)
        self.assertTrue(all(b["t"] > a["t"] for a, b in zip(frames, frames[1:])))

    def test_teleop_lock_gripper_keeps_follower_opening(self):
        self.connected()
        self.lb.raw["gripper"] = 2900
        inbox = ScriptedInbox(self.clock, timed=[(2.0, "q")])
        self.tk.teleop(self.follower, self.leader, inbox, lock_gripper=True,
                       clock=self.clock, sleep=self.clock.sleep)
        self.assertTrue(all(abs(g["gripper"] - 2000) <= 1 for g in self.goals()))

    def test_enter_before_alignment_does_not_start_recording(self):
        self.connected()
        self.lb.raw["shoulder_lift"] = 2600
        saved = []
        inbox = ScriptedInbox(self.clock, timed=[(0.1, ""), (0.2, ""), (3.0, "q")])
        self.tk.teleop(self.follower, self.leader, inbox, on_recording=saved.append,
                       clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(saved, [])
        self.assertIn("wait for FOLLOWING", self.out.getvalue())

    # --- playback -----------------------------------------------------------------------------

    def recording(self):
        def pose(**over):
            p = self.tk.read_follower(self.follower)
            p.update(over)
            return p

        self.connected()
        base = self.tk.read_follower(self.follower)
        frames = []
        for i in range(60):  # 2 s: dip the wrist 6 deg and come back
            depth = 6.0 * (1 - abs(i - 30) / 30)
            goal = dict(base, wrist_flex=base["wrist_flex"] - depth)
            frames.append({"t": round(i / 30, 4), "goal": goal, "leader": goal, "follower": goal})
        self.fb.log.clear()
        return {"key": "C", "frames": frames}, base, pose

    def test_replay_ramps_to_start_then_streams_taught_goals(self):
        rec, base, _ = self.recording()
        self.fb.raw["elbow_flex"] = 2400  # start 35 deg away from the taught start
        self.fb.goal["elbow_flex"] = 2400
        ok = self.tk.replay(self.follower, rec, ScriptedInbox(self.clock), clock=self.clock,
                            sleep=self.clock.sleep)
        self.assertTrue(ok)
        elbow = [g["elbow_flex"] for g in self.goals()]
        step_limit = 4095 / 360 * self.tk.RAMP_SPEED / self.tk.FPS + 1
        self.assertTrue(all(abs(b - a) <= step_limit for a, b in zip(elbow, elbow[1:])))
        streamed = self.goals()[-60:]
        expected = [self.fb._unnorm(f["goal"]) for f in rec["frames"]]
        for got, want in zip(streamed, expected):
            for n in MOTORS:
                self.assertLessEqual(abs(got[n] - want[n]), 1)

    def test_replay_limits_push_when_blocked_and_says_so(self):
        rec, base, _ = self.recording()
        start = self.fb.raw["wrist_flex"]
        self.fb.floor["wrist_flex"] = start - 20  # a key stops the wrist after ~1.8 deg
        with mock.patch.object(self.tk, "FOLLOW_CAP", 2.0):
            self.tk.replay(self.follower, rec, ScriptedInbox(self.clock), clock=self.clock,
                           sleep=self.clock.sleep)
        deepest = min(g["wrist_flex"] for g in self.goals())
        cap_ticks = 2.0 * 4095 / 360
        # Taught dip is 6 deg (~68 ticks); against the key the goal stays within the cap of the stop.
        self.assertLess(deepest, start - 20)
        self.assertGreaterEqual(deepest, start - 20 - cap_ticks - 1)
        self.assertIn("limited to 2 deg", self.out.getvalue())

    def test_replay_asks_when_start_pose_not_reached(self):
        rec, base, _ = self.recording()
        self.fb.raw["shoulder_pan"] = 1500
        self.fb.goal["shoulder_pan"] = 1500
        self.fb.rate = 0  # something is in the way
        inbox = ScriptedInbox(self.clock, answers=["q"])
        ok = self.tk.replay(self.follower, rec, inbox, clock=self.clock, sleep=self.clock.sleep)
        self.assertFalse(ok)
        self.assertIn("shoulder_pan", inbox.asked[0])
        self.assertNotIn("PLAYING", self.out.getvalue())

    def test_s_stops_playback_with_measured_hold(self):
        rec, base, _ = self.recording()
        inbox = ScriptedInbox(self.clock, timed=[(self.tk.SETTLE_S + 0.6, "s")])
        ok = self.tk.replay(self.follower, rec, inbox, clock=self.clock, sleep=self.clock.sleep)
        self.assertFalse(ok)
        self.assertLess(len(self.goals()), 60)
        self.assertEqual(self.fb.goal, self.fb.raw)

    # --- exit -----------------------------------------------------------------------------------

    def test_finish_holds_then_releases_only_on_enter(self):
        self.connected()
        self.fb.goal["elbow_flex"] = 2300  # was pushing somewhere
        self.tk.finish(self.follower, ScriptedInbox(self.clock, answers=[""]))
        self.assertEqual(self.fb.log[-1], ("disconnect", True))
        self.assertFalse(any(self.fb.torque.values()))

    def test_closed_terminal_never_releases_torque(self):
        self.connected()
        inbox = self.tk.Inbox(io.StringIO(""))  # stdin already at EOF (terminal tab closed)
        for _ in range(100):
            if not inbox.lines.empty():
                break
            import time
            time.sleep(0.01)
        self.tk.finish(self.follower, inbox)
        self.assertEqual(self.fb.log[-1], ("disconnect", False))
        self.assertTrue(all(self.fb.torque.values()))
        self.assertEqual(inbox.ask("again? "), "q")  # never blocks after EOF

    def test_finish_hold_answer_leaves_torque_on(self):
        self.connected()
        self.tk.finish(self.follower, ScriptedInbox(self.clock, answers=["hold"]))
        self.assertEqual(self.fb.log[-1], ("disconnect", False))
        self.assertTrue(all(self.fb.torque.values()))
        self.assertFalse(self.follower.is_connected)
        del self.follower  # LeRobot's destructor must not drop torque either
        self.assertTrue(all(self.fb.torque.values()))

    # --- calibration and files --------------------------------------------------------------------

    def test_sync_calibration_copies_eeprom_and_backs_up_without_motor_writes(self):
        self.fb.eeprom["elbow_flex"].homing_offset = 321
        self.fb.eeprom["elbow_flex"].range_min = 1111
        self.tk.sync_calibration(self.follower, "follower")
        written = json.loads((self.tmp / "robots" / "so101_follower.json").read_text())
        self.assertEqual(written["elbow_flex"]["homing_offset"], 321)
        self.assertEqual(written["elbow_flex"]["range_min"], 1111)
        self.assertEqual(len(list((self.tmp / "robots").glob("so101_follower.before-*.json"))), 1)
        self.assertEqual(self.fb.writes(), [])
        self.assertEqual(self.fb.log[-1], ("disconnect", False))

    def test_sync_refuses_interrupted_calibration(self):
        self.fb.eeprom["shoulder_lift"].range_min = 0
        self.fb.eeprom["shoulder_lift"].range_max = 4095
        before = (self.tmp / "robots" / "so101_follower.json").read_text()
        with self.assertRaises(SystemExit):
            self.tk.sync_calibration(self.follower, "follower")
        self.assertEqual((self.tmp / "robots" / "so101_follower.json").read_text(), before)

    def test_saved_recording_round_trips_and_play_refuses_other_calibration(self):
        self.connected()
        frames = [{"t": 0.0, "goal": {}, "leader": {}, "follower": {}},
                  {"t": 1.0, "goal": {}, "leader": {}, "follower": {}}]
        with mock.patch.object(self.tk, "KEYS_DIR", self.tmp / "keys"):
            self.tk.save_recording("C#", frames, self.follower, lock_gripper=False)
            self.tk.save_recording("C#", frames, self.follower, lock_gripper=False)
            self.assertTrue((self.tmp / "keys" / "C#.prev.json").exists())
            self.assertEqual(self.tk.load_recording("C#")["frames"], frames)
            self.follower.calibration["elbow_flex"].homing_offset = 5
            args = mock.Mock(key="C#", speed=0.5, follower_port="x")
            with mock.patch.object(self.tk, "make_follower", return_value=self.follower):
                with self.assertRaises(SystemExit) as caught:
                    self.tk.cmd_play(args)
            self.assertIn("different follower calibration", str(caught.exception))

    # --- the commands as run, in real time (a few seconds) ------------------------------------------

    def real_time(self):
        import time

        t0 = time.perf_counter()
        self.fb.clock, self.fb.moved_at = time.perf_counter, t0
        return lambda: time.perf_counter() - t0

    def patched(self, inbox):
        return mock.patch.multiple(self.tk, KEYS_DIR=self.tmp / "keys", Inbox=lambda: inbox,
                                   make_follower=lambda port: self.follower, make_leader=lambda port: self.leader)

    def test_record_command_saves_and_releases_on_enter(self):
        clock = self.real_time()
        inbox = ScriptedInbox(clock, timed=[(0.3, ""), (1.0, ""), (1.2, "q")], answers=["", ""])
        args = mock.Mock(key="E", lock_gripper=False, follower_port="f", leader_port="l")
        with self.patched(inbox):
            self.tk.cmd_record(args)
        saved = json.loads((self.tmp / "keys" / "E.json").read_text())
        self.assertAlmostEqual(saved["frames"][-1]["t"], 0.7, delta=0.15)
        self.assertEqual(saved["follower_calibration_sha256"], self.tk.calibration_digest(self.follower.calibration))
        self.assertEqual(self.fb.log[-1], ("disconnect", True))
        self.assertFalse(self.lb.is_connected)
        self.assertIn("play E --speed 0.5", self.out.getvalue())

    def test_record_command_sends_no_motion_until_start_enter(self):
        self.fb.torque = dict.fromkeys(MOTORS, 1)  # left holding by another program
        self.lb.raw["elbow_flex"] = 2600  # leader far from the follower
        inbox = ScriptedInbox(self.clock, answers=["", "q", "hold"])  # support, quit at start prompt, keep holding
        args = mock.Mock(key="E", lock_gripper=False, follower_port="f", leader_port="l")
        with self.patched(inbox):
            self.tk.cmd_record(args)
        self.assertIn("Hands off the follower", inbox.asked[1])
        self.assertTrue(all(abs(g["elbow_flex"] - 2000) <= 1 for g in self.goals()))  # only holds, no ramp
        self.assertEqual(self.fb.log[-1], ("disconnect", False))

    def test_play_command_replays_then_quits_and_releases(self):
        self.tk.connect_follower("x", ScriptedInbox(self.clock), robot=self.follower)
        base = self.tk.read_follower(self.follower)
        frames = [{"t": i / 30, "goal": dict(base, wrist_flex=base["wrist_flex"] - (3 if 5 <= i < 10 else 0)),
                   "leader": base, "follower": base} for i in range(15)]
        with mock.patch.object(self.tk, "KEYS_DIR", self.tmp / "keys"):
            self.tk.save_recording("C", frames, self.follower, lock_gripper=False)
        self.follower.bus.disconnect(disable_torque=True)
        self.fb.log.clear()
        clock = self.real_time()
        inbox = ScriptedInbox(clock, answers=["q", ""])
        with self.patched(inbox):
            self.tk.cmd_play(mock.Mock(key="C", speed=1.0, follower_port="f"))
        dip = [g["wrist_flex"] for g in self.goals()]
        self.assertLess(min(dip), max(dip) - 30)  # the 3 deg press was sent
        self.assertIn("PLAYING C", self.out.getvalue())
        self.assertEqual(self.fb.log[-1], ("disconnect", True))

    def test_port_detection_only_blocks_on_arms_the_command_needs(self):
        arms = {"warnings": [], "arms": [
            {"port": "/dev/ttyACM2", "role": "follower", "voltage": 12.3, "motor_ids": [1, 2, 3, 4, 5, 6]},
            {"port": "/dev/ttyACM0", "role": "leader", "voltage": 5.0, "motor_ids": [1, 2, 3, 4, 5]}]}
        args = mock.Mock(follower_port=None, leader_port=None)
        with mock.patch("orchid_demo.discovery.discover_arms", return_value=arms):
            self.assertEqual(self.tk.resolve_ports(args, need_leader=False), ("/dev/ttyACM2", None))
            with self.assertRaises(SystemExit) as caught:
                self.tk.resolve_ports(args, need_leader=True)
        self.assertIn("[6]", str(caught.exception))

    def test_key_names(self):
        self.assertEqual(self.tk.key_path("C#").name, "C#.json")
        with self.assertRaises(SystemExit):
            self.tk.key_path("../x")


if __name__ == "__main__":
    unittest.main()
