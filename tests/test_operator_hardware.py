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
        self.hardware_calibration = {name: Calibration(i, 0, 10, 1000, 3100) for i, name in enumerate(m.MOTORS, 1)}
        self.calibration = deepcopy(self.hardware_calibration)
        self.values = {"Torque_Enable": dict.fromkeys(m.MOTORS, 0), "Lock": dict.fromkeys(m.MOTORS, 1),
                       "Present_Voltage": dict.fromkeys(m.MOTORS, 120), "Operating_Mode": dict.fromkeys(m.MOTORS, 0),
                       "Present_Position": dict.fromkeys(m.MOTORS, 2047)}
        self.values["Present_Temperature"] = dict.fromkeys(m.MOTORS, 28)
        for register, field in [("Homing_Offset", "homing_offset"), ("Min_Position_Limit", "range_min"), ("Max_Position_Limit", "range_max")]:
            self.values[register] = {name: getattr(cal, field) for name, cal in self.hardware_calibration.items()}
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
        if register == "Homing_Offset":
            self.values["Present_Position"][name] = (self.values["Present_Position"][name] + self.values[register][name] - value) % 4096
        self.values[register][name] = value
        field = {"Homing_Offset": "homing_offset", "Min_Position_Limit": "range_min", "Max_Position_Limit": "range_max"}.get(register)
        if field:
            setattr(self.hardware_calibration[name], field, value)

    def read_calibration(self):
        if self.corrupt_readback:
            self.corrupt_readback = False
            return {}
        return deepcopy(self.hardware_calibration)

    def write_calibration(self, cal):
        for name, entry in cal.items():
            for register, field in [("Homing_Offset", "homing_offset"), ("Min_Position_Limit", "range_min"), ("Max_Position_Limit", "range_max")]:
                self.write(register, name, getattr(entry, field))
        self.calibration = deepcopy(cal)


@pytest.fixture
def arm(monkeypatch):
    module = ModuleType("lerobot.motors")
    module.MotorCalibration = Calibration
    monkeypatch.setitem(sys.modules, "lerobot.motors", module)
    a = HardwareArm.__new__(HardwareArm)
    a.bus = Bus()
    a.calibration = {name: vars(cal).copy() for name, cal in a.bus.hardware_calibration.items()}
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
    old = deepcopy(arm.bus.hardware_calibration)
    arm.begin_calibration()
    arm.center(sleep=lambda _: None)
    assert not arm.calibration_matches
    assert arm.bus.hardware_calibration != old
    arm.abort_calibration()
    assert arm.bus.hardware_calibration == old
    assert arm.bus.values["Lock"] == dict.fromkeys(m.MOTORS, 1)
    assert arm.bus.values["Torque_Enable"] == dict.fromkeys(m.MOTORS, 0)
    assert all(register in ("Lock", "Homing_Offset", "Min_Position_Limit", "Max_Position_Limit") for register, _, _ in arm.bus.writes)


def test_failed_write_verification_can_restore_backup(arm):
    arm.open()
    old = deepcopy(arm.bus.hardware_calibration)
    arm.begin_calibration()
    arm.center(sleep=lambda _: None)
    changed = {name: {**cal, "homing_offset":0} for name,cal in arm.calibration.items()}
    arm.bus.corrupt_readback = True
    with pytest.raises(m.SafetyError, match="verification"):
        arm.commit_calibration(changed)
    arm.abort_calibration()
    assert arm.bus.hardware_calibration == old
    assert arm.bus.values["Lock"] == dict.fromkeys(m.MOTORS, 1)


def test_successful_calibration_readback_and_no_torque_enable(arm):
    arm.open()
    arm.begin_calibration()
    offsets = arm.center(sleep=lambda _: None)
    changed = {name: {**cal, "homing_offset":offsets[name]} for name,cal in arm.calibration.items()}
    arm.commit_calibration(changed)
    assert arm.calibration == changed
    assert arm.signature == m.fingerprint(changed)
    assert arm.calibration_matches
    assert all(register in ("Lock", "Homing_Offset", "Min_Position_Limit", "Max_Position_Limit") for register, _, _ in arm.bus.writes)


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


def test_midpoint_uses_existing_offsets_without_a_transient_zero_reference(arm):
    arm.open()
    arm.bus.values['Homing_Offset']['shoulder_lift'] = -379
    arm.bus.values['Present_Position']['shoulder_lift'] = 2500
    arm.begin_calibration()
    offsets = arm.center(sleep=lambda _: None)
    assert offsets['shoulder_lift'] == 74  # 2500 - 379 - 2047
    assert arm.bus.values['Present_Position'] == dict.fromkeys(m.MOTORS, 2047)
    assert [v for r, name, v in arm.bus.writes if r == 'Homing_Offset' and name == 'shoulder_lift'] == [74]
    assert not any(r in ('Goal_Position', 'Torque_Enable') for r, _, _ in arm.bus.writes)


def test_midpoint_waits_for_delayed_feedback_without_rewriting_offsets(arm):
    arm.open()
    arm.begin_calibration()
    read = arm.bus.sync_read
    reads, sleeps = [], []
    def delayed(register, **kwargs):
        result = read(register, **kwargs)
        if register == 'Present_Position' and arm.homing_changed:
            reads.append(result)
            if len(reads) < 3:
                result['elbow_flex'] = 2150  # Old reference briefly still visible.
        return result
    arm.bus.sync_read = delayed
    arm.center(sleep=sleeps.append)
    assert len(reads) == 5  # Two stale, then three centered samples.
    assert sleeps == [0.05] * 4
    assert sum(r == 'Homing_Offset' for r, _, _ in arm.bus.writes) == 6


@pytest.mark.parametrize('positions', [[2100], [2044, 2050]])
def test_midpoint_rejects_persistent_reference_error_or_motion_and_rolls_back(arm, positions):
    arm.open()
    old = arm.bus.read_calibration()
    arm.begin_calibration()
    read = arm.bus.sync_read
    reads = []
    def unsettled(register, **kwargs):
        result = read(register, **kwargs)
        if register == 'Present_Position' and arm.homing_changed:
            result['elbow_flex'] = positions[len(reads) % len(positions)]
            reads.append(result)
        return result
    arm.bus.sync_read = unsettled
    with pytest.raises(m.SafetyError, match='elbow_flex: .*ticks') as exc:
        arm.center(sleep=lambda _: None)
    assert 'Midpoint moved' not in str(exc.value)
    assert len(reads) == 12
    arm.abort_calibration()
    assert arm.bus.read_calibration() == old
    assert arm.bus.values['Torque_Enable'] == dict.fromkeys(m.MOTORS, 0)


def test_wrong_offset_readback_is_distinct_from_movement(arm):
    arm.open()
    arm.begin_calibration()
    read = arm.bus.sync_read
    def wrong(register, **kwargs):
        result = read(register, **kwargs)
        if register == 'Homing_Offset' and arm.homing_changed:
            result['wrist_roll'] += 1
        return result
    arm.bus.sync_read = wrong
    with pytest.raises(m.SafetyError, match='offset write verification failed: wrist_roll'):
        arm.center(sleep=lambda _: None)
    arm.abort_calibration()
    assert not arm.homing_changed


def test_midpoint_checks_stop_during_reference_settle(arm):
    arm.open()
    old = arm.bus.read_calibration()
    arm.begin_calibration()
    stop = False
    def guard():
        m.require(not stop, 'Stop requested')
    def sleep(_):
        nonlocal stop
        stop = True
    with pytest.raises(m.SafetyError, match='Stop requested'):
        arm.center(guard=guard, sleep=sleep)
    arm.abort_calibration()
    assert arm.bus.read_calibration() == old
