"""Hardware boundary. No import, discovery, or connection in simulation mode."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import importlib.metadata

from . import motion as m


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

    def release(self):
        self.enabled = False

    def begin_calibration(self):
        self.require_torque(False)
        self.backup = deepcopy(self.calibration)
        return deepcopy(self.backup)

    def center(self):
        self.require_torque(False)
        self.current = dict.fromkeys(m.MOTORS, 2047)
        return dict.fromkeys(m.MOTORS, 0)

    def commit_calibration(self, calibration):
        self.require_torque(False)
        self.calibration = deepcopy(calibration)
        self.backup = None

    def abort_calibration(self):
        self.require_torque(False)
        self.calibration = deepcopy(self.backup)
        self.backup = None


class HardwareArm(m.Arm):
    """Same checked motion adapter as the prototype, without robot.configure()."""
    def __init__(self, port, calibration=None):
        from lerobot.motors import Motor, MotorCalibration, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus

        self.version = importlib.metadata.version("lerobot")
        m.require(self.version == "0.6.1", "This hardware adapter requires the validated LeRobot 0.6.1 interface.")
        self.calibration = deepcopy(calibration)
        self.signature = m.fingerprint(calibration or {})
        self.bus = FeetechMotorsBus(
            port=port,
            motors={name: Motor(i, "sts3215", MotorNormMode.DEGREES) for i, name in enumerate(m.MOTORS, 1)},
            calibration={name: MotorCalibration(**value) for name, value in (calibration or {}).items()},
        )
        self.backup = None
        self.locks = None
        self.homing_changed = False
        self.calibration_matches = False

    def open(self):
        self.bus.connect()
        # pyserial's exclusive flock also prevents another cooperating serial owner.
        self.bus.port_handler.ser.exclusive = True
        values = self.bus.sync_read("Present_Voltage", normalize=False, num_retry=0)
        m.require(set(values) == set(m.MOTORS) and all(v >= 80 for v in values.values()),
                  "Expected six 12 V follower motors. Check the selected port and power.")
        self.voltage = min(values.values()) / 10
        modes = self.bus.sync_read("Operating_Mode", normalize=False, num_retry=0)
        m.require(set(modes) == set(m.MOTORS) and all(v == 0 for v in modes.values()),
                  "Motors must already be in position mode. No settings were changed.")
        self.calibration_matches = bool(self.calibration) and self.bus.is_calibrated

    def check_pose(self, pose):
        m.require(self.calibration is not None and self.calibration_matches,
                  "Complete and verify motor calibration before any note motion.")
        super().check_pose(pose)

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
        self.backup = self.bus.read_calibration()
        self.locks = self.bus.sync_read("Lock", normalize=False, num_retry=0)
        m.require(set(self.locks) == set(m.MOTORS) and all(v in (0, 1) for v in self.locks.values()),
                  "Incomplete EEPROM lock readback")
        return {name: asdict(value) for name, value in self.backup.items()}

    def center(self):
        self.require_torque(False)
        m.require(self.backup is not None, "Calibration backup is missing")
        for name in m.MOTORS:
            self.bus.write("Lock", name, 0, normalize=False, num_retry=0)
        self.homing_changed = True
        self.calibration_matches = False
        offsets = self.bus.set_half_turn_homings()
        self.require_torque(False)
        centered = self.read_raw()
        m.require(all(abs(v - 2047) <= 3 for v in centered.values()),
                  "Midpoint moved during calibration. Keep the arm supported.")
        return offsets

    def restore_locks(self):
        self.require_torque(False)
        for name, value in (self.locks or {}).items():
            self.bus.write("Lock", name, value, normalize=False, num_retry=0)

    def commit_calibration(self, calibration):
        from lerobot.motors import MotorCalibration
        self.require_torque(False)
        expected = {name: MotorCalibration(**value) for name, value in calibration.items()}
        self.bus.write_calibration(expected)
        m.require(self.bus.read_calibration() == expected, "Calibration write verification failed")
        self.restore_locks()
        self.calibration = deepcopy(calibration)
        self.signature = m.fingerprint(calibration)
        self.calibration_matches = True
        self.homing_changed = False
        self.backup = None

    def abort_calibration(self):
        self.require_torque(False)
        if self.homing_changed and self.backup is not None:
            self.bus.write_calibration(self.backup)
            m.require(self.bus.read_calibration() == self.backup, "Unable to verify calibration rollback")
        self.restore_locks()
        self.homing_changed = False
        self.backup = None
        self.calibration_matches = bool(self.calibration) and self.bus.is_calibrated
