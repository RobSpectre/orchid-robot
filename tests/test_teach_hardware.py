"""The web console's hardware adapter for record -> replay, over real LeRobot code and a fake bus.

Run with the hardware environment (LeRobot installed):
    PYTHONPATH=. ~/.virtualenvs/replay-lw/bin/python -m unittest discover -s tests -p test_teach_hardware.py
No serial port is opened. Skipped where LeRobot is not installed.
"""
import importlib.util
import unittest

from test_teach_key import MOTORS, Clock, FakeBus, calibration_json

HAVE_LEROBOT = importlib.util.find_spec("lerobot") is not None


@unittest.skipUnless(HAVE_LEROBOT, "LeRobot is not installed in this environment")
class TeachHardwareAdapterTest(unittest.TestCase):
    def device(self, cls):
        device = cls("/dev/not-opened", calibration_json())
        real = device.bus
        real.connect = lambda *a, **k: self.fail("a test tried to open a serial port")
        device.bus = FakeBus(real, calibration_json(), clock=Clock())
        device.bus.is_connected = True
        device.calibration_matches = True
        return device

    def setUp(self):
        from orchid_demo.devices import HardwareArm, HardwareLeader

        self.arm = self.device(HardwareArm)
        self.leader = self.device(HardwareLeader)
        self.bus = self.arm.bus

    def test_arming_seeds_the_measured_pose_then_runs_lerobot_configure(self):
        self.bus.raw = {n: 1500 + 100 * i for i, n in enumerate(MOTORS)}
        self.bus.goal = dict.fromkeys(MOTORS, 2900)  # stale target from another program
        self.arm.arm_for_teleop()
        torque_on = [e for e in self.bus.log if e[0] == "torque_on"]
        self.assertEqual(len(torque_on), 6)
        for _, motor, goal, raw in torque_on:
            self.assertEqual(goal, raw, motor)  # no jump when torque engages
        written = {(e[1], e[2]) for e in self.bus.log if e[0] == "write"}
        for n in MOTORS:
            self.assertIn(("P_Coefficient", n), written)  # SOFollower.configure itself ran
        self.assertIn(("Protection_Current", "gripper"), written)

    def test_arming_adopts_a_follower_that_is_already_holding(self):
        self.bus.torque = dict.fromkeys(MOTORS, 1)
        self.arm.arm_for_teleop()
        self.assertTrue(all(self.bus.torque.values()))

    def test_goals_and_reads_use_lerobot_degrees(self):
        self.arm.arm_for_teleop()
        raw, joints = self.arm.teleop_read()
        self.assertEqual(raw, self.bus.raw)
        goal = dict(joints, elbow_flex=joints["elbow_flex"] + 9.0)
        self.arm.teleop_goal(goal)
        self.assertAlmostEqual(self.bus.goal["elbow_flex"] - raw["elbow_flex"], 9.0 * 4095 / 360, delta=1)

    def test_leader_reads_but_never_arms_or_moves(self):
        from orchid_demo.motion import SafetyError

        raw, joints = self.leader.teleop_read()
        self.assertEqual(set(joints), set(MOTORS))
        with self.assertRaises(SafetyError):
            self.leader.arm_for_teleop()
        with self.assertRaises(SafetyError):
            self.leader.teleop_goal(joints)
        self.assertFalse(any(e[0] == "goal" for e in self.leader.bus.log))


if __name__ == "__main__":
    unittest.main()
