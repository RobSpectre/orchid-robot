"""Offline conformance check; run with the same Python/LeRobot as the hardware app.

PYTHONPATH=. /path/to/hardware/python tests/check_native_teleop.py
No ports are opened. Real SO101 get_action/get_observation/send_action methods
operate on fake serial I/O with the real Feetech normalization implementation.
"""
import io
import unittest

from orchid_demo import motion as m
from orchid_demo.devices import HardwareArm, HardwareLeader
from orchid_demo.leader import LeaderController
from lerobot.robots.so_follower import SO101FollowerConfig
from lerobot.teleoperators.so_leader import SO101LeaderConfig


def calibration(leader=False):
    result = {n: {'id': i, 'drive_mode': 0, 'homing_offset': 0, 'range_min': 800,
                  'range_max': 3200} for i, n in enumerate(m.MOTORS, 1)}
    result['shoulder_lift'].update(range_min=839 if leader else 938, range_max=3183 if leader else 3339)
    result['wrist_roll'].update(range_min=0, range_max=4095)
    result['gripper'].update(range_min=2000 if leader else 1000, range_max=3000)
    return result


class Wire:
    def __init__(self, real, *, leader):
        self.port, self.motors = real.port, real.motors
        self._normalize, self._unnormalize = real._normalize, real._unnormalize
        self.is_connected = True
        self.raw = dict.fromkeys(m.MOTORS, 2000)
        self.goal = dict(self.raw)
        self.torque = dict.fromkeys(m.MOTORS, 0 if leader else 1)
        self.reads, self.writes = [], []
        self.follow = True

    def sync_read(self, name, **kwargs):
        self.reads.append((name, kwargs))
        assert kwargs.get('normalize') is False
        return dict(self.raw if name == 'Present_Position' else self.torque)

    def sync_write(self, name, values, **kwargs):
        assert name == 'Goal_Position'
        assert not kwargs  # Native send_action uses default normalized sync_write.
        decoded = self._unnormalize({self.motors[n].id: v for n, v in values.items()})
        raw = {n: decoded[self.motors[n].id] for n in values}
        self.writes.append(raw)
        self.goal.update(raw)
        if self.follow:
            self.raw.update(raw)


class NativeTeleopTest(unittest.TestCase):
    def setUp(self):
        self.leader = HardwareLeader('/dev/not-opened-leader', calibration(True))
        self.follower = HardwareArm('/dev/not-opened-follower', calibration())
        for device in (self.leader, self.follower):
            real = device.bus
            self.assertFalse(real.is_connected)
            # Any accidental connection remains a hard failure in the real SDK.
            real.connect = lambda: self.fail('A conformance test tried to open a port')
            device.bus = Wire(real, leader=device.role == 'leader')
        self.now = 0.0
        def sleep(seconds):
            self.now += seconds
        self.control = LeaderController(self.follower, self.leader, dict(self.follower.bus.raw), io.StringIO(),
            guard=lambda: None, permission=lambda: True, observe=lambda _: None, leader_feedback=lambda _: None,
            feedback=lambda *_: None, clock=lambda: self.now, sleep=sleep)
        self.control.enabled = True

    def test_actual_library_defaults_and_read_retry_configuration(self):
        settings = self.control.settings
        defaults = SO101FollowerConfig(port='/dev/not-opened')
        self.assertEqual(settings['max_relative_target'], defaults.max_relative_target)
        self.assertIsNone(settings['max_relative_target'])
        self.assertEqual(settings['num_read_retries'], defaults.num_read_retries)
        self.control.tick()
        for device, config in ((self.leader, SO101LeaderConfig(port='unused')), (self.follower, defaults)):
            positions = [kw for name, kw in device.bus.reads if name == 'Present_Position']
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0]['num_retry'], config.num_read_retries)

    def test_reported_831_reading_uses_absolute_calibrated_pose_without_range_fault(self):
        self.leader.bus.raw['shoulder_lift'] = 839
        self.control.resume()
        self.control.tick()
        expected = self.follower.joint_target(self.leader.joint_action(self.leader.bus.raw, self.control.controlled_motors))
        self.assertEqual(self.follower.bus.raw["shoulder_lift"], expected["shoulder_lift"])
        self.leader.bus.raw['shoulder_lift'] = 831
        self.control.tick()
        expected = self.follower.joint_target(self.leader.joint_action(self.leader.bus.raw, self.control.controlled_motors))
        self.assertEqual(self.follower.bus.raw['shoulder_lift'], max(938, expected['shoulder_lift']))
        self.assertFalse(self.leader.bus.writes)
        self.assertTrue(all('gripper' not in write for write in self.follower.bus.writes))

    def test_encoder_round_trip_preserves_every_possible_hold_position(self):
        for value in range(4096):
            raw = dict.fromkeys(m.MOTORS, value)
            action = self.control.exact_action(raw)
            self.assertEqual(self.follower.joint_target(action), {n: value for n in m.MOTORS[:-1]})

    def test_direct_position_target_has_no_invented_speed_or_stall_gate(self):
        self.control.resume()
        self.follower.bus.follow = False  # No perfect-servo assumption.
        self.leader.bus.raw['elbow_flex'] += 200
        for _ in range(40):
            self.control.tick()
        self.assertEqual(self.follower.bus.goal['elbow_flex'], 2200)
        self.assertEqual(self.follower.bus.raw['elbow_flex'], 2000)
        self.assertTrue(self.control.engaged)
        self.assertEqual(self.control.last_sample['target']['elbow_flex'], 2200)
        self.control.pause()
        self.assertEqual(self.follower.bus.goal['elbow_flex'], 2000)
        self.control.resume()
        self.control.tick()
        self.assertEqual(self.follower.bus.goal['elbow_flex'], 2200)

    def test_recorded_follower_endpoint_saturates_and_reverses_without_margin(self):
        self.follower.bus.raw['shoulder_lift'] = 938
        self.control.previous = dict(self.follower.bus.raw)
        self.control.resume()
        self.leader.bus.raw['shoulder_lift'] = 800
        self.control.tick()
        self.assertEqual(self.follower.bus.raw['shoulder_lift'], 938)
        self.leader.bus.raw['shoulder_lift'] = 1000
        self.control.tick()
        expected = self.follower.joint_target(self.leader.joint_action(self.leader.bus.raw, m.MOTORS[:-1]))
        self.assertEqual(self.follower.bus.raw['shoulder_lift'], expected['shoulder_lift'])

    def test_home_gripper_uses_native_calibrated_ranges_and_capture_locks_it(self):
        self.control.follow_gripper = True
        self.control.resume()
        self.control.tick()
        self.assertEqual(self.follower.bus.raw["gripper"], 1000)
        # Leader span 1000; follower span 2000. 10% opening is 100/200 ticks.
        self.leader.bus.raw['gripper'] += 100
        self.control.tick()
        self.assertEqual(self.follower.bus.raw['gripper'], 1200)
        self.leader.bus.raw['gripper'] -= 50
        self.control.tick()
        self.assertEqual(self.follower.bus.raw['gripper'], 1100)
        self.control.pause()
        self.leader.bus.raw['gripper'] += 400
        self.control.tick()
        self.control.resume()
        self.control.tick()
        self.assertEqual(self.follower.bus.raw['gripper'], 1900)
        self.control.pause()
        self.control.set_travel('approach', dict(self.follower.bus.raw))
        self.control.resume()
        count = len(self.follower.bus.writes)
        self.leader.bus.raw['gripper'] += 200
        self.control.tick()
        self.assertEqual(self.follower.bus.raw['gripper'], 1900)
        self.assertTrue(all('gripper' not in w for w in self.follower.bus.writes[count:]))
        self.assertFalse(self.leader.bus.writes)

    def test_home_gripper_encoder_round_trip_and_endpoint_reversal(self):
        self.control.follow_gripper = True
        for value in range(1000, 3001):
            raw = {**self.follower.bus.raw, 'gripper': value}
            self.assertEqual(self.follower.joint_target(self.control.exact_action(raw)), raw)
        self.follower.bus.raw['gripper'] = 3000
        self.leader.bus.raw['gripper'] = 3000
        self.control.previous = dict(self.follower.bus.raw)
        self.control.resume()
        self.leader.bus.raw['gripper'] += 100
        self.control.tick()
        self.assertEqual(self.follower.bus.raw['gripper'], 3000)
        self.leader.bus.raw['gripper'] -= 200
        self.control.tick()
        self.assertEqual(self.follower.bus.raw['gripper'], 2800)
        self.follower.bus.raw['gripper'] = 2700
        self.control.stop()
        self.assertEqual(self.follower.bus.goal['gripper'], 2700)

    def test_homing_uses_real_native_send_and_keeps_leader_and_torque_untouched(self):
        self.control.follow_gripper = True
        self.follower.calibration_matches = True
        target = {**self.follower.bus.raw, 'elbow_flex': 2200, 'gripper': 1700}
        progress = []
        self.control.move_home(target, 1, progress.append)
        self.assertEqual(self.follower.bus.raw, target)
        self.assertEqual(progress[-1], 1)
        self.assertEqual(len(progress), 20)
        self.assertFalse(self.control.engaged)
        self.assertFalse(self.leader.bus.writes)
        self.assertTrue(all(2000 <= p['elbow_flex'] <= 2200 for p in self.follower.bus.writes))

    def test_direct_home_sends_native_target_without_ramp_or_arrival_gate(self):
        self.control.follow_gripper = True
        self.follower.calibration_matches = True
        self.follower.bus.follow = False
        target = {**self.follower.bus.raw, 'elbow_flex': 2200, 'gripper': 1700}
        progress = []
        self.control.move_home(target, 0, progress.append)
        self.assertEqual(self.follower.bus.goal, target)
        self.assertNotEqual(self.follower.bus.raw, target)
        self.assertEqual(progress, [1])
        self.assertEqual(len(self.follower.bus.writes), 2)  # Existing hold, then direct target.
        self.assertAlmostEqual(self.now, 2 * m.PERIOD)
        self.assertFalse(self.leader.bus.writes)
        self.assertIsNone(self.control.settings['max_relative_target'])
        self.control.follow_gripper = False
        self.control.resume()
        self.leader.bus.raw['elbow_flex'] = 2400
        self.control.tick()
        self.assertEqual(self.follower.bus.goal['elbow_flex'], 2400)
        self.assertTrue(self.control.engaged)


if __name__ == '__main__':
    unittest.main()
