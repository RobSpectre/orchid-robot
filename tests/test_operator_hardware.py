"""Hardware adapter contract tests; no ports are opened."""
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace, ModuleType
import sys

import pytest

from orchid_demo import motion as m
from orchid_demo.devices import HardwareArm


@dataclass
class Calibration:
    id: int
    drive_mode: int
    homing_offset: int
    range_min: int
    range_max: int


class Bus:
    def __init__(self):
        self.is_connected = False
        self.is_calibrated = True
        self.port_handler = SimpleNamespace(ser=SimpleNamespace(exclusive=False))
        self.calibration = {name: Calibration(i, 0, 10, 1000, 3100) for i, name in enumerate(m.MOTORS, 1)}
        self.values = {"Torque_Enable": dict.fromkeys(m.MOTORS, 0), "Lock": dict.fromkeys(m.MOTORS, 1),
                       "Present_Voltage": dict.fromkeys(m.MOTORS, 120), "Operating_Mode": dict.fromkeys(m.MOTORS, 0),
                       "Present_Position": dict.fromkeys(m.MOTORS, 2047)}
        self.values["Present_Temperature"] = dict.fromkeys(m.MOTORS, 28)
        self.writes = []
        self.corrupt_readback = False

    def connect(self):
        self.is_connected = True

    def disconnect(self, disable_torque):
        assert disable_torque is False
        self.is_connected = False

    def sync_read(self, name, **kwargs):
        return dict(self.values[name])

    def write(self, register, name, value, **kwargs):
        self.writes.append((register, name, value))
        self.values[register][name] = value

    def read_calibration(self):
        if self.corrupt_readback:
            self.corrupt_readback = False
            return {}
        return deepcopy(self.calibration)

    def write_calibration(self, cal):
        self.calibration = deepcopy(cal)

    def set_half_turn_homings(self):
        for cal in self.calibration.values():
            cal.homing_offset = 0
        return dict.fromkeys(m.MOTORS, 0)


@pytest.fixture
def arm(monkeypatch):
    module = ModuleType("lerobot.motors")
    module.MotorCalibration = Calibration
    monkeypatch.setitem(sys.modules, "lerobot.motors", module)
    a = HardwareArm.__new__(HardwareArm)
    a.bus = Bus()
    a.calibration = {name: vars(cal).copy() for name, cal in a.bus.calibration.items()}
    a.signature = m.fingerprint(a.calibration)
    a.backup = a.locks = None
    a.homing_changed = a.calibration_matches = False
    return a


def test_open_only_reads_and_disconnect_never_drops_torque(arm):
    arm.open()
    assert arm.bus.port_handler.ser.exclusive
    assert arm.bus.writes == []
    assert arm.calibration_matches
    arm.close()
    assert not arm.bus.is_connected


@pytest.mark.parametrize("register,value", [("Present_Voltage",52), ("Operating_Mode",1)])
def test_rejects_leader_voltage_or_wrong_operating_mode(arm, register, value):
    arm.bus.values[register]["elbow_flex"] = value
    with pytest.raises(m.SafetyError):
        arm.open()
    assert not arm.bus.writes


def test_calibration_backup_rollback_and_lock_restore(arm):
    arm.open()
    old = deepcopy(arm.bus.calibration)
    arm.begin_calibration()
    arm.center()
    assert not arm.calibration_matches
    assert arm.bus.calibration != old
    arm.abort_calibration()
    assert arm.bus.calibration == old
    assert arm.bus.values["Lock"] == dict.fromkeys(m.MOTORS, 1)
    assert arm.bus.values["Torque_Enable"] == dict.fromkeys(m.MOTORS, 0)
    assert all(register == "Lock" for register, _, _ in arm.bus.writes)


def test_failed_write_verification_can_restore_backup(arm):
    arm.open()
    old = deepcopy(arm.bus.calibration)
    arm.begin_calibration()
    arm.center()
    changed = {name: {**cal, "homing_offset":0} for name,cal in arm.calibration.items()}
    arm.bus.corrupt_readback = True
    with pytest.raises(m.SafetyError, match="verification"):
        arm.commit_calibration(changed)
    arm.abort_calibration()
    assert arm.bus.calibration == old
    assert arm.bus.values["Lock"] == dict.fromkeys(m.MOTORS, 1)


def test_successful_calibration_readback_and_no_torque_enable(arm):
    arm.open()
    arm.begin_calibration()
    offsets = arm.center()
    changed = {name: {**cal, "homing_offset":offsets[name]} for name,cal in arm.calibration.items()}
    arm.commit_calibration(changed)
    assert arm.calibration == changed
    assert arm.signature == m.fingerprint(changed)
    assert arm.calibration_matches
    assert all(register == "Lock" for register, _, _ in arm.bus.writes)


def test_calibration_refuses_any_enabled_motor(arm):
    arm.bus.values["Torque_Enable"]["elbow_flex"] = 1
    with pytest.raises(m.SafetyError, match="torque off"):
        arm.begin_calibration()
    assert arm.bus.writes == []


def test_health_readback_is_read_only_and_requires_all_torque_off(arm):
    arm.open()
    values = arm.read_diagnostics()
    assert all(value == {"voltage_v": 12.0, "temperature_c": 28} for value in values.values())
    assert not arm.bus.writes
    arm.bus.values["Torque_Enable"]["gripper"] = 1
    with pytest.raises(m.SafetyError, match="torque off"):
        arm.read_diagnostics()
    assert not arm.bus.writes


def test_incomplete_health_readback_is_rejected(arm):
    del arm.bus.values["Present_Temperature"]["wrist_roll"]
    with pytest.raises(m.SafetyError, match="Incomplete"):
        arm.read_diagnostics()
    assert not arm.bus.writes
