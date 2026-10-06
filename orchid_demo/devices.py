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
