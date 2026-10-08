"""Hardware boundary. No import, discovery, or connection in simulation mode."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import importlib.metadata
import time

from . import motion as m


def validate_calibration(calibration):
    """Accept only the six-motor raw calibration format written by this app."""
    m.require(isinstance(calibration, dict) and set(calibration) == set(m.MOTORS),
              "A complete saved calibration for all six motors is required.")
    fields = {"id", "drive_mode", "homing_offset", "range_min", "range_max"}
    for i, name in enumerate(m.MOTORS, 1):
        value = calibration[name]
        m.require(isinstance(value, dict) and set(value) == fields
                  and all(type(v) is int for v in value.values()), f"Invalid saved calibration for {name}.")
        m.require(value["id"] == i and value["drive_mode"] == 0
                  and -2047 <= value["homing_offset"] <= 2047
                  and 0 <= value["range_min"] < value["range_max"] <= 4095
                  and value["range_max"] - value["range_min"] > 2 * m.JOINT_MARGIN,
                  f"Invalid saved calibration for {name}; perform a full calibration.")


def check_calibrated_pose(calibration, pose):
    m.pose_valid(pose)
    m.require(calibration is not None, "Complete motor calibration first.")
    for name, value in pose.items():
        low, high = calibration[name]["range_min"], calibration[name]["range_max"]
        m.require(low <= value <= high,
                  f"{name}: {value} ticks is outside recorded travel {low}..{high}. No motion authorized.")


class SimulatedArm(m.Arm):
    version = "simulator-1"

    def __init__(self, calibration=None):
        self.calibration = deepcopy(calibration)
        self.current = dict.fromkeys(m.MOTORS, 2047)
        self.enabled = False
        self.connected = False
        self.backup = None
        self.jammed = False
        self.voltage = 12.0

    @property
    def signature(self):
        return m.fingerprint(self.calibration or {})

    def open(self):
        self.connected = True

    def close(self):
        self.connected = False  # Do not change torque as a side effect.

    def read_raw(self):
        m.require(self.connected, "Simulator disconnected")
        return dict(self.current)

    def joint_action(self, raw, motors=m.MOTORS[:-1]):
        """Mirror LeRobot degrees for joints and calibrated 0..100 for grip."""
        action = {}
        for n in motors:
            low, high = self.calibration[n]["range_min"], self.calibration[n]["range_max"]
            action[n] = ((max(low, min(high, raw[n])) - low) * 100 / (high - low) if n == "gripper"
                         else (raw[n] - (low + high) / 2) * 360 / 4095)
        return action

    def joint_target(self, action):
        target = {}
        for n, value in action.items():
            low, high = self.calibration[n]["range_min"], self.calibration[n]["range_max"]
            target[n] = int(max(0, min(100, value)) / 100 * (high - low) + low if n == "gripper"
                            else value * 4095 / 360 + (low + high) / 2)
        return target

    def teleop_read(self):
        raw = self.read_raw()
        m.pose_valid(raw)
        return raw, self.joint_action(raw, m.MOTORS)

    def teleop_send(self, action, gripper):
        target = {"gripper": gripper, **self.joint_target(action)}
        self.send_calibrated(target)
        return dict(action)

    @property
    def teleop_settings(self):
        return {"driver": "simulated LeRobot SO101", "use_degrees": True,
                "max_relative_target": None, "num_read_retries": 2}

    def arm_for_teleop(self):
        """Hold the measured pose with torque on, whether or not torque was already on."""
        m.require(self.calibration is not None, "Complete motor calibration first.")
        self.enabled = True

    def teleop_goal(self, goal):
        """Degrees/0-100 goal; out-of-range values stop at the calibrated ends like servo firmware limits."""
        m.require(self.enabled, "Simulated follower torque is off.")
        raw = self.joint_target(goal)
        bounded = {n: max(self.calibration[n]["range_min"], min(self.calibration[n]["range_max"], v)) for n, v in raw.items()}
        if not self.jammed:
            self.current.update(bounded)
        return dict(goal)

    def check_pose(self, pose):
        m.require(self.calibration is not None, "Complete motor calibration first.")
        super().check_pose(pose)

    def torque_status(self):
        return dict.fromkeys(m.MOTORS, int(self.enabled))

    def read_diagnostics(self):
        self.require_torque(False)
        return {name: {"voltage_v": self.voltage, "temperature_c": 25} for name in m.MOTORS}

    def send(self, pose):
        self.check_pose(pose)
        if not self.jammed:
            self.current = dict(pose)

    def arm_at_current(self, current):
        self.require_torque(False)
        m.require(current == self.current, "Simulator moved while arming")
        self.enabled = True

    def check_calibrated_pose(self, pose):
        check_calibrated_pose(self.calibration, pose)

    def send_calibrated(self, pose):
        self.check_calibrated_pose(pose)
        if not self.jammed:
            self.current = dict(pose)

    def arm_at_calibrated(self, current):
        self.check_calibrated_pose(current)
        self.arm_at_current(current)

    def release(self):
        self.enabled = False

    def begin_calibration(self):
        self.require_torque(False)
        self.backup = deepcopy(self.calibration)
        return deepcopy(self.backup)

    def center(self, *, guard=lambda: None, sleep=time.sleep):
        guard()
        self.require_torque(False)
        self.current = dict.fromkeys(m.MOTORS, 2047)
        return dict.fromkeys(m.MOTORS, 0)

    def commit_calibration(self, calibration, *, guard=lambda: None):
        validate_calibration(calibration)
        guard()
        self.require_torque(False)
        self.calibration = deepcopy(calibration)
        self.backup = None

    def abort_calibration(self):
        self.require_torque(False)
        self.calibration = deepcopy(self.backup)
        self.backup = None


class HardwareArm(m.Arm):
    """Read-only connection; LeRobot position-control setup at explicit arming."""
    role = "follower"

    def __init__(self, port, calibration=None):
        from lerobot.motors import Motor, MotorCalibration, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus

        self.version = importlib.metadata.version("lerobot")
        m.require(self.version == "0.6.1", "This hardware adapter requires the validated LeRobot 0.6.1 interface.")
        self.calibration = deepcopy(calibration)
        self.signature = m.fingerprint(calibration or {})
        self.bus = FeetechMotorsBus(
            port=port,
            motors={name: Motor(i, "sts3215", MotorNormMode.RANGE_0_100 if name == "gripper" else MotorNormMode.DEGREES)
                    for i, name in enumerate(m.MOTORS, 1)},
            calibration={name: MotorCalibration(**value) for name, value in (calibration or {}).items()},
        )
        self.backup = None
        self.locks = None
        self.homing_changed = False
        self.calibration_matches = False

    def open(self):
        self.connection_readings = {"stage": "open_serial"}
        self.bus.connect()
        # pyserial's exclusive flock also prevents another cooperating serial owner.
        self.bus.port_handler.ser.exclusive = True
        self.connection_readings["stage"] = "voltage"
        values = self.bus.sync_read("Present_Voltage", normalize=False, num_retry=0)
        self.connection_readings["voltage_raw"] = dict(values)
        valid_voltage = (lambda v: 40 <= v < 80) if self.role == "leader" else (lambda v: v >= 80)
        m.require(set(values) == set(m.MOTORS) and all(valid_voltage(v) for v in values.values()),
                  f"Expected six {'low-voltage leader' if self.role == 'leader' else '12 V follower'} motors. Check the selected port and power.")
        self.voltage = min(values.values()) / 10
        self.connection_readings["stage"] = "operating_mode"
        modes = self.bus.sync_read("Operating_Mode", normalize=False, num_retry=0)
        m.require(set(modes) == set(m.MOTORS) and all(v == 0 for v in modes.values()),
                  "Motors must already be in position mode. No settings were changed.")
        self.connection_readings["stage"] = "saved_calibration_readback"
        self.calibration_matches = bool(self.calibration) and self.bus.is_calibrated
        self.connection_readings["stage"] = "complete"

    def check_pose(self, pose):
        m.require(self.calibration is not None and self.calibration_matches,
                  "Complete and verify motor calibration before any note motion.")
        super().check_pose(pose)

    def check_calibrated_pose(self, pose):
        m.require(self.calibration_matches, "Verify the follower calibration before motion.")
        check_calibrated_pose(self.calibration, pose)

    def joint_action(self, raw, motors=m.MOTORS[:-1]):
        # Same conversion used by SOLeader.get_action's normalized sync_read.
        # Consume this tick's cached read; no second serial request or connection.
        values = self.bus._normalize({self.calibration[n]["id"]: raw[n] for n in motors})
        return {n: values[self.calibration[n]["id"]] for n in motors}

    def joint_target(self, action):
        # Same conversion used by SOFollower.send_action's normalized sync_write.
        values = self.bus._unnormalize({self.calibration[n]["id"]: action[n] for n in action})
        return {n: values[self.calibration[n]["id"]] for n in action}

    @property
    def teleop_driver(self):
        from .lerobot_teleop import NativeTeleop
        if not hasattr(self, "_teleop_driver"):
            self._teleop_driver = NativeTeleop(self)
        return self._teleop_driver

    @property
    def teleop_settings(self):
        return self.teleop_driver.settings

    def teleop_read(self):
        raw, action = self.teleop_driver.read()
        m.pose_valid(raw)
        return raw, action

    def teleop_send(self, action, gripper):
        m.require(self.role == "follower", "The leader is input-only; motor goals are forbidden.")
        # The controller includes gripper only during home positioning. Omitted
        # joints retain their last goal; no separate jaw command is fabricated.
        return self.teleop_driver.send(action)

    def arm_for_teleop(self):
        """teach_key's connect: seed Goal_Position at the measured pose, then run LeRobot's own
        SOFollower.configure (torque briefly off while gains are written, then on, holding the seed).
        Works whether torque was off or another program left the arm holding."""
        from lerobot.robots.so_follower import SO101Follower

        m.require(self.role == "follower", "Leader torque enable is forbidden.")
        m.require(self.calibration_matches, "Verify the follower calibration before motion.")
        present = self.bus.sync_read("Present_Position", normalize=False, num_retry=2)
        m.pose_valid(present)
        self.bus.sync_write("Goal_Position", present, normalize=False)
        SO101Follower.configure(self.teleop_driver.view)
        self.require_torque(True)

    def teleop_goal(self, goal):
        """Degrees/0-100 goal through SO101Follower.send_action; servo firmware enforces the EEPROM limits."""
        return self.teleop_send(goal, None)

    def prepare_motion(self, *, guard=lambda: None):
        """Match SOFollower.configure in pinned LeRobot 0.6.1, without its
        torque_disabled context manager (which enables torque on exit).

        Controllers call this before their stable-pose check and target seeding.
        Configuration never authorizes a move or changes saved calibration.
        """
        m.require(self.role == "follower", "The leader is input-only; motion setup is forbidden.")
        guard()
        self.require_torque(False)
        m.require(self.calibration_matches, "Verify follower calibration before motion setup.")
        config = self.teleop_driver.config
        self.position_control_setup = {"verified": False, "profile": "lerobot-0.6.1-so101"}
        profile = {name: dict.fromkeys(m.MOTORS, value) for name, value in {
            "Return_Delay_Time": 0, "Maximum_Acceleration": 254, "Acceleration": 254,
            "Operating_Mode": 0, "P_Coefficient": config.position_p_coefficient,
            "I_Coefficient": config.position_i_coefficient, "D_Coefficient": config.position_d_coefficient,
        }.items()}
        profile.update({name: {"gripper": value} for name, value in {
            "Max_Torque_Limit": 500, "Protection_Current": 250, "Overload_Torque": 25,
        }.items()})
        phase = self.bus.sync_read("Phase", normalize=False, num_retry=0)
        m.require(set(phase) == set(m.MOTORS) and all(type(v) is int for v in phase.values()),
                  "Incomplete motor phase readback; torque remains off.")
        profile["Phase"] = {name: value & ~0x10 for name, value in phase.items()}
        locks = self.bus.sync_read("Lock", normalize=False, num_retry=0)
        m.require(set(locks) == set(m.MOTORS) and all(v in (0, 1) for v in locks.values()),
                  "Incomplete EEPROM lock readback; torque remains off.")
        try:
            for name in m.MOTORS:
                guard()
                self.bus.write("Lock", name, 0, normalize=False, num_retry=0)
            guard()
            # Use the same library helper as replay: response delay, acceleration,
            # and STS3215 angle feedback mode. It does not enable torque.
            self.bus.configure_motors()
            for register in ("Operating_Mode", "P_Coefficient", "I_Coefficient", "D_Coefficient",
                             "Max_Torque_Limit", "Protection_Current", "Overload_Torque"):
                for name, value in profile[register].items():
                    guard()
                    self.bus.write(register, name, value, normalize=False, num_retry=0)
            for register, expected in profile.items():
                guard()
                actual = self.bus.sync_read(register, normalize=False, num_retry=0)
                self.position_control_setup[register] = actual
                m.require(all(actual.get(name) == value for name, value in expected.items()),
                          f"Motor setup readback mismatch for {register}: expected {expected}, got {actual}. Torque remains off.")
        finally:
            # Restore the original locks even when setup/guard fails. Never use
            # torque_disabled here: its exit would power an unseeded target.
            for name, value in locks.items():
                self.bus.write("Lock", name, value, normalize=False, num_retry=0)
        guard()
        self.require_torque(False)
        m.require(self.bus.sync_read("Lock", normalize=False, num_retry=0) == locks,
                  "Could not verify motor setup lock restoration; torque remains off.")
        self.position_control_setup["verified"] = True

    def send_calibrated(self, pose):
        # Used only by the home-route adapter, which imposes tighter route bounds.
        self.check_calibrated_pose(pose)
        self.bus.sync_write("Goal_Position", pose, normalize=False, num_retry=0)

    def read_goal(self):
        """Read actual servo goal registers, distinct from encoder feedback."""
        goals = self.bus.sync_read("Goal_Position", normalize=False, num_retry=0)
        m.pose_valid(goals)
        return goals

    def arm_at_calibrated(self, current):
        self.require_torque(False)
        self.send_calibrated(current)
        actual = self.bus.sync_read("Goal_Position", normalize=False, num_retry=0)
        m.require(actual == current, "Could not verify home hold targets; torque remains off.")
        measured = self.read_raw()
        self.check_calibrated_pose(measured)
        m.require(max(abs(measured[n] - current[n]) for n in m.MOTORS) <= 2,
                  "Follower moved before the home hold. Support it steadily and retry.")
        self.bus.enable_torque(num_retry=0)
        self.require_torque(True)

    def read_diagnostics(self):
        # Explicit idle-only read. Extra bus traffic never runs in a motion tick.
        self.require_torque(False)
        volts = self.bus.sync_read("Present_Voltage", normalize=False, num_retry=0)
        temperatures = self.bus.sync_read("Present_Temperature", normalize=False, num_retry=0)
        m.require(set(volts) == set(temperatures) == set(m.MOTORS), "Incomplete motor health readback.")
        m.require(all(type(v) is int and 0 <= v <= 255 for v in [*volts.values(), *temperatures.values()]),
                  "Invalid motor health readback.")
        return {name: {"voltage_v": volts[name] / 10, "temperature_c": temperatures[name]} for name in m.MOTORS}

    def begin_calibration(self):
        self.require_torque(False)
        backup = self.bus.read_calibration()
        m.require(set(backup) == set(m.MOTORS), "Incomplete calibration backup")
        locks = self.bus.sync_read("Lock", normalize=False, num_retry=0)
        m.require(set(locks) == set(m.MOTORS) and all(v in (0, 1) for v in locks.values()),
                  "Incomplete EEPROM lock readback")
        self.backup, self.locks = backup, locks
        return {name: asdict(value) for name, value in self.backup.items()}

    def center(self, *, guard=lambda: None, sleep=time.sleep):
        guard()
        self.require_torque(False)
        m.require(self.backup is not None, "Calibration backup is missing")
        previous_offsets = self.bus.sync_read("Homing_Offset", normalize=False, num_retry=0)
        m.require(set(previous_offsets) == set(m.MOTORS)
                  and all(type(v) is int and -2047 <= v <= 2047 for v in previous_offsets.values()),
                  "Invalid homing-offset readback; midpoint was not written.")
        before = self.read_raw()
        # Feetech: Present_Position = Actual_Position - Homing_Offset.
        # Compute from the existing reference. Resetting offsets to zero and
        # immediately sampling position can mix two encoder reference frames.
        offsets = {name: (before[name] + previous_offsets[name]) % 4096 - 2047 for name in m.MOTORS}
        m.require(all(-2047 <= v <= 2047 for v in offsets.values()),
                  "Midpoint is on the encoder wrap boundary. Reposition slightly while supported and retry.")
        for name in m.MOTORS:
            guard()
            self.bus.write("Lock", name, 0, normalize=False, num_retry=0)
        self.homing_changed = True
        self.calibration_matches = False
        self.bus.calibration = {}
        for name, offset in offsets.items():
            guard()
            self.bus.write("Homing_Offset", name, offset, normalize=False, num_retry=0)
            self.bus.write("Min_Position_Limit", name, 0, normalize=False, num_retry=0)
            self.bus.write("Max_Position_Limit", name, 4095, normalize=False, num_retry=0)
        guard()
        actual_offsets = self.bus.sync_read("Homing_Offset", normalize=False, num_retry=0)
        mismatches = [f"{name}: wrote {offset}, read {actual_offsets.get(name, 'missing')}"
                      for name, offset in offsets.items() if actual_offsets.get(name) != offset]
        m.require(not mismatches, "Midpoint offset write verification failed: " + "; ".join(mismatches))
        # Allow a bounded interval for the new reference to reach feedback, then
        # require three steady samples. Keep the original ±3 tick midpoint bound.
        samples = []
        for attempt in range(12):
            guard()
            started = time.monotonic()
            self.require_torque(False)
            centered = self.read_raw()
            m.require(time.monotonic() - started <= m.MAX_IO_TIME, "Midpoint feedback is stale; calibration was not accepted.")
            samples = (samples + [centered])[-3:]
            if len(samples) == 3 and all(
                all(abs(p[name] - 2047) <= 3 for p in samples)
                and max(p[name] for p in samples) - min(p[name] for p in samples) <= 2
                for name in m.MOTORS
            ):
                guard()
                return offsets
            if attempt < 11:
                sleep(0.05)
        detail = "; ".join(
            f"{name}: {centered[name]} ticks ({centered[name] - 2047:+d} from midpoint), "
            f"spread {max(p[name] for p in samples) - min(p[name] for p in samples)}"
            for name in m.MOTORS
            if any(abs(p[name] - 2047) > 3 for p in samples)
            or max(p[name] for p in samples) - min(p[name] for p in samples) > 2
        )
        raise m.SafetyError("Midpoint readback did not settle at 2047 ±3 ticks: " + detail
                            + ". This can be a reference/readback issue or movement; calibration was not accepted.")

    def restore_locks(self):
        self.require_torque(False)
        for name, value in (self.locks or {}).items():
            self.bus.write("Lock", name, value, normalize=False, num_retry=0)
        if self.locks is not None:
            m.require(self.bus.sync_read("Lock", normalize=False, num_retry=0) == self.locks,
                      "Unable to verify EEPROM lock restoration")

    def commit_calibration(self, calibration, *, guard=lambda: None):
        from lerobot.motors import MotorCalibration
        validate_calibration(calibration)
        guard()
        self.require_torque(False)
        m.require(self.backup is not None and self.locks is not None, "Calibration backup is missing")
        expected = {name: MotorCalibration(**value) for name, value in calibration.items()}
        # Reload can start with EEPROM locked and without a midpoint capture.
        # Keep the backup live before the first write, including partial failure.
        for name in m.MOTORS:
            guard()
            self.bus.write("Lock", name, 0, normalize=False, num_retry=0)
        self.homing_changed = True
        self.calibration_matches = False
        self.bus.calibration = {}
        for name, entry in expected.items():
            guard()
            self.require_torque(False)
            for register, field in (("Homing_Offset", "homing_offset"), ("Min_Position_Limit", "range_min"),
                                    ("Max_Position_Limit", "range_max")):
                self.bus.write(register, name, getattr(entry, field), normalize=False, num_retry=0)
        guard()
        m.require(self.bus.read_calibration() == expected, "Calibration write verification failed")
        self.restore_locks()
        self.bus.calibration = expected
        self.calibration = deepcopy(calibration)
        self.signature = m.fingerprint(calibration)
        self.calibration_matches = True
        self.homing_changed = False
        self.backup = None

    def abort_calibration(self):
        from lerobot.motors import MotorCalibration
        self.require_torque(False)
        if self.homing_changed and self.backup is not None:
            # A failed lock restoration may have already locked some motors.
            for name in m.MOTORS:
                self.bus.write("Lock", name, 0, normalize=False, num_retry=0)
            self.bus.write_calibration(self.backup)
            m.require(self.bus.read_calibration() == self.backup, "Unable to verify calibration rollback")
        self.restore_locks()
        self.homing_changed = False
        self.backup = None
        # The backed-up hardware settings may already have differed from the
        # saved app calibration. Never treat that rollback as a verified match.
        self.bus.calibration = {name: MotorCalibration(**value) for name, value in (self.calibration or {}).items()}
        self.calibration_matches = bool(self.calibration) and self.bus.read_calibration() == self.bus.calibration


class HardwareLeader(HardwareArm):
    """Torque-off input device. Calibration is explicit; leader motion is forbidden."""
    role = "leader"

    def send(self, pose):
        raise m.SafetyError("The leader is input-only; motor goals are forbidden.")

    def arm_at_current(self, current):
        raise m.SafetyError("Leader torque enable is forbidden.")

    def send_calibrated(self, pose):
        raise m.SafetyError("The leader is input-only; motor goals are forbidden.")

    def arm_at_calibrated(self, current):
        raise m.SafetyError("Leader torque enable is forbidden.")
